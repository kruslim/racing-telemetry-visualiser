"""Channel, lap-comparison and track-map query endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rtv.api.deps import get_repo
from rtv.store.queries import QueryError
from rtv.store.repository import Repository

router = APIRouter(tags=["telemetry"])


def _split(csv: str) -> list[str]:
    return [s for s in (part.strip() for part in csv.split(",")) if s]


@router.get(
    "/sessions/{session_id}/channels",
    summary="Query one or more channels over a lap (downsampled)",
)
def get_channels(
    session_id: str,
    names: str = Query(..., description="Comma-separated channel names."),
    lap: int = Query(..., ge=0),
    max_points: int = Query(2000, ge=10, le=200_000),
    mode: str = Query("minmax", pattern="^(minmax|raw)$"),
    x: str = Query("lap_dist_pct", pattern="^(tick|session_time|lap_dist_pct)$"),
    repo: Repository = Depends(get_repo),
) -> dict:
    try:
        return repo.get_channels(
            session_id, _split(names), lap=lap, max_points=max_points, mode=mode, x=x
        )
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except QueryError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get(
    "/sessions/{session_id}/channels/{name}",
    summary="Query a single channel over a lap",
)
def get_channel(
    session_id: str,
    name: str,
    lap: int = Query(..., ge=0),
    max_points: int = Query(2000, ge=10, le=200_000),
    mode: str = Query("minmax", pattern="^(minmax|raw)$"),
    x: str = Query("lap_dist_pct", pattern="^(tick|session_time|lap_dist_pct)$"),
    repo: Repository = Depends(get_repo),
) -> dict:
    try:
        return repo.get_channels(
            session_id, [name], lap=lap, max_points=max_points, mode=mode, x=x
        )
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get(
    "/sessions/{session_id}/compare",
    summary="Compare a channel across laps, aligned on lap distance",
)
def compare(
    session_id: str,
    name: str = Query(..., description="Channel to compare."),
    laps: str = Query(..., description="Comma-separated lap numbers."),
    grid: int = Query(1000, ge=50, le=20_000),
    repo: Repository = Depends(get_repo),
) -> dict:
    try:
        lap_nums = [int(s) for s in _split(laps)]
    except ValueError as exc:
        raise HTTPException(400, "laps must be comma-separated integers") from exc
    try:
        return repo.compare(session_id, name, lap_nums, grid=grid)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get(
    "/sessions/{session_id}/trackmap",
    summary="Decimated GPS/position polyline for a track map",
)
def trackmap(
    session_id: str,
    lap: int | None = Query(None, ge=0),
    color: str | None = Query(None, description="Channel to colour the path by."),
    max_points: int = Query(3000, ge=50, le=100_000),
    repo: Repository = Depends(get_repo),
) -> dict:
    try:
        return repo.get_trackmap(
            session_id, lap=lap, color=color, max_points=max_points
        )
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
