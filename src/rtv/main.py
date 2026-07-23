"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

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

    _mount_frontend(app)
    return app


#: Vanilla JS + canvas/DOM, no build step. Served from the repo root so the
#: pitwall radio page and its self-test are one URL away from the API they talk
#: to -- no second server, no CORS dance, no bundler.
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


class FrontendFiles(StaticFiles):
    """``StaticFiles`` that answers 404 rather than 405 for a non-GET.

    A catch-all mount on ``/`` sits behind every router, so it is what an
    unmatched request finally reaches. Starlette's default is a 405 ("this file
    server only does GET"), which would turn every unknown ``POST /api/v1/...``
    into a method error instead of the "no such endpoint" the API has always
    returned. Mounting a frontend must not change what the API says about paths
    it does not have.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        if scope["method"] not in ("GET", "HEAD"):
            raise StarletteHTTPException(status_code=404)
        return await super().get_response(path, scope)


def _mount_frontend(app: FastAPI) -> None:
    """Serve ``frontend/`` at the root, if it is there.

    Mounted **after** every router, so an unmatched ``/api/v1/...`` path still
    reaches FastAPI's own 404 rather than the static handler's, and the v1
    surface is unchanged whether or not this directory exists.
    """
    directory = FRONTEND_DIR
    if not directory.is_dir():  # pragma: no cover - a source checkout always has it
        log.info("No frontend/ directory at %s; serving the API only.", directory)
        return
    app.mount("/", FrontendFiles(directory=str(directory), html=True), name="frontend")


app = create_app()
