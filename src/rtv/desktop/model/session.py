"""Session lifecycle and agent arming, as the desktop shows them (stage 07).

Pure functions over ``RaceState.lifecycle`` (``/api/v1/racestate``),
``/api/v1/pitwall/status`` and one ``/pitwall-runs`` entry. Desktop only: the
frozen web pages have no counterpart, so these are not in the view golden file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rtv.desktop.model.format import is_num

SESSION_LABEL = {"practice": "Practice", "qualify": "Qualifying", "race": "Race"}

PHASE_LABEL = {
    "idle": "waiting",
    "practice": "starting",
    "qualify": "starting",
    "race": "starting",
    "pre_green": "pre-green",
    "green": "green",
    "checkered": "checkered",
    "cool_down": "cool-down",
    "ended": "ended",
    "unknown": "unknown",
}


def session_label(session_type: Any) -> str:
    return SESSION_LABEL.get(session_type, "Session") if session_type else "Session"


def lifecycle_view(state: dict | None, status: dict | None) -> dict:
    """The lifecycle pill (session type and phase) and the agents' arming."""
    state = state or {}
    lc = state.get("lifecycle") or {}
    caps = state.get("capabilities") or {}
    phase = lc.get("state") or "idle"
    kind = session_label(lc.get("session_type"))
    notes = list(lc.get("notes") or [])
    if caps.get("session") and caps.get("lifecycle") is False:
        wanted = ("SessionNum", "SessionState", "SessionFlags")
        missing = [m for m in caps.get("missing") or [] if m in wanted]
        if missing:
            notes.append("No " + ", ".join(missing) + " in this telemetry.")
    segment = lc.get("segment") if is_num(lc.get("segment")) else None
    text = f"{kind} · {PHASE_LABEL.get(phase, phase)}"
    if phase == "idle":
        text = "No session yet"
    view = {
        "phase": phase,
        "session_type": lc.get("session_type"),
        "session_label": kind,
        "segment": segment,
        "text": text,
        "detail": " ".join(notes),
        **agents_view(status),
    }
    return view


def agents_view(status: dict | None) -> dict:
    """Armed, standby (and why), suspended or off, from ``/pitwall/status``."""
    if not status or status.get("available") is False or "enabled" not in status:
        reason = (status or {}).get("reason") or "agent layer not mounted"
        return {"agents": "unmounted", "agents_label": "AGENTS OFF", "agents_detail": reason,
                "tone": "muted"}
    mode = status.get("mode")
    if not mode:  # a server from before stage 07
        mode = (
            "off" if status.get("enabled") is False
            else "suspended" if status.get("suspended") else "on"
        )
    roster = status.get("armed_roster")
    detail = {
        "off": "kill switch: the operator turned the agents off",
        "suspended": status.get("suspend_reason") or "telemetry lost",
        "standby": status.get("standby_reason") or "waiting",
        "armed": ("roster: " + ", ".join(roster)) if roster else "every agent",
        "on": "autonomy off: the operator controls the agents",
    }.get(mode, "")
    label = {
        "off": "AGENTS OFF",
        "suspended": "SUSPENDED",
        "standby": "STANDBY",
        "armed": "ARMED",
        "on": "AGENTS ON",
    }.get(mode, mode.upper())
    tone = {"armed": "ok", "on": "ok", "suspended": "warn"}.get(mode, "muted")
    return {"agents": mode, "agents_label": label, "agents_detail": detail, "tone": tone}


def run_label(run: dict) -> str:
    """One run in the review's picker: ``Race · replay · 31 Oct 21:04 · agents on``."""
    started = run.get("started_wall_ms")
    when = (
        datetime.fromtimestamp(started / 1000).strftime("%d %b %H:%M") if is_num(started) else "?"
    )
    parts = []
    if run.get("session_type") or is_num(run.get("session_num")):
        parts.append(session_label(run.get("session_type")))
    parts += [
        str(run.get("source")),
        when,
        "agents on" if run.get("has_agents") else "agents off",
        "complete" if run.get("complete") else "incomplete",
    ]
    return " · ".join(parts)
