"""The projection layer: what happens next, worked out deterministically.

:mod:`rtv.racestate.strategy_math` answers "what is true now" (the fuel window, what a
stop on lap N costs). This module answers the forward-looking questions a race plan
needs, in the same discipline: pure functions over a :class:`RaceState`, no LLM, no
I/O, no clock, every number with its unit in the field name, and a ``grounded=False``
result with a stated ``reason`` when the inputs are missing, never a default.

* :func:`lap_time_degradation` fits lap time against stint lap over the current stint's
  green, non-pit, non-outlier laps (``RaceState.laps``). The existing
  :func:`~rtv.racestate.strategy_math.tyre_trend` is a *temperature* trend and stays.
* :func:`fuel_save_options` is the litres per lap that stretch the stint by N laps, from
  the learned consumption model.
* :func:`traffic_projection` says when the player catches the car ahead or is caught by
  the car behind, from the standings gaps and the last-lap pace. A replayed ``.ibt`` has
  no ``CarIdx`` channels, so it is ungrounded there by design.
* :func:`undercut_overcut` prices an earlier and a later stop with
  :func:`~rtv.racestate.strategy_math.simulate_pit_outcome` and returns the net.

Field names follow :func:`rtv.grounding.units.field_unit` so the typed FactSet reads
the right unit family from them: ``..._s`` is seconds, ``..._l`` litres, ``..._lap`` a
lap number, ``..._laps`` a count of laps, ``..._pct`` a fraction.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field

from rtv.racestate import strategy_math as sm
from rtv.racestate.models import LapRecord, RaceState

#: Fewer clean stint laps than this and degradation is not fitted.
DEFAULT_MIN_DEGRADATION_LAPS = 4
#: A clean lap slower than the stint median by more than this fraction is an outlier
#: (traffic, a moment, a slow lap behind a safety car that the flag bits missed).
OUTLIER_FRACTION = 0.03
#: Laps ahead the projected lap time is quoted for, unless the caller asks otherwise.
DEFAULT_HORIZON_LAPS = 5
#: How far ahead a catch is still worth projecting.
TRAFFIC_HORIZON_LAPS = 30
#: Closing rates below this are noise in last-lap times, not pace.
MIN_CLOSING_S_PER_LAP = 0.05


def _rounded(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


# --------------------------------------------------------------------------
# lap-time degradation
# --------------------------------------------------------------------------
class Degradation(BaseModel):
    """Lap time against stint lap for the tyres on the car now."""

    grounded: bool = False
    reason: str | None = None
    stint: int | None = None
    samples: int = 0
    #: Lap numbers the fit used, and the clean laps dropped as outliers.
    laps_used: list[int] = Field(default_factory=list)
    outlier_laps: list[int] = Field(default_factory=list)
    #: Seconds a lap gets slower per stint lap (negative: still getting faster).
    loss_per_lap_s: float | None = None
    #: Standard error of that slope, in seconds per lap.
    loss_stderr_s: float | None = None
    #: The fitted lap time at the latest stint lap.
    current_lap_time_s: float | None = None
    #: The fit's lap time on the first lap of the stint (fresh tyres).
    fresh_lap_time_s: float | None = None
    horizon_laps: int = DEFAULT_HORIZON_LAPS
    #: The fitted lap time ``horizon_laps`` stint laps after the latest one.
    projected_lap_time_s: float | None = None
    #: The latest stint lap the fit saw (1 = the first lap of the stint).
    stint_laps: int | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    assumptions: list[str] = Field(default_factory=list)

    def to_tool(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def clean_stint_laps(state: RaceState) -> list[LapRecord]:
    """The current stint's timed, green laps with no pit-lane time in them."""
    stint = state.player.stint
    return [
        lap
        for lap in state.laps
        if lap.stint == stint and lap.green and not lap.pit and lap.lap_time
    ]


def lap_time_degradation(
    state: RaceState,
    *,
    min_laps: int = DEFAULT_MIN_DEGRADATION_LAPS,
    horizon_laps: int = DEFAULT_HORIZON_LAPS,
    outlier_fraction: float = OUTLIER_FRACTION,
) -> Degradation:
    """Least-squares lap time over stint lap, or a stated reason it cannot be fitted."""
    horizon_laps = max(0, int(horizon_laps))
    out = Degradation(stint=state.player.stint, horizon_laps=horizon_laps)
    if not state.laps:
        out.reason = "No completed laps in this session segment yet."
        return out
    clean = clean_stint_laps(state)
    if clean:
        median = _median([float(lap.lap_time or 0.0) for lap in clean])
        limit = median * (1 + outlier_fraction)
        kept = [lap for lap in clean if float(lap.lap_time or 0.0) <= limit]
        out.outlier_laps = [lap.lap for lap in clean if lap not in kept]
    else:
        kept = []
    out.samples = len(kept)
    out.laps_used = [lap.lap for lap in kept]
    if len(kept) < max(2, min_laps):
        out.reason = (
            f"Only {len(kept)} clean laps in stint {state.player.stint}; "
            f"{max(2, min_laps)} are needed to fit lap-time degradation."
        )
        return out

    xs = [float(lap.stint_lap) for lap in kept]
    ys = [float(lap.lap_time or 0.0) for lap in kept]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        out.reason = "Every clean lap has the same stint lap, so no slope can be fitted."
        return out
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = my - slope * mx
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=True)]
    stderr = math.sqrt(sum(r * r for r in residuals) / (n - 2) / sxx) if n > 2 else None

    last_x = max(xs)
    out.grounded = True
    out.loss_per_lap_s = round(slope, 4)
    out.loss_stderr_s = _rounded(stderr, 4)
    out.current_lap_time_s = round(intercept + slope * last_x, 3)
    out.fresh_lap_time_s = round(intercept + slope * 1.0, 3)
    out.projected_lap_time_s = round(intercept + slope * (last_x + horizon_laps), 3)
    out.stint_laps = int(last_x)
    if stderr is None or n < min_laps + 2:
        out.confidence = "low"
    elif stderr < 0.05:
        out.confidence = "high"
    elif stderr < 0.15:
        out.confidence = "medium"
    out.assumptions.append(
        f"Linear fit over {n} clean laps of stint {state.player.stint}; laps more than "
        f"{outlier_fraction * 100:.0f}% slower than the stint median are left out."
    )
    out.assumptions.append(
        "Fuel burn-off and tyre wear are not separated: the slope is their net effect."
    )
    return out


# --------------------------------------------------------------------------
# fuel save
# --------------------------------------------------------------------------
class FuelSaveOption(BaseModel):
    extra_laps: int
    #: Consumption that makes the fuel aboard last ``extra_laps`` longer.
    target_per_lap_l: float
    save_per_lap_l: float
    #: The saving as a fraction of today's consumption.
    save_pct: float
    #: The last lap the tank then reaches.
    reach_lap: int | None = None


class FuelSave(BaseModel):
    grounded: bool = False
    reason: str | None = None
    current_lap: int | None = None
    per_lap_l: float | None = None
    level_l: float | None = None
    laps_remaining: float | None = None
    #: The last lap the tank reaches at today's consumption.
    reach_lap: int | None = None
    options: list[FuelSaveOption] = Field(default_factory=list)
    #: Saving needed to reach the flag with no stop, when a stop is needed.
    to_flag: FuelSaveOption | None = None
    assumptions: list[str] = Field(default_factory=list)

    def to_tool(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _reach_lap(state: RaceState, laps: float) -> int | None:
    """The last lap whose line the car reaches with ``laps`` of fuel from where it is."""
    lap = state.player.lap
    if lap is None:
        return None
    whole = math.floor(laps - sm.lap_left(state) + 1e-6)
    return lap + whole if whole >= 0 else None


def fuel_save_options(state: RaceState, *, extra_laps: tuple[int, ...] = (1, 2, 3)) -> FuelSave:
    """Litres per lap that stretch the fuel aboard by each of ``extra_laps`` laps."""
    fuel = state.fuel
    out = FuelSave(current_lap=state.player.lap, per_lap_l=fuel.per_lap, level_l=fuel.level)
    if not state.capabilities.fuel:
        out.reason = "This session's catalog has no fuel channels."
        return out
    if not fuel.per_lap or fuel.per_lap <= 0 or fuel.level is None or fuel.laps_remaining is None:
        out.reason = "Fuel consumption has not been learned yet."
        return out
    out.grounded = True
    out.laps_remaining = round(fuel.laps_remaining, 3)
    out.reach_lap = _reach_lap(state, fuel.laps_remaining)
    for n in sorted({int(n) for n in extra_laps if int(n) > 0}):
        out.options.append(_option(state, n, fuel.laps_remaining + n))
    if fuel.margin_laps is not None and fuel.margin_laps < 0 and fuel.laps_to_finish:
        short = math.ceil(-fuel.margin_laps - 1e-6)
        option = _option(state, short, fuel.laps_to_finish)
        out.to_flag = option
    out.assumptions.append(
        f"Consumption {fuel.per_lap:.3f} L/lap from {fuel.samples} green laps; a saving is "
        "spread evenly over every lap still to run on this tank."
    )
    return out


def _option(state: RaceState, extra: int, laps: float) -> FuelSaveOption:
    fuel = state.fuel
    per_lap = float(fuel.per_lap or 0.0)
    target = float(fuel.level or 0.0) / laps if laps > 0 else 0.0
    return FuelSaveOption(
        extra_laps=extra,
        target_per_lap_l=round(target, 4),
        save_per_lap_l=round(per_lap - target, 4),
        save_pct=round((per_lap - target) / per_lap, 4) if per_lap else 0.0,
        reach_lap=_reach_lap(state, laps),
    )


# --------------------------------------------------------------------------
# traffic
# --------------------------------------------------------------------------
class TrafficSide(BaseModel):
    grounded: bool = False
    reason: str | None = None
    car_idx: int | None = None
    position: int | None = None
    gap_s: float | None = None
    #: Seconds a lap the gap shrinks (positive = closing, negative = opening).
    closing_per_lap_s: float | None = None
    #: Laps until the gap is zero, when it is closing inside the horizon.
    catch_in_laps: float | None = None
    catch_lap: int | None = None


class TrafficProjection(BaseModel):
    grounded: bool = False
    reason: str | None = None
    gap_basis: str | None = None
    current_lap: int | None = None
    #: The player catching the car ahead.
    ahead: TrafficSide = Field(default_factory=TrafficSide)
    #: The car behind catching the player.
    behind: TrafficSide = Field(default_factory=TrafficSide)
    horizon_laps: int = TRAFFIC_HORIZON_LAPS
    assumptions: list[str] = Field(default_factory=list)

    def to_tool(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def traffic_projection(
    state: RaceState, *, horizon_laps: int = TRAFFIC_HORIZON_LAPS
) -> TrafficProjection:
    """When the gaps either side of the player close, at the last lap's relative pace."""
    out = TrafficProjection(
        gap_basis=state.standings.gap_basis,
        current_lap=state.player.lap,
        horizon_laps=horizon_laps,
    )
    if state.standings.gap_basis is None:
        out.reason = (
            "No standings gap basis: gaps between cars are unknown (a replayed .ibt "
            "has no CarIdx channels; live telemetry has them)."
        )
        return out
    order = list(state.standings.cars)
    idx = next((i for i, car in enumerate(order) if car.is_player), None)
    if idx is None:
        out.reason = "The player is not present in the standings array."
        return out
    player = order[idx]
    if not player.last_lap_time:
        out.reason = "The player has no completed lap time to compare pace with."
        return out
    lap = state.player.lap
    # Ahead: we close when our lap is quicker than theirs.
    if idx > 0:
        car = order[idx - 1]
        out.ahead = _side(
            car, player.gap_ahead, car.last_lap_time, player.last_lap_time, lap, horizon_laps
        )
    else:
        out.ahead = TrafficSide(reason="The player leads the running order.")
    if idx + 1 < len(order):
        car = order[idx + 1]
        out.behind = _side(
            car, player.gap_behind, player.last_lap_time, car.last_lap_time, lap, horizon_laps
        )
    else:
        out.behind = TrafficSide(reason="No car behind the player in the running order.")
    out.grounded = out.ahead.grounded or out.behind.grounded
    if not out.grounded:
        out.reason = out.ahead.reason or out.behind.reason
    out.assumptions.append(
        "Relative pace is the difference of the two cars' last lap times, held constant."
    )
    return out


def _side(
    car: Any,
    gap: float | None,
    slow_lap_s: float | None,
    fast_lap_s: float | None,
    lap: int | None,
    horizon: int,
) -> TrafficSide:
    side = TrafficSide(car_idx=car.idx, position=car.position, gap_s=_rounded(gap))
    if gap is None:
        side.reason = "The gap to this car is unknown."
        return side
    if not slow_lap_s or not fast_lap_s:
        side.reason = "This car has no completed lap time to compare pace with."
        return side
    closing = float(slow_lap_s) - float(fast_lap_s)
    side.grounded = True
    side.closing_per_lap_s = round(closing, 3)
    if closing >= MIN_CLOSING_S_PER_LAP:
        laps = abs(float(gap)) / closing
        if laps <= horizon:
            side.catch_in_laps = round(laps, 2)
            if lap is not None:
                side.catch_lap = lap + math.ceil(laps)
    return side


# --------------------------------------------------------------------------
# undercut / overcut
# --------------------------------------------------------------------------
class UndercutProjection(BaseModel):
    grounded: bool = False
    reason: str | None = None
    early_lap: int | None = None
    late_lap: int | None = None
    early: dict[str, Any] = Field(default_factory=dict)
    late: dict[str, Any] = Field(default_factory=dict)
    #: Seconds the early stop gains over the laps between the two stops, from fresh
    #: tyres against worn ones (None without a degradation fit).
    tyre_gain_s: float | None = None
    #: Extra litres the early stop has to add.
    extra_fuel_l: float | None = None
    #: Rejoin position of the early stop minus that of the late one (negative: better).
    position_delta: int | None = None
    #: Seconds the early stop is ahead of the late one at the late stop's lap.
    net_s: float | None = None
    favours: Literal["early", "late", "even"] | None = None
    assumptions: list[str] = Field(default_factory=list)

    def to_tool(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def undercut_overcut(
    state: RaceState,
    *,
    early_lap: int | None = None,
    late_lap: int | None = None,
    pit_lane_loss_s: float = sm.DEFAULT_PIT_LANE_LOSS_S,
) -> UndercutProjection:
    """An earlier stop against a later one, both priced by ``simulate_pit_outcome``."""
    plan = sm.stop_plan(state)
    lap = state.player.lap
    if early_lap is None:
        early_lap = lap
    if late_lap is None:
        late_lap = plan.target_lap if plan.target_lap is not None else None
    out = UndercutProjection(early_lap=early_lap, late_lap=late_lap)
    if lap is None or early_lap is None or late_lap is None:
        out.reason = plan.reason or "No later stop lap to compare with."
        return out
    if late_lap <= early_lap:
        out.reason = "The later stop lap must come after the earlier one."
        return out
    early = sm.simulate_pit_outcome(state, stop_lap=early_lap, pit_lane_loss_s=pit_lane_loss_s)
    late = sm.simulate_pit_outcome(state, stop_lap=late_lap, pit_lane_loss_s=pit_lane_loss_s)
    out.early, out.late = early.to_tool(), late.to_tool()
    if not early.grounded or not late.grounded:
        out.reason = early.reason or late.reason
        return out
    out.grounded = True
    out.assumptions.extend(early.assumptions)
    if early.fuel_to_add_l is not None and late.fuel_to_add_l is not None:
        out.extra_fuel_l = round(early.fuel_to_add_l - late.fuel_to_add_l, 3)
    if early.rejoin_position is not None and late.rejoin_position is not None:
        out.position_delta = early.rejoin_position - late.rejoin_position
    deg = lap_time_degradation(state)
    if deg.grounded and deg.loss_per_lap_s is not None and deg.stint_laps is not None:
        laps_between = late_lap - early_lap
        # Each lap between the stops, the early stopper is on tyres that are
        # stint_laps + k laps younger than the late stopper's.
        gain = sum(deg.loss_per_lap_s * (deg.stint_laps + k) for k in range(laps_between))
        out.tyre_gain_s = round(gain, 3)
        out.net_s = out.tyre_gain_s
        if out.net_s > 0.2:
            out.favours = "early"
        elif out.net_s < -0.2:
            out.favours = "late"
        else:
            out.favours = "even"
        out.assumptions.append(
            f"Fresh tyres are worth {deg.loss_per_lap_s:.3f} s a lap per lap of tyre age "
            "(this stint's degradation fit); the pit-lane loss is the same in both branches."
        )
    else:
        out.assumptions.append(
            "No lap-time degradation fit, so the tyre gain of stopping early is not priced."
        )
    return out
