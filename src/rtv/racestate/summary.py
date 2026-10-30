"""The deterministic post-race summary: laps, stints, fuel, events and the radio.

Built from the two things a finished session leaves behind: the lap table in the
store and the run's race journal (:mod:`rtv.store.journal`). No LLM, no estimate:
a figure is ``None`` when the session did not record what it would be computed
from, and ``notes`` says why.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable
from typing import Any

#: A rise in the tank smaller than this between two snapshots is sensor noise
#: (fuel sloshing), not a refuel.
_REFUEL_MIN_L = 0.5


def _clean(lap: dict[str, Any]) -> bool:
    return bool(
        lap.get("is_valid")
        and not lap.get("is_in_lap")
        and not lap.get("is_out_lap")
        and lap.get("lap_time")
    )


def _lap_stats(laps: list[dict[str, Any]]) -> dict[str, Any]:
    times = [lap["lap_time"] for lap in laps if _clean(lap)]
    best = min((lap for lap in laps if _clean(lap)), key=lambda r: r["lap_time"], default=None)
    return {
        "laps": len(laps),
        "clean_laps": len(times),
        "best_lap": best["lap"] if best else None,
        "best_time": best["lap_time"] if best else None,
        "median_time": statistics.median(times) if times else None,
        "stdev_time": statistics.stdev(times) if len(times) > 1 else None,
    }


def _stints(laps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Valid laps split into stints, each ending on an in-lap."""
    stints: list[list[dict[str, Any]]] = [[]]
    for lap in sorted(laps, key=lambda r: r["lap"]):
        if not lap.get("is_valid"):
            continue
        stints[-1].append(lap)
        if lap.get("is_in_lap"):
            stints.append([])
    out = []
    for i, group in enumerate(s for s in stints if s):
        out.append(
            {
                "stint": i + 1,
                "from_lap": group[0]["lap"],
                "to_lap": group[-1]["lap"],
                **_lap_stats(group),
            }
        )
    return out


def race_summary(
    laps: list[dict[str, Any]], records: Iterable[dict[str, Any]] | None
) -> dict[str, Any]:
    """Summarise a session from its lap rows and (optionally) one journal run."""
    notes: list[str] = []
    valid = [lap for lap in laps if lap.get("is_valid")]
    summary: dict[str, Any] = {
        **_lap_stats(valid),
        "pit_stops": sum(1 for lap in valid if lap.get("is_in_lap")),
        "stints": _stints(laps),
    }

    events: Counter[str] = Counter()
    radio: Counter[str] = Counter()
    refused = ungrounded = 0
    runs: dict[str, dict[str, Any]] = {}
    llm_calls = tool_calls = llm_errors = 0
    latency: list[float] = []
    fuel_used = fuel_added = 0.0
    fuel_seen = False
    last_fuel: float | None = None
    start_position: int | None = None
    last_state = None

    if records is None:
        notes.append("No race journal for this session: events, fuel and radio are unknown.")
    for rec in records or ():
        kind = rec.get("kind")
        data = rec.get("data") or {}
        if kind == "race_event":
            events[data.get("event_type") or "unknown"] += 1
        elif kind == "radio":
            radio[data.get("agent") or "unknown"] += 1
            refused += bool(data.get("refused"))
            ungrounded += not data.get("grounded", True)
        elif kind == "run_start" and rec.get("run_id"):
            runs[rec["run_id"]] = {"agent": rec.get("agent"), "outcome": None}
        elif kind == "run_end" and rec.get("run_id"):
            runs.setdefault(rec["run_id"], {"agent": rec.get("agent")})["outcome"] = data.get(
                "outcome"
            )
        elif kind == "llm_call_end":
            llm_calls += 1
            llm_errors += not data.get("ok", True)
            if isinstance(data.get("latency_ms"), int | float):
                latency.append(float(data["latency_ms"]))
        elif kind == "tool_call":
            tool_calls += 1
        elif kind == "state":
            last_state = data
            pos = (data.get("player") or {}).get("position")
            # iRacing reports position 0 until the car has a classified place.
            if start_position is None and isinstance(pos, int) and pos > 0:
                start_position = pos
            level = (data.get("fuel") or {}).get("level")
            if isinstance(level, int | float):
                fuel_seen = True
                if last_fuel is not None:
                    if level < last_fuel:
                        fuel_used += last_fuel - level
                    elif level - last_fuel >= _REFUEL_MIN_L:
                        fuel_added += level - last_fuel
                last_fuel = level

    if records is not None and not fuel_seen:
        notes.append("The session recorded no fuel level, so fuel use is unknown.")
    player1 = (last_state or {}).get("player") or {}
    outcomes = Counter(r.get("outcome") or "unfinished" for r in runs.values())
    summary.update(
        {
            "start_position": start_position,
            "finish_position": player1.get("position") or None,
            "incidents": player1.get("incidents"),
            "fuel_used_l": round(fuel_used, 2) if fuel_seen else None,
            "fuel_added_l": round(fuel_added, 2) if fuel_seen else None,
            "fuel_per_lap_l": (
                round(fuel_used / summary["laps"], 3)
                if fuel_seen and summary["laps"]
                else None
            ),
            "events": dict(events.most_common()),
            "radio_calls": sum(radio.values()),
            "radio_by_agent": dict(radio.most_common()),
            "radio_refused": refused,
            "radio_ungrounded": ungrounded,
            "agent_runs": len(runs),
            "agent_outcomes": dict(outcomes.most_common()),
            "model_calls": llm_calls,
            "model_errors": llm_errors,
            "model_latency_ms": round(statistics.mean(latency)) if latency else None,
            "tool_calls": tool_calls,
            "notes": notes,
        }
    )
    return summary
