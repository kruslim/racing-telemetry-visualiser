"""The race plan as the desktop app shows it (stage 08). Pure Python, no Qt.

Desktop only: the web pages are frozen, so there is no JS twin and no golden case.
The n/a rule holds: an element with no grounding renders ``n/a`` with the reason the
plan gives, never zero. The plan itself is ``state.plan`` (live: ``/ws/pitwall``
state frames; review: the journal's state snapshots), its revisions are
``plan_revised`` race events and its turned-down proposals ``plan_proposal_rejected``
activity records.
"""

from __future__ import annotations

from typing import Any

from .format import NA, is_num, lap_time, num

#: element -> what the panel calls it, in display order.
LABELS: dict[str, str] = {
    "stop_count": "Stops left",
    "next_stop_lap": "Next stop",
    "fuel_to_add_l": "Fuel to add",
    "fuel_per_lap_target_l": "Fuel per lap",
    "pace_target_s": "Pace target",
    "tyre_posture": "Tyre posture",
}
SOURCE_LABELS = {"derived": "derived", "agent_proposed": "strategist", "operator_set": "operator"}
#: posture -> the tone the panel colours it with.
POSTURE_TONE = {"push": "warn", "neutral": "muted", "conserve": "good"}


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def element_text(name: str, value: Any) -> str:
    """One plan value as the panel prints it; ``n/a`` when there is none."""
    if value is None:
        return NA
    if name == "next_stop_lap":
        return f"lap {int(value)}" if is_num(value) else NA
    if name == "stop_count":
        return str(int(value)) if is_num(value) else NA
    if name == "fuel_to_add_l":
        return num(value, 1, " L")
    if name == "fuel_per_lap_target_l":
        return num(value, 2, " L/lap")
    if name == "pace_target_s":
        return lap_time(value)
    if name == "tyre_posture":
        return str(value)
    return str(value)


def plan_card(state: dict | None) -> dict:
    """The live view's plan panel: every element, its provenance, and why one is missing."""
    plan = _d(state).get("plan") if state else None
    if not isinstance(plan, dict):
        lifecycle = _d(_d(state).get("lifecycle"))
        reason = (
            "The race plan exists only in a race."
            if lifecycle.get("session_type") not in (None, "race")
            else "No plan yet: it appears once the fuel model has a green lap."
        )
        return {"available": False, "reason": reason, "rows": [], "version": NA}
    missing = _d(plan.get("missing"))
    rows = []
    for name, title in LABELS.items():
        element = plan.get(name)
        if isinstance(element, dict):
            source = element.get("provenance") or "derived"
            rows.append(
                {
                    "key": name,
                    "label": title,
                    "value": element_text(name, element.get("value")),
                    "source": source,
                    "source_label": SOURCE_LABELS.get(source, source),
                    "version": element.get("version"),
                    "note": element.get("rationale") or "",
                    "grounded": element.get("value") is not None,
                }
            )
        else:
            rows.append(
                {
                    "key": name,
                    "label": title,
                    "value": NA,
                    "source": None,
                    "source_label": "",
                    "version": None,
                    "note": missing.get(name) or "No grounding for this yet.",
                    "grounded": False,
                }
            )
    posture = _d(plan.get("tyre_posture")).get("value")
    return {
        "available": True,
        "reason": "",
        "version": f"v{plan.get('version')}" if is_num(plan.get("version")) else NA,
        "stops_done": plan.get("stops_done") if is_num(plan.get("stops_done")) else 0,
        "rows": rows,
        "posture": posture or NA,
        "posture_tone": POSTURE_TONE.get(posture or "", "muted"),
    }


def _changes_text(changes: Any) -> str:
    parts = []
    for name, change in _d(changes).items():
        change = _d(change)
        old, new = change.get("from"), change.get("to")
        title = LABELS.get(name, name).lower()
        if old is None:
            parts.append(f"{title} {element_text(name, new)}")
        else:
            parts.append(f"{title} {element_text(name, old)} → {element_text(name, new)}")
    return ", ".join(parts) or "no change"


def revision_rows(plan: dict | None, limit: int = 8) -> list[dict]:
    """The plan's revision log, newest first: what changed, by whom, and why."""
    rows = []
    for rev in reversed(_d(plan).get("revisions") or []):
        rev = _d(rev)
        accepted = bool(rev.get("accepted"))
        who = rev.get("agent") or SOURCE_LABELS.get(rev.get("provenance") or "", "derived")
        lap = rev.get("lap")
        head = f"v{rev.get('version')}" if accepted else "rejected"
        rows.append(
            {
                "accepted": accepted,
                "title": f"{head} · {who}" + (f" · lap {lap}" if lap is not None else ""),
                "changes": _changes_text(rev.get("changes")),
                "why": (rev.get("rationale") or "") if accepted else (rev.get("reason") or ""),
                "rationale": rev.get("rationale") or "",
            }
        )
        if len(rows) >= limit:
            break
    return rows


def timeline_items(records: list[dict] | None) -> list[dict]:
    """The review's race-plan lane: accepted revisions and rejected proposals."""
    items = []
    for rec in records or []:
        t = rec.get("session_time")
        if not is_num(t):
            continue
        data = _d(rec.get("data"))
        if rec.get("kind") == "race_event" and data.get("event_type") == "plan_revised":
            payload = _d(data.get("payload"))
            who = payload.get("agent") or SOURCE_LABELS.get(payload.get("provenance") or "", "")
            items.append(
                {
                    "t0": t,
                    "label": f"plan v{payload.get('new_version')} ({who}): "
                    + _changes_text(payload.get("changes")),
                    "cls": "plan-accepted",
                    "accepted": True,
                    "agent_proposed": payload.get("provenance") == "agent_proposed",
                    "record": rec,
                }
            )
        elif rec.get("kind") == "plan_proposal_rejected":
            items.append(
                {
                    "t0": t,
                    "label": f"plan proposal rejected ({rec.get('agent')}): "
                    + str(data.get("reason") or ""),
                    "cls": "plan-rejected",
                    "accepted": False,
                    "agent_proposed": True,
                    "record": rec,
                }
            )
    return items


def audit_notes(records: list[dict] | None) -> dict[str, str]:
    """run_id -> what its plan proposals did ("v2 accepted", "rejected: ..."), for the audit."""
    notes: dict[str, list[str]] = {}
    for rec in records or []:
        data = _d(rec.get("data"))
        if rec.get("kind") != "tool_call" or data.get("tool") != "propose_plan_revision":
            continue
        result = data.get("result_full")
        result = result if isinstance(result, dict) else {}
        if result.get("accepted"):
            text = f"v{result.get('plan_version')} accepted"
        elif result.get("unavailable"):
            text = "no plan engine"
        elif result:
            text = "rejected: " + str(result.get("reason") or "")
        else:
            text = "proposed"
        notes.setdefault(str(rec.get("run_id")), []).append(text)
    return {run: "; ".join(parts) for run, parts in notes.items()}


def is_plan_item(item: dict) -> bool:
    """A shared race-events lane item that is a plan revision (it has its own lane)."""
    rec = _d(_d(item).get("record"))
    return _d(rec.get("data")).get("event_type") == "plan_revised"
