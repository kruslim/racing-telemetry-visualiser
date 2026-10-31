"""The live VLM: a flagged race event, the frames around it, one grounded note.

Event-triggered only, as the research plan set out. The race-state engine's
detectors pick the moment (an incident, an off, a lock-up); this worker waits until
the frames after it exist, sends a few of them through the same
:class:`~rtv.vision.analyzer.VisionAnalyzer` and grounding as a post-race pass, stores
the note in the session's vision journal and publishes the kept observations as
``visual_observation`` events (:mod:`rtv.vision.live`). The engine never reads them.

Frames come from one of two places, whichever covers the session the engine is
consuming:

* the live capture's :class:`~rtv.vision.capture.FrameRing` (iRacing running);
* a registered, synced video of that session, during a replay of it. That is how
  live mode is tested on a recording without the sim.

Spend is bounded: it runs only with ``RTV_VISION_MODE=live`` and a key, merges
events closer together than ``RTV_VISION_LIVE_MIN_GAP_S``, and stops after
``RTV_VISION_LIVE_MAX_CALLS`` VLM calls per session run. Every skip is counted.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from rtv.logging import get_logger
from rtv.racestate.models import EventType, RaceEvent
from rtv.vision import live
from rtv.vision.analyzer import FrameGrab, VisionAnalyzer
from rtv.vision.models import Moment, TriggerRef, VisualNote
from rtv.vision.moments import EVENT_TRIGGERS
from rtv.vision.store import RunWriter, VisionStore

log = get_logger("vision.live_worker")

#: The moment window around a live event, as for post-race event moments.
PRE_S, POST_S = 2.0, 1.0


class NoFrames(RuntimeError):
    """The source has no frame near the asked session time."""


class LiveSource:
    """Where a live moment's frames come from (see :class:`LiveVisionWorker`)."""

    kind: str = "none"
    video_id: str = "live"

    async def ready(self, t_end: float, timeout_s: float) -> bool:  # pragma: no cover
        return True

    def grab(self) -> FrameGrab:  # pragma: no cover
        raise NotImplementedError


class RunState:
    """One session's live run: its journal file, call count and skip counters."""

    def __init__(self, session_id: str, analysis_id: str, writer: RunWriter) -> None:
        self.session_id = session_id
        self.analysis_id = analysis_id
        self.writer = writer
        self.calls = 0
        self.kept = 0
        self.skipped: dict[str, int] = {}
        self.last_t: float | None = None
        self.recent: list[dict[str, Any]] = []

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


class LiveVisionWorker:
    """Subscribes to the engine's bus; analyses flagged moments on its own thread."""

    def __init__(
        self,
        *,
        engine: Any,
        store: VisionStore,
        analyzer_factory: Callable[[], VisionAnalyzer],
        source_for: Callable[[str], LiveSource | None],
        enabled: Callable[[], bool],
        triggers: frozenset[str],
        max_calls: int,
        min_gap_s: float,
        facts_for: Callable[[str, Moment], dict[str, Any]] | None = None,
        ready_timeout_s: float = 6.0,
    ) -> None:
        self.engine = engine
        self.store = store
        self._analyzer_factory = analyzer_factory
        self._analyzer: VisionAnalyzer | None = None
        self._source_for = source_for
        self._enabled = enabled
        self.triggers = frozenset(t for t in triggers if t in EVENT_TRIGGERS)
        self.max_calls = max_calls
        self.min_gap_s = min_gap_s
        self._facts_for = facts_for
        self.ready_timeout_s = ready_timeout_s
        self._queue: queue.Queue[tuple[RunState, LiveSource, Moment] | None] = queue.Queue()
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.pending = 0
        self._n = 0

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self.engine is not None:
            self.engine.bus.subscribe_callback(self.on_event)
        self._thread = threading.Thread(target=self._loop, name="vision-live", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.bus.unsubscribe(self.on_event)
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(10.0)
        with self._lock:
            for sid in list(self._runs):
                self._close_run(sid, "stopped")

    def end_session(self, session_id: str, reason: str = "session_end") -> None:
        """Close a session's live run (the capture stopped, the replay ended)."""
        with self._lock:
            self._close_run(session_id, reason)

    def _close_run(self, session_id: str, reason: str) -> None:
        run = self._runs.pop(session_id, None)
        if run is None:
            return
        run.writer.write(
            "lifecycle",
            {"phase": "end", "status": "complete", "reason": reason, "notes": run.calls,
             "observations_kept": run.kept, "skipped": run.skipped},
        )  # fmt: skip
        run.writer.close()

    # ---- events (producer thread: keep it short) ------------------------
    def on_event(self, event: RaceEvent) -> None:
        etype = event.event_type.value if isinstance(event.event_type, EventType) else None
        if etype not in self.triggers:
            return
        try:
            self._admit(event, etype)
        except Exception:  # pragma: no cover - never break the producer
            log.exception("Live vision could not take %s", etype)

    def _session_id(self) -> str | None:
        try:
            return self.engine.snapshot().session.session_id
        except Exception:  # pragma: no cover
            return None

    def _admit(self, event: RaceEvent, etype: str) -> None:
        sid = self._session_id()
        if not sid or not self._enabled():
            return
        source = self._source_for(sid)
        if source is None:
            return
        t = float(event.session_time)
        with self._lock:
            run = self._run(sid, source)
            if run.last_t is not None and abs(t - run.last_t) < self.min_gap_s:
                run.skip("merged")
                return
            if run.calls + self.pending >= self.max_calls:
                run.skip("over_cap")
                return
            run.last_t = t
            self.pending += 1
            self._n += 1
            moment_id = f"L{self._n:03d}"
        kind, label = EVENT_TRIGGERS[etype]
        payload = dict(event.payload or {})
        moment = Moment(
            moment_id=moment_id,
            session_time=t,
            t_start=t - PRE_S,
            t_end=t + POST_S,
            lap=event.lap,
            lap_dist_pct=payload.get("lap_dist_pct"),
            triggers=[
                TriggerRef(
                    kind=kind, session_time=t, lap=event.lap, label=label,
                    detail={"severity": str(event.severity), "live": True, **payload},
                )
            ],
        )  # fmt: skip
        self._queue.put((run, source, moment))

    def _run(self, sid: str, source: LiveSource) -> RunState:
        run = self._runs.get(sid)
        if run is not None:
            return run
        analysis_id = self.store.new_analysis_id(sid, f"live-{source.video_id}")
        writer = self.store.open_run(sid, analysis_id)
        writer.write(
            "lifecycle",
            {"phase": "start", "session_id": sid, "video_id": source.video_id, "live": True,
             "source": source.kind, "model": self._model(), "moments_planned": None,
             "max_calls": self.max_calls, "triggers": sorted(self.triggers)},
        )  # fmt: skip
        run = RunState(sid, analysis_id, writer)
        self._runs[sid] = run
        return run

    def _model(self) -> str | None:
        try:
            return self._get_analyzer().model
        except Exception:  # pragma: no cover - no key yet
            return None

    def _get_analyzer(self) -> VisionAnalyzer:
        if self._analyzer is None:
            self._analyzer = self._analyzer_factory()
        return self._analyzer

    # ---- the worker thread ---------------------------------------------
    def _loop(self) -> None:
        asyncio.run(self._aloop())

    async def _aloop(self) -> None:
        while True:
            item = await asyncio.to_thread(self._queue.get)
            if item is None:
                return
            run, source, moment = item
            try:
                await self._analyse(run, source, moment)
            except Exception:  # pragma: no cover - one moment, not the worker
                log.exception("Live vision moment %s failed", moment.moment_id)
            finally:
                with self._lock:
                    self.pending -= 1

    async def _analyse(self, run: RunState, source: LiveSource, moment: Moment) -> None:
        if not await source.ready(moment.t_end, self.ready_timeout_s):
            with self._lock:
                run.skip("no_frames")
            return
        if self._facts_for is not None:
            try:
                moment.facts = self._facts_for(run.session_id, moment)
            except Exception:  # pragma: no cover - facts are optional
                moment.facts = {}
        started = time.monotonic()
        note = await self._get_analyzer().analyze_frames(
            moment, run.analysis_id, session_id=run.session_id, video_id=source.video_id,
            grab=source.grab(),
        )  # fmt: skip
        if note.status == "error" and (note.error or "").startswith("frames:"):
            # No frames, no call was made: it does not count against the cap.
            with self._lock:
                run.skip("no_frames")
            run.writer.note(note)
            return
        published = live.publish_note(self.engine, run.session_id, note)
        with self._lock:
            run.calls += 1
            run.kept += len(note.kept)
            run.writer.note(note)
            run.recent = (run.recent + [_brief(note, published)])[-20:]
        log.info(
            "Live vision %s: %s, %d kept, %d events, %.1fs",
            moment.moment_id, note.status, len(note.kept), published,
            time.monotonic() - started,
        )  # fmt: skip

    # ---- status --------------------------------------------------------
    def status(self, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or self._session_id()
        with self._lock:
            run = self._runs.get(sid) if sid else None
            return {
                "enabled": self._enabled(),
                "triggers": sorted(self.triggers),
                "max_calls": self.max_calls,
                "min_gap_s": self.min_gap_s,
                "pending": self.pending,
                "session_id": sid,
                "analysis_id": run.analysis_id if run else None,
                "calls": run.calls if run else 0,
                "observations_kept": run.kept if run else 0,
                "skipped": dict(run.skipped) if run else {},
                "recent": list(run.recent) if run else [],
            }


def _brief(note: VisualNote, published: int) -> dict[str, Any]:
    return {
        "note_id": note.note_id,
        "session_time": note.moment.session_time,
        "lap": note.moment.lap,
        "trigger": note.moment.primary.kind.value,
        "status": note.status,
        "latency_ms": note.latency_ms,
        "published": published,
        "kept": [
            {"kind": o.kind, "text": o.text, "confidence": o.confidence, "status": o.status}
            for o in note.kept
        ],
    }
