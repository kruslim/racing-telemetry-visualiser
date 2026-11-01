"""Track facts for a stored session: header facts plus the named corners on its own trace."""

from __future__ import annotations

from typing import Any

import numpy as np

from rtv.tracks.reference import TrackReference
from rtv.tracks.session import corner_map, header_facts, reference_for

#: Resample buckets for the trace the corner map is built on (~3-5 m per bucket).
_GRID = 1500


def _fill(series: list[float | None]) -> np.ndarray:
    return np.array([np.nan if v is None else float(v) for v in series], dtype=float)


def _trace_lap(repo, session_id: str) -> int | None:
    """The fastest clean lap: a full lap on the racing line, not through the pits."""
    laps = [
        r for r in repo.get_laps(session_id)
        if r.get("lap_time") and r.get("is_valid")
        and not r.get("is_in_lap") and not r.get("is_out_lap")
    ]
    if not laps:
        return None
    return min(laps, key=lambda r: r["lap_time"])["lap"]


def lap_trace(repo, session_id: str, lap: int, *, grid: int = _GRID):
    """(lat, lon, pct) for one lap resampled on lap distance, or ``None`` without Lat/Lon."""
    cols = repo.valid_columns(session_id)
    if "Lat" not in cols or "Lon" not in cols or "LapDistPct" not in cols:
        return None
    lat = repo.compare(session_id, "Lat", [lap], grid=grid)["laps"].get(str(lap))
    lon = repo.compare(session_id, "Lon", [lap], grid=grid)["laps"].get(str(lap))
    if not lat or not lon:
        return None
    pct = (np.arange(grid) + 0.5) / grid
    return _fill(lat), _fill(lon), pct


def session_reference(repo, session_id: str) -> TrackReference | None:
    return reference_for(repo.get_session_info(session_id))


def session_track(repo, session_id: str, lap: int | None = None) -> dict[str, Any]:
    """Everything known about this session's track. Raises ``KeyError`` for an unknown session."""
    if repo.get_session(session_id) is None:
        raise KeyError(f"Unknown session {session_id}")
    info = repo.get_session_info(session_id)
    ref = reference_for(info)
    out: dict[str, Any] = {
        "session_id": session_id,
        "header": header_facts(info),
        "reference": ref.summary() if ref else None,
        "trace_lap": None,
        "corners": [],
        "notes": [],
    }
    if ref is None:
        out["notes"].append("No pre-loaded reference for this venue: corners stay unnamed.")
        return out
    if not ref.corners:
        out["notes"].append(f"{ref.name} has no named corners in the reference data.")
    lap = lap if lap is not None else _trace_lap(repo, session_id)
    trace = lap_trace(repo, session_id, lap) if lap is not None else None
    if trace is None:
        out["notes"].append("No lap with a Lat/Lon trace: named corners not located on the lap.")
        return out
    out["trace_lap"] = lap
    out["corners"] = corner_map(ref, *trace)
    return out


def _clean_laps(repo, session_id: str) -> list[dict[str, Any]]:
    """Full timed laps on the racing line, in lap order (no pit laps, no partial segments)."""
    return sorted(
        (
            r for r in repo.get_laps(session_id)
            if r.get("lap_time") and r.get("is_valid")
            and not r.get("is_in_lap") and not r.get("is_out_lap")
        ),
        key=lambda r: r["lap"],
    )


def session_corner_map(repo, session_id: str) -> dict[str, Any]:
    """The session's one corner numbering, built from its stored laps as the live engine builds it.

    Layer 1 detects apexes on each lap afresh, so on its own ``T3`` is whatever the third dip
    of that lap was: Les Combes on most Spa laps, Bruxelles on lap 8, which misses Les
    Combes' neighbour. This runs the live map's own procedure (:class:`LiveCornerMap`) over
    the stored clean laps in lap order: the first two laps whose apexes agree fix the list,
    so the labels are the ones the live events used. With no agreeing pair the fastest clean
    lap's corners are used (``agreed`` False). Returns
    ``{"lap", "agreed", "corners": [{label, name, lap_dist_pct}]}``; ``corners`` is empty
    without two usable channels (``Speed``, ``LapDist``) or a clean lap.
    """
    from rtv.tracks.live import GRID, LiveCornerMap
    from rtv.tracks.live import _fill as _interp

    out: dict[str, Any] = {"lap": None, "agreed": False, "corners": []}
    clean = _clean_laps(repo, session_id)
    cols = repo.valid_columns(session_id)
    if not clean or not {"Speed", "LapDist", "LapDistPct"} <= set(cols):
        return out
    laps = [r["lap"] for r in clean]

    def series(name: str) -> dict[str, list]:
        if name not in cols:
            return {}
        return repo.compare(session_id, name, laps, grid=GRID)["laps"]

    speed, dist, lat, lon = (series(c) for c in ("Speed", "LapDist", "Lat", "Lon"))
    built = LiveCornerMap(session_reference(repo, session_id), grid=GRID)

    def arrays(lap: int):
        sp, d = speed.get(str(lap)), dist.get(str(lap))
        if not sp or not d:
            return None
        la, lo = lat.get(str(lap)), lon.get(str(lap))
        return {
            "speed": _interp(_fill(sp)),
            "dist": _interp(_fill(d)),
            "lat": _fill(la) if la else None,
            "lon": _fill(lo) if lo else None,
        }

    for lap in laps:
        lap_arrays = arrays(lap)
        if lap_arrays is not None and built.offer(lap, **lap_arrays):
            out.update(lap=lap, agreed=True, corners=list(built.corners))
            return out
    fastest = min(clean, key=lambda r: r["lap_time"])["lap"]
    lap_arrays = arrays(fastest)
    if lap_arrays is not None and built.build(fastest, **lap_arrays):
        out.update(lap=fastest, corners=list(built.corners))
    return out

