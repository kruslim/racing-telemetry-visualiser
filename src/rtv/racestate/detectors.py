"""Deterministic detectors: pure functions over a window of recent frames.

Each function inspects the tail of a frame window and returns either ``None`` or
a plain payload ``dict`` describing what it saw. They hold no state and never
emit events themselves -- latching, cooldowns and :class:`RaceEvent` construction
are the engine's job (:mod:`rtv.racestate.engine`). Keeping the predicates pure
is what makes "lockup fires, near-miss doesn't" a two-line unit test.

A detector whose channels are missing from the window returns ``None`` rather
than guessing: no data, no finding.

One exception, added in stage 3: :class:`CornerRecurrence` holds state, because
its window is *laps* rather than frames. It is still a detector by the definition
that matters -- it returns a payload and never constructs an event -- and it is
what stops a single lockup ever waking an agent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rtv.catalog.enums import SESSION_FLAGS as SESSION_FLAG_BITS
from rtv.catalog.enums import TRK_LOC, decode_bitfield
from rtv.racestate import channels as ch
from rtv.racestate.models import FlagPhase

#: A frame window is an ordered sequence of value dicts, oldest first.
Window = Sequence[Mapping[str, Any]]

#: Track-surface labels that count as "off track" for the incident detector.
OFF_TRACK_LABELS = frozenset({"off_track"})


@dataclass(frozen=True)
class DetectorConfig:
    """Thresholds for the wheel-slip detectors, in SI units (m/s, 0..1 pedals)."""

    # lock-up: a front wheel decelerating faster than the car under braking
    lockup_brake_min: float = 0.35
    lockup_slip: float = 0.15  # wheel speed <= (1 - slip) * car speed
    lockup_min_speed: float = 12.0
    lockup_samples: int = 3

    # wheelspin: a driven wheel outrunning the car under power
    wheelspin_throttle_min: float = 0.40
    wheelspin_slip: float = 0.12  # wheel speed >= (1 + slip) * car speed
    wheelspin_min_speed: float = 3.0
    wheelspin_samples: int = 3

    # lapped traffic closing on the player
    blue_flag_gap_s: float = 2.5
    blue_flag_lap_margin: float = 0.7  # laps ahead before a car counts as lapping us

    # fuel
    fuel_critical_laps: float = 1.5  # laps of fuel left before it is critical
    #: Laps of slack (laps_remaining - laps_to_finish) below which the strategist
    #: wants to know. Fires once per stint, re-armed on refuel.
    fuel_margin_laps: float = 1.0

    # strategy triggers (deterministic; consumed by the pitwall agent layer)
    #: Laps before the run-dry bound at which the pit window counts as closing.
    pit_window_closing_laps: int = 1
    #: Emit a stint milestone every N green laps on the current set.
    stint_milestone_laps: int = 5
    #: A rival's stop is only reported when they are within N positions.
    rival_position_window: int = 3

    # --- corner recurrence (the vehicle engineer's / coach's wake-up) -----
    #: Lap fractions the track is divided into when labelling "the same corner".
    #: 20 buckets = 5 % of a lap, roughly one corner on a typical road course.
    corner_buckets: int = 20
    #: Repeats of the same issue at the same corner before it is a finding.
    recurrence_min: int = 3
    #: Only repeats within this many laps of each other count.
    recurrence_window_laps: int = 5

    # --- tyre bands ------------------------------------------------------
    #: Absolute bands are car-specific, so they are OFF unless an operator sets
    #: them. Everything below them is *relative* and needs no per-car knowledge.
    tyre_temp_max_c: float | None = None
    tyre_temp_min_c: float | None = None
    tyre_pressure_max: float | None = None
    tyre_pressure_min: float | None = None
    #: Sustained per-lap drift across the current stint (from TyreState trends).
    tyre_temp_trend_c_per_lap: float = 3.0
    tyre_pressure_trend_per_lap: float = 2.0
    #: Left-to-right spread across one axle. Relative, so no band is needed.
    tyre_axle_imbalance_c: float = 15.0
    #: Trends need at least this many completed stint laps to mean anything.
    tyre_min_stint_laps: int = 3

    # --- car health ------------------------------------------------------
    oil_temp_max_c: float = 130.0
    water_temp_max_c: float = 105.0
    #: Engine-warning bits that are faults. The limiter bits are normal driving.
    engine_fault_bits: tuple[str, ...] = (
        "water_temp_warning",
        "fuel_pressure_warning",
        "oil_pressure_warning",
        "oil_temp_warning",
        "engine_stalled",
    )

    # --- traffic (the spotter's wake-up) ---------------------------------
    #: A car is "close" inside this many seconds of track-position gap.
    traffic_gap_s: float = 1.5
    #: ...and only interesting if it is actually closing, this fast (s per s).
    traffic_closing_rate: float = 0.15
    #: Seconds between the gap samples the closing rate is measured over.
    traffic_sample_s: float = 1.0


DEFAULT_CONFIG = DetectorConfig()


# --------------------------------------------------------------------------
# small typed accessors -- absent/garbage channels degrade to None, never 0.0
# --------------------------------------------------------------------------
def as_float(values: Mapping[str, Any], name: str) -> float | None:
    raw = values.get(name)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        out = float(raw)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # reject NaN


def as_int(values: Mapping[str, Any], name: str) -> int | None:
    raw = values.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def as_bool(values: Mapping[str, Any], name: str) -> bool | None:
    raw = values.get(name)
    return None if raw is None else bool(raw)


# --------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------
def flag_phase(raw: int | None) -> tuple[FlagPhase, list[str]]:
    """Distil the SessionFlags bitfield into a coarse phase + active flag names.

    Priority is deliberately most-urgent-first: a yellow thrown on the last lap
    matters more to a pitwall than the white flag that is also still set.
    """
    if raw is None:
        return FlagPhase.UNKNOWN, []
    active = [name for name, on in decode_bitfield(SESSION_FLAG_BITS, raw).items() if on]
    aset = set(active)
    if "checkered" in aset:
        phase = FlagPhase.CHECKERED
    elif "red" in aset:
        phase = FlagPhase.RED
    elif aset & {"yellow", "yellow_waving", "caution", "caution_waving"}:
        phase = FlagPhase.YELLOW
    elif "white" in aset:
        phase = FlagPhase.WHITE
    elif aset & {"green", "green_held"}:
        phase = FlagPhase.GREEN
    else:
        phase = FlagPhase.UNKNOWN
    return phase, active


# --------------------------------------------------------------------------
# wheel-slip detectors
# --------------------------------------------------------------------------
def _wheel_speeds(values: Mapping[str, Any], corners: Sequence[str]) -> list[float]:
    out = []
    for corner in corners:
        v = as_float(values, ch.WHEEL_SPEED[corner])
        if v is not None:
            out.append(v)
    return out


def detect_lockup(window: Window, cfg: DetectorConfig = DEFAULT_CONFIG) -> dict | None:
    """Front wheel speed collapsing below car speed while the driver is braking.

    Requires the condition to hold across ``cfg.lockup_samples`` consecutive
    frames so a single noisy sample cannot trip it.
    """
    n = cfg.lockup_samples
    if len(window) < n:
        return None
    worst_ratio = 1.0
    worst: dict[str, Any] | None = None
    for values in window[-n:]:
        speed = as_float(values, ch.SPEED)
        brake = as_float(values, ch.BRAKE)
        if speed is None or brake is None:
            return None
        if brake < cfg.lockup_brake_min or speed < cfg.lockup_min_speed:
            return None
        speeds = _wheel_speeds(values, ch.FRONT_WHEELS)
        if not speeds:
            return None
        slowest = min(speeds)
        ratio = slowest / speed
        if ratio > (1.0 - cfg.lockup_slip):
            return None
        if ratio < worst_ratio:
            worst_ratio = ratio
            wheel = (
                ch.FRONT_WHEELS[speeds.index(slowest)]
                if len(speeds) == len(ch.FRONT_WHEELS)
                else None
            )
            worst = {
                "speed": round(speed, 3),
                "brake": round(brake, 3),
                "wheel_speed": round(slowest, 3),
                # ``corner`` here is the *wheel* (LF/RF) and predates the
                # track-corner labels; ``wheel`` is the unambiguous name, and
                # ``lap_dist_pct`` is where on the lap it happened, which is what
                # the corner-recurrence aggregator keys on.
                "corner": wheel,
                "wheel": wheel,
                "lap_dist_pct": as_float(values, ch.LAP_DIST_PCT),
            }
    if worst is None:
        return None
    return {**worst, "slip": round(1.0 - worst_ratio, 4), "axle": "front"}


def detect_wheelspin(window: Window, cfg: DetectorConfig = DEFAULT_CONFIG) -> dict | None:
    """Driven (rear) wheel speed exceeding car speed while on the throttle."""
    n = cfg.wheelspin_samples
    if len(window) < n:
        return None
    worst_ratio = 1.0
    worst: dict[str, Any] | None = None
    for values in window[-n:]:
        speed = as_float(values, ch.SPEED)
        throttle = as_float(values, ch.THROTTLE)
        if speed is None or throttle is None:
            return None
        if throttle < cfg.wheelspin_throttle_min or speed < cfg.wheelspin_min_speed:
            return None
        speeds = _wheel_speeds(values, ch.REAR_WHEELS)
        if not speeds:
            return None
        fastest = max(speeds)
        ratio = fastest / speed
        if ratio < (1.0 + cfg.wheelspin_slip):
            return None
        if ratio > worst_ratio:
            worst_ratio = ratio
            wheel = (
                ch.REAR_WHEELS[speeds.index(fastest)]
                if len(speeds) == len(ch.REAR_WHEELS)
                else None
            )
            worst = {
                "speed": round(speed, 3),
                "throttle": round(throttle, 3),
                "wheel_speed": round(fastest, 3),
                "corner": wheel,
                "wheel": wheel,
                "lap_dist_pct": as_float(values, ch.LAP_DIST_PCT),
            }
    if worst is None:
        return None
    return {**worst, "slip": round(worst_ratio - 1.0, 4), "axle": "rear"}


# --------------------------------------------------------------------------
# edge detectors (need the last two frames)
# --------------------------------------------------------------------------
def _last_two(window: Window) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    if len(window) < 2:
        return None
    return window[-2], window[-1]


def detect_offtrack(window: Window) -> dict | None:
    """Rising edge of PlayerTrackSurface entering the off-track state."""
    pair = _last_two(window)
    if pair is None:
        return None
    prev, cur = pair
    prev_s, cur_s = as_int(prev, ch.PLAYER_TRACK_SURFACE), as_int(cur, ch.PLAYER_TRACK_SURFACE)
    if prev_s is None or cur_s is None:
        return None
    prev_label = TRK_LOC.get(prev_s)
    cur_label = TRK_LOC.get(cur_s)
    if cur_label in OFF_TRACK_LABELS and prev_label not in OFF_TRACK_LABELS:
        return {
            "surface": cur_label,
            "from": prev_label,
            "lap_dist_pct": as_float(cur, ch.LAP_DIST_PCT),
            "speed": as_float(cur, ch.SPEED),
        }
    return None


def detect_incident(window: Window) -> dict | None:
    """Any increase in the player's incident counter."""
    pair = _last_two(window)
    if pair is None:
        return None
    prev, cur = pair
    prev_n, cur_n = as_int(prev, ch.PLAYER_INCIDENTS), as_int(cur, ch.PLAYER_INCIDENTS)
    if prev_n is None or cur_n is None or cur_n <= prev_n:
        return None
    return {
        "incidents": cur_n,
        "delta": cur_n - prev_n,
        "lap_dist_pct": as_float(cur, ch.LAP_DIST_PCT),
    }


def detect_pit_transition(window: Window) -> str | None:
    """``"entry"`` / ``"exit"`` on the OnPitRoad edge, else ``None``."""
    pair = _last_two(window)
    if pair is None:
        return None
    prev, cur = pair
    prev_p, cur_p = as_bool(prev, ch.ON_PIT_ROAD), as_bool(cur, ch.ON_PIT_ROAD)
    if prev_p is None or cur_p is None or prev_p == cur_p:
        return None
    return "entry" if cur_p else "exit"


def detect_lap_completion(window: Window) -> dict | None:
    """Rising edge of the Lap counter -- the lap that just *ended* is Lap-1."""
    pair = _last_two(window)
    if pair is None:
        return None
    prev, cur = pair
    prev_l, cur_l = as_int(prev, ch.LAP), as_int(cur, ch.LAP)
    if prev_l is None or cur_l is None or cur_l <= prev_l:
        return None
    last_time = as_float(cur, ch.LAP_LAST_LAP_TIME)
    return {
        "lap": prev_l,
        "new_lap": cur_l,
        "lap_time": last_time if (last_time or 0) > 0 else None,
    }


# --------------------------------------------------------------------------
# standings-derived detectors
# --------------------------------------------------------------------------
def detect_blue_flag(standings, cfg: DetectorConfig = DEFAULT_CONFIG) -> dict | None:
    """Lapped-traffic warning: a car at least a lap up closing from behind.

    Derived purely from standings deltas (lap counts + converted gaps), so it
    works even when the sim has not yet waved the blue flag at us.
    """
    if standings is None or standings.player_idx is None or not standings.gap_basis:
        return None
    player = next((c for c in standings.cars if c.is_player), None)
    if player is None or player.laps_completed is None:
        return None

    best: dict[str, Any] | None = None
    for car in standings.cars:
        if car.is_player or car.laps_completed is None or car.gap_to_player is None:
            continue
        # A car lapping us is *up* on the timesheet but closing from behind on
        # track, i.e. a negative (behind) track gap with a positive lap surplus.
        laps_ahead = car.laps_completed - player.laps_completed
        if laps_ahead < cfg.blue_flag_lap_margin:
            continue  # not lapping us
        gap = car.gap_to_player
        if gap >= 0 or -gap > cfg.blue_flag_gap_s:
            continue
        if best is None or -gap < best["gap"]:
            best = {
                "car_idx": car.idx,
                "gap": round(-gap, 3),
                "position": car.position,
                "laps_ahead": round(laps_ahead, 3),
            }
    return best


def detect_closing_traffic(
    standings,
    previous: Mapping[int, float],
    dt: float,
    cfg: DetectorConfig = DEFAULT_CONFIG,
) -> dict | None:
    """The nearest car that is both close *and* actually closing on us.

    Proximity alone is useless as a trigger: in a tight race someone is within a
    second for the whole stint, and a spotter that says so every lap is noise. The
    rate is measured against a gap sampled ``cfg.traffic_sample_s`` ago, which the
    engine holds -- this function stays pure.
    """
    if standings is None or standings.player_idx is None or not standings.gap_basis:
        return None
    if dt <= 0:
        return None
    best: dict[str, Any] | None = None
    for car in standings.cars:
        if car.is_player or car.gap_to_player is None or car.on_pit_road:
            continue
        gap = abs(car.gap_to_player)
        if gap > cfg.traffic_gap_s:
            continue
        was = previous.get(car.idx)
        if was is None:
            continue
        rate = (abs(was) - gap) / dt
        if rate < cfg.traffic_closing_rate:
            continue
        if best is None or gap < best["gap"]:
            best = {
                "car_idx": car.idx,
                "gap": round(gap, 3),
                "side": "ahead" if car.gap_to_player > 0 else "behind",
                "closing_rate_s_per_s": round(rate, 3),
                "position": car.position,
                "threshold_gap_s": cfg.traffic_gap_s,
            }
    return best


# --------------------------------------------------------------------------
# car condition (state-derived, evaluated at lap boundaries)
# --------------------------------------------------------------------------
def detect_tyre_condition(tyres, cfg: DetectorConfig = DEFAULT_CONFIG) -> dict | None:
    """The worst single way the tyres are outside their band, or ``None``.

    Absolute bands (``tyre_temp_max_c`` and friends) are per-car numbers, so they
    default to ``None`` and are simply skipped -- inventing a "normal" tyre
    temperature for an unknown car would be exactly the kind of unbacked figure
    the rest of this package refuses to produce. The relative measures below need
    no per-car knowledge: a 4 C-per-lap climb and a 20 C spread across one axle
    are findings whatever the car is.
    """
    if tyres is None:
        return None
    temps, press = dict(tyres.temps), dict(tyres.pressures)

    def _band(values: dict[str, float], lo, hi, measure: str, unit: str):
        worst = None
        for corner, value in values.items():
            if hi is not None and value > hi:
                excess, limit = value - hi, hi
            elif lo is not None and value < lo:
                excess, limit = lo - value, lo
            else:
                continue
            if worst is None or excess > worst["excess"]:
                worst = {
                    "measure": measure,
                    "corner": corner,
                    "value": round(value, 3),
                    "threshold": limit,
                    "excess": round(excess, 3),
                    "unit": unit,
                }
        return worst

    finding = _band(temps, cfg.tyre_temp_min_c, cfg.tyre_temp_max_c, "tyre_temp", "C")
    finding = finding or _band(
        press, cfg.tyre_pressure_min, cfg.tyre_pressure_max, "tyre_pressure", "kPa"
    )

    if finding is None and tyres.stint_laps >= cfg.tyre_min_stint_laps:
        finding = _trend_finding(
            tyres.temp_trend, cfg.tyre_temp_trend_c_per_lap, "tyre_temp_trend", "C/lap"
        ) or _trend_finding(
            tyres.pressure_trend,
            cfg.tyre_pressure_trend_per_lap,
            "tyre_pressure_trend",
            "kPa/lap",
        )

    if finding is None:
        finding = _axle_imbalance(temps, cfg.tyre_axle_imbalance_c)

    if finding is None:
        return None
    return {
        **finding,
        "temps_c": temps,
        "pressures": press,
        "temp_trend_c_per_lap": dict(tyres.temp_trend),
        "pressure_trend_per_lap": dict(tyres.pressure_trend),
        "stint_laps": tyres.stint_laps,
    }


def _trend_finding(
    trends: Mapping[str, float], threshold: float, measure: str, unit: str
) -> dict | None:
    worst = None
    for corner, slope in trends.items():
        if abs(slope) < threshold:
            continue
        if worst is None or abs(slope) > abs(worst["value"]):
            worst = {
                "measure": measure,
                "corner": corner,
                "value": round(slope, 4),
                "threshold": threshold,
                "excess": round(abs(slope) - threshold, 4),
                "unit": unit,
            }
    return worst


def _axle_imbalance(temps: Mapping[str, float], threshold: float) -> dict | None:
    worst = None
    for axle, (left, right) in (("front", ("LF", "RF")), ("rear", ("LR", "RR"))):
        if left not in temps or right not in temps:
            continue
        spread = abs(temps[left] - temps[right])
        if spread < threshold:
            continue
        if worst is None or spread > worst["value"]:
            worst = {
                "measure": "tyre_axle_imbalance",
                "corner": left if temps[left] > temps[right] else right,
                "axle": axle,
                "value": round(spread, 3),
                "threshold": threshold,
                "excess": round(spread - threshold, 3),
                "unit": "C",
            }
    return worst


def detect_car_health(health, cfg: DetectorConfig = DEFAULT_CONFIG) -> dict | None:
    """Engine faults, a tow, or a temperature over its limit -- worst one wins.

    The sim's own warning bits come first because they are the car saying it, not
    us inferring it. ``pit_speed_limiter`` and ``rev_limiter_active`` are normal
    driving and are excluded by ``cfg.engine_fault_bits``.
    """
    if health is None:
        return None
    faults = [w for w in health.engine_warnings if w in cfg.engine_fault_bits]
    if faults:
        return {
            "measure": "engine_warning",
            "warnings": faults,
            "oil_temp_c": health.oil_temp,
            "water_temp_c": health.water_temp,
            "critical": True,
        }
    if health.tow_time is not None and health.tow_time > 0:
        return {
            "measure": "tow",
            "tow_time_s": round(health.tow_time, 3),
            "critical": True,
        }
    for measure, value, limit, unit in (
        ("oil_temp", health.oil_temp, cfg.oil_temp_max_c, "C"),
        ("water_temp", health.water_temp, cfg.water_temp_max_c, "C"),
    ):
        if value is not None and value > limit:
            return {
                "measure": measure,
                "value": round(value, 3),
                "threshold": limit,
                "excess": round(value - limit, 3),
                "unit": unit,
                "oil_temp_c": health.oil_temp,
                "water_temp_c": health.water_temp,
                "critical": False,
            }
    return None


# --------------------------------------------------------------------------
# corner recurrence
# --------------------------------------------------------------------------
def corner_label(lap_dist_pct: float | None, buckets: int) -> str | None:
    """A stable name for "the same place on the track", from lap distance alone.

    Deliberately not a real corner number: without the track map we do not know
    where T7 is, and naming one would be a figure we cannot back. A bucket index
    is honest, reproducible, and enough to say "this keeps happening here". The
    payload carries the lap-fraction range so a UI can point at it.
    """
    if lap_dist_pct is None or buckets <= 0:
        return None
    pct = float(lap_dist_pct) % 1.0
    index = min(buckets - 1, max(0, int(pct * buckets)))
    return f"C{index:02d}"


#: Issues worth aggregating by corner. All three are driver-facing repeats.
RECURRING_ISSUE_TYPES = ("lockup", "wheelspin", "offtrack")


class CornerRecurrence:
    """Counts repeats of an issue at the same corner and reports the third one.

    The one stateful object in this module, and it is here rather than in the
    engine on purpose: it *is* a detector, just one whose window is laps instead
    of frames. It builds a payload and returns it; constructing the
    :class:`~rtv.racestate.models.RaceEvent` is still the engine's job.

    Why aggregate at all: one lockup is a driver having a moment, and waking an
    LLM for it would be both noisy and expensive. Three at the same corner inside
    five laps is a brake bias or a technique problem, and that is worth a radio
    call. The threshold is deterministic, so the agent never sees the singles.
    """

    def __init__(self, cfg: DetectorConfig = DEFAULT_CONFIG) -> None:
        self.cfg = cfg
        self._hits: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._announced: dict[tuple[str, str], int] = {}

    def reset(self) -> None:
        self._hits.clear()
        self._announced.clear()

    def record(
        self,
        issue: str,
        *,
        lap: int | None,
        session_time: float,
        lap_dist_pct: float | None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict | None:
        """Log one occurrence; return a finding when it crosses the threshold."""
        corner = corner_label(lap_dist_pct, self.cfg.corner_buckets)
        if corner is None or lap is None:
            return None  # no lap distance => no notion of "the same corner"
        key = (issue, corner)
        payload = payload or {}
        hits = self._hits.setdefault(key, [])
        hits.append(
            {
                "lap": lap,
                "session_time": round(session_time, 3),
                "lap_dist_pct": round(float(lap_dist_pct), 4),
                "slip": payload.get("slip"),
                "speed": payload.get("speed"),
            }
        )
        window = self.cfg.recurrence_window_laps
        recent = [h for h in hits if lap - h["lap"] < window]
        self._hits[key] = recent

        count = len(recent)
        if count < self.cfg.recurrence_min:
            return None
        # Report at the threshold, then only after another full threshold's worth
        # of *new* repeats, so a driver locking up every lap gets one call rather
        # than one per lap. Counting new hits since the last call rather than the
        # running total matters: the total is capped by the window, so a "total
        # reaches 2x" rule would never fire a second time.
        since = self._announced.get(key)
        if since is not None:
            fresh = sum(1 for h in recent if h["lap"] > since)
            if fresh < self.cfg.recurrence_min:
                return None
        self._announced[key] = lap

        laps = sorted({h["lap"] for h in recent})
        slips = [h["slip"] for h in recent if h["slip"] is not None]
        speeds = [h["speed"] for h in recent if h["speed"] is not None]
        pcts = [h["lap_dist_pct"] for h in recent]
        finding: dict[str, Any] = {
            "issue": issue,
            "corner": corner,
            "occurrences": count,
            "threshold": self.cfg.recurrence_min,
            "window_laps": window,
            "laps": laps,
            "first_lap": laps[0],
            "last_lap": laps[-1],
            "lap_dist_pct_from": round(min(pcts), 4),
            "lap_dist_pct_to": round(max(pcts), 4),
        }
        if slips:
            finding["worst_slip"] = round(max(slips), 4)
            finding["mean_slip"] = round(sum(slips) / len(slips), 4)
        if speeds:
            finding["mean_speed"] = round(sum(speeds) / len(speeds), 3)
        return finding
