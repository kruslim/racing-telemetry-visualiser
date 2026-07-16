"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from rtv import __version__
from rtv.api import (
    routes_catalog,
    routes_coaching,
    routes_ingest,
    routes_sessions,
    routes_telemetry,
    ws,
)
from rtv.config import get_settings
from rtv.logging import configure_logging, get_logger
from rtv.services import build_services

log = get_logger("main")

API_PREFIX = "/api/v1"

# The vanilla-JS analysis frontend, served as static files (repo_root/frontend).
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    services = build_services(settings)
    app.state.services = services

    # Seed a simulated demo session so a fresh install has real chart + coaching
    # data to show (no .ibt required). Idempotent — skips if already present.
    if settings.seed_demo:
        try:
            from rtv.demo.seed import seed_demo_session

            seed_demo_session(services.repo, services.writer)
        except Exception:  # pragma: no cover - demo seeding must never block boot
            log.exception("Demo seeding failed; continuing without it.")

    log.info("Racing Telemetry Visualiser %s ready.", __version__)

    # Optionally start the live poller on boot (Windows + iRacing only). Disable
    # autostart when running uvicorn with --reload to avoid two processes
    # contending for the single shared-memory feed.
    if settings.autostart_live:
        services.poller.start()

    try:
        yield
    finally:
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

    @app.get("/api/v1/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    # Serve the analysis frontend (chat + graphs) at /app, and send the root there.
    # Mounted last so the API + WebSocket routes above take precedence.
    if FRONTEND_DIR.is_dir():
        @app.get("/", include_in_schema=False)
        def _root() -> RedirectResponse:
            return RedirectResponse(url="/app/")

        app.mount(
            "/app",
            StaticFiles(directory=str(FRONTEND_DIR), html=True),
            name="frontend",
        )
    else:  # pragma: no cover - frontend absent (e.g. API-only deployment)
        log.warning("Frontend dir %s not found; UI will not be served.", FRONTEND_DIR)

    return app


app = create_app()
