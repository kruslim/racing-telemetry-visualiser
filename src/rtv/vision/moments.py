"""Which moments to look at: telemetry picks them, the VLM only describes them.

Two deterministic sources, both already in the app:

* **Race events** from the session's race journal (``rtv.store.journal``): the
  detectors' ``incident``, ``offtrack``, ``lockup``, ``wheelspin``,
  ``traffic_close`` and ``recurring_issue``. A journal exists once the session
  has been replayed (free, no agents) or captured live.
* **Layer-1 findings** (``CoachingService.lap_findings``): the chief summary's
  top corners on each lap against the session's fastest valid lap, located in
  session time through the lap's own ``LapDist`` trace.

Moments whose windows overlap are merged (an off at the corner that also lost the
most time is one look, not two), anything the video does not cover is dropped,
and the rest is cut to the frame budget by trigger rank. Every number on a moment
comes from the telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from rtv.coaching.features import CoachingService
from rtv.logging import get_logger
from rtv.store import journal
from rtv.store.repository import Repository
from rtv.vision.models import TRIGGER_RANK, Moment, TriggerKind, TriggerRef, VideoInfo

log = get_logger("vision.moments")

#: Race-event types that make a moment, and the number-free label the VLM sees.
EVENT_TRIGGERS: dict[str, tuple[TriggerKind, str]] = {
    "incident": (TriggerKind.INCIDENT, "an incident was recorded"),
    "offtrack": (TriggerKind.OFFTRACK, "the car went off track"),
    "lockup": (TriggerKind.LOCKUP, "a wheel locked under braking"),
    "wheelspin": (TriggerKind.WHEELSPIN, "the rear wheels spun on throttle"),
    "traffic_close": (TriggerKind.TRAFFIC_CLOSE, "another car was close and closing"),
    "recurring_issue": (TriggerKind.RECURRING_ISSUE, "the same mistake repeated here"),
}

SOURCES = ("events", "findings")

#: VertAccel peak deviation from the window median, m/s^2: above this a kerb or
#: bump was hit; below the lower bound the ride was flat. Between is undecided.
KERB_HIT_MS2 = 5.0
KERB_FLAT_MS2 = 2.0


@dataclass
class MomentPlan:
    moments: list[Moment]
    skipped_outside_video: int = 0
    skipped_over_budget: int = 0
    candidates: int = 0
    notes: list[str] = field(default_factory=list)
    event_run: str | None = None

    def to_api(self, frames_per_moment: int) -> dict[str, Any]:
        return {
            "moments": [m.model_dump(mode="json") for m in self.moments],
            "count": len(self.moments),
            "frames": len(self.moments) * frames_per_moment,
            "candidates": self.candidates,
            "skipped_outside_video": self.skipped_outside_video,
            "skipped_over_budget": self.skipped_over_budget,
            "event_run": self.event_run,
            "notes": self.notes,
        }


class _LapTimeline:
    """Lap lookup by session time, and lap distance -> session time per lap."""

    def __init__(self, repo: Repository, session_id: str) -> None:
        self._repo = repo
        self._sid = session_id
        self.laps = repo.get_laps(session_id)
        self._starts = [
            (r["start_session_time"], r["lap"])
            for r in self.laps
            if r.get("start_session_time") is not None
        ]
        self._cols = repo.valid_columns(session_id)
        self._dist: dict[int, tuple[np.ndarray, np.ndarray] | None] = {}
        self._vert: dict[int, tuple[np.ndarray, np.ndarray] | None] = {}

    def lap_at(self, t: float) -> int | None:
        lap = None
        for start, n in self._starts:
            if start <= t:
                lap = n
            else:
                break
        return lap

    def lap_span(self, lap: int) -> tuple[float, float] | None:
        for i, (start, n) in enumerate(self._starts):
            if n == lap:
                row = next(r for r in self.laps if r["lap"] == lap)
                if row.get("lap_time"):
                    return start, start + float(row["lap_time"])
                if i + 1 < len(self._starts):
                    return start, self._starts[i + 1][0]
        return None

    def _raw(self, lap: int, name: str) -> tuple[np.ndarray, np.ndarray] | None:
        if name not in self._cols:
            return None
        try:
            data = self._repo.get_channels(self._sid, [name], lap=lap, mode="raw", x="session_time")
        except (FileNotFoundError, KeyError):
            return None
        t = np.array([np.nan if v is None else v for v in data["x_values"]], dtype=float)
        y = np.array([np.nan if v is None else v for v in data["channels"][name]["y"]], dtype=float)
        good = ~(np.isnan(t) | np.isnan(y))
        return (t[good], y[good]) if good.any() else None

    def time_at_distance(self, lap: int, dist_m: float) -> float | None:
        if lap not in self._dist:
            self._dist[lap] = self._raw(lap, "LapDist")
        trace = self._dist[lap]
        if trace is None:
            return None
        t, d = trace
        # LapDist is monotonic within a lap apart from the odd reset sample at the
        # line; keep the running maximum so np.interp sees an increasing x.
        mono = np.maximum.accumulate(d)
        if dist_m < mono[0] or dist_m > mono[-1]:
            return None
        return float(np.interp(dist_m, mono, t))

    def kerb_signal(
        self, lap: int | None, t0: float, t1: float
    ) -> tuple[bool | None, float | None]:
        if lap is None:
            return None, None
        if lap not in self._vert:
            self._vert[lap] = self._raw(lap, "VertAccel")
        trace = self._vert[lap]
        if trace is None:
            return None, None
        t, a = trace
        win = a[(t >= t0) & (t <= t1)]
        if win.size < 5:
            return None, None
        dev = float(np.max(np.abs(win - np.median(win))))
        if dev >= KERB_HIT_MS2:
            return True, dev
        if dev <= KERB_FLAT_MS2:
            return False, dev
        return None, dev


def _event_candidates(
    records: list[dict[str, Any]],
    run_id: str,
    timeline: _LapTimeline,
    *,
    pre_s: float,
    post_s: float,
) -> list[Moment]:
    out: list[Moment] = []
    for rec in records:
        data = rec.get("data") or {}
        etype = data.get("event_type")
        if etype not in EVENT_TRIGGERS:
            continue
        t = data.get("session_time", rec.get("session_time"))
        if t is None:
            continue
        kind, label = EVENT_TRIGGERS[etype]
        payload = data.get("payload") or {}
        lap = timeline.lap_at(float(t))
        trig = TriggerRef(
            kind=kind,
            session_time=float(t),
            lap=lap,
            journal_seq=rec.get("seq"),
            journal_run=run_id,
            label=label,
            detail={"severity": data.get("severity"), **payload},
        )
        out.append(
            Moment(
                moment_id="",
                session_time=float(t),
                t_start=float(t) - pre_s,
                t_end=float(t) + post_s,
                lap=lap,
                lap_dist_pct=payload.get("lap_dist_pct"),
                triggers=[trig],
            )
        )
    return out


def _finding_candidates(
    coaching: CoachingService,
    session_id: str,
    timeline: _LapTimeline,
    laps: list[int],
    ref_lap: int,
    *,
    min_gain_s: float,
    max_window_s: float,
    notes: list[str],
) -> list[Moment]:
    out: list[Moment] = []
    for lap in laps:
        if lap == ref_lap:
            continue
        try:
            f = coaching.lap_findings(session_id, lap, ref_lap)
        except (KeyError, FileNotFoundError) as exc:
            notes.append(f"Lap {lap}: no findings ({exc}).")
            continue
        corners = {c.label: c for c in f.corners}
        for pri in f.chief.top3:
            if pri.gain < min_gain_s or pri.label not in corners:
                continue
            c = corners[pri.label]
            apex = timeline.time_at_distance(lap, c.distance)
            if apex is None:
                continue
            t0 = timeline.time_at_distance(lap, c.entry_m) or apex - max_window_s / 2
            t1 = timeline.time_at_distance(lap, c.exit_m) or apex + max_window_s / 2
            if t1 - t0 > max_window_s:
                t0, t1 = apex - max_window_s / 2, apex + max_window_s / 2
            trig = TriggerRef(
                kind=TriggerKind.FINDING,
                session_time=apex,
                lap=lap,
                corner=c.label,
                ref_lap=ref_lap,
                gain_s=round(pri.gain, 3),
                label=f"time was lost through {c.label} against the reference lap",
                detail={"type": c.type, "why": pri.why, "distance_m": c.distance},
            )
            out.append(
                Moment(
                    moment_id="",
                    session_time=apex,
                    t_start=t0,
                    t_end=t1,
                    lap=lap,
                    corner=c.label,
                    lap_dist_pct=c.distance / f.lap_length_m if f.lap_length_m else None,
                    triggers=[trig],
                )
            )
    return out


def _merge(candidates: list[Moment]) -> list[Moment]:
    merged: list[Moment] = []
    for m in sorted(candidates, key=lambda m: m.t_start):
        last = merged[-1] if merged else None
        if last is not None and m.t_start <= last.t_end and m.lap == last.lap:
            triggers = last.triggers + m.triggers
            primary = min(triggers, key=lambda t: TRIGGER_RANK[t.kind])
            merged[-1] = last.model_copy(
                update={
                    "t_start": min(last.t_start, m.t_start),
                    "t_end": max(last.t_end, m.t_end),
                    "session_time": primary.session_time,
                    "corner": last.corner or m.corner,
                    "lap_dist_pct": last.lap_dist_pct
                    if last.lap_dist_pct is not None
                    else m.lap_dist_pct,
                    "triggers": triggers,
                }
            )
        else:
            merged.append(m)
    return merged


def _priority(m: Moment) -> tuple[int, float]:
    gain = max((t.gain_s or 0.0) for t in m.triggers)
    return TRIGGER_RANK[m.primary.kind], -gain


def plan_moments(
    *,
    repo: Repository,
    coaching: CoachingService,
    journal_root: Path,
    session_id: str,
    video: VideoInfo,
    sources: tuple[str, ...] = SOURCES,
    laps: list[int] | None = None,
    ref_lap: int | None = None,
    event_run: str | None = None,
    max_moments: int = 15,
    pre_s: float = 2.0,
    post_s: float = 1.0,
    max_window_s: float = 4.0,
    min_gain_s: float = 0.1,
) -> MomentPlan:
    """Choose the moments one analysis will look at. Deterministic and free."""
    if repo.get_session(session_id) is None:
        raise KeyError(f"Unknown session {session_id}")
    span = video.session_span()
    if span is None:
        raise ValueError(
            "The video is not synced to the session yet (or its duration is unknown): "
            "set its sync first."
        )
    timeline = _LapTimeline(repo, session_id)
    notes: list[str] = []
    candidates: list[Moment] = []
    run_id: str | None = None

    if "events" in sources:
        runs = journal.list_runs(journal_root, session_id)
        run_id = event_run or journal.default_run(runs)
        if run_id is None:
            notes.append(
                "No race journal for this session, so no detector events: replay it once "
                "(POST /api/v1/replay/start, free) to record one."
            )
        else:
            path = journal.run_path(journal_root, session_id, run_id)
            if not path.is_file():
                raise FileNotFoundError(f"Unknown journal run {run_id}")
            records = journal.read_records(path, kinds=["race_event"])
            candidates += _event_candidates(records, run_id, timeline, pre_s=pre_s, post_s=post_s)

    if "findings" in sources:
        lap_rows = {r["lap"]: r for r in timeline.laps}
        if ref_lap is None:
            valid = [
                r
                for r in timeline.laps
                if r.get("is_valid")
                and r.get("lap_time")
                and not (r.get("is_in_lap") or r.get("is_out_lap"))
            ]
            ref_lap = min(valid, key=lambda r: r["lap_time"])["lap"] if valid else None
        if ref_lap is None:
            notes.append("No valid timed lap to use as the reference, so no finding moments.")
        else:
            if laps is None:
                laps = []
                for n, row in lap_rows.items():
                    if row.get("is_in_lap") or row.get("is_out_lap") or not row.get("lap_time"):
                        continue
                    lspan = timeline.lap_span(n)
                    if lspan and lspan[0] < span[1] and lspan[1] > span[0]:
                        laps.append(n)
            candidates += _finding_candidates(
                coaching,
                session_id,
                timeline,
                sorted(laps),
                ref_lap,
                min_gain_s=min_gain_s,
                max_window_s=max_window_s,
                notes=notes,
            )

    merged = _merge(candidates)
    inside = [m for m in merged if m.t_start >= span[0] and m.t_end <= span[1]]
    chosen = sorted(inside, key=_priority)[:max_moments]
    chosen.sort(key=lambda m: m.session_time)

    for m in chosen:
        kerb, dev = timeline.kerb_signal(m.lap, m.t_start, m.t_end)
        m.facts = {
            "triggers": sorted({t.kind.value for t in m.triggers}),
            "kerb_signal": kerb,
            "vert_accel_peak_dev_ms2": round(dev, 2) if dev is not None else None,
        }
        lap_tag = m.lap if m.lap is not None else "x"
        m.moment_id = f"m{lap_tag}-{m.primary.kind.value}-{int(round(m.session_time * 10))}"

    return MomentPlan(
        moments=chosen,
        skipped_outside_video=len(merged) - len(inside),
        skipped_over_budget=len(inside) - len(chosen),
        candidates=len(merged),
        notes=notes,
        event_run=run_id,
    )
