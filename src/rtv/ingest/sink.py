"""The sink interface the ingest layer writes to.

Defined here (not in ``store``) so the ingest layer stays free of any DuckDB /
storage import. The store and API layers provide concrete implementations.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from rtv.catalog.models import Catalog
from rtv.domain.models import Lap, Session, SessionInfoSnapshot


@runtime_checkable
class TelemetrySink(Protocol):
    def begin_session(self, session: Session, catalog: Catalog) -> None:
        """Register a new session and its variable catalog."""

    def write_telemetry(self, session_id: str, table) -> None:
        """Persist a columnar pyarrow Table of ticks (partitioned by lap internally)."""

    def write_session_info(self, snapshot: SessionInfoSnapshot) -> None:
        """Persist a parsed session-info YAML snapshot."""

    def write_laps(self, session_id: str, laps: list[Lap]) -> None:
        """Persist (or upsert) lap markers."""

    def end_session(self, session_id: str, *, sample_count: int, ended_at: float) -> None:
        """Mark a session finalised."""
