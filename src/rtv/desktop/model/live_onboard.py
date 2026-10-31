"""The live view's onboard panel as data: what ``GET /vision/live`` says, in words.

Two sources (``docs/VISION.md``, Live): a running capture of the sim window, or a
synced recording of the session being replayed. Visual notes are words only (the
server drops any figure); the lap and trigger shown beside them come from the
telemetry side of the note.
"""

from __future__ import annotations

from typing import Any

from .format import is_num
from .vision import KIND_WORD, STATUS_HINT, TRIGGER_WORD

EMPTY_TEXT = (
    "No onboard video. Start a capture while iRacing runs, or replay a session that has "
    "a synced recording."
)

SKIP_WORD = {"merged": "merged", "over_cap": "over the cap", "no_frames": "no frames"}

#: Frame refresh, ms: the capture's newest frame, or a seek into a recording.
CAPTURE_FRAME_MS = 250
VIDEO_FRAME_MS = 500


def onboard_view(status: dict | None) -> dict[str, Any]:
    """Mode, the source and VLM lines, and note rows (newest first). Pure."""
    s = status or {}
    capture = s.get("capture") or {}
    source = s.get("source") or None
    vlm = s.get("vlm") or {}
    if capture.get("running"):
        mode = "capture"
    elif source and source.get("kind") == "video" and source.get("video"):
        mode = "video"
    else:
        mode = "empty"

    if capture.get("error"):
        source_line = f"capture failed: {capture['error']}"
    elif mode == "capture":
        source_line = (
            f"capturing the sim window · {capture.get('frames') or 0} frames stamped"
            if capture.get("session_id")
            else "capturing, waiting for a live session to stamp frames"
        )
    elif mode == "video":
        video = source["video"]
        source_line = f"recording: {video.get('label') or video.get('video_id')}"
    else:
        source_line = EMPTY_TEXT

    if not vlm.get("enabled"):
        vlm_line = (
            "Live VLM off: no Kimi key, or RTV_VISION_LIVE_MAX_CALLS is 0."
            if s.get("mode") == "live"
            else "Live VLM off. Set RTV_VISION_MODE=live to describe flagged moments "
            "(uses Kimi quota)."
        )
    else:
        parts = [f"Live VLM on · {vlm.get('calls') or 0} of {vlm.get('max_calls')} calls"]
        if vlm.get("pending"):
            parts.append(f"{vlm['pending']} looking")
        for why, n in (vlm.get("skipped") or {}).items():
            parts.append(f"{n} {SKIP_WORD.get(why, why)}")
        vlm_line = " · ".join(parts)

    notes = []
    for n in reversed(vlm.get("recent") or []):
        kept = n.get("kept") or []
        lap = n.get("lap")
        head = f"lap {lap if lap is not None else '—'} · " + TRIGGER_WORD.get(
            n.get("trigger") or "", str(n.get("trigger") or "")
        )
        status_ = n.get("status") or "ok"
        if kept:
            empty = None
        elif status_ in STATUS_HINT:
            empty = STATUS_HINT[status_]
        else:
            empty = "Nothing it could say without a number."
        notes.append(
            {
                "id": n.get("note_id"),
                "head": head,
                "lines": [
                    {
                        "kind": KIND_WORD.get(o.get("kind") or "", "note"),
                        "text": o.get("text") or "",
                        "confidence": o.get("confidence") or "low",
                    }
                    for o in kept
                ],
                "empty": empty,
            }
        )
    return {
        "mode": mode,
        "source_line": source_line,
        "vlm_line": vlm_line,
        "notes": notes,
        "capture_running": bool(capture.get("running")),
        "video": source["video"] if mode == "video" else None,
    }


def frame_request(
    view: dict, session_time: float | None, width: int = 480
) -> tuple[str, dict | None] | None:
    """Which frame to fetch now: ``(path, params)``, or None when there is none to show."""
    if view.get("mode") == "capture":
        return "/vision/live/frame", None
    video = view.get("video")
    if view.get("mode") != "video" or not video or not is_num(session_time):
        return None
    start, end = video.get("session_time_start"), video.get("session_time_end")
    if is_num(start) and session_time < start:
        return None
    if is_num(end) and session_time > end:
        return None
    path = f"/sessions/{video['session_id']}/videos/{video['video_id']}/frame"
    return path, {"session_time": round(float(session_time), 2), "width": width}
