"""Per-session track facts: what the telemetry header says, plus the venue's named corners.

Two sources, kept apart on purpose:

- the session-info YAML iRacing writes into every ``.ibt`` and live stream
  (layout name, measured and official length, turn count, pit speed limit, the
  official sector split points). That is the authority for this layout;
- the pre-loaded venue reference (:mod:`rtv.tracks.reference`), which only adds
  real corner names, by matching the session's own Lat/Lon trace to the mapped
  corner polylines. A corner the reference does not cover stays unnamed.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from rtv.tracks.reference import MATCH_RADIUS_M, TrackReference, _header_float, resolve

#: Fewer resampled points than this on one named stretch is a graze, not a pass.
_MIN_RUN_POINTS = 3


def header_facts(info: dict[str, Any] | None) -> dict[str, Any]:
    """The layout facts from a parsed session-info dict (``WeekendInfo``, ``SplitTimeInfo``)."""
    info = info or {}
    weekend = info.get("WeekendInfo") or {}
    sectors = [
        float(s["SectorStartPct"])
        for s in (info.get("SplitTimeInfo") or {}).get("Sectors") or []
        if isinstance(s, dict) and s.get("SectorStartPct") is not None
    ]
    length_km = _header_float(weekend.get("TrackLength"))
    official_km = _header_float(weekend.get("TrackLengthOfficial"))
    turns = weekend.get("TrackNumTurns")
    return {
        "iracing_track_id": weekend.get("TrackID"),
        "iracing_track_name": weekend.get("TrackName"),
        "display_name": weekend.get("TrackDisplayName"),
        "short_name": weekend.get("TrackDisplayShortName"),
        "config_name": weekend.get("TrackConfigName"),
        "city": weekend.get("TrackCity"),
        "country": weekend.get("TrackCountry"),
        "length_m": round(length_km * 1000.0, 1) if length_km else None,
        "official_length_m": round(official_km * 1000.0, 1) if official_km else None,
        "num_turns": int(turns) if isinstance(turns, int | float) else None,
        "pit_speed_limit_kph": _header_float(weekend.get("TrackPitSpeedLimit")),
        "track_type": weekend.get("TrackType"),
        "latitude": _header_float(weekend.get("TrackLatitude")),
        "longitude": _header_float(weekend.get("TrackLongitude")),
        # SectorStartPct of each official split, lap-distance fraction; [0.0, ...].
        "sector_starts_pct": sectors,
    }


def reference_for(info: dict[str, Any] | None) -> TrackReference | None:
    return resolve((info or {}).get("WeekendInfo"))


def corner_map(
    ref: TrackReference,
    lat: np.ndarray,
    lon: np.ndarray,
    pct: np.ndarray,
    *,
    max_m: float = MATCH_RADIUS_M,
) -> list[dict[str, Any]]:
    """Locate the venue's named stretches on this session's lap, by lap-distance fraction.

    ``lat``/``lon``/``pct`` are one lap's trace in lap order (resampled is fine).
    Each point takes the nearest named stretch within ``max_m``; contiguous runs
    of the same stretch become one entry. Stretches belonging to another layout
    at the venue are never near the trace, so they drop out on their own.
    """
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    pct = np.asarray(pct, dtype=float)
    good = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(pct) & (lat != 0) & (lon != 0)
    lat, lon, pct = lat[good], lon[good], pct[good]
    if lat.size == 0 or not ref.segments:
        return []
    idx, dist = ref.nearest_segments(lat, lon)
    idx = np.where(dist <= max_m, idx, -1)

    runs: list[tuple[int, int, int]] = []  # (segment, first point, last point)
    start = 0
    for i in range(1, idx.size + 1):
        if i == idx.size or idx[i] != idx[start]:
            if idx[start] >= 0:
                runs.append((int(idx[start]), start, i - 1))
            start = i
    # a stretch across the start/finish line shows up as a run at each end
    if len(runs) >= 2 and runs[0][0] == runs[-1][0] and runs[0][1] == 0 \
            and runs[-1][2] == idx.size - 1:
        seg, a, _ = runs[-1]
        runs = [(seg, a, runs[0][2] + idx.size)] + runs[1:-1]

    out: list[dict[str, Any]] = []
    for seg_i, a, b in runs:
        if b - a + 1 < _MIN_RUN_POINTS:
            continue
        seg = ref.segments[seg_i]
        n = idx.size
        start_pct = float(pct[a % n])
        end_pct = float(pct[b % n])
        mid = (a + b) // 2
        out.append({
            "name": seg.name,
            "kind": seg.kind,
            "ref": seg.ref,
            "start_pct": round(start_pct, 5),
            "end_pct": round(end_pct, 5),
            "mid_pct": round(float(pct[mid % n]), 5),
            "max_offset_m": round(float(dist[[k % n for k in range(a, b + 1)]].max()), 1),
        })
    out.sort(key=lambda c: c["start_pct"])
    return out


def name_at_pct(mapped: list[dict[str, Any]], lap_pct: float, *, kinds=("corner",)) -> str | None:
    """The named stretch covering a lap-distance fraction, from a :func:`corner_map`."""
    p = float(lap_pct) % 1.0
    for c in mapped:
        if c["kind"] not in kinds:
            continue
        a, b = c["start_pct"], c["end_pct"]
        inside = a <= p <= b if a <= b else (p >= a or p <= b)
        if inside:
            return c["name"]
    return None
