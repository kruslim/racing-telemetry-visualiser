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

import math
from typing import Any, Literal

from pydantic import BaseModel, Field

from rtv.racestate.derived import axle_split, lap_time_deltas
from rtv.racestate.models import CarState, RaceState

#: Seconds lost relative to staying out, for a green-flag stop. A real number for
#: a real track would come from measured pit-lane transit; this is the default the
#: operator overrides with ``RTV_PITWALL_PIT_LANE_LOSS_S``.
DEFAULT_PIT_LANE_LOSS_S = 25.0

#: Under a full-course yellow the field is slowed, so a stop costs less track
#: position. The discount is an *assumption* and is always reported as one.
YELLOW_PIT_DISCOUNT = 0.45

#: The SessionFlags bits of a full-course caution. ``yellow`` and ``yellow_waving``
#: on their own are a local yellow: one sector slows, the field does not, and a
#: stop costs the full pit-lane loss.
CAUTION_FLAGS = frozenset({"caution", "caution_waving"})


def full_course_caution(state: RaceState) -> bool:
    """True only under a full-course caution, not a local yellow."""
    return bool(CAUTION_FLAGS.intersection(state.flags.active))


#: Laps of fuel to leave in hand when computing a refuel target.
DEFAULT_FUEL_RESERVE_LAPS = 0.5


def lap_left(state: RaceState) -> float:
    """Fraction of the current lap still to run, from LapDistPct (1.0 if unknown).

    Every fuel distance is measured from where the car is now, so "stop at the
    end of lap N" is the rest of this lap plus ``N - lap`` whole laps.
    """
    pct = state.player.lap_dist_pct
    if pct is None or not 0.0 <= pct < 1.0:
        return 1.0
    return 1.0 - pct


def laps_to_end_of(state: RaceState, stop_lap: int) -> float:
    """Distance in laps from the car to the line at the end of ``stop_lap``."""
    lap = state.player.lap or 0
    return max(0, stop_lap - lap) + lap_left(state)


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
        under_yellow = full_course_caution(state)
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
    # Distance to the pit entry at the end of the stop lap, from the car's
    # position: stopping "this lap" still burns the rest of this lap.
    outcome.laps_before_stop = round(laps_to_end_of(state, stop_lap), 3)
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
# the stop plan: *when* to stop, decided deterministically
# --------------------------------------------------------------------------
#: Fewer green-lap fuel samples than this and the plan says its confidence is low.
MIN_CONFIDENT_FUEL_SAMPLES = 3

StopCall = Literal[
    "no_data",
    "no_stop_needed",
    "box_this_lap",
    "box_next_lap",
    "stop_later",
]


class StopPlan(BaseModel):
    """The one stop the fuel picture calls for, and when to make it.

    The window says when a stop is *possible*; this says when to take it. With no
    standings there is nothing to trade track position against, so the default is
    the latest lap that still leaves ``reserve_laps`` in the tank: it keeps the
    option of a cheap stop under a caution for as long as possible. On the Spa
    replay that is lap 12, the lap the driver actually pitted on, where the
    strategist had been calling "box this lap" since lap 2.
    """

    grounded: bool = False
    reason: str | None = None
    call: StopCall = "no_data"

    current_lap: int | None = None
    stop_required: bool = False
    #: Laps of fuel short of the flag (positive when a stop is needed).
    laps_short: float | None = None
    earliest_lap: int | None = None
    latest_lap: int | None = None
    target_lap: int | None = None
    laps_until_target: int | None = None
    reserve_laps: float = DEFAULT_FUEL_RESERVE_LAPS
    fuel_to_add_l: float | None = None
    #: A full-course caution is out and this lap is inside the window.
    caution_stop_available: bool = False
    #: ``low`` while fuel use rests on fewer than MIN_CONFIDENT_FUEL_SAMPLES laps.
    confidence: Literal["low", "high"] = "low"
    fuel_samples: int = 0
    assumptions: list[str] = Field(default_factory=list)

    def to_tool(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)


def stop_plan(
    state: RaceState,
    *,
    reserve_laps: float = DEFAULT_FUEL_RESERVE_LAPS,
) -> StopPlan:
    """Decide the stop lap from the fuel window alone. Pure; no LLM."""
    fuel = state.fuel
    lap = state.player.lap
    plan = StopPlan(
        current_lap=lap,
        reserve_laps=reserve_laps,
        fuel_samples=fuel.samples,
        confidence="high" if fuel.samples >= MIN_CONFIDENT_FUEL_SAMPLES else "low",
    )
    if lap is None:
        plan.reason = "No lap counter in this session's catalog."
        return plan
    if not fuel.per_lap or fuel.per_lap <= 0 or fuel.laps_remaining is None:
        plan.reason = "Fuel consumption has not been learned yet."
        return plan
    if fuel.margin_laps is None or fuel.laps_to_finish is None:
        plan.reason = "Race distance unknown, so no stop can be planned."
        return plan

    plan.grounded = True
    plan.stop_required = fuel.margin_laps < 0
    plan.earliest_lap = fuel.pit_window_earliest_lap
    plan.latest_lap = fuel.pit_window_latest_lap
    if not plan.stop_required:
        plan.call = "no_stop_needed"
        plan.laps_short = 0.0
        plan.assumptions.append(
            f"The tank covers the {fuel.laps_to_finish:.1f} laps to the flag with "
            f"{fuel.margin_laps:.2f} laps to spare."
        )
        return plan

    plan.laps_short = round(-fuel.margin_laps, 3)
    # The latest stop lap that still arrives with the reserve in the tank.
    reach = math.floor(fuel.laps_remaining - lap_left(state) - reserve_laps + 1e-3)
    target = lap + max(0, reach)
    if plan.latest_lap is not None:
        target = min(target, plan.latest_lap)
    if plan.earliest_lap is not None and target < plan.earliest_lap:
        # Even the reserve-free run-dry lap is before a one-stop finish is
        # possible: the earliest lap is the least-bad single stop.
        plan.assumptions.append(
            f"No single stop reaches the flag with {reserve_laps:.1f} laps in hand; "
            f"stopping at the earliest one-stop lap {plan.earliest_lap}."
        )
        target = plan.earliest_lap
    target = max(target, lap)
    plan.target_lap = target
    plan.laps_until_target = target - lap
    if target <= lap:
        plan.call = "box_this_lap"
    elif target == lap + 1:
        plan.call = "box_next_lap"
    else:
        plan.call = "stop_later"

    outcome = simulate_pit_outcome(state, stop_lap=target, fuel_reserve_laps=reserve_laps)
    plan.fuel_to_add_l = outcome.fuel_to_add_l
    plan.caution_stop_available = bool(
        full_course_caution(state)
        and plan.earliest_lap is not None
        and plan.earliest_lap <= lap <= (plan.latest_lap if plan.latest_lap is not None else lap)
    )
    plan.assumptions.append(
        f"Target lap {target} is the latest lap that keeps {reserve_laps:.1f} laps "
        "of fuel in hand; no standings, rival or caution trade is priced in."
    )
    return plan


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
        "stop_plan": stop_plan(state).to_tool(),
    }
    # Task A3: "lap 17 of 18" needs the race distance next to the lap counter.
    if state.session.laps_total:
        out["race_total_laps"] = state.session.laps_total
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
        # Task A3: the changes agents kept computing in their heads, ready to copy.
        "since_stint_start": {
            "temp_change_c": dict(tyres.temp_change_since_stint_start),
            "pressure_change_kpa": dict(tyres.pressure_change_since_stint_start),
        },
        "vs_previous_lap": {
            "temp_change_c": dict(tyres.temp_change_vs_previous_lap),
            "pressure_change_kpa": dict(tyres.pressure_change_vs_previous_lap),
        },
        "axles": {
            "temps": axle_split(tyres.temps, "c"),
            "pressures": axle_split(tyres.pressures, "kpa"),
        },
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
    out: dict[str, Any] = {
        "current_stint": state.player.stint,
        "stint_start_lap": state.player.stint_start_lap,
        "laps_on_tyres": state.player.laps_on_tyres,
        "stints": stints,
    }
    # Task A3: the lap-time comparisons agents kept computing, for the current stint.
    deltas = lap_time_deltas(current["laps"], state.player.best_lap_time)
    if deltas:
        out["current_stint_laps"] = deltas
    total = state.session.laps_total
    if total:
        out["race_total_laps"] = total
    return out
