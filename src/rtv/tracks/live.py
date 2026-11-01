"""The session's corner map, built live from clean laps, so live labels match the review.

The post-race review labels corners ``T1``, ``T2``... from Layer 1's apex detection on a stored
lap (:mod:`rtv.coaching.features`) and names them from the venue reference
(:mod:`rtv.tracks`). The live detectors only know a lap-distance fraction. This module closes the
gap without a second heuristic: it keeps the frames of the lap being driven, resampled exactly
as the store resamples a lap (``floor(LapDistPct * grid)`` bucket means), and as each clean lap
completes it runs the *same* apex detector and the *same* name lookup on it. The map is adopted
once two clean laps agree on their apexes, because one atypical lap would otherwise renumber
the whole lap (see :meth:`LiveCornerMap.offer`).

Every place an event names is then given as::

    {"label": "T1", "name": "La Source", "lap_dist_pct": 0.0412, "provisional": False}

Before that the label is the lap-distance bucket (``C00``-``C19``) with ``provisional: True``:
honest about where it was, without a corner number nobody has measured. A place belongs to the
nearest apex along the lap (each apex owns the stretch halfway to its neighbours), which is how
``get_corner_detail`` picks a Layer-1 corner too.

Names need ``Lat``/``Lon``. iRacing writes them to ``.ibt`` files; a live stream that lacks them
still gets ``T`` labels, with ``name`` left ``None``, which is also what its own review shows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from rtv.tracks.reference import TrackReference

#: Same resample grid as the Layer-1 findings, so apexes land on the same buckets.
GRID = 1500
#: A lap with a stretch longer than this lap fraction and no samples is partial (joined
#: mid-lap, a pause, a tow) and is not used for the map.
MAX_GAP_PCT = 0.02
_CHANNELS = ("Speed", "LapDist", "Lat", "Lon")
#: Two laps agree when they have the same apexes, each within this lap fraction (~140 m at
#: Spa; Layer 1 merges minima closer than 120 m, so nearer apexes would be one corner).
AGREE_PCT = 0.02
#: How far (lap fraction) one lap's apex may sit from a session-map corner and still be that
#: corner. Spa's closest pair (Bruxelles, Speaker's Corner) is 0.029 apart, so half of that.
MATCH_PCT = 0.014
#: Clean laps kept to agree with; more than this many disagreeing laps in a row is unusual.
MAX_CANDIDATES = 4


def bucket_label(lap_dist_pct: float, buckets: int) -> str:
    """The provisional lap-distance bucket, ``C00``-``C19`` for 20 buckets."""
    pct = float(lap_dist_pct) % 1.0
    return f"C{min(buckets - 1, max(0, int(pct * buckets))):02d}"


def _agree(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b, strict=True):
        d = abs(x["lap_dist_pct"] - y["lap_dist_pct"])
        if min(d, 1.0 - d) > AGREE_PCT:
            return False
    return True


def _fill(arr: np.ndarray) -> np.ndarray:
    """Interpolate empty buckets; edge-pad the ends (``features._fill`` on an array)."""
    good = np.isfinite(arr)
    if not good.any():
        return np.full(arr.size, np.nan)
    idx = np.arange(arr.size)
    return np.interp(idx, idx[good], arr[good])


class LiveCornerMap:
    """Collects one lap at a time; adopts the corner list once, when two clean laps agree."""

    def __init__(
        self,
        reference: TrackReference | None = None,
        *,
        buckets: int = 20,
        grid: int = GRID,
    ) -> None:
        self.reference = reference
        self.buckets = buckets
        self.grid = grid
        #: ``[{label, name, lap_dist_pct}]`` in lap order, empty until a clean lap is built.
        self.corners: list[dict[str, Any]] = []
        self.lap: int | None = None
        self._apex_pct = np.empty(0)
        self._lap_samples: dict[int, dict[str, Any]] = {}
        self._candidates: list[tuple[int, list[dict[str, Any]]]] = []

    @classmethod
    def from_corners(
        cls, corners: list[dict[str, Any]], *, lap: int | None, buckets: int = 20
    ) -> LiveCornerMap:
        """A map already built elsewhere (``RaceState.track``), for lookups only."""
        out = cls(buckets=buckets)
        if corners:
            out.corners = [dict(c) for c in corners]
            out._apex_pct = np.array([float(c["lap_dist_pct"]) for c in corners])
            out.lap = lap if lap is not None else -1
        return out

    @property
    def ready(self) -> bool:
        return self.lap is not None

    # ---- collection -----------------------------------------------------
    def observe(self, values: Mapping[str, Any]) -> None:
        """Add one frame to its lap's bucket sums. A no-op once the map is built."""
        if self.ready:
            return
        lap, pct = values.get("Lap"), values.get("LapDistPct")
        if not isinstance(lap, int | float) or not isinstance(pct, int | float):
            return
        if not 0.0 <= pct < 1.0:
            return
        lap = int(lap)
        acc = self._lap_samples.get(lap)
        if acc is None:
            # Keep the lap in progress and the one before (its completion edge comes a frame
            # or two after the counter moves); anything older will never be used.
            for old in [k for k in self._lap_samples if k < lap - 1]:
                del self._lap_samples[old]
            acc = self._lap_samples[lap] = {
                "n": np.zeros(self.grid),
                **{c: np.zeros(self.grid) for c in _CHANNELS},
                **{f"n_{c}": np.zeros(self.grid) for c in _CHANNELS},
            }
        b = min(self.grid - 1, int(pct * self.grid))
        acc["n"][b] += 1
        for c in _CHANNELS:
            v = values.get(c)
            if not isinstance(v, int | float) or not np.isfinite(v):
                continue
            if c in ("Lat", "Lon") and v == 0:  # iRacing's "no fix"
                continue
            acc[c][b] += float(v)
            acc[f"n_{c}"][b] += 1

    # ---- building ---------------------------------------------------------
    def lap_done(self, lap: int | None, *, clean: bool) -> bool:
        """The lap ``lap`` just ended. Offer it when clean and complete; True if adopted."""
        if self.ready or lap is None:
            return False
        acc = self._lap_samples.pop(int(lap), None)
        if not clean or acc is None:
            return False
        filled = np.flatnonzero(acc["n"])
        if filled.size < 2:
            return False
        gaps = np.diff(np.concatenate([filled, [filled[0] + self.grid]]))
        if gaps.max() > MAX_GAP_PCT * self.grid:
            return False
        mean = {}
        for c in _CHANNELS:
            n = acc[f"n_{c}"]
            with np.errstate(invalid="ignore", divide="ignore"):
                mean[c] = np.where(n > 0, acc[c] / np.maximum(n, 1), np.nan)
        if not np.isfinite(mean["Speed"]).any() or not np.isfinite(mean["LapDist"]).any():
            return False
        return self.offer(
            int(lap),
            speed=_fill(mean["Speed"]),
            dist=_fill(mean["LapDist"]),
            lat=mean["Lat"],
            lon=mean["Lon"],
        )

    def candidate(
        self,
        speed: np.ndarray,
        dist: np.ndarray,
        lat: np.ndarray | None = None,
        lon: np.ndarray | None = None,
    ) -> list[dict[str, Any]]:
        """The review's corner list for one resampled lap: Layer-1 apexes, venue names."""
        from rtv.coaching.features import detect_apexes  # lazy: features imports the store

        apexes = detect_apexes(speed, dist)
        n = speed.size
        pct = (np.arange(n) + 0.5) / n
        corners = []
        for i, ax in enumerate(apexes):
            name = None
            if (
                self.reference is not None
                and lat is not None
                and lon is not None
                and np.isfinite(lat[ax])
                and np.isfinite(lon[ax])
            ):
                seg = self.reference.name_at(float(lat[ax]), float(lon[ax]))
                name = seg.name if seg else None
            corners.append(
                {"label": f"T{i + 1}", "name": name, "lap_dist_pct": round(float(pct[ax]), 4)}
            )
        return corners

    def offer(
        self,
        lap: int,
        *,
        speed: np.ndarray,
        dist: np.ndarray,
        lat: np.ndarray | None = None,
        lon: np.ndarray | None = None,
    ) -> bool:
        """One clean lap's corners. Adopted once two clean laps agree; True when adopted.

        Layer 1 numbers corners per lap, so one atypical lap renumbers everything after it: on
        the real Spa race, lap 1 (the start, in traffic) has an extra apex at Raidillon and lap 8
        misses Speaker's Corner, while twelve of fifteen clean laps agree. The review compares a
        typical lap, so the live map waits for two clean laps with the same apexes and is then
        the numbering those typical laps get.
        """
        mine = self.candidate(speed, dist, lat, lon)
        if not mine:
            return False
        for _lap, theirs in reversed(self._candidates):
            if _agree(mine, theirs):
                for c, o in zip(mine, theirs, strict=True):
                    c["name"] = c["name"] or o["name"]  # a name either lap could map
                self._adopt(lap, mine)
                return True
        self._candidates.append((lap, mine))
        del self._candidates[:-MAX_CANDIDATES]
        return False

    def build(
        self,
        lap: int,
        *,
        speed: np.ndarray,
        dist: np.ndarray,
        lat: np.ndarray | None = None,
        lon: np.ndarray | None = None,
    ) -> bool:
        """Adopt one lap's corners as they are, without waiting for agreement."""
        corners = self.candidate(speed, dist, lat, lon)
        if not corners:
            return False
        self._adopt(lap, corners)
        return True

    def _adopt(self, lap: int, corners: list[dict[str, Any]]) -> None:
        self.corners = corners
        self._apex_pct = np.array([c["lap_dist_pct"] for c in corners])
        self.lap = lap
        self._lap_samples.clear()
        self._candidates.clear()

    # ---- lookup -----------------------------------------------------------
    def corner_at(self, lap_dist_pct: float) -> dict[str, Any] | None:
        """The corner owning a lap fraction: the nearest apex around the lap."""
        if not self.corners:
            return None
        p = float(lap_dist_pct) % 1.0
        d = np.abs(self._apex_pct - p)
        d = np.minimum(d, 1.0 - d)
        return self.corners[int(np.argmin(d))]

    def place(self, lap_dist_pct: float | None) -> dict[str, Any] | None:
        """``{label, name, lap_dist_pct, provisional}`` for a lap fraction, or None without one."""
        if not isinstance(lap_dist_pct, int | float) or not np.isfinite(lap_dist_pct):
            return None
        pct = round(float(lap_dist_pct) % 1.0, 4)
        corner = self.corner_at(pct)
        if corner is None:
            return {
                "label": bucket_label(pct, self.buckets),
                "name": None,
                "lap_dist_pct": pct,
                "provisional": True,
            }
        return {
            "label": corner["label"],
            "name": corner["name"],
            "lap_dist_pct": pct,
            "provisional": False,
        }

    def find(self, label: str | None) -> dict[str, Any] | None:
        """A corner by its label (``T7``) or label plus name (``T7 Pouhon``)."""
        if not label:
            return None
        head = str(label).split()[0].strip(":-,").upper()
        return next((c for c in self.corners if c["label"] == head), None)

    def summary(self) -> dict[str, Any]:
        return {"lap": self.lap, "provisional": not self.ready, "corners": list(self.corners)}


def place_of(payload: Mapping[str, Any]) -> float | None:
    """Where on the lap an event payload happened, or None if it names no place.

    A recurring issue spans its hits: the middle of that range, across the line if need be.
    """
    pct = payload.get("lap_dist_pct")
    if isinstance(pct, int | float):
        return float(pct)
    a, b = payload.get("lap_dist_pct_from"), payload.get("lap_dist_pct_to")
    if isinstance(a, int | float) and isinstance(b, int | float):
        if b - a > 0.5:  # the hits straddle the start/finish line
            return ((a + 1.0 + b) / 2.0) % 1.0
        return (a + b) / 2.0
    return None
