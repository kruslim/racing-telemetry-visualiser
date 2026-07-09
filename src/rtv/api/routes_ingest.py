"""Ingestion control: .ibt import jobs and the live poller."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from rtv.api.deps import get_services
from rtv.services import AppServices

router = APIRouter(tags=["ingest"])


class ImportRequest(BaseModel):
    path: str = Field(..., description="Absolute path to an .ibt telemetry file.")
    label: str | None = Field(
        None, description="Optional friendly name for the imported session."
    )


@router.post("/import", summary="Start an async .ibt import")
def start_import(
    body: ImportRequest, services: AppServices = Depends(get_services)
) -> dict:
    job_id = services.imports.start(body.path, body.label)
    return {"job_id": job_id, "status": "pending"}


@router.get("/import/discover", summary="List importable .ibt files in a folder")
def discover_ibt(
    dir: str | None = Query(None, description="Folder to scan; defaults to the "
                            "configured iRacing telemetry directory."),
    services: AppServices = Depends(get_services),
) -> dict:
    folder = Path(dir) if dir else services.settings.telemetry_dir
    files: list[dict] = []
    if folder.is_dir():
        for p in folder.glob("*.ibt"):
            try:
                st = p.stat()
            except OSError:  # pragma: no cover - race on listing
                continue
            files.append(
                {
                    "name": p.name,
                    "path": str(p),
                    "size": st.st_size,
                    "modified": datetime.fromtimestamp(
                        st.st_mtime, tz=UTC
                    ).isoformat(),
                }
            )
    files.sort(key=lambda f: f["modified"], reverse=True)
    return {"dir": str(folder), "count": len(files), "files": files}


@router.get("/import/{job_id}", summary="Import job status")
def import_status(job_id: str, services: AppServices = Depends(get_services)) -> dict:
    job = services.imports.get(job_id)
    if job is None:
        raise HTTPException(404, f"Unknown import job {job_id}")
    return job


@router.post("/live/start", summary="Start the live shared-memory poller")
def live_start(services: AppServices = Depends(get_services)) -> dict:
    services.poller.start()
    return _live_status(services)


@router.post("/live/stop", summary="Stop the live poller")
def live_stop(services: AppServices = Depends(get_services)) -> dict:
    services.poller.stop()
    return _live_status(services)


@router.get("/live/status", summary="Live poller connection state")
def live_status(services: AppServices = Depends(get_services)) -> dict:
    return _live_status(services)


def _live_status(services: AppServices) -> dict:
    return {
        "running": services.poller.running,
        "state": services.poller.state.value,
        "session_id": services.poller.session_id,
        "poll_hz": services.settings.poll_hz,
    }
