"""Session metadata, per-session catalog, laps and session-info endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rtv.api.deps import get_repo
from rtv.store.repository import Repository

router = APIRouter(tags=["sessions"])


@router.get("/sessions", summary="List captured sessions")
def list_sessions(
    kind: str | None = Query(None, pattern="^(live|ibt)$"),
    car_id: str | None = None,
    track_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    repo: Repository = Depends(get_repo),
) -> dict:
    sessions = repo.list_sessions(
        kind=kind, car_id=car_id, track_id=track_id, limit=limit, offset=offset
    )
    return {"count": len(sessions), "sessions": sessions}


@router.get("/sessions/{session_id}", summary="Session metadata")
def get_session(session_id: str, repo: Repository = Depends(get_repo)) -> dict:
    session = repo.get_session(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown session {session_id}")
    return session


@router.get("/sessions/{session_id}/variables", summary="This session's catalog")
def get_session_variables(
    session_id: str, repo: Repository = Depends(get_repo)
) -> dict:
    catalog = repo.get_catalog(session_id)
    if catalog is None:
        raise HTTPException(404, f"Unknown session {session_id}")
    return catalog


@router.get("/sessions/{session_id}/laps", summary="Lap list with times")
def get_laps(session_id: str, repo: Repository = Depends(get_repo)) -> dict:
    if repo.get_session(session_id) is None:
        raise HTTPException(404, f"Unknown session {session_id}")
    laps = repo.get_laps(session_id)
    return {"session_id": session_id, "count": len(laps), "laps": laps}


@router.get("/sessions/{session_id}/info", summary="Parsed session-info YAML")
def get_session_info(
    session_id: str,
    seq: int | None = Query(None, description="Specific snapshot sequence."),
    repo: Repository = Depends(get_repo),
) -> dict:
    info = repo.get_session_info(session_id, seq)
    if info is None:
        raise HTTPException(404, f"No session info for {session_id}")
    return {"session_id": session_id, "seq": seq, "info": info}
