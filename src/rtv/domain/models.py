"""Core domain dataclasses (storage-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SessionKind(StrEnum):
    LIVE = "live"
    IBT = "ibt"


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    IN_SESSION = "in_session"


@dataclass
class Session:
    """A captured driving session (one live stint or one imported .ibt)."""

    session_id: str
    kind: SessionKind
    source_path: str | None = None
    car_id: str | None = None
    track_id: str | None = None
    track_name: str | None = None
    ir_session_id: int | None = None
    ir_subsession_id: int | None = None
    schema_hash: str | None = None
    started_at: float | None = None  # unix seconds
    ended_at: float | None = None
    sample_count: int = 0
    poll_hz: int | None = None
    status: str = "open"  # open | finalising | ready | error
    label: str | None = None  # user-supplied friendly name (e.g. "Alien – Verstappen")


@dataclass
class Lap:
    """A single lap marker derived from the telemetry stream."""

    session_id: str
    lap: int
    start_tick: int
    end_tick: int | None = None
    start_session_time: float = 0.0
    lap_time: float | None = None
    is_valid: bool = True
    is_out_lap: bool = False
    is_in_lap: bool = False


@dataclass
class SessionInfoSnapshot:
    """A parsed session-info YAML document captured when it changed."""

    session_id: str
    update_seq: int
    captured_tick: int
    captured_at: float
    info: dict[str, Any]
