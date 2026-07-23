"""Deterministic detectors: pure functions over a window of recent frames.

Each function inspects the tail of a frame window and returns either ``None`` or
a plain payload ``dict`` describing what it saw. They hold no state and never
emit events themselves -- latching, cooldowns and :class:`RaceEvent` construction
are the engine's job (:mod:`rtv.racestate.engine`). Keeping the predicates pure
is what makes "lockup fires, near-miss doesn't" a two-line unit test.

A detector whose channels are missing from the window returns ``None`` rather
than guessing: no data, no finding.
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
            worst = {
                "speed": round(speed, 3),
                "brake": round(brake, 3),
                "wheel_speed": round(slowest, 3),
                "corner": ch.FRONT_WHEELS[speeds.index(slowest)]
                if len(speeds) == len(ch.FRONT_WHEELS)
                else None,
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
            worst = {
                "speed": round(speed, 3),
                "throttle": round(throttle, 3),
                "wheel_speed": round(fastest, 3),
                "corner": ch.REAR_WHEELS[speeds.index(fastest)]
                if len(speeds) == len(ch.REAR_WHEELS)
                else None,
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
