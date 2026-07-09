"""AI coaching layer.

Deterministic *feature extraction* (this package) turns ~1M raw telemetry points
per lap into a compact set of corner-level findings — the LLM never sees raw 60 Hz
telemetry. Those findings then feed:

* the REST endpoint ``/api/v1/coaching/lap-findings`` (and the MCP server on top of it),
* the Claude API orchestrator (:mod:`rtv.coaching.orchestrator`),
* the eval harness, which cross-checks LLM claims against these findings as ground truth.

The analysis here is a faithful Python port of the frontend ``coach.js`` heuristics.
"""

from __future__ import annotations

from rtv.coaching.features import CoachingService
from rtv.coaching.models import (
    ChiefSummary,
    CornerFinding,
    Diagnostic,
    LapFindings,
    Priority,
    SectorDelta,
)

__all__ = [
    "CoachingService",
    "LapFindings",
    "CornerFinding",
    "Diagnostic",
    "SectorDelta",
    "Priority",
    "ChiefSummary",
]
