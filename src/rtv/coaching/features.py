"""Deterministic telemetry feature extraction — the Python port of ``coach.js``.

Turns raw per-lap telemetry into ~20 corner-level findings. The heavy lifting
(resampling each channel onto a uniform lap-distance grid) is delegated to the
existing :meth:`rtv.store.repository.Repository.compare`, which already aligns laps
on ``lap_dist_pct``. Everything here is plain NumPy on those aligned arrays — no LLM.

Faithful to coach.js ``build()`` (lines 36-153): corner detection by speed minima,
an elapsed-time delta curve scaled to the API lap time, and the eight diagnostics
(speed, brake, lock-up, throttle, gear, grip, consistency, sector).
"""

from __future__ import annotations

import numpy as np

from rtv.coaching.models import (
    AGENTS,
    ChiefSummary,
    CornerFinding,
    Diagnostic,
    LapFindings,
    Priority,
    SectorDelta,
)
from rtv.logging import get_logger
from rtv.store.repository import Repository

log = get_logger("coaching.features")

# iRacing channel names we pull (logical -> catalog name).
_CHANNELS = {
    "speed": "Speed",
    "throttle": "Throttle",
    "brake": "Brake",
    "gear": "Gear",
    "latG": "LatAccel",
    "dist": "LapDist",
}
# speed and dist are required; the rest degrade gracefully when absent.
_REQUIRED = ("speed", "dist")

_GRID = 1500  # resample buckets per lap (~2-3 m on a typical road course)
_SECTORS = (1.0 / 3.0, 2.0 / 3.0)  # equal-distance sector boundaries (fractions)
_G = 9.80665  # m/s^2 per g


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _fill(series: list[float | None]) -> np.ndarray:
    """Linear-interpolate gaps (None) left by per-bucket resampling; edge-pad ends."""
    arr = np.array([np.nan if v is None else float(v) for v in series], dtype=float)
    n = arr.size
    idx = np.arange(n)
    good = ~np.isnan(arr)
    if not good.any():
        return np.zeros(n)
    return np.interp(idx, idx[good], arr[good])


def _corner_type(min_kmh: float) -> str:
    if min_kmh < 95:
        return "Hairpin"
    if min_kmh < 140:
        return "Slow corner"
    if min_kmh < 200:
        return "Medium corner"
    return "Fast corner"


class CoachingService:
    """Computes :class:`LapFindings` from stored telemetry. No LLM involved."""

    def __init__(self, repo: Repository, *, grid: int = _GRID) -> None:
        self._repo = repo
        self._grid = grid

    # ------------------------------------------------------------------ public
    def lap_findings(self, session_id: str, main_lap: int, ref_lap: int) -> LapFindings:
        session = self._repo.get_session(session_id)
        if session is None:
            raise KeyError(f"Unknown session {session_id}")
        laps = {row["lap"]: row for row in self._repo.get_laps(session_id)}
        if main_lap not in laps:
            raise KeyError(f"Session {session_id} has no lap {main_lap}")
        if ref_lap not in laps:
            raise KeyError(f"Session {session_id} has no lap {ref_lap}")

        notes: list[str] = []
        cols = self._repo.valid_columns(session_id)
        chans = self._fetch_channels(session_id, main_lap, ref_lap, cols, notes)

        dist = chans["dist"]["main"]
        lap_length = float(np.nanmax(dist)) if dist.size else 0.0
        if lap_length <= 0:
            raise FileNotFoundError(f"No usable distance data for session {session_id}")
        n = dist.size
        step = lap_length / n

        def di(d_m: float) -> int:
            return int(_clamp(int(np.searchsorted(dist, d_m)), 0, n - 1))

        sp_main = chans["speed"]["main"]
        sp_ref = chans["speed"]["ref"]

        # elapsed-time delta curve (main - ref), scaled to API lap times.
        delta = self._delta_curve(
            sp_main, sp_ref, dist, laps[main_lap].get("lap_time"), laps[ref_lap].get("lap_time")
        )

        apexes = self._detect_corners(sp_main, dist, step)
        corners = [
            self._build_corner(idx, ax, chans, dist, delta, lap_length, di, step)
            for idx, ax in enumerate(apexes)
        ]

        self._consistency(session_id, corners, di, step, notes)
        sectors = self._sectors(sp_main, sp_ref, dist, lap_length, laps, main_lap, ref_lap)
        chief = self._chief(corners, delta, laps)

        return LapFindings(
            session_id=session_id,
            car_id=session.get("car_id"),
            track_id=session.get("track_id"),
            track_name=session.get("track_name"),
            main_lap=main_lap,
            ref_lap=ref_lap,
            main_time=laps[main_lap].get("lap_time"),
            ref_time=laps[ref_lap].get("lap_time"),
            lap_length_m=lap_length,
            corners=corners,
            sectors=sectors,
            chief=chief,
            notes=notes,
        )

    # ------------------------------------------------------------------ fetch
    def _fetch_channels(
        self, session_id: str, main: int, ref: int, cols: set[str], notes: list[str]
    ) -> dict[str, dict[str, np.ndarray]]:
        out: dict[str, dict[str, np.ndarray]] = {}
        for key, name in _CHANNELS.items():
            if name not in cols:
                if key in _REQUIRED:
                    raise FileNotFoundError(f"Channel {name!r} missing for session {session_id}")
                notes.append(f"{name} unavailable - {key} diagnostics skipped.")
                continue
            cmp = self._repo.compare(session_id, name, [main, ref], grid=self._grid)
            series = cmp["laps"]
            if str(main) not in series or str(ref) not in series:
                if key in _REQUIRED:
                    raise FileNotFoundError(
                        f"No telemetry for laps {main}/{ref} in session {session_id}"
                    )
                notes.append(f"{name} missing for one lap - {key} diagnostics skipped.")
                continue
            arr = {"main": _fill(series[str(main)]), "ref": _fill(series[str(ref)])}
            if key == "latG":  # m/s^2 -> g
                arr = {k: v / _G for k, v in arr.items()}
            out[key] = arr
        return out

    # ------------------------------------------------------------------ curves
    @staticmethod
    def _delta_curve(
        sp_main: np.ndarray,
        sp_ref: np.ndarray,
        dist: np.ndarray,
        t_main: float | None,
        t_ref: float | None,
    ) -> np.ndarray:
        """Cumulative (main - ref) elapsed-time delta along distance.

        Integrate dt = Δdistance / speed for each lap, scale each to its API lap time
        (iRacing's per-tick lap clock is unreliable near the line — see frontend wiring),
        then subtract. ``delta[-1]`` is the net lap delta.
        """
        ds = np.diff(dist, prepend=dist[0])
        ds[ds < 0] = 0.0

        def integ(speed: np.ndarray, lap_time: float | None) -> np.ndarray:
            t = np.cumsum(ds / np.maximum(speed, 0.1))
            if lap_time and t[-1] > 0:
                t = t * (lap_time / t[-1])
            return t

        return integ(sp_main, t_main) - integ(sp_ref, t_ref)

    @staticmethod
    def _detect_corners(speed: np.ndarray, dist: np.ndarray, step: float) -> list[int]:
        """Apex indices = prominent local minima of the (smoothed) speed trace."""
        n = speed.size
        if n < 5:
            return []
        # light smoothing to suppress sensor noise before minima detection.
        # edge-pad (not zero-pad) so the start/finish line isn't read as a false dip.
        k = max(1, int(round(7.0 / step)))  # ~7 m window
        kern = np.ones(2 * k + 1) / (2 * k + 1)
        sm = np.convolve(np.pad(speed, k, mode="edge"), kern, mode="valid")

        win = max(1, int(round(90.0 / step)))  # ±90 m local-min window
        prom_thresh = 4.0  # m/s (~14 km/h) dip to count as a corner
        spacing = max(1, int(round(120.0 / step)))  # merge minima closer than 120 m

        cand: list[int] = []
        for i in range(n):
            lo, hi = max(0, i - win), min(n - 1, i + win)
            seg = sm[lo : hi + 1]
            if sm[i] > seg.min() + 1e-9:
                continue
            # prominence: dip relative to the higher shoulder within a wider window
            wlo, whi = max(0, i - 3 * win), min(n - 1, i + 3 * win)
            shoulder = min(sm[wlo : i + 1].max(), sm[i : whi + 1].max())
            if shoulder - sm[i] >= prom_thresh:
                cand.append(i)

        # dedupe near-duplicates / plateau minima: keep the lowest within `spacing`
        merged: list[int] = []
        for i in cand:
            if merged and i - merged[-1] <= spacing:
                if sm[i] < sm[merged[-1]]:
                    merged[-1] = i
            else:
                merged.append(i)
        return merged

    # ------------------------------------------------------------------ corner
    def _build_corner(
        self,
        idx: int,
        apex: int,
        chans: dict[str, dict[str, np.ndarray]],
        dist: np.ndarray,
        delta: np.ndarray,
        lap_length: float,
        di,
        step: float,
    ) -> CornerFinding:
        n = dist.size
        apex_d = float(dist[apex])
        entry, exit_ = di(apex_d - 150.0), min(n - 1, di(apex_d + 170.0))
        net_dt = float(delta[exit_] - delta[entry])

        sp_main, sp_ref = chans["speed"]["main"], chans["speed"]["ref"]
        mm = self._local_min(sp_main, apex_d, 90.0, dist, di)
        rm = self._local_min(sp_ref, apex_d, 90.0, dist, di)
        min_main, min_ref = mm["v"] * 3.6, rm["v"] * 3.6
        apex_d = mm["d"]  # refine apex to the actual speed minimum (coach.js parity)

        diags: list[Diagnostic] = []

        # SPEED / LINE — minimum corner speed
        dk = min_main - min_ref
        if abs(dk) >= 1.5:
            diags.append(
                Diagnostic(
                    "speed", f"Min speed {'+' if dk >= 0 else '-'}{abs(dk):.0f} km/h vs reference",
                    apex_d, abs(dk), good=dk > 0,
                )
            )

        # BRAKE — brake point + lock-up
        if "brake" in chans:
            bpm = self._brake_point(chans["brake"]["main"], apex_d, dist, di)
            bpr = self._brake_point(chans["brake"]["ref"], apex_d, dist, di)
            if bpm is not None and bpr is not None:
                bd = bpm - bpr
                if abs(bd) >= 7:
                    word = f"{abs(bd):.0f} m early" if bd < 0 else f"{bd:.0f} m late"
                    diags.append(Diagnostic("brake", f"Brake point {word}", bpm, abs(bd) / 3))
            lock_dip = min_ref - min_main
            brake_peak = self._peak_abs(chans["brake"]["main"], apex_d, 80.0, dist, di)
            if lock_dip >= 5 and brake_peak > 0.5:
                diags.append(
                    Diagnostic(
                        "brake", f"Lock-up - {lock_dip:.0f} km/h scrubbed at entry",
                        apex_d, lock_dip / 1.6, lockup=True,
                    )
                )

        # THROTTLE — application point
        if "throttle" in chans:
            tom = self._throttle_on(chans["throttle"]["main"], apex_d, dist, di)
            tor = self._throttle_on(chans["throttle"]["ref"], apex_d, dist, di)
            if tom is not None and tor is not None:
                td = tom - tor
                if abs(td) >= 10:
                    word = f"{abs(td):.0f} m later" if td > 0 else f"{abs(td):.0f} m earlier"
                    diags.append(
                        Diagnostic("throttle", f"Full throttle {word} than ref", tom,
                                   abs(td) / 3, good=td < 0)
                    )

        # GEARING — gear used at apex
        if "gear" in chans:
            gm = round(float(chans["gear"]["main"][di(apex_d)]))
            gr = round(float(chans["gear"]["ref"][di(apex_d)]))
            if gm != gr:
                diags.append(
                    Diagnostic("gear", f"Apex in gear {gm} - reference used {gr}", apex_d, 1.3)
                )

        # GRIP / TYRES — lateral g held
        if "latG" in chans:
            lgm = self._peak_abs(chans["latG"]["main"], apex_d, 90.0, dist, di)
            lgr = self._peak_abs(chans["latG"]["ref"], apex_d, 90.0, dist, di)
            if lgr - lgm >= 0.12:
                diags.append(
                    Diagnostic("grip", f"Peak {lgm:.2f}g lateral - ref held {lgr:.2f}g",
                               apex_d, (lgr - lgm) * 3)
                )

        # apportion the corner's lost time across the loss diagnostics
        lost = max(0.0, net_dt)
        tot_mag = sum(d.magnitude for d in diags if not d.good) or 1.0
        for d in diags:
            d.time_loss = 0.0 if d.good else lost * (d.magnitude / tot_mag)

        sector = self._sector_of(apex_d, lap_length)
        return CornerFinding(
            index=idx, label=f"T{idx + 1}", distance=apex_d, sector=sector,
            type=_corner_type(min_main), min_main=min_main, min_ref=min_ref,
            net_dt=net_dt, diags=diags,
        )

    # ------------------------------------------------------------------ stint consistency
    def _consistency(self, session_id: str, corners, di, step: float, notes: list[str]) -> None:
        if not corners:
            return
        valid = [
            r["lap"] for r in self._repo.get_laps(session_id)
            if r.get("is_valid") and not r.get("is_out_lap") and not r.get("is_in_lap")
        ]
        if len(valid) < 2:
            notes.append("Fewer than two clean laps - consistency spread not computed.")
            return
        cmp = self._repo.compare(session_id, "Speed", valid, grid=self._grid)
        lap_arrs = {lap: _fill(s) for lap, s in cmp["laps"].items()}
        spreads: list[tuple[float, int]] = []
        for ci, c in enumerate(corners):
            lo, hi = max(0, di(c.distance - 90)), di(c.distance + 90)
            mins = [a[lo : hi + 1].min() * 3.6 for a in lap_arrs.values() if a.size]
            spread = (max(mins) - min(mins)) if mins else 0.0
            spreads.append((spread, ci))
        for spread, ci in sorted(spreads, reverse=True)[:2]:
            if spread >= 6:
                c = corners[ci]
                c.diags.append(
                    Diagnostic(
                        "consistency", f"+-{spread:.0f} km/h min-speed spread over stint",
                        c.distance, spread / 3, variance=True,
                    )
                )

    # ------------------------------------------------------------------ sectors
    def _sectors(
        self, sp_main, sp_ref, dist, lap_length, laps, main_lap, ref_lap
    ) -> list[SectorDelta]:
        """Equal-distance 3-sector splits derived from the integrated time curves.

        iRacing official sector splits are not stored, so these are even thirds — useful
        for an at-a-glance breakdown, flagged in ``notes`` upstream when it matters.
        """
        t_main = self._scaled_time(sp_main, dist, laps[main_lap].get("lap_time"))
        t_ref = self._scaled_time(sp_ref, dist, laps[ref_lap].get("lap_time"))
        bounds = [0.0, *(_f * lap_length for _f in _SECTORS), lap_length]
        out: list[SectorDelta] = []
        for i in range(3):
            a = int(np.searchsorted(dist, bounds[i]))
            b = min(dist.size - 1, int(np.searchsorted(dist, bounds[i + 1])))
            m = float(t_main[b] - t_main[a])
            r = float(t_ref[b] - t_ref[a])
            out.append(SectorDelta(index=i, name=f"S{i + 1}", main=m, ref=r, delta=m - r))
        return out

    @staticmethod
    def _scaled_time(speed: np.ndarray, dist: np.ndarray, lap_time: float | None) -> np.ndarray:
        ds = np.diff(dist, prepend=dist[0])
        ds[ds < 0] = 0.0
        t = np.cumsum(ds / np.maximum(speed, 0.1))
        if lap_time and t[-1] > 0:
            t = t * (lap_time / t[-1])
        return t

    # ------------------------------------------------------------------ chief
    @staticmethod
    def _chief(corners, delta: np.ndarray, laps) -> ChiefSummary:
        lost = sum(max(0.0, c.net_dt) for c in corners)
        net_lap = float(delta[-1]) if delta.size else 0.0
        losing = [c for c in corners if c.net_dt > 0.02]
        top3 = [
            Priority(index=c.index, label=c.label, gain=c.net_dt, why=_top_reason(c))
            for c in sorted(losing, key=lambda c: c.net_dt, reverse=True)[:3]
        ]
        times = [r["lap_time"] for r in laps.values() if r.get("lap_time")]
        spread = (max(times) - min(times)) if len(times) >= 2 else None
        return ChiefSummary(
            lost=lost, net_lap=net_lap, losing_n=len(losing), total=len(corners),
            top3=top3, time_spread=spread,
        )

    # ------------------------------------------------------------------ channel helpers
    @staticmethod
    def _local_min(arr, c_m: float, win_m: float, dist, di) -> dict:
        lo, hi = di(c_m - win_m), di(c_m + win_m)
        seg = arr[lo : hi + 1]
        if seg.size == 0:
            return {"v": float(arr[di(c_m)]), "d": c_m}
        j = int(seg.argmin())
        return {"v": float(seg[j]), "d": float(dist[lo + j])}

    @staticmethod
    def _peak_abs(arr, c_m: float, win_m: float, dist, di) -> float:
        lo, hi = di(c_m - win_m), di(c_m + win_m)
        seg = arr[lo : hi + 1]
        return float(np.abs(seg).max()) if seg.size else 0.0

    @staticmethod
    def _brake_point(brk, c_m: float, dist, di) -> float | None:
        lo, hi = di(c_m - 270.0), di(c_m)
        for i in range(lo, hi + 1):
            if brk[i] > 0.09:
                return float(dist[i])
        return None

    @staticmethod
    def _throttle_on(thr, c_m: float, dist, di) -> float | None:
        lo, hi = di(c_m), di(c_m + 360.0)
        for i in range(lo, hi + 1):
            if thr[i] > 0.9:
                return float(dist[i])
        return None

    @staticmethod
    def _sector_of(d_m: float, lap_length: float) -> int:
        f = d_m / lap_length if lap_length else 0.0
        if f < _SECTORS[0]:
            return 1
        if f < _SECTORS[1]:
            return 2
        return 3


def _top_reason(corner: CornerFinding) -> str:
    losses = [d for d in corner.diags if not d.variance]
    if losses:
        return max(losses, key=lambda d: d.time_loss).text
    return "Low minimum speed" if corner.min_main < corner.min_ref else "Lost on line"


# Keep the AGENTS tuple importable from here too (used by orchestrator/evals).
__all__ = ["CoachingService", "AGENTS"]
