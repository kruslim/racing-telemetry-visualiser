"""Variable catalog endpoint — the frontend's contract for what's plottable."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from rtv.api.deps import get_repo, get_services
from rtv.services import AppServices
from rtv.store.repository import Repository

router = APIRouter(tags=["catalog"])


@router.get("/variables", summary="Variable catalog for the live/most-recent session")
def get_variables(
    services: AppServices = Depends(get_services),
    repo: Repository = Depends(get_repo),
) -> dict:
    # Prefer the live catalog if the poller is connected.
    catalog = services.hub.catalog
    if catalog is not None:
        return catalog.to_api()

    # Otherwise fall back to the most recent persisted session's catalog.
    recent = repo.list_sessions(limit=1)
    if recent:
        cat = repo.get_catalog(recent[0]["session_id"])
        if cat is not None:
            return cat

    raise HTTPException(
        status_code=404,
        detail="No catalog available yet. Start the live poller (POST /live/start) "
        "while iRacing is running, or import an .ibt file (POST /import).",
    )
