"""The post-race review as data: journal records and laps in, lanes and audit out.

A port of ``frontend/js/review/review-model.js``. Every figure the review shows is
either the backend's as-is (lap times, findings, the summary) or a grouping of
recorded records here; nothing is computed into existence.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from typing import Any

from .activity import AGENTS, round_figures
from .format import NA, integer, is_num

OWN_LANE = {"lap_completed", "flag_change"}

# Records whose session_time is the race clock at the moment they were written (agent
# records instead carry the time of the event that woke them).
CLOCK_KINDS = {"state", "race_event", "radio_aired", "lifecycle"}


def _seq_key(record: dict) -> int:
    seq = record.get("seq")
    return seq if isinstance(seq, int) else 0


def is_clean(lap: dict | None) -> bool:
    """Valid, neither an in- nor an out-lap, and timed."""
    return bool(
        lap
        and lap.get("is_valid")
        and not lap.get("is_in_lap")
        and not lap.get("is_out_lap")
        and is_num(lap.get("lap_time"))
        and lap["lap_time"] > 0
    )


def default_laps(laps: list[dict] | None) -> dict:
    """The clean lap nearest the clean median against the best clean lap."""
    clean = [lap for lap in laps or [] if is_clean(lap)]
    if not clean:
        return {"main": None, "ref": None}
    best = clean[0]
    for lap in clean[1:]:
        if lap["lap_time"] < best["lap_time"]:
            best = lap
    others = [lap for lap in clean if lap["lap"] != best["lap"]]
    if not others:
        return {"main": best["lap"], "ref": best["lap"]}
    times = sorted(lap["lap_time"] for lap in clean)
    median = times[len(times) // 2]
    typical = others[0]
    for lap in others[1:]:
        if abs(lap["lap_time"] - median) < abs(typical["lap_time"] - median):
            typical = lap
    return {"main": typical["lap"], "ref": best["lap"]}


def lap_at(laps: list[dict] | None, t: float) -> dict | None:
    """The lap being driven at session time ``t``."""
    found = None
    for lap in laps or []:
        start = lap.get("start_session_time")
        if is_num(start) and start <= t and (found is None or start > found["start_session_time"]):
            found = lap
    return found


def bounds(run: dict | None, laps: list[dict] | None) -> tuple[float, float] | None:
    """Session-time bounds of a run, from its summary or else its laps."""
    t0 = (run or {}).get("session_time_start")
    t1 = (run or {}).get("session_time_end")
    for lap in laps or []:
        start = lap.get("start_session_time")
        if not is_num(start):
            continue
        end = start + (lap.get("lap_time") or 0)
        t0 = min(t0, start) if is_num(t0) else start
        t1 = max(t1, end) if is_num(t1) else end
    if not is_num(t0) or not is_num(t1) or t1 <= t0:
        return None
    return (t0, t1)


def session_clock(records: list[dict] | None):
    """Map wall-clock ms onto session time using the journal's own state records.

    Agent records carry the session time of the event that woke them, so a run's
    end, or the moment a call aired, is only on the race clock through the state
    snapshots, race events and aired calls recorded around it. Returns ``None`` when
    the journal has none.
    """
    pairs = sorted(
        (r["wall_ms"], r["session_time"])
        for r in records or []
        if r.get("kind") in CLOCK_KINDS
        and is_num(r.get("wall_ms"))
        and is_num(r.get("session_time"))
    )
    if len(pairs) < 2:
        return None
    walls = [w for w, _ in pairs]

    def at(wall_ms: Any) -> float | None:
        if not is_num(wall_ms):
            return None
        i = bisect_left(walls, wall_ms)
        if i <= 0:
            return pairs[0][1]
        if i >= len(pairs):
            return pairs[-1][1]
        (w0, s0), (w1, s1) = pairs[i - 1], pairs[i]
        return s0 + (s1 - s0) * ((wall_ms - w0) / (w1 - w0) if w1 > w0 else 0)

    return at


def aired_times(records: list[dict] | None) -> dict:
    """``message_id -> session time it went on air`` from ``radio_aired`` records."""
    out = {}
    for rec in records or []:
        if rec.get("kind") == "radio_aired":
            mid = (rec.get("data") or {}).get("message_id")
            if mid and is_num(rec.get("session_time")):
                out.setdefault(mid, rec["session_time"])
    return out


def air_badge(message: dict | None) -> dict | None:
    """What the radio decided at air time, for a badge: late, dropped, or nothing.

    ``{"kind": "late", "text": "late +27s"}`` for a call aired after its agent's
    limit, ``{"kind": "expired", "text": "not aired", "reason": ...}`` for one
    dropped because it had stopped being true. ``None`` for an ordinary call and
    for anything recorded before the radio checked air time.
    """
    if not isinstance(message, dict):
        return None
    if message.get("expired"):
        return {
            "kind": "expired",
            "text": "not aired",
            "reason": message.get("expired_reason") or "no longer valid",
        }
    if message.get("late"):
        by = message.get("late_by_s")
        return {"kind": "late", "text": f"late +{integer(by)}s" if is_num(by) else "late"}
    return None


def _radio_when(data: dict, aired: dict, t: Any) -> Any:
    """When a call reached the channel: aired, or (for a dropped one) dropped."""
    if data.get("expired") and is_num(data.get("air_session_time")):
        return data["air_session_time"]
    return aired.get(data.get("message_id"), t)


def lane_items(records: list[dict] | None, laps: list[dict] | None) -> dict:
    """Every timeline lane as items: laps, flags, events, one per agent, radio.

    Agent spans run from the waking event to the run's end on the race clock, and
    radio items sit where the call aired, so a slow model call is visibly long.
    """
    clock_at = session_clock(records)
    aired = aired_times(records)
    lanes: dict[str, list] = {"laps": [], "flags": [], "events": [], "radio": []}
    for agent in AGENTS:
        lanes[agent] = []

    for lap in laps or []:
        start, length = lap.get("start_session_time"), lap.get("lap_time")
        if not is_num(start) or not (is_num(length) and length > 0):
            continue
        tags = ", ".join(
            tag
            for tag, on in (
                ("in", lap.get("is_in_lap")),
                ("out", lap.get("is_out_lap")),
                ("invalid", not lap.get("is_valid")),
            )
            if on
        )
        lanes["laps"].append(
            {
                "t0": start,
                "t1": start + length,
                "label": f"lap {lap.get('lap')}" + (f" ({tags})" if tags else ""),
                "cls": "lap-clean" if is_clean(lap) else "lap-other",
                "record": {"kind": "lap", "data": lap},
            }
        )

    flag = None
    runs: dict[str, dict] = {}
    ordered = sorted(records or [], key=_seq_key)
    for rec in ordered:
        t = rec.get("session_time")
        if not is_num(t):
            continue
        data = rec.get("data") or {}
        kind = rec.get("kind")
        if kind == "race_event":
            if data.get("event_type") == "flag_change":
                if flag:
                    flag["t1"] = t
                phase = (data.get("payload") or {}).get("to") or "flag"
                flag = {
                    "t0": t,
                    "t1": None,
                    "label": f"{phase} flag",
                    "cls": f"flag-{phase}",
                    "phase": phase,
                    "record": rec,
                }
                lanes["flags"].append(flag)
            elif data.get("event_type") not in OWN_LANE:
                lap = data.get("lap")
                lanes["events"].append(
                    {
                        "t0": t,
                        "label": f"{data.get('event_type')}"
                        + (f" · lap {lap}" if lap is not None else ""),
                        "cls": f"sev-{data.get('severity') or 'info'}",
                        "severity": data.get("severity") or "info",
                        "record": rec,
                    }
                )
        elif kind == "run_start" and rec.get("agent") in lanes:
            item = {
                "t0": t,
                "t1": t,
                "label": f"{rec['agent']} woken by {rec.get('event_key') or '?'}",
                "cls": f"agent-{rec['agent']}",
                "agent": rec["agent"],
                "outcome": None,
                "record": rec,
            }
            runs[rec.get("run_id")] = item
            lanes[rec["agent"]].append(item)
        elif kind == "run_end" and rec.get("run_id") in runs:
            item = runs[rec["run_id"]]
            end_t = clock_at(rec.get("wall_ms")) if clock_at else None
            item["t1"] = max(item["t0"], end_t if is_num(end_t) else t)
            item["label"] += f" → {data.get('outcome') or '?'}"
            item["outcome"] = data.get("outcome")
            if data.get("outcome") and data["outcome"] != "radio":
                item["cls"] += " run-quiet"
        elif kind == "radio":
            badge = air_badge(data)
            note = ""
            if badge and badge["kind"] == "expired":
                note = f" (not aired: {badge['reason']})"
            elif badge:
                note = f" ({badge['text']})"
            lanes["radio"].append(
                {
                    "t0": _radio_when(data, aired, t),
                    "event_t": t,
                    "label": f"{data.get('agent')}{note}: {data.get('spoken_text') or ''}",
                    "cls": f"agent-{data.get('agent')}"
                    + (" refused" if data.get("refused") else "")
                    + (f" {badge['kind']}" if badge else ""),
                    "agent": data.get("agent"),
                    "refused": bool(data.get("refused")),
                    "late": bool(data.get("late")),
                    "expired": bool(data.get("expired")),
                    "record": rec,
                }
            )
    end = ordered[-1].get("session_time") if ordered else None
    if flag and flag["t1"] is None:
        flag["t1"] = end if is_num(end) else flag["t0"]
    # A run that never ended (the session stopped mid-call) stays a point.
    for item in runs.values():
        if item["t1"] == item["t0"]:
            del item["t1"]
    return lanes


def audit_rows(records: list[dict] | None) -> list[dict]:
    """One row per agent run: trigger, model and tool calls, validator, outcome."""
    rows: dict[str, dict] = {}
    by_message: dict[Any, dict] = {}
    for rec in sorted(records or [], key=_seq_key):
        data = rec.get("data") or {}
        kind = rec.get("kind")
        if kind == "radio":
            by_message[data.get("message_id")] = data
            continue
        run_id = rec.get("run_id")
        if not run_id:
            continue
        row = rows.get(run_id)
        if row is None:
            row = {
                "run_id": run_id,
                "agent": rec.get("agent"),
                "event_key": rec.get("event_key"),
                "t": rec.get("session_time"),
                "model": None,
                "llm_calls": 0,
                "llm_errors": 0,
                "llm_ms": 0.0,
                "tools": [],
                "tool_errors": 0,
                "validations": 0,
                "unsupported": [],
                "reasons": [],
                "repaired": False,
                "outcome": None,
                "latency_ms": None,
                "message_id": None,
                "context_from": [],
            }
            rows[run_id] = row
        if kind == "run_start":
            row["model"] = data.get("model") or row["model"]
            row["context_from"] = [
                m.get("agent") for m in data.get("context_radio") or [] if m.get("agent")
            ]
        elif kind == "llm_call_end":
            row["llm_calls"] += 1
            if not data.get("ok"):
                row["llm_errors"] += 1
            row["llm_ms"] += float(data.get("latency_ms") or 0)
        elif kind == "tool_call":
            row["tools"].append(data.get("tool"))
            if not data.get("ok"):
                row["tool_errors"] += 1
        elif kind == "validate":
            row["validations"] += 1
            if not data.get("ok"):
                for u in data.get("unsupported") or []:
                    shown = round_figures(u)
                    if shown not in row["unsupported"]:
                        row["unsupported"].append(shown)
                for r in data.get("unsupported_detail") or []:
                    shown = round_figures(r)
                    if shown not in row["reasons"]:
                        row["reasons"].append(shown)
            if data.get("repaired"):
                row["repaired"] = True
        elif kind == "run_end":
            row["outcome"] = data.get("outcome")
            row["latency_ms"] = data.get("latency_ms") if is_num(data.get("latency_ms")) else None
            row["message_id"] = data.get("message_id")
    out = []
    for row in rows.values():
        message = by_message.get(row["message_id"]) if row["message_id"] else None
        out.append(
            {
                **row,
                "spoken": message.get("spoken_text") if message else None,
                "grounded": (message.get("grounded") is not False) if message else None,
                "lap": ((message or {}).get("event_ref") or {}).get("lap"),
            }
        )
    return out


def filter_audit(
    rows: list[dict], agent: str = "", outcome: str = "", sort: str = "time"
) -> list[dict]:
    """Filter and sort the audit. ``sort``: time | latency | calls."""
    out = [
        r
        for r in rows
        if (not agent or r["agent"] == agent)
        and (not outcome or (r["outcome"] or "unfinished") == outcome)
    ]
    keys = {
        "time": lambda r: r["t"] if r["t"] is not None else 0,
        "latency": lambda r: -(r["latency_ms"] if r["latency_ms"] is not None else -1),
        "calls": lambda r: -(r["llm_calls"] + len(r["tools"])),
    }
    return sorted(out, key=keys.get(sort, keys["time"]))


def _newest_first(records: list[dict]) -> list[dict]:
    return sorted(records, key=lambda r: r.get("session_time") or 0, reverse=True)


def radio_up_to(records: list[dict] | None, t: float | None, limit: int = 40) -> list[dict]:
    """Radio calls that had aired at or before ``t``, newest first."""
    aired = aired_times(records)

    def when(rec: dict) -> Any:
        return _radio_when(rec.get("data") or {}, aired, rec.get("session_time"))

    out = [
        rec
        for rec in records or []
        if rec.get("kind") == "radio" and not (is_num(t) and is_num(when(rec)) and when(rec) > t)
    ]
    return sorted(out, key=lambda r: when(r) or 0, reverse=True)[:limit]


def events_up_to(records: list[dict] | None, t: float | None, limit: int = 12) -> list[dict]:
    """Race events at or before ``t``, newest first."""
    out = [
        rec
        for rec in records or []
        if rec.get("kind") == "race_event"
        and (not is_num(t) or not is_num(rec.get("session_time")) or rec["session_time"] <= t)
    ]
    return _newest_first(out)[:limit]


def to_metres(x_values: list[float] | None, lap_length: Any) -> list[float]:
    """Lap-distance fractions from ``/compare`` onto metres."""
    if not (is_num(lap_length) and lap_length > 0):
        return list(x_values or [])
    return [x * lap_length for x in x_values or []]


def corner_span(corner: dict | None) -> tuple[float, float] | None:
    """Corner region for highlighting: ``(entry, exit)`` in metres."""
    if not corner:
        return None
    d = corner.get("distance") or 0
    a = corner["entry_m"] if is_num(corner.get("entry_m")) else d - 60
    b = corner["exit_m"] if is_num(corner.get("exit_m")) else d + 60
    return (min(a, b), max(a, b))


def corner_title(corner: dict | None) -> str:
    """``T1 La Source`` where the circuit's name is mapped, else just ``T1``."""
    if not corner:
        return NA
    name = corner.get("name")
    return f"{corner.get('label')} {name}" if name else str(corner.get("label"))


def priority_title(item: dict, corners: list[dict] | None) -> str:
    """A top-3 opportunity's corner, named from the findings' own corner list."""
    by_label = {c.get("label"): c for c in corners or []}
    return corner_title(by_label.get(item.get("label")) or {"label": item.get("label")})


def track_names(track: dict | None) -> list[dict]:
    """Named corners on the map, from ``/sessions/{id}/track``: ``{pct, name}``.

    Only corners (not named straights), placed at the middle of their mapped run.
    """
    out = []
    for c in (track or {}).get("corners") or []:
        if c.get("kind") == "corner" and is_num(c.get("mid_pct")) and c.get("name"):
            out.append({"pct": c["mid_pct"], "name": c["name"]})
    return out


def track_facts(track: dict | None) -> str:
    """One line of layout facts from the session header, plus where names come from."""
    if not track:
        return ""
    h = track.get("header") or {}
    parts = [h.get("display_name") or h.get("iracing_track_name")]
    if h.get("config_name"):
        parts.append(h["config_name"])
    if is_num(h.get("length_m")):
        parts.append(f"{h['length_m'] / 1000:.2f} km")
    if is_num(h.get("num_turns")):
        parts.append(f"{h['num_turns']} turns")
    if is_num(h.get("pit_speed_limit_kph")):
        parts.append(f"pit {h['pit_speed_limit_kph']:.0f} km/h")
    line = " · ".join(str(p) for p in parts if p)
    if track.get("corners"):
        line += " · corner names © OpenStreetMap contributors"
    elif track.get("reference") is None:
        line += " · no corner names for this venue"
    return line


def track_events(records: list[dict] | None) -> list[dict]:
    """Events pinned to a place on track, for the map: offs, incidents, lock-ups."""
    out = []
    for rec in records or []:
        if rec.get("kind") != "race_event":
            continue
        data = rec.get("data") or {}
        pct = (data.get("payload") or {}).get("lap_dist_pct")
        if not is_num(pct):
            continue
        out.append(
            {
                "pct": pct,
                "type": data.get("event_type"),
                "severity": data.get("severity") or "info",
                "lap": data.get("lap"),
                "t": rec.get("session_time"),
            }
        )
    return out


def point_at_pct(points: list[dict] | None, pct: float) -> dict | None:
    """The nearest map point to a lap-distance fraction (the lap wraps)."""
    best, best_d = None, math.inf
    for p in points or []:
        x = p.get("lap_dist_pct")
        if not is_num(x):
            continue
        d = abs(x - pct)
        d = min(d, 1 - d)
        if d < best_d:
            best, best_d = p, d
    return best
