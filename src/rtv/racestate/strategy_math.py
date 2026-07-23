"""Deterministic pit-strategy arithmetic over a :class:`RaceState` snapshot.

Pure functions, no LLM, no I/O, no clock. This is the ground truth the strategist
agent is allowed to cite: every number an agent puts on the radio has to come out
of here (or out of ``RaceState`` itself), which is what makes the citation
validator in :mod:`rtv.pitwall.validator` a real check rather than a formality.

The grounding discipline of stage 1 carries over verbatim: when the inputs are not
available -- no gap basis, no learned fuel consumption, no lap counter -- the
result is ``grounded=False`` with a stated ``reason`` rather than an invented
number. A strategist that says "you'll come out P4" off a guessed pit-lane time is
worse than one that says it doesn't know.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from rtv.racestate.models import CarState, FlagPhase, RaceState

#: Seconds lost relative to staying out, for a green-flag stop. A real number for
#: a real track would come from measured pit-lane transit; this is the default the
#: operator overrides with ``RTV_PITWALL_PIT_LANE_LOSS_S``.
DEFAULT_PIT_LANE_LOSS_S = 25.0

#: Under a full-course yellow the field is slowed, so a stop costs less track
#: position. The discount is an *assumption* and is always reported as one.
YELLOW_PIT_DISCOUNT = 0.45

#: Laps of fuel to leave in hand when computing a refuel target.
DEFAULT_FUEL_RESERVE_LAPS = 0.5


class PitOutcome(BaseModel):
    """What a stop on a given lap is projected to cost, and why.

    ``grounded`` is the field that matters: ``False`` means the state did not
    contain what was needed, ``reason`` says what, and every projected number is
    ``None``. Consumers must not fill the blanks in.
    """

    grounded: bool = False
    reason: str | None = None

    stop_lap: int | None = None
    current_lap: int | None = None
    laps_before_stop: float | None = None
    under_yellow: bool = False

    pit_lane_loss_s: float | None = None
    #: Pit-lane loss after the yellow discount, i.e. what the driver actually loses.
    effective_loss_s: float | None = None

    position_before: int | None = None
    rejoin_position: int | None = None
    positions_lost: int | None = None
    #: Car indices projected to be ahead after the stop that are behind now.
    cars_clearing: list[int] = Field(default_factory=list)
    #: Seconds to whoever is ahead once the player rejoins.
    gap_to_car_ahead_after: float | None = None
    gap_basis: str | None = None

    fuel_at_stop_l: float | None = None
    fuel_to_add_l: float | None = None
    laps_after_stop: float | None = None
    covers_to_finish: bool | None = None

    #: Every modelling choice made, in plain words, for the agent to quote.
    assumptions: list[str] = Field(default_factory=list)

    def to_tool(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)


def _player(state: RaceState) -> tuple[list[CarState], int | None]:
    order = list(state.standings.cars)
    for i, car in enumerate(order):
        if car.is_player:
            return order, i
    return order, None


def _cumulative_gaps_behind(order: list[CarState], i: int) -> list[tuple[CarState, float]]:
    """(car, seconds behind the player) for cars behind, nearest first.

    Walks the running order accumulating each car's gap to the one in front, and
    stops at the first unknown gap -- a chain with a hole in it cannot be summed.
    """
    out: list[tuple[CarState, float]] = []
    cum = 0.0
    for j in range(i + 1, len(order)):
        gap = order[j].gap_ahead
        if gap is None:
            break
        cum += gap
        out.append((order[j], cum))
    return out


def simulate_pit_outcome(
    state: RaceState,
    *,
    stop_lap: int | None = None,
    pit_lane_loss_s: float = DEFAULT_PIT_LANE_LOSS_S,
    under_yellow: bool | None = None,
    fuel_reserve_laps: float = DEFAULT_FUEL_RESERVE_LAPS,
) -> PitOutcome:
    """Project the result of pitting on ``stop_lap``.

    The rejoin estimate is deliberately simple and stated: the player loses
    ``effective_loss_s`` of track position, and every car currently within that
    many seconds behind comes out ahead. It assumes the field holds station over
    the stop, which is exactly the assumption a race engineer makes on the radio.
    """
    player_state = state.player
    current_lap = player_state.lap
    if under_yellow is None:
        under_yellow = state.flags.phase is FlagPhase.YELLOW
    if stop_lap is None:
        stop_lap = current_lap

    outcome = PitOutcome(
        stop_lap=stop_lap,
        current_lap=current_lap,
        under_yellow=bool(under_yellow),
        pit_lane_loss_s=round(pit_lane_loss_s, 3),
    )

    if pit_lane_loss_s <= 0:
        outcome.reason = "pit_lane_loss_s must be positive."
        return outcome
    if current_lap is None or stop_lap is None:
        outcome.reason = "No lap counter in this session's catalog."
        return outcome

    effective = pit_lane_loss_s * (YELLOW_PIT_DISCOUNT if under_yellow else 1.0)
    outcome.effective_loss_s = round(effective, 3)
    outcome.laps_before_stop = float(max(0, stop_lap - current_lap))
    outcome.assumptions.append(
        f"Pit-lane loss {pit_lane_loss_s:.1f}s"
        + (
            f", discounted to {effective:.1f}s under yellow (x{YELLOW_PIT_DISCOUNT})"
            if under_yellow
            else ""
        )
        + "."
    )

    _fuel_projection_into(outcome, state, fuel_reserve_laps)
    _rejoin_into(outcome, state, effective)

    if outcome.rejoin_position is None and outcome.fuel_to_add_l is None:
        # Nothing at all could be projected; keep the first stated reason.
        outcome.reason = outcome.reason or "Neither standings gaps nor fuel data are available."
        return outcome
    outcome.grounded = True
    return outcome


def _fuel_projection_into(
    outcome: PitOutcome, state: RaceState, reserve_laps: float
) -> None:
    fuel = state.fuel
    per_lap, level = fuel.per_lap, fuel.level
    if not per_lap or per_lap <= 0 or level is None:
        outcome.reason = outcome.reason or "Fuel consumption has not been learned yet."
        return

    laps_before = outcome.laps_before_stop or 0.0
    fuel_at_stop = level - laps_before * per_lap
    outcome.fuel_at_stop_l = round(fuel_at_stop, 3)

    if fuel.laps_to_finish is None:
        outcome.assumptions.append("Race distance unknown, so no refuel target computed.")
        return

    laps_after = max(0.0, fuel.laps_to_finish - laps_before)
    outcome.laps_after_stop = round(laps_after, 3)
    needed = (laps_after + reserve_laps) * per_lap
    target = min(fuel.capacity, needed) if fuel.capacity else needed
    outcome.fuel_to_add_l = round(max(0.0, target - max(0.0, fuel_at_stop)), 3)
    outcome.covers_to_finish = target + 1e-9 >= laps_after * per_lap
    outcome.assumptions.append(
        f"Refuel target {target:.2f} L = {laps_after:.1f} laps at "
        f"{per_lap:.3f} L/lap plus {reserve_laps:.1f} laps reserve"
        + (", capped at tank capacity." if fuel.capacity and needed > fuel.capacity else ".")
    )


def _rejoin_into(outcome: PitOutcome, state: RaceState, effective_loss_s: float) -> None:
    standings = state.standings
    order, idx = _player(state)
    if standings.gap_basis is None or idx is None:
        outcome.assumptions.append(
            "No standings gap basis, so no rejoin position was projected."
        )
        outcome.reason = outcome.reason or "Standings gaps are unavailable."
        return

    outcome.gap_basis = standings.gap_basis
    player = order[idx]
    position_before = player.position if player.position is not None else idx + 1
    outcome.position_before = position_before

    behind = _cumulative_gaps_behind(order, idx)
    clearing = [(car, cum) for car, cum in behind if cum < effective_loss_s]
    outcome.cars_clearing = [car.idx for car, _ in clearing]
    outcome.positions_lost = len(clearing)
    outcome.rejoin_position = position_before + len(clearing)

    if clearing:
        # The last car to clear us is now the closest one ahead.
        outcome.gap_to_car_ahead_after = round(effective_loss_s - clearing[-1][1], 3)
    elif player.gap_ahead is not None:
        outcome.gap_to_car_ahead_after = round(player.gap_ahead + effective_loss_s, 3)

    if len(behind) < len(order) - idx - 1:
        outcome.assumptions.append(
            "Gap chain behind the player is incomplete; cars past the gap are not counted."
        )


# --------------------------------------------------------------------------
# compact projections the strategist's tools hand to the model
# --------------------------------------------------------------------------
def fuel_projection(state: RaceState) -> dict[str, Any]:
    """The fuel picture as numbers, plus an explicit note when it is unknown."""
    fuel = state.fuel
    out: dict[str, Any] = {
        "level_l": fuel.level,
        "capacity_l": fuel.capacity,
        "per_lap_l": fuel.per_lap,
        "per_lap_std_l": fuel.per_lap_std,
        "samples": fuel.samples,
        "laps_remaining": fuel.laps_remaining,
        "laps_to_finish": fuel.laps_to_finish,
        "fuel_to_finish_l": fuel.fuel_to_finish,
        "margin_l": fuel.margin_l,
        "margin_laps": fuel.margin_laps,
        "pit_window_earliest_lap": fuel.pit_window_earliest_lap,
        "pit_window_latest_lap": fuel.pit_window_latest_lap,
        "window_open": fuel.window_open,
        "current_lap": state.player.lap,
    }
    if not state.capabilities.fuel:
        out["unavailable"] = "This session's catalog has no fuel channels."
    elif not fuel.per_lap:
        out["unavailable"] = "Not enough green laps yet to learn fuel consumption."
    return out


def tyre_trend(state: RaceState) -> dict[str, Any]:
    tyres = state.tyres
    out: dict[str, Any] = {
        "temps_c": dict(tyres.temps),
        "pressures_kpa": dict(tyres.pressures),
        "temp_trend_c_per_lap": dict(tyres.temp_trend),
        "pressure_trend_per_lap": dict(tyres.pressure_trend),
        "stint_laps": tyres.stint_laps,
        "laps_on_tyres": state.player.laps_on_tyres,
    }
    if not state.capabilities.tyres:
        out["unavailable"] = "This session's catalog has no tyre channels."
    elif not tyres.temp_trend and not tyres.pressure_trend:
        out["unavailable"] = "Fewer than two completed stint laps, so no trend yet."
    return out


def standings_around_player(state: RaceState, window: int = 3) -> dict[str, Any]:
    """The ``window`` cars either side of the player, with gaps."""
    order, idx = _player(state)
    out: dict[str, Any] = {
        "gap_basis": state.standings.gap_basis,
        "reference_lap_time_s": state.standings.reference_lap_time,
        "player_position": state.player.position,
        "field_size": len(order),
        "cars": [],
    }
    if idx is None:
        out["unavailable"] = "The player is not present in the standings array."
        return out
    lo, hi = max(0, idx - window), min(len(order), idx + window + 1)
    for offset, car in enumerate(order[lo:hi], start=lo):
        out["cars"].append(
            {
                "idx": car.idx,
                "position": car.position,
                "is_player": car.is_player,
                "running_order": offset + 1,
                "gap_ahead_s": car.gap_ahead,
                "gap_behind_s": car.gap_behind,
                "gap_to_player_s": car.gap_to_player,
                "on_pit_road": car.on_pit_road,
                "last_lap_time_s": car.last_lap_time,
                "lap": car.lap,
            }
        )
    if state.standings.gap_basis is None:
        out["unavailable"] = "No lap time or track length, so gaps are unknown."
    return out


def stint_history(state: RaceState, events: list[Any]) -> dict[str, Any]:
    """Stints reconstructed from the deterministic event log."""
    stints: list[dict[str, Any]] = []
    current: dict[str, Any] = {
        "stint": 1,
        "start_lap": None,
        "laps": [],
        "fuel_at_start_l": None,
    }
    for event in events:
        key = getattr(event, "event_type", None)
        key = getattr(key, "value", key)
        payload = getattr(event, "payload", {}) or {}
        if key == "lap_completed":
            current["laps"].append(
                {
                    "lap": payload.get("lap"),
                    "lap_time_s": payload.get("lap_time"),
                    "green": payload.get("green"),
                }
            )
        elif key == "pit_entry":
            current["end_lap"] = payload.get("lap")
            current["fuel_at_end_l"] = payload.get("fuel")
        elif key == "stint_start":
            stints.append(current)
            current = {
                "stint": payload.get("stint"),
                "start_lap": payload.get("lap"),
                "laps": [],
                "fuel_at_start_l": None,
            }
    stints.append(current)
    return {
        "current_stint": state.player.stint,
        "stint_start_lap": state.player.stint_start_lap,
        "laps_on_tyres": state.player.laps_on_tyres,
        "stints": stints,
    }
