"""The vision service the API and the coach use: videos, sync, plans, analyses, notes.

Analyses run in a background thread with their own event loop, like ``.ibt``
imports, so a slow VLM never blocks a request. A paid analysis needs
``RTV_VISION_MODE`` set to ``post`` or ``live`` and a Kimi key; everything else
(registering and syncing a video, frames, a dry-run plan, reading notes) is free
and works with the mode ``off``.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rtv import llm
from rtv.coaching.features import CoachingService
from rtv.config import Settings
from rtv.logging import get_logger
from rtv.store.repository import Repository
from rtv.vision import live
from rtv.vision.analyzer import VisionAnalyzer
from rtv.vision.models import SyncAnchor, SyncMap, VideoInfo, VisualNote
from rtv.vision.moments import SOURCES, MomentPlan, plan_moments
from rtv.vision.store import VisionStore, check_id
from rtv.vision.video import VIDEO_EXTENSIONS, FfmpegFrames, FrameSource, probe

log = get_logger("vision.service")


class VisionDisabled(RuntimeError):
    """A paid analysis was asked for while RTV_VISION_MODE is off."""


class VisionUnavailable(RuntimeError):
    """No Kimi key (or the ai extra) for a paid analysis."""


def _default_provider() -> Any:
    from rtv.pitwall.provider import KimiProvider

    return KimiProvider(thinking=False)


class VisionService:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        coaching: CoachingService,
        *,
        store: VisionStore | None = None,
        frames: FrameSource | None = None,
        provider_factory: Callable[[], Any] | None = None,
        engine: Any | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.coaching = coaching
        self.store = store or VisionStore(settings.journal_dir)
        self.frames: FrameSource = frames or FfmpegFrames(settings.vision_ffmpeg)
        #: Swapped for a ScriptedProvider in tests; the app builds a KimiProvider.
        self.provider_factory = provider_factory or _default_provider
        #: The race-state engine, for the live seam (None when the pitwall is off).
        self.engine = engine
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ---- status --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        from rtv.vision.video import available

        return {
            "mode": self.settings.vision_mode,
            "analysis_enabled": self.settings.vision_mode != "off",
            "live_events": self.settings.vision_mode == "live" and self.engine is not None,
            "key_present": llm.is_configured(self.settings),
            "model": self.model,
            "tools": available(self.settings.vision_ffmpeg, self.settings.vision_ffprobe),
            "budget": {
                "max_frames": self.settings.vision_max_frames,
                "frames_per_moment": self.settings.vision_frames_per_moment,
                "max_moments": self.max_moments,
                "frame_width": self.settings.vision_frame_width,
            },
        }

    @property
    def model(self) -> str:
        return llm.model("reasoning", self.settings.vision_model, self.settings)

    @property
    def max_moments(self) -> int:
        return max(1, self.settings.vision_max_frames // self.settings.vision_frames_per_moment)

    # ---- videos --------------------------------------------------------
    def _require_session(self, session_id: str) -> None:
        check_id(session_id)
        if self.repo.get_session(session_id) is None:
            raise KeyError(f"Unknown session {session_id}")

    def register_video(
        self,
        session_id: str,
        path: Path,
        *,
        label: str | None = None,
        uploaded: bool = False,
        sync: SyncMap | None = None,
    ) -> VideoInfo:
        self._require_session(session_id)
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"No such video file: {path}")
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"Not a video file ({', '.join(VIDEO_EXTENSIONS)}): {path.name}")
        meta = probe(path, ffprobe=self.settings.vision_ffprobe)
        info = VideoInfo(
            video_id=uuid.uuid4().hex[:10],
            session_id=session_id,
            path=str(path.resolve()),
            label=label or path.stem,
            uploaded=uploaded,
            duration_s=meta.duration_s,
            fps=meta.fps,
            width=meta.width,
            height=meta.height,
            codec=meta.codec,
            bytes=path.stat().st_size,
            registered_wall_ms=int(time.time() * 1000),
            sync=sync or SyncMap(),
        )
        return self.store.save_video(info)

    def get_video(self, session_id: str, video_id: str) -> VideoInfo:
        video = self.store.get_video(session_id, video_id)
        if video is None:
            raise KeyError(f"Unknown video {video_id} for session {session_id}")
        return video

    def set_sync(
        self,
        session_id: str,
        video_id: str,
        *,
        offset_s: float | None = None,
        anchors: list[SyncAnchor] | None = None,
        lap_starts: list[tuple[int, float]] | None = None,
    ) -> VideoInfo:
        """Replace the video's sync. Exactly one way of giving it per call.

        ``lap_starts`` pairs an RTV lap number (as the review shows it) with the
        video time the car crosses the start/finish line to begin that lap; the
        lap's ``start_session_time`` supplies the other side of the anchor.
        """
        video = self.get_video(session_id, video_id)
        given = [x is not None for x in (offset_s, anchors, lap_starts)]
        if sum(given) != 1:
            raise ValueError("Give exactly one of offset_s, anchors or lap_starts.")
        if offset_s is not None:
            new = [SyncAnchor(video_time=0.0, session_time=float(offset_s), source="offset")]
        elif anchors is not None:
            new = list(anchors)
        else:
            starts = {r["lap"]: r.get("start_session_time") for r in self.repo.get_laps(session_id)}
            new = []
            for lap, vt in lap_starts or []:
                if starts.get(lap) is None:
                    raise KeyError(f"Session {session_id} has no lap {lap}")
                new.append(
                    SyncAnchor(
                        video_time=float(vt),
                        session_time=float(starts[lap]),
                        source=f"lap_start:{lap}",
                    )
                )
        if not new:
            raise ValueError("No sync anchors given.")
        video.sync = SyncMap(anchors=new)
        return self.store.save_video(video)

    # ---- planning ------------------------------------------------------
    def plan(
        self,
        session_id: str,
        video_id: str,
        *,
        sources: tuple[str, ...] = SOURCES,
        laps: list[int] | None = None,
        ref_lap: int | None = None,
        event_run: str | None = None,
        max_moments: int | None = None,
    ) -> MomentPlan:
        self._require_session(session_id)
        video = self.get_video(session_id, video_id)
        bad = [s for s in sources if s not in SOURCES]
        if bad:
            raise ValueError(f"Unknown moment sources {bad}; use {list(SOURCES)}")
        limit = self.max_moments if max_moments is None else min(max_moments, self.max_moments)
        return plan_moments(
            repo=self.repo,
            coaching=self.coaching,
            journal_root=self.settings.journal_dir,
            session_id=session_id,
            video=video,
            sources=tuple(sources),
            laps=laps,
            ref_lap=ref_lap,
            event_run=event_run,
            max_moments=limit,
        )

    # ---- analyses ------------------------------------------------------
    def start_analysis(self, session_id: str, video_id: str, **plan_kw: Any) -> dict[str, Any]:
        """Plan, then run the VLM over the plan in the background. Costs quota."""
        if self.settings.vision_mode == "off":
            raise VisionDisabled(
                "Vision analyses are off: set RTV_VISION_MODE=post (or live) to allow "
                "VLM calls. Dry-run plans work without it."
            )
        if not llm.is_configured(self.settings):
            raise VisionUnavailable(
                f"No Kimi key: set {llm.key_env_var(self.settings)} to run a vision analysis."
            )
        plan = self.plan(session_id, video_id, **plan_kw)
        if not plan.moments:
            raise ValueError("No moments to analyse: " + (" ".join(plan.notes) or "none found."))
        provider = self.provider_factory()
        video = self.get_video(session_id, video_id)
        analysis_id = self.store.new_analysis_id(session_id, video_id)
        writer = self.store.open_run(session_id, analysis_id)
        analyzer = VisionAnalyzer(
            provider,
            self.frames,
            self.store,
            model=self.model,
            frames_per_moment=self.settings.vision_frames_per_moment,
            frame_width=self.settings.vision_frame_width,
        )
        writer.write(
            "lifecycle",
            {
                "phase": "start",
                "session_id": session_id,
                "video_id": video_id,
                "model": analyzer.model,
                "moments_planned": len(plan.moments),
                "frames_per_moment": analyzer.frames_per_moment,
                "frame_width": analyzer.frame_width,
                "event_run": plan.event_run,
                "plan_notes": plan.notes,
            },
        )
        job = {
            "analysis_id": analysis_id,
            "session_id": session_id,
            "video_id": video_id,
            "status": "running",
            "done": 0,
            "total": len(plan.moments),
            "live_events": 0,
            "error": None,
        }
        cancel = threading.Event()
        with self._lock:
            self._jobs[analysis_id] = job
            self._cancel[analysis_id] = cancel

        def on_note(note: VisualNote) -> None:
            job["done"] += 1
            if self.settings.vision_mode == "live":
                job["live_events"] += live.publish_note(self.engine, session_id, note)

        def work() -> None:
            try:
                notes = asyncio.run(
                    analyzer.run(
                        video, plan.moments, writer, on_note=on_note, cancelled=cancel.is_set
                    )
                )
                job["status"] = "cancelled" if cancel.is_set() else "complete"
                writer.write(
                    "lifecycle",
                    {
                        "phase": "end",
                        "status": job["status"],
                        "notes": len(notes),
                        "observations_kept": sum(len(n.kept) for n in notes),
                    },
                )
            except Exception as exc:  # pragma: no cover - surfaced in the job status
                log.exception("Vision analysis %s failed", analysis_id)
                job["status"], job["error"] = "failed", str(exc)[:300]
                writer.write(
                    "lifecycle", {"phase": "end", "status": "failed", "error": job["error"]}
                )
            finally:
                writer.close()

        threading.Thread(target=work, name=f"vision-{analysis_id}", daemon=True).start()
        return dict(job)

    def cancel(self, analysis_id: str) -> bool:
        event = self._cancel.get(analysis_id)
        if event is None:
            return False
        event.set()
        return True

    def job(self, analysis_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(analysis_id)
        return dict(job) if job else None

    def list_analyses(self, session_id: str) -> list[dict[str, Any]]:
        runs = self.store.list_runs(session_id)
        for r in runs:
            job = self._jobs.get(r["analysis_id"])
            if job is not None:
                r["status"], r["progress"] = (
                    job["status"],
                    {"done": job["done"], "total": job["total"]},
                )
        return runs

    # ---- notes ---------------------------------------------------------
    def notes(
        self,
        session_id: str,
        *,
        analysis_id: str | None = None,
        lap: int | None = None,
        corner: str | None = None,
        t_from: float | None = None,
        t_to: float | None = None,
    ) -> tuple[str | None, list[VisualNote]]:
        check_id(session_id)
        analysis_id = analysis_id or self.store.default_run(session_id)
        if analysis_id is None:
            return None, []
        out = []
        for n in self.store.notes(session_id, analysis_id):
            m = n.moment
            if lap is not None and m.lap != lap:
                continue
            if corner is not None and corner not in {m.corner, *(t.corner for t in m.triggers)}:
                continue
            if t_from is not None and m.t_end < t_from:
                continue
            if t_to is not None and m.t_start > t_to:
                continue
            out.append(n)
        return analysis_id, out

    def coach_notes(self, session_id: str, lap: int | None = None) -> dict[str, Any]:
        """Kept observations in the compact, number-light shape the coach reads."""
        try:
            analysis_id, notes = self.notes(session_id, lap=lap)
        except (FileNotFoundError, ValueError):
            analysis_id, notes = None, []
        items = []
        for n in notes:
            kept = n.kept
            if not kept:
                continue
            items.append(
                {
                    "lap": n.moment.lap,
                    "corner": n.moment.corner,
                    "trigger": n.moment.primary.kind.value,
                    "trigger_seen": n.status == "ok",
                    "observations": [
                        {
                            "kind": o.kind,
                            "text": o.text,
                            "confidence": o.confidence,
                            "status": o.status,
                        }
                        for o in kept
                    ],
                }
            )
        return {
            "session_id": session_id,
            "analysis_id": analysis_id,
            "available": analysis_id is not None,
            "notes": items,
            "caveat": "Visual notes are a VLM's description of onboard frames. They carry no "
            "numbers; the lap findings are the ground truth for every figure.",
        }

    def summary(self, session_id: str) -> dict[str, Any]:
        """The vision block of the post-race summary: what exists, at a glance."""
        try:
            videos = self.store.list_videos(session_id)
            analysis_id, notes = self.notes(session_id)
        except (FileNotFoundError, ValueError):
            videos, analysis_id, notes = [], None, []
        by_kind: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for n in notes:
            by_status[n.status] = by_status.get(n.status, 0) + 1
            for o in n.kept:
                by_kind[o.kind] = by_kind.get(o.kind, 0) + 1
        return {
            "videos": len(videos),
            "synced_videos": sum(1 for v in videos if v.sync.synced),
            "analysis_id": analysis_id,
            "notes": len(notes),
            "observations_kept": sum(by_kind.values()),
            "by_kind": by_kind,
            "note_status": by_status,
        }
