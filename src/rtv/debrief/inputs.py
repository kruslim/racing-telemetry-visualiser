"""What the debrief is built from: one journal run, its stored summary and the lap findings.

Everything here is deterministic and read from what a finished session leaves behind
(stage 09 part B, DB1). No raw telemetry reaches the model: the findings are the
Layer-1 output of :class:`~rtv.coaching.features.CoachingService` (the same service
``/coaching/lap-findings`` serves), compacted to what a debrief talks about.

From the journal run:

* race events (the periodic state snapshots are left out: a Spa journal is ~6.5 MB,
  mostly snapshots), each tagged with the lap it happened on and the review's corner
  label (``payload.place``, plan task E1);
* the radio as it went out, with its air-time outcome (aired, late, expired,
  superseded, refused; plan task B1/B3);
* agent run outcomes per agent;
* plan revisions, accepted and rejected (stage 08). A rejected proposal keeps its lap,
  agent and changed element names but never its numbers, so it cannot become a fact;
* the final state snapshot, plus one fuel and tyre row per lap boundary (the
  projection as it stood at each lap, for "fuel and tyres against the projection").

Every figure the model may write must trace back to this dict (:mod:`rtv.debrief.checks`
builds the typed FactSet over it), so the dict is also the debrief's evidence and is
stored alongside it.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from rtv.logging import get_logger
from rtv.store import journal

log = get_logger("debrief.inputs")

#: Caps on what goes to the model; a long race still fits one call.
MAX_EVENTS = 160
MAX_RADIO = 80
#: A corner counts as "losing" on a lap when its net delta is at least this (s).
LOSS_MIN_S = 0.05
#: How many of a lap's losing corners are listed per lap.
TOP_PER_LAP = 3

#: Race events with no place in a debrief (bookkeeping, or the plan, kept separately).
_SKIP_EVENTS = frozenset({"session_state_change", "plan_revised"})
#: Summary keys the debrief keeps (the rest is model-call accounting).
_SUMMARY_KEYS = (
    "laps",
    "clean_laps",
    "best_lap",
    "best_time",
    "median_time",
    "stdev_time",
    "pit_stops",
    "stints",
    "start_position",
    "finish_position",
    "incidents",
    "fuel_used_l",
    "fuel_added_l",
    "fuel_per_lap_l",
    "events",
    "radio_calls",
    "radio_by_agent",
    "radio_refused",
    "agent_runs",
    "agent_outcomes",
    "notes",
)


def _r(value: Any, nd: int = 3) -> Any:
    if isinstance(value, bool) or isinstance(value, int) or not isinstance(value, float):
        return value
    if not math.isfinite(value):
        return None
    return round(value, nd)


def is_clean(lap: dict[str, Any]) -> bool:
    """A lap the analysis compares: valid, not an in- or out-lap, with a time."""
    return bool(
        lap.get("is_valid")
        and not lap.get("is_in_lap")
        and not lap.get("is_out_lap")
        and lap.get("lap_time")
    )


def load_summary(repo: Any, session_id: str, path: Path) -> dict[str, Any]:
    """The run's stored ``<run_id>.summary.json``, else the same shape computed."""
    stored = journal.summary_path(path)
    if stored.is_file():
        try:
            return json.loads(stored.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Unreadable summary %s; recomputing it", stored)
    from rtv.session_end import run_summary

    return run_summary(repo, session_id, path)


def _place(payload: dict[str, Any]) -> dict[str, Any] | None:
    place = payload.get("place")
    if not isinstance(place, dict) or not place.get("label"):
        return None
    return {
        "corner": place.get("label"),
        "name": place.get("name"),
        "provisional": bool(place.get("provisional")),
    }


def _event_row(rec: dict[str, Any], lap: int | None) -> dict[str, Any]:
    data = rec.get("data") or {}
    payload = data.get("payload") or {}
    row: dict[str, Any] = {
        "lap": data.get("lap") if isinstance(data.get("lap"), int) else lap,
        "t_s": _r(rec.get("session_time"), 1),
        "type": data.get("event_type") or data.get("key"),
        "severity": data.get("severity"),
    }
    place = _place(payload)
    if place:
        row.update(place)
    # A few scalar facts of the payload, never nested blobs.
    keep = {}
    for key, value in payload.items():
        if key == "place" or isinstance(value, dict | list):
            continue
        if isinstance(value, int | float | str | bool) or value is None:
            keep[key] = _r(value)
    if keep:
        row["detail"] = dict(list(keep.items())[:8])
    return row


def _radio_status(data: dict[str, Any]) -> str:
    if data.get("refused"):
        return "refused"
    if data.get("expired"):
        return "expired"
    if data.get("superseded"):
        return "superseded"
    if data.get("late"):
        return "late"
    return "aired"


def _fuel_row(lap: int, state: dict[str, Any]) -> dict[str, Any] | None:
    fuel = state.get("fuel") or {}
    if not isinstance(fuel.get("level"), int | float):
        return None
    return {
        "lap": lap,
        "level_l": _r(fuel.get("level"), 2),
        "per_lap_l": _r(fuel.get("per_lap"), 3),
        "laps_remaining": _r(fuel.get("laps_remaining"), 2),
        "margin_laps": _r(fuel.get("margin_laps"), 2),
    }


def _tyre_row(lap: int, state: dict[str, Any]) -> dict[str, Any] | None:
    tyres = state.get("tyres") or {}
    temps = tyres.get("temps") or {}
    if not temps:
        return None
    pressures = tyres.get("pressures") or {}
    return {
        "lap": lap,
        "temps_c": {k: _r(v, 1) for k, v in temps.items()},
        "pressures_kpa": {k: _r(v, 1) for k, v in pressures.items()},
        "stint_laps": tyres.get("stint_laps"),
    }


def _final_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    player = state.get("player") or {}
    fuel = state.get("fuel") or {}
    race_control = state.get("race_control")
    return {
        # Stage 09 part A: incidents against the limit, track-limit offs, penalties.
        "race_control": race_control if isinstance(race_control, dict) else None,
        "player": {
            k: _r(player.get(k))
            for k in (
                "lap",
                "position",
                "class_position",
                "incidents",
                "best_lap_time",
                "last_lap_time",
                "stint",
                "laps_on_tyres",
            )
            if player.get(k) is not None
        },
        "fuel": {
            k: _r(fuel.get(k))
            for k in ("level", "per_lap", "laps_remaining", "margin_laps", "capacity")
            if fuel.get(k) is not None
        },
    }


def read_run(path: Path) -> dict[str, Any]:
    """One pass over the journal: everything DB1 asks for except the findings."""
    events: list[dict[str, Any]] = []
    radio: list[dict[str, Any]] = []
    outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    revisions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    fuel_by_lap: list[dict[str, Any]] = []
    tyres_by_lap: list[dict[str, Any]] = []
    lap: int | None = None
    last_state: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []  # bounds and laps_for_run need every record's time
    for rec in journal.iter_records(path):
        kind = rec.get("kind")
        data = rec.get("data") or {}
        records.append(
            {"kind": kind, "session_time": rec.get("session_time"), "data": data}
            if kind == "state"
            else {"kind": kind, "session_time": rec.get("session_time")}
        )
        if kind == "state":
            last_state = data
            seen = (data.get("player") or {}).get("lap")
            if isinstance(seen, int) and seen != lap:
                lap = seen
                if (row := _fuel_row(seen, data)) is not None:
                    fuel_by_lap.append(row)
                if (row := _tyre_row(seen, data)) is not None:
                    tyres_by_lap.append(row)
        elif kind == "race_event":
            etype = data.get("event_type") or data.get("key")
            if etype == "plan_revised":
                payload = data.get("payload") or {}
                revisions.append(
                    {
                        "lap": payload.get("lap", lap),
                        "agent": payload.get("agent"),
                        "provenance": payload.get("provenance"),
                        "from_version": payload.get("old_version"),
                        "to_version": payload.get("new_version"),
                        # {name: value} on each side, so the typed FactSet reads the
                        # unit from the element name (fuel_to_add_l, next_stop_lap).
                        "from": {
                            k: _r((v or {}).get("from"))
                            for k, v in (payload.get("changes") or {}).items()
                        },
                        "to": {
                            k: _r((v or {}).get("to"))
                            for k, v in (payload.get("changes") or {}).items()
                        },
                    }
                )
            elif etype not in _SKIP_EVENTS:
                events.append(_event_row(rec, lap))
        elif kind == "radio":
            radio.append(
                {
                    "lap": lap,
                    "t_s": _r(rec.get("session_time"), 1),
                    "agent": data.get("agent"),
                    "priority": data.get("priority"),
                    "source": data.get("source"),
                    "status": _radio_status(data),
                    "text": data.get("spoken_text") or "",
                }
            )
        elif kind == "run_end" and rec.get("agent"):
            outcomes[rec["agent"]][data.get("outcome") or "unfinished"] += 1
        elif kind == "plan_proposal_rejected":
            rejected.append(
                {
                    "lap": data.get("lap", lap),
                    "agent": rec.get("agent") or data.get("agent"),
                    "changed": sorted((data.get("changes") or {}).keys())
                    or sorted((data.get("proposal") or {}).keys()),
                    "reason": data.get("reason"),
                }
            )
    plan = (last_state or {}).get("plan")
    return {
        "events": events,
        "radio": radio,
        "agent_runs": {a: dict(c.most_common()) for a, c in sorted(outcomes.items())},
        "plan": {
            "final": _plan_values(plan),
            "revisions": revisions,
            "rejected": rejected,
        },
        "final_state": _final_state(last_state),
        "fuel_by_lap": fuel_by_lap,
        "tyres_by_lap": tyres_by_lap,
        "_records": records,
    }


def _plan_values(plan: Any) -> dict[str, Any] | None:
    """The final plan's element values (no rationales: those are free text)."""
    if not isinstance(plan, dict):
        return None
    out: dict[str, Any] = {"version": plan.get("version"), "stops_done": plan.get("stops_done")}
    for name, element in plan.items():
        if isinstance(element, dict) and "value" in element:
            out[name] = _r(element.get("value"))
            out.setdefault("provenance", {})[name] = element.get("provenance")
    missing = plan.get("missing")
    if isinstance(missing, dict) and missing:
        out["missing"] = dict(missing)
    return out


def lap_rows(laps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The run's laps as the debrief cites them, with the gap to the best clean lap."""
    best = min((lap["lap_time"] for lap in laps if is_clean(lap)), default=None)
    out = []
    for lap in sorted(laps, key=lambda r: r["lap"]):
        clean = is_clean(lap)
        row = {
            "lap": lap["lap"],
            "lap_time_s": _r(lap.get("lap_time")) if lap.get("is_valid") else None,
            "clean": clean,
            "in_lap": bool(lap.get("is_in_lap")),
            "out_lap": bool(lap.get("is_out_lap")),
        }
        if clean and best is not None:
            row["to_best_s"] = _r(lap["lap_time"] - best)
        out.append(row)
    return out


def findings_digest(
    coaching: Any, session_id: str, laps: list[dict[str, Any]]
) -> dict[str, Any]:
    """Layer-1 findings of every clean lap against the best one, compacted.

    The analysis itself is :meth:`CoachingService.lap_findings`, unchanged; this only
    counts what it found per corner across the race (DB1: reuse, do not re-implement).
    ``corners`` holds, per corner label, the full Layer-1 finding of the lap that lost
    most there: the prose check holds a corner claim to it.
    """
    clean = [lap for lap in laps if is_clean(lap)]
    notes: list[str] = []
    if coaching is None or len(clean) < 2:
        notes.append(
            "Fewer than two clean laps were recorded, so there is no lap-against-lap "
            "analysis."
            if coaching is not None
            else "No coaching service: the lap findings are unavailable."
        )
        return {"reference_lap": None, "laps": [], "corners": [], "recurring": [], "notes": notes}
    ref = min(clean, key=lambda r: r["lap_time"])["lap"]
    per_lap: list[dict[str, Any]] = []
    worst: dict[str, tuple[float, int, dict[str, Any]]] = {}
    losing: dict[str, list[tuple[int, float]]] = defaultdict(list)
    reasons: dict[str, Counter[str]] = defaultdict(Counter)
    names: dict[str, str | None] = {}
    for lap in clean:
        if lap["lap"] == ref:
            continue
        try:
            found = coaching.lap_findings(session_id, lap["lap"], ref).to_dict()
        except (KeyError, FileNotFoundError, ValueError) as exc:
            notes.append(f"Lap {lap['lap']}: no findings ({exc}).")
            continue
        corners = found.get("corners") or []
        top = sorted(
            (c for c in corners if (c.get("net_dt") or 0) >= LOSS_MIN_S),
            key=lambda c: -(c.get("net_dt") or 0),
        )
        chief = found.get("chief") or {}
        per_lap.append(
            {
                "lap": lap["lap"],
                "lap_time_s": _r(found.get("main_time")),
                "net_lap_s": _r(chief.get("net_lap")),
                "lost_s": _r(chief.get("lost")),
                "losing_corners": chief.get("losing_n"),
                "top": [
                    {
                        "corner": c["label"],
                        "name": c.get("name"),
                        "loss_s": _r(c.get("net_dt")),
                        "why": _top_reason(c),
                    }
                    for c in top[:TOP_PER_LAP]
                ],
            }
        )
        for c in corners:
            label = c.get("label")
            if not label:
                continue
            names.setdefault(label, c.get("name"))
            dt = float(c.get("net_dt") or 0.0)
            if dt >= LOSS_MIN_S:
                losing[label].append((lap["lap"], dt))
                if (why := _top_reason(c)) is not None:
                    reasons[label][why] += 1
            if label not in worst or dt > worst[label][0]:
                worst[label] = (dt, lap["lap"], c)
        notes.extend(n for n in found.get("notes") or [] if n not in notes)
    by_corner = []
    for label, hits in losing.items():
        total = sum(dt for _lap, dt in hits)
        worst_lap, worst_dt = max(hits, key=lambda h: h[1])
        by_corner.append(
            {
                "corner": label,
                "name": names.get(label),
                "laps_losing": len(hits),
                "laps": [lap for lap, _dt in hits],
                "total_loss_s": _r(total),
                "mean_loss_s": _r(total / len(hits)),
                "worst_lap": worst_lap,
                "worst_loss_s": _r(worst_dt),
                "top_reason": reasons[label].most_common(1)[0][0] if reasons[label] else None,
            }
        )
    by_corner.sort(key=lambda r: -(r["total_loss_s"] or 0))
    corners_full = []
    for _label, (_dt, lap_no, c) in sorted(worst.items(), key=lambda kv: _label_key(kv[0])):
        corners_full.append(
            {
                **c,
                "lap": lap_no,
                # The same figures under unit-bearing names, for the typed FactSet.
                "apex_m": _r(c.get("distance"), 1),
                "min_speed_kmh": _r(c.get("min_main"), 1),
                "ref_min_speed_kmh": _r(c.get("min_ref"), 1),
                "loss_s": _r(c.get("net_dt")),
            }
        )
    return {
        "reference_lap": ref,
        "laps": per_lap,
        "corners": corners_full,
        "recurring": by_corner,
        "notes": notes,
    }


def _label_key(label: str) -> tuple[int, str]:
    digits = "".join(ch for ch in label if ch.isdigit())
    return (int(digits) if digits else 10_000, label)


def _top_reason(corner: dict[str, Any]) -> str | None:
    diags = [d for d in corner.get("diags") or [] if not d.get("good") and not d.get("variance")]
    if not diags:
        return None
    return max(diags, key=lambda d: d.get("time_loss") or 0.0).get("text")


def build_inputs(
    *,
    session_id: str,
    path: Path,
    repo: Any,
    coaching: Any,
) -> dict[str, Any]:
    """The debrief's whole input for one journal run (no model, no network)."""
    meta = journal.summarize_run(path)
    run = read_run(path)
    records = run.pop("_records")
    summary = load_summary(repo, session_id, path)
    all_laps = repo.get_laps(session_id) if repo is not None else []
    laps = journal.laps_for_run(all_laps, records)
    # The unfinished last segment and lap 0 are not race laps.
    laps = [lap for lap in laps if lap.get("lap")]
    findings = findings_digest(coaching, session_id, laps)
    corner_names = {
        c["corner"]: c.get("name") for c in run["events"] if c.get("corner") and c.get("name")
    }
    corner_names.update(
        {c["label"]: c.get("name") for c in findings["corners"] if c.get("name")}
    )
    events = run["events"]
    radio = run["radio"]
    return {
        "run": {
            "session_id": session_id,
            "run_id": path.stem,
            "source": meta.get("source"),
            "session_type": summary.get("session_type") or meta.get("session_type"),
            "session_num": summary.get("session_num", meta.get("session_num")),
            "end_reason": meta.get("end_reason"),
            "calls_used": meta.get("calls_used"),
        },
        "summary": {k: summary.get(k) for k in _SUMMARY_KEYS if k in summary},
        "laps": lap_rows(laps),
        # Laps the run completed (an unfinished last segment is not a lap).
        "recorded_laps": sorted({lap["lap"] for lap in laps if lap.get("is_valid")}),
        "events": events[:MAX_EVENTS],
        "events_dropped": max(0, len(events) - MAX_EVENTS),
        "radio": radio[-MAX_RADIO:],
        "radio_dropped": max(0, len(radio) - MAX_RADIO),
        "agent_runs": run["agent_runs"],
        "plan": run["plan"],
        "final_state": run["final_state"],
        "fuel_by_lap": run["fuel_by_lap"],
        "tyres_by_lap": run["tyres_by_lap"],
        "findings": findings,
        "corner_names": dict(sorted(corner_names.items(), key=lambda kv: _label_key(kv[0]))),
    }
