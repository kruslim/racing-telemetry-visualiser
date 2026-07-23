"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rtv import __version__
from rtv.api import (
    routes_catalog,
    routes_coaching,
    routes_ingest,
    routes_pitwall,
    routes_racestate,
    routes_sessions,
    routes_telemetry,
    ws,
    ws_pitwall,
)
from rtv.config import get_settings
from rtv.logging import configure_logging, get_logger
from rtv.services import build_services

log = get_logger("main")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    services = build_services(settings)
    app.state.services = services
    log.info("Racing Telemetry Visualiser %s ready.", __version__)

    # Optionally start the live poller on boot (Windows + iRacing only). Disable
    # autostart when running uvicorn with --reload to avoid two processes
    # contending for the single shared-memory feed.
    if settings.autostart_live:
        services.poller.start()

    # The agent layer needs the running loop, so it starts here rather than in
    # build_services(). A failure here must never take the API down with it.
    if services.pitwall is not None:
        try:
            await services.pitwall.start()
        except Exception:  # pragma: no cover - defensive
            log.exception("Pitwall agent layer failed to start; continuing without it.")

    try:
        yield
    finally:
        if services.pitwall is not None:
            await services.pitwall.stop()
        services.close()
        log.info("Shut down cleanly.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Racing Telemetry Visualiser",
        version=__version__,
        lifespan=lifespan,
        description="Backend serving all iRacing telemetry (live SDK + .ibt) to a "
        "web frontend for graphs, charts and track maps.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # frontend is built separately; tighten for production
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(routes_catalog.router, prefix=API_PREFIX)
    app.include_router(routes_sessions.router, prefix=API_PREFIX)
    app.include_router(routes_telemetry.router, prefix=API_PREFIX)
    app.include_router(routes_coaching.router, prefix=API_PREFIX)
    app.include_router(routes_ingest.router, prefix=API_PREFIX)
    app.include_router(ws.router)  # /ws/live (no prefix)

    # v2 pitwall surface, behind RTV_PITWALL. The v1 routes above are unaffected.
    if get_settings().pitwall:
        app.include_router(routes_racestate.router, prefix=API_PREFIX)
        app.include_router(routes_pitwall.router, prefix=API_PREFIX)
        app.include_router(ws_pitwall.router)  # /ws/pitwall (no prefix)

    @app.get("/api/v1/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
