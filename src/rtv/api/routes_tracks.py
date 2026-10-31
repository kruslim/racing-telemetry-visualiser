"""Pre-loaded track reference data and the per-session track facts built on it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rtv.api.deps import get_repo
from rtv.store.repository import Repository
from rtv.tracks import get as get_reference
from rtv.tracks import load_all
from rtv.tracks.service import session_track

router = APIRouter(tags=["tracks"])


@router.get("/tracks", summary="Pre-loaded venues and how much of each is named")
def list_tracks() -> dict:
    refs = [ref.summary() for ref in load_all().values()]
    return {"count": len(refs), "tracks": refs}


@router.get("/tracks/{key}", summary="One venue's named corners, straights and pit lane")
def get_track(key: str) -> dict:
    ref = get_reference(key)
    if ref is None:
        raise HTTPException(404, f"Unknown track {key}")
    return ref.detail()


@router.get(
    "/sessions/{session_id}/track",
    summary="This session's layout facts and named corners located on its lap",
)
def get_session_track(
    session_id: str,
    lap: int | None = Query(None, ge=0, description="Lap whose trace to locate corners on "
                            "(default: the fastest clean lap)."),
    repo: Repository = Depends(get_repo),
) -> dict:
    try:
        return session_track(repo, session_id, lap)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
