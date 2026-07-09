"""Coaching endpoints.

``/coaching/lap-findings`` is purely deterministic feature extraction (no LLM): it
returns the compact ~20-finding model that every coaching surface — the MCP server,
the orchestrator, the evals — consumes. Keeping it a plain REST endpoint means the
findings have one source of truth.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rtv.api.deps import get_coaching
from rtv.coaching.features import CoachingService

router = APIRouter(tags=["coaching"])


@router.get(
    "/coaching/lap-findings",
    summary="Deterministic corner-level findings for a lap vs a reference lap",
)
def lap_findings(
    session_id: str = Query(..., description="Session to analyse."),
    main_lap: int = Query(..., ge=0, description="The lap being coached."),
    ref_lap: int = Query(..., ge=0, description="The reference (target) lap to compare against."),
    coaching: CoachingService = Depends(get_coaching),
) -> dict:
    try:
        return coaching.lap_findings(session_id, main_lap, ref_lap).to_dict()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
