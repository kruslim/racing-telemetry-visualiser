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


@router.get("/pitwall/health", summary="Is the live pitwall path actually running?")
def pitwall_health(services: AppServices = Depends(get_services)) -> dict:
    """One answer for an operator mid-race, and it never returns an error.

    ``/racestate`` says what the race is doing; this says whether anything is
    still *watching* it. The distinction matters because a state object full of
    ``None`` looks the same whether the session has not started, the catalog is
    missing the channels we wanted, or frames stopped arriving four minutes ago.

    Reported rather than judged, with one exception: ``ok`` is false when a
    source that should be producing frames is not, or when the agent layer has
    had to restart something or has tripped a breaker. Everything else is a fact
    the caller can weigh for itself.
    """
    engine = services.engine
    if engine is None:  # pragma: no cover - router is not mounted when off
        return {"ok": False, "pitwall": False, "reason": "RTV_PITWALL is false."}

    health = engine.health()
    replay = services.replay.status().to_api() if services.replay is not None else None
    live = {
        "running": services.poller.running,
        "state": services.poller.state.value,
        "session_id": services.poller.session_id,
    }
    pitwall = services.pitwall
    agents: dict = {"mounted": pitwall is not None}
    if pitwall is not None:
        status = pitwall.status()
        agents.update(
            enabled=status["enabled"],
            running=status["running"],
            healthy=status["healthy"],
            restarts=status["restarts"],
            director=status["director"],
            degraded=[
                a["name"] for a in status["agents"] if a.get("disabled_reason")
            ],
        )

    producing = live["running"] or bool(replay and replay["running"])
    ok = (not producing or health["frames"] > 0) and agents.get("healthy", True)
    return {
        "ok": bool(ok),
        "pitwall": True,
        "engine": health,
        "live": live,
        "replay": replay,
        "agents": agents,
    }


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
    if services.poller.running:
        # Two frame sources into one engine is not a degraded race state, it is a
        # nonsensical one: lap counters, fuel and gaps would interleave between
        # two different races. Refuse rather than produce numbers nobody can use.
        raise HTTPException(
            409,
            "The live poller is running; a replay would interleave two frame "
            "sources into one race state. Stop it with POST /api/v1/live/stop first.",
        )

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
