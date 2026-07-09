"""Live session identity + session-info reading helpers."""

from __future__ import annotations

from typing import Any

# Top-level sections of the iRacing session-info YAML we surface.
SESSION_INFO_KEYS = (
    "WeekendInfo",
    "SessionInfo",
    "DriverInfo",
    "QualifyResultsInfo",
    "CameraInfo",
    "RadioInfo",
    "SplitTimeInfo",
    "CarSetup",
)


def read_live_session_info(ir) -> dict[str, Any]:
    """Assemble the parsed session-info dict from a live IRSDK connection."""
    info: dict[str, Any] = {}
    for key in SESSION_INFO_KEYS:
        try:
            value = ir[key]
        except Exception:
            value = None
        if value is not None:
            info[key] = value
    return info


def subsession_id(info: dict[str, Any]) -> int | None:
    weekend = info.get("WeekendInfo", {}) if isinstance(info, dict) else {}
    try:
        return int(weekend.get("SubSessionID"))
    except (TypeError, ValueError):
        return None
