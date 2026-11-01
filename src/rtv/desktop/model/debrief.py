"""The post-race debrief as the desktop review shows it (stage 09 part B). Pure Python, no Qt.

Desktop only: the web pages are frozen, so there is no JS twin and no golden case. The
debrief document is ``GET /api/v1/sessions/{id}/debrief?run=`` (``rtv.debrief``); this
module turns it into what the panel paints and decides nothing about the race itself. The
n/a rule holds: a figure the deterministic half does not have prints ``n/a``, never zero.

Every lap and corner the debrief names becomes a *reference* the panel can click: a lap
opens it in the review's lap analysis (against the debrief's reference lap), a corner also
highlights that corner's window there.
"""

from __future__ import annotations

import re
from typing import Any

from .format import NA, integer, is_num, lap_time, num, signed

STATUS_LABELS = {
    "ok": "checked",
    "repaired": "checked after one repair",
    "refused": "refused",
    "unavailable": "unavailable",
}
#: status -> the tone the panel colours its badge with.
STATUS_TONE = {"ok": "good", "repaired": "good", "refused": "warn", "unavailable": "muted"}
BASIS_LABELS = {
    "time_lost": "where time was lost",
    "recurring": "recurring issues",
    "tyres_fuel": "tyres and fuel",
    "plan": "the plan",
}

REGENERATE_TEXT = (
    "Regenerating reads the recorded journal again (it never re-runs the live agents) and, "
    "with a Kimi key set, calls Kimi once with thinking on, plus one repair call if a "
    "section fails its checks. That uses your Kimi quota. Without a key it rebuilds the "
    "deterministic half only."
)

_LAP_RE = re.compile(r"\blaps?\s+(\d+(?:\s*(?:,|-|–|to|and|&)\s*\d+)*)", re.IGNORECASE)
_NUM_RE = re.compile(r"\d+")
_LABEL_RE = re.compile(r"\b(?:T|Turn\s)(\d+)\b")


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _label(cited: Any) -> str | None:
    m = _LABEL_RE.search(str(cited or ""))
    return f"T{m.group(1)}" if m else None


def refs(text: str, laps: list[int] | None, corner: Any, recorded: set[int]) -> list[dict]:
    """The clickable references of one point: its laps and corner, then any in its prose.

    Only recorded laps become references (a planned stop lap beyond the flag is text).
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    label = _label(corner)
    lap_list = [int(n) for n in laps or [] if int(n) in recorded]
    for m in _LAP_RE.finditer(text or ""):
        lap_list += [int(n) for n in _NUM_RE.findall(m.group(1)) if int(n) in recorded]
    labels = [label] if label else []
    labels += [f"T{n}" for n in _LABEL_RE.findall(text or "")]
    for lap in lap_list:
        key = ("lap", lap)
        if key not in seen:
            seen.add(key)
            out.append({"kind": "lap", "lap": lap, "corner": label, "text": f"Lap {lap}"})
    for lb in labels:
        key = ("corner", lb)
        if key not in seen:
            seen.add(key)
            out.append(
                {
                    "kind": "corner",
                    "corner": lb,
                    "lap": lap_list[0] if lap_list else None,
                    "text": lb,
                }
            )
    return out


def section_rows(doc: dict | None) -> list[dict]:
    """One row per analysis section: title, status badge, and its text or points."""
    doc = _d(doc)
    recorded = set(_d(doc.get("references")).get("laps") or [])
    rows = []
    for section in doc.get("sections") or []:
        status = section.get("status") or "unavailable"
        items = []
        if "items" in section:
            for item in section.get("items") or []:
                text = item.get("text") or ""
                if item.get("basis"):
                    text = f"{text} (from {BASIS_LABELS.get(item['basis'], item['basis'])})"
                items.append(
                    {
                        "text": text,
                        "refs": refs(item.get("text") or "", item.get("laps"), item.get("corner"),
                                     recorded),
                    }
                )
        text = section.get("text")
        if status == "refused":
            body = section.get("reason") or "Refused."
        elif status == "unavailable":
            body = "The analysis is unavailable for this run."
        elif text is not None:
            body = text
        elif not items:
            body = "Nothing to report."
        else:
            body = ""
        kept = status in {"ok", "repaired"}
        rows.append(
            {
                "name": section.get("name"),
                "title": section.get("title") or section.get("name"),
                "status": status,
                "badge": STATUS_LABELS.get(status, status),
                "tone": STATUS_TONE.get(status, "muted"),
                "body": body,
                "refs": refs(text or "", None, None, recorded) if kept else [],
                "items": items if kept else [],
            }
        )
    return rows


def header(doc: dict | None) -> dict:
    """The panel's header line and the note it shows above the sections."""
    doc = _d(doc)
    analysis = _d(doc.get("analysis"))
    kept = doc.get("kept_sections")
    total = len(doc.get("sections") or [])
    if analysis.get("available"):
        status = f"{integer(kept)}/{total} sections checked · {doc.get('model') or NA}"
    else:
        status = "deterministic only"
    meta = " · ".join(
        part
        for part in (
            f"run {doc.get('run_id')}" if doc.get("run_id") else "",
            status,
            "written at session end" if doc.get("trigger") == "session_end" else "regenerated",
            doc.get("generated_at") or "",
        )
        if part
    )
    return {"meta": meta, "note": doc.get("note") or ""}


def result_rows(doc: dict | None) -> list[tuple[str, str]]:
    """The deterministic result as key/value pairs; ``n/a`` where it was not recorded."""
    det = _d(_d(doc).get("deterministic"))
    res = _d(det.get("result"))
    fuel = _d(det.get("fuel"))

    def pos(value: Any) -> str:
        return f"P{int(value)}" if is_num(value) and value else NA

    best = lap_time(res.get("best_time"))
    if is_num(res.get("best_lap")) and best != NA:
        best = f"{best} (lap {int(res['best_lap'])})"
    return [
        ("Start", pos(res.get("start_position"))),
        ("Finish", pos(res.get("finish_position"))),
        ("Laps", integer(res.get("laps"))),
        ("Clean laps", integer(res.get("clean_laps"))),
        ("Best lap", best),
        ("Median lap", lap_time(res.get("median_time"))),
        ("Pit stops", integer(res.get("pit_stops"))),
        ("Incidents", integer(res.get("incidents"), "x")),
        ("Fuel used", num(fuel.get("used_l"), 1, " L")),
        ("Fuel per lap", num(fuel.get("per_lap_l"), 2, " L")),
    ]


def loss_rows(doc: dict | None, limit: int = 8) -> list[dict]:
    """Recurring corners first, from the Layer-1 findings: clickable corner rows."""
    det = _d(_d(doc).get("deterministic"))
    out = []
    for row in (det.get("recurring") or [])[:limit]:
        name = row.get("name")
        label = row.get("corner")
        out.append(
            {
                "corner": label,
                "title": f"{label} {name}" if name else str(label),
                "text": (
                    f"lost time on {integer(row.get('laps_losing'))} laps, "
                    f"{signed(row.get('total_loss_s'), 2, ' s')} in all; worst lap "
                    f"{integer(row.get('worst_lap'))} ({signed(row.get('worst_loss_s'), 2, ' s')})"
                ),
                "why": row.get("top_reason") or "",
                "lap": row.get("worst_lap"),
            }
        )
    return out


def reference_lap(doc: dict | None) -> int | None:
    ref = _d(_d(doc).get("references")).get("reference_lap")
    return int(ref) if is_num(ref) else None


def empty_text(run_id: str | None) -> str:
    if not run_id:
        return "No journal run selected: record or replay a session to debrief it."
    return (
        "No debrief for this run yet. One is written when a race segment ends; "
        "Regenerate writes one now."
    )
