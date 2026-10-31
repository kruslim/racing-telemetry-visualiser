"""Derived figures the agents keep computing, computed once so they can be copied.

Task A3. On the Spa replays most validator failures were not inventions but
arithmetic: a pressure rise since the stint started, a front-to-rear temperature
split, a lap time against the previous lap, "three laps running". A model that
subtracts two facts in its head says a number that is in no tool result, and the
validator refuses it. The cure is to hand the agent the number, with a path and a
unit, so it copies instead of computes.

Speeds get the same treatment for a different reason. The detectors report speed in
m/s (the sim's own unit), while the Layer-1 corner data and every racing radio
speak km/h. On the A1 replay three of four refusals were an off-track speed of
27.7 m/s said as "27.7 km/h": the agent assumed the unit. The agent's view of an
event therefore carries the speed in km/h *instead of* m/s, so the only speed it
can quote is the one it means.

Everything here is pure and deterministic, a view over the race state and the
event payloads; nothing calls a model. The engine's own events keep the sim's m/s:
only what an agent is shown is converted.
"""

from __future__ import annotations

import math
from typing import Any

from rtv.grounding.units import convert

__all__ = [
    "agent_payload",
    "axle_split",
    "change",
    "consecutive_run",
    "lap_time_deltas",
]

#: Event payload speeds in m/s -> the km/h key the agent sees instead.
_SPEED_KEYS = {
    "speed": "speed_kmh",
    "mean_speed": "mean_speed_kmh",
    "wheel_speed": "wheel_speed_kmh",
}

_FRONT, _REAR, _LEFT, _RIGHT = ("LF", "RF"), ("LR", "RR"), ("LF", "LR"), ("RF", "RR")


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _kmh(value: Any) -> float | None:
    ms = _num(value)
    return None if ms is None else round(convert(ms, "speed", "ms", "kmh"), 1)


def consecutive_run(laps: Any) -> int:
    """How many laps in a row, ending at the last one: ``[2, 5, 6, 7]`` -> 3."""
    nums = sorted({int(v) for v in laps or [] if _num(v) is not None})
    if not nums:
        return 0
    run = 1
    for prev, cur in zip(reversed(nums[:-1]), reversed(nums[1:]), strict=True):
        if cur - prev != 1:
            break
        run += 1
    return run


def agent_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """An event payload as an agent is shown it (and as its FactSet holds it).

    m/s speeds become km/h; a recurring issue says how many laps running it has
    happened (``consecutive_laps``) and how many laps the window covers so far.
    Unknown keys pass through untouched, nested payloads too.
    """
    if not isinstance(payload, dict):
        return payload  # type: ignore[return-value]
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _SPEED_KEYS and _num(value) is not None:
            out[_SPEED_KEYS[key]] = _kmh(value)
            continue
        if isinstance(value, dict):
            value = agent_payload(value)
        out[key] = value
    laps = payload.get("laps")
    if isinstance(laps, list) and laps and "occurrences" in payload:
        out["consecutive_laps"] = consecutive_run(laps)
        # "three laps running wide" for three offs: the count of occurrences is said
        # as laps, so the distinct laps it happened on are a laps fact too.
        out["laps_with_issue"] = len({v for v in laps if _num(v) is not None})
        first, last = _num(payload.get("first_lap")), _num(payload.get("last_lap"))
        if first is not None and last is not None:
            out["laps_spanned"] = int(last - first) + 1
    return out


def _mean(values: dict[str, float], corners: tuple[str, str]) -> float | None:
    got = [_num(values.get(c)) for c in corners]
    if any(v is None for v in got):
        return None
    return sum(got) / len(got)  # type: ignore[arg-type]


def axle_split(values: dict[str, float], unit_suffix: str) -> dict[str, Any]:
    """Front-minus-rear and left-minus-right of a per-corner reading, plus the spread."""
    out: dict[str, Any] = {}
    nums = {c: v for c, v in (values or {}).items() if _num(v) is not None}
    if not nums:
        return out
    front, rear = _mean(nums, _FRONT), _mean(nums, _REAR)
    left, right = _mean(nums, _LEFT), _mean(nums, _RIGHT)
    if front is not None and rear is not None:
        out[f"front_minus_rear_{unit_suffix}"] = round(front - rear, 2)
    if left is not None and right is not None:
        out[f"left_minus_right_{unit_suffix}"] = round(left - right, 2)
    hi = max(nums, key=lambda c: nums[c])
    lo = min(nums, key=lambda c: nums[c])
    out[f"spread_{unit_suffix}"] = round(nums[hi] - nums[lo], 2)
    out["highest"] = hi
    out["lowest"] = lo
    return out


def change(now: dict[str, float], then: dict[str, float]) -> dict[str, float]:
    """Per-corner ``now - then``, where both are known."""
    out: dict[str, float] = {}
    for corner, value in (now or {}).items():
        a, b = _num(value), _num((then or {}).get(corner))
        if a is not None and b is not None:
            out[corner] = round(a - b, 2)
    return out


def lap_time_deltas(laps: list[dict[str, Any]], best: float | None) -> dict[str, Any]:
    """The last timed lap against the previous one, the stint's first and the best."""
    timed = [
        (row.get("lap"), _num(row.get("lap_time_s")), bool(row.get("green")))
        for row in laps or []
    ]
    timed = [(lap, t, green) for lap, t, green in timed if t is not None and t > 0]
    out: dict[str, Any] = {}
    if not timed:
        return out
    last_lap, last, _ = timed[-1]
    out["last_lap"] = last_lap
    out["last_lap_time_s"] = last
    if len(timed) >= 2:
        prev_lap, prev, _ = timed[-2]
        out["previous_lap"] = prev_lap
        out["previous_lap_time_s"] = prev
        out["delta_vs_previous_lap_s"] = round(last - prev, 3)
        first_lap, first, _ = timed[0]
        out["stint_first_timed_lap"] = first_lap
        out["delta_vs_stint_first_lap_s"] = round(last - first, 3)
    best = _num(best)
    if best is not None and best > 0:
        out["delta_vs_best_lap_s"] = round(last - best, 3)
    green_run = 0
    for row in reversed(laps or []):
        if not row.get("green"):
            break
        green_run += 1
    out["green_streak_laps"] = green_run
    return out
