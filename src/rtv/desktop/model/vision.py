"""Onboard video and visual notes as data: the review's video panel without Qt.

Everything here reads the vision API as the backend serves it (``docs/VISION.md``):
the sync arithmetic is the server's own ``rate`` and ``offset``, and every note line
is the note's kept observations, as is. Notes carry no numbers; the only figures
shown next to them (a lap, a corner label, a clock time) come from the telemetry
side of the note.
"""

from __future__ import annotations

from typing import Any

from .format import NA, clock, is_num

#: Short display words for a note's primary trigger.
TRIGGER_WORD = {
    "incident": "incident",
    "offtrack": "off track",
    "lockup": "lock-up",
    "wheelspin": "wheelspin",
    "traffic_close": "traffic",
    "recurring_issue": "repeated mistake",
    "finding": "time lost",
}

KIND_WORD = {
    "kerb_use": "kerb",
    "line": "line",
    "traffic": "traffic",
    "car_behaviour": "car",
    "damage": "damage",
    "track_state": "track",
    "other": "note",
}

#: Severity a trigger reads as on the timeline (icon + colour, never colour alone).
TRIGGER_SEVERITY = {
    "incident": "critical",
    "offtrack": "advisory",
    "lockup": "advisory",
    "recurring_issue": "advisory",
}

STATUS_HINT = {
    "trigger_not_seen": "The frames did not show this moment: check the video sync.",
    "no_answer": "No valid answer from the model.",
    "error": "This moment could not be analysed.",
}


def pick_video(videos: list[dict] | None, prefer: str | None = None) -> dict | None:
    """The chosen video, else the newest synced one, else the newest."""
    videos = videos or []
    if prefer:
        for v in videos:
            if v.get("video_id") == prefer:
                return v
    newest = sorted(videos, key=lambda v: v.get("registered_wall_ms") or 0)
    synced = [v for v in newest if (v.get("sync") or {}).get("synced")]
    return (synced or newest or [None])[-1]


def is_synced(video: dict | None) -> bool:
    return bool(video and (video.get("sync") or {}).get("synced"))


def to_video(video: dict | None, session_time: Any) -> float | None:
    """The video time for a session time, or ``None`` outside the video / unsynced."""
    if not is_synced(video) or not is_num(session_time):
        return None
    sync = video["sync"]
    rate = sync.get("rate") or 1.0
    vt = (session_time - (sync.get("offset") or 0.0)) / rate
    duration = video.get("duration_s")
    if vt < 0 or (is_num(duration) and vt > duration):
        return None
    return vt


def sync_text(video: dict | None) -> str:
    """One line on how the video lines up, for the panel header."""
    if not video:
        return "no video"
    if not is_synced(video):
        return "not synced yet"
    anchors = (video.get("sync") or {}).get("anchors") or []
    laps = [
        a["source"].split(":", 1)[1]
        for a in anchors
        if str(a.get("source", "")).startswith("lap_start:")
    ]
    span = f"{clock(video.get('session_time_start'))} → {clock(video.get('session_time_end'))}"
    how = f"from lap {', '.join(laps)} start" if laps else "by offset"
    return f"synced {how} · covers {span}"


def video_title(video: dict | None) -> str:
    if not video:
        return NA
    bits = [video.get("label") or "video"]
    if video.get("width") and video.get("height"):
        bits.append(f"{video['width']}×{video['height']}")
    if is_num(video.get("fps")):
        bits.append(f"{round(video['fps'])} fps")
    if is_num(video.get("duration_s")):
        bits.append(clock(video["duration_s"]))
    return " · ".join(bits)


def note_head(note: dict) -> str:
    """``L5 · T4 · 12:03 · off track + time lost``."""
    m = note.get("moment") or {}
    kinds: list[str] = []
    for trig in sorted(m.get("triggers") or [], key=lambda t: _rank(t.get("kind"))):
        word = TRIGGER_WORD.get(trig.get("kind"), trig.get("kind") or "?")
        if word not in kinds:
            kinds.append(word)
    parts = []
    if m.get("lap") is not None:
        parts.append(f"L{m['lap']}")
    if m.get("corner"):
        parts.append(m["corner"])
    parts.append(clock(m.get("session_time")))
    parts.append(" + ".join(kinds) or "moment")
    return " · ".join(parts)


def _rank(kind: Any) -> int:
    order = list(TRIGGER_WORD)
    return order.index(kind) if kind in order else len(order)


def primary_kind(note: dict) -> str | None:
    triggers = (note.get("moment") or {}).get("triggers") or []
    if not triggers:
        return None
    return min(triggers, key=lambda t: _rank(t.get("kind"))).get("kind")


def note_rows(notes: list[dict] | None, *, corner: str | None = None) -> list[dict]:
    """One row per note, in race order: head, kept lines, hint, cursor time, frame."""
    rows = []
    for n in sorted(notes or [], key=lambda n: (n.get("moment") or {}).get("session_time") or 0):
        m = n.get("moment") or {}
        corners = {m.get("corner"), *(t.get("corner") for t in m.get("triggers") or [])}
        if corner is not None and corner not in corners:
            continue
        kept = n.get("kept")
        if kept is None:  # an /analyses/{id} item from an older server
            kept = [
                o
                for o in n.get("observations") or []
                if o.get("status") in ("corroborated", "uncheckable")
            ]
        lines = [
            {
                "tag": KIND_WORD.get(o.get("kind"), o.get("kind") or "note"),
                "text": o.get("text") or "",
                "confidence": o.get("confidence") or "low",
                "checked": o.get("status") == "corroborated",
            }
            for o in kept
        ]
        frames = n.get("frames") or []
        rows.append(
            {
                "note_id": n.get("note_id"),
                "analysis_id": n.get("analysis_id"),
                "head": note_head(n),
                "severity": TRIGGER_SEVERITY.get(primary_kind(n), "info"),
                "lines": lines,
                "hint": STATUS_HINT.get(n.get("status") or "ok"),
                "session_time": m.get("session_time"),
                "lap": m.get("lap"),
                "corner": m.get("corner"),
                "frame": frames[len(frames) // 2]["name"] if frames else None,
                "dropped": sum(
                    1
                    for o in n.get("observations") or []
                    if o.get("status") in ("dropped", "contradicted")
                ),
            }
        )
    return rows


def timeline_items(notes: list[dict] | None) -> list[dict]:
    """The "camera" lane: one span per note with something kept, over its window."""
    items = []
    for n in notes or []:
        m = n.get("moment") or {}
        if not is_num(m.get("t_start")):
            continue
        has_kept = bool(n.get("kept")) or any(
            o.get("status") in ("corroborated", "uncheckable") for o in n.get("observations") or []
        )
        items.append(
            {
                "t0": m["t_start"],
                "t1": m.get("t_end"),
                "severity": TRIGGER_SEVERITY.get(primary_kind(n), "info"),
                "quiet": not has_kept or n.get("status") != "ok",
                "label": note_head(n),
                "note_id": n.get("note_id"),
            }
        )
    return sorted(items, key=lambda i: i["t0"])


def note_at(notes: list[dict] | None, t: Any) -> dict | None:
    """The note whose window holds ``t`` (the nearest centre wins)."""
    if not is_num(t):
        return None
    inside = [
        n
        for n in notes or []
        if is_num((n.get("moment") or {}).get("t_start"))
        and n["moment"]["t_start"] <= t <= (n["moment"].get("t_end") or n["moment"]["t_start"])
    ]
    if not inside:
        return None
    return min(inside, key=lambda n: abs((n["moment"].get("session_time") or t) - t))


def plan_text(plan: dict | None) -> str:
    """What an analysis would cost, from the free dry run."""
    if not plan:
        return NA
    count, frames = plan.get("count") or 0, plan.get("frames") or 0
    if not count:
        why = " ".join(plan.get("notes") or []) or "nothing the telemetry flagged is in the video."
        return f"No moments to analyse: {why}"
    skipped = plan.get("skipped_over_budget") or 0
    more = f", {skipped} more over the frame budget" if skipped else ""
    return f"{count} moments · {frames} frames · {count} {plan.get('model') or 'model'} calls{more}"


def analysis_text(summary: dict | None) -> str:
    if not summary:
        return "No analysis yet."
    status = summary.get("status") or "unknown"
    progress = summary.get("progress") or {}
    if status == "running":
        done = progress.get("done") or 0
        total = progress.get("total") if progress.get("total") is not None else "?"
        return f"Analysing… {done} / {total} moments"
    kept = summary.get("observations_kept")
    notes = summary.get("notes")
    word = {"complete": "Analysed", "cancelled": "Cancelled", "failed": "Failed"}.get(
        status, status
    )
    tail = f": {summary['error']}" if summary.get("error") else ""
    return f"{word} · {_count(notes, 'moment')} · {_count(kept, 'observation')} kept{tail}"


def _count(n: Any, word: str) -> str:
    if not is_num(n):
        return f"{NA} {word}s"
    return f"{n} {word}" + ("" if n == 1 else "s")


def vision_blocker(status: dict | None) -> str | None:
    """Why an analysis cannot run from here, in words; ``None`` when it can."""
    if not status:
        return "The vision status is unknown."
    if not status.get("analysis_enabled"):
        return "Vision analyses are off. Set RTV_VISION_MODE=post to allow them."
    if not status.get("key_present"):
        return "No Kimi key: set KIMI_CODE_API_KEY to run an analysis."
    tools = status.get("tools") or {}
    if not tools.get("ffmpeg"):
        return "ffmpeg was not found: install it (winget install ffmpeg) to grab frames."
    return None
