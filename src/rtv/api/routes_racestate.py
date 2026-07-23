"""Pitwall race-state surface: current state, event log and replay control."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from rtv.api.deps import get_services
from rtv.racestate.replay import SCENARIO_SESSION_ID, scenario_source, store_source
from rtv.services import AppServices

router = APIRouter(tags=["pitwall"])


def _engine(services: AppServices):
    if services.engine is None:  # pragma: no cover - router is not mounted when off
        raise HTTPException(503, "The pitwall race-state engine is disabled (RTV_PITWALL).")
    return services.engine


@router.get("/racestate", summary="Current race-state snapshot")
def race_state(services: AppServices = Depends(get_services)) -> dict:
    return _engine(services).snapshot().to_api()


@router.get("/racestate/events", summary="Recent race events (bounded ring buffer)")
def race_events(
    limit: int = Query(50, ge=1, le=500),
    services: AppServices = Depends(get_services),
) -> dict:
    engine = _engine(services)
    events = engine.bus.history(limit)
    return {"count": len(events), "events": [e.to_api() for e in events]}


class ReplayRequest(BaseModel):
    session_id: str = Field(
        SCENARIO_SESSION_ID,
        description="A stored session id, or 'scenario' for the built-in "
        "scripted synthetic race.",
    )
    speed: float = Field(
        1.0,
        ge=0.0,
        description="Playback rate: 1.0 = real time, N = N-times real time, "
        "0 = as fast as possible.",
    )


@router.post("/replay/start", summary="Replay a stored or synthetic session")
def replay_start(
    body: ReplayRequest, services: AppServices = Depends(get_services)
) -> dict:
    engine = _engine(services)
    driver = services.replay
    if driver is None:  # pragma: no cover - paired with the engine
        raise HTTPException(503, "Replay is unavailable.")
    if driver.running:
        raise HTTPException(409, "A replay is already running; stop it first.")

    if body.session_id == SCENARIO_SESSION_ID:
        source = scenario_source()
    else:
        try:
            source = store_source(
                services.db,
                services.settings.parquet_dir,
                body.session_id,
                flatten_max=services.settings.array_flatten_max,
                session_info=services.repo.get_session_info(body.session_id),
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    engine.reset()
    driver.start(source, speed=body.speed)
    return driver.status().to_api()


@router.post("/replay/stop", summary="Stop the running replay")
def replay_stop(services: AppServices = Depends(get_services)) -> dict:
    driver = services.replay
    if driver is None:  # pragma: no cover
        raise HTTPException(503, "Replay is unavailable.")
    driver.stop()
    return driver.status().to_api()


@router.get("/replay/status", summary="Replay progress")
def replay_status(services: AppServices = Depends(get_services)) -> dict:
    driver = services.replay
    if driver is None:  # pragma: no cover
        raise HTTPException(503, "Replay is unavailable.")
    return driver.status().to_api()
