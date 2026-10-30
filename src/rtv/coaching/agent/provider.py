"""The in-process data seam for the graph coach.

The Layer-2 MCP server reaches the backend over HTTP (``httpx`` → REST); that is correct for
Claude Desktop but fatal for hermetic evals, which would then need a live uvicorn and a
populated store. The graph's tools instead go through this thin ``FindingsProvider`` protocol,
whose *only* job is to make the coach swappable between the live store and canned findings.

This is deliberately **not** a canopy-style source-agnostic "reader seam": RTV has one data
source, so a protocol over it would be ceremony. ``FindingsProvider`` exposes exactly the five
operations the tools need, all synchronous, all returning JSON-able dicts. The live
implementation reuses ``CoachingService``/``Repository`` unchanged; the eval fixtures provide a
synthetic implementation (``evals/fixtures.py``).

Errors are raised here (below the seam) and turned into structured payloads by the executor —
never allowed to crash the loop (the reference project's "errors as results" discipline).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from rtv.coaching.features import CoachingService
from rtv.store.repository import Repository


@runtime_checkable
class FindingsProvider(Protocol):
    """The data operations the coach's tools call in-process."""

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """Recorded sessions, most recent first."""
        ...

    def list_laps(self, session_id: str) -> list[dict]:
        """Laps in a session with lap times and validity. Raises ``KeyError`` if the session
        does not exist."""
        ...

    def lap_findings(self, session_id: str, main_lap: int, ref_lap: int) -> dict:
        """Deterministic ~20-finding model for one lap vs a reference (``LapFindings.to_dict()``).

        Raises ``KeyError`` for an unknown session/lap and ``FileNotFoundError`` when the laps
        have no usable distance data."""
        ...

    def available_channels(self, session_id: str) -> list[str]:
        """The channels this session actually captured — the ground truth a refusal cites.

        Raises ``KeyError`` if the session does not exist."""
        ...

    def compare_channel(
        self, session_id: str, name: str, laps: list[int], grid: int = 200
    ) -> dict:
        """One channel aligned across laps on lap distance (0..1). Raises ``KeyError`` if the
        channel is not captured."""
        ...


# Fields kept when trimming a raw session row for the model (mirrors the MCP server).
_SESSION_FIELDS = (
    "session_id",
    "kind",
    "car_id",
    "track_id",
    "track_name",
    "label",
    "started_at",
)


class RepositoryFindingsProvider:
    """The live provider — reuses ``CoachingService``/``Repository`` with no changes.

    ``CoachingService`` is instantiated per call (it is a thin stateless wrapper over the
    repository), so a single provider can serve any session.
    """

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def list_sessions(self, limit: int = 20) -> list[dict]:
        rows = self._repo.list_sessions(limit=limit)
        return [{k: r.get(k) for k in _SESSION_FIELDS} for r in rows]

    def list_laps(self, session_id: str) -> list[dict]:
        if self._repo.get_session(session_id) is None:
            raise KeyError(f"Unknown session {session_id}")
        return self._repo.get_laps(session_id)

    def lap_findings(self, session_id: str, main_lap: int, ref_lap: int) -> dict:
        return CoachingService(self._repo).lap_findings(session_id, main_lap, ref_lap).to_dict()

    def available_channels(self, session_id: str) -> list[str]:
        if self._repo.get_session(session_id) is None:
            raise KeyError(f"Unknown session {session_id}")
        return sorted(self._repo.valid_columns(session_id))

    def compare_channel(
        self, session_id: str, name: str, laps: list[int], grid: int = 200
    ) -> dict:
        return self._repo.compare(session_id, name, laps, grid=grid)
