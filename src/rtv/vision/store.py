"""Where vision keeps things: the video registry and the vision journal.

Everything lives beside the race journal of the session it belongs to::

    data/sessions/<session_id>/vision/videos.json                 registered videos
    data/sessions/<session_id>/vision/videos/<video_id>_<name>     uploaded files
    data/sessions/<session_id>/vision/runs/<analysis_id>.jsonl     one analysis
    data/sessions/<session_id>/vision/frames/<analysis_id>/*.jpg   frames it sent

An analysis file uses the race journal's envelope (``seq``, ``wall_ms``,
``session_time``, ``tick``, ``kind``, ``agent``, ``run_id``, ``event_key``,
``data``), with three kinds: ``lifecycle`` (start/end), ``visual_note`` (one per
analysed moment) and nothing else. It sits in its own folder rather than in
``runs/`` so a vision pass is never mistaken for an engine run by
:func:`rtv.store.journal.default_run`, and the review keeps replaying the agents'
run unchanged. The review joins the two on ``session_time``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rtv.vision.models import VideoInfo, VisualNote

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

NOTE_KIND = "visual_note"


def check_id(value: str) -> str:
    """Ids become path segments: refuse anything that could escape the folder."""
    if not _SAFE_ID.match(value or "") or value in {".", ".."}:
        raise ValueError(f"Unsafe id {value!r}")
    return value


def safe_filename(name: str) -> str:
    stem = _SAFE_NAME.sub("_", Path(name).name).strip("._") or "video"
    return stem[:120]


class VisionStore:
    """File-backed registry and journal under ``<data_dir>/sessions``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    # ---- paths ---------------------------------------------------------
    def vision_dir(self, session_id: str) -> Path:
        return self.root / check_id(session_id) / "vision"

    def upload_dir(self, session_id: str) -> Path:
        return self.vision_dir(session_id) / "videos"

    def runs_dir(self, session_id: str) -> Path:
        return self.vision_dir(session_id) / "runs"

    def frames_dir(self, session_id: str, analysis_id: str) -> Path:
        return self.vision_dir(session_id) / "frames" / check_id(analysis_id)

    def run_path(self, session_id: str, analysis_id: str) -> Path:
        return self.runs_dir(session_id) / f"{check_id(analysis_id)}.jsonl"

    # ---- video registry ------------------------------------------------
    def _registry(self, session_id: str) -> Path:
        return self.vision_dir(session_id) / "videos.json"

    def list_videos(self, session_id: str) -> list[VideoInfo]:
        path = self._registry(session_id)
        if not path.is_file():
            return []
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [VideoInfo.model_validate(r) for r in rows]

    def get_video(self, session_id: str, video_id: str) -> VideoInfo | None:
        check_id(video_id)
        return next((v for v in self.list_videos(session_id) if v.video_id == video_id), None)

    def save_video(self, info: VideoInfo) -> VideoInfo:
        with self._lock:
            videos = [v for v in self.list_videos(info.session_id) if v.video_id != info.video_id]
            videos.append(info)
            self._write_registry(info.session_id, videos)
        return info

    def delete_video(self, session_id: str, video_id: str) -> VideoInfo | None:
        with self._lock:
            videos = self.list_videos(session_id)
            gone = next((v for v in videos if v.video_id == video_id), None)
            if gone is None:
                return None
            self._write_registry(session_id, [v for v in videos if v.video_id != video_id])
        if gone.uploaded:
            # Only a file we stored ourselves is ours to remove; a registered path
            # is the user's recording and stays where it is.
            path = Path(gone.path)
            if path.is_file() and self.upload_dir(session_id) in path.parents:
                path.unlink(missing_ok=True)
        return gone

    def _write_registry(self, session_id: str, videos: list[VideoInfo]) -> None:
        path = self._registry(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps([v.model_dump(mode="json") for v in videos], indent=1), encoding="utf-8"
        )
        tmp.replace(path)

    # ---- analyses ------------------------------------------------------
    def new_analysis_id(self, session_id: str, video_id: str) -> str:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        folder = self.runs_dir(session_id)
        n = 1 + (len(list(folder.glob(f"{stamp}-*.jsonl"))) if folder.is_dir() else 0)
        return f"{stamp}-{n:02d}-{check_id(video_id)}"

    def open_run(self, session_id: str, analysis_id: str) -> RunWriter:
        path = self.run_path(session_id, analysis_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return RunWriter(path, analysis_id)

    def list_runs(self, session_id: str) -> list[dict[str, Any]]:
        folder = self.runs_dir(session_id)
        if not folder.is_dir():
            return []
        return [summarize_run(p) for p in sorted(folder.glob("*.jsonl"))]

    def default_run(self, session_id: str) -> str | None:
        """The newest analysis that finished, else the newest one at all."""
        runs = self.list_runs(session_id)
        done = [r for r in runs if r["complete"]]
        return (done or runs)[-1]["analysis_id"] if runs else None

    def notes(self, session_id: str, analysis_id: str) -> list[VisualNote]:
        path = self.run_path(session_id, analysis_id)
        if not path.is_file():
            raise FileNotFoundError(f"Unknown vision analysis {analysis_id}")
        return [
            VisualNote.model_validate(r["data"])
            for r in iter_records(path)
            if r.get("kind") == NOTE_KIND
        ]


class RunWriter:
    """Appends one analysis in the journal envelope. Each line is flushed."""

    def __init__(self, path: Path, analysis_id: str) -> None:
        self.path = path
        self.analysis_id = analysis_id
        self._fh = path.open("a", encoding="utf-8")
        self._seq = 0
        self._lock = threading.Lock()

    def write(
        self,
        kind: str,
        data: dict[str, Any],
        *,
        session_time: float | None = None,
        event_key: str | None = None,
    ) -> None:
        with self._lock:
            self._seq += 1
            item = {
                "seq": self._seq,
                "wall_ms": int(time.time() * 1000),
                "session_time": session_time,
                "tick": None,
                "kind": kind,
                "agent": "vision",
                "run_id": self.analysis_id,
                "event_key": event_key,
                "data": data,
            }
            self._fh.write(json.dumps(item, separators=(",", ":"), default=str) + "\n")
            self._fh.flush()

    def note(self, note: VisualNote) -> None:
        self.write(
            NOTE_KIND,
            note.model_dump(mode="json"),
            session_time=note.moment.session_time,
            event_key=note.moment.primary.kind.value,
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except OSError:  # pragma: no cover - disk gone
                pass


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Every record; a torn last line (a crash mid-analysis) is skipped."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def summarize_run(path: Path) -> dict[str, Any]:
    start: dict[str, Any] = {}
    end: dict[str, Any] | None = None
    notes = kept = 0
    statuses: dict[str, int] = {}
    for rec in iter_records(path):
        kind = rec.get("kind")
        data = rec.get("data") or {}
        if kind == "lifecycle":
            if data.get("phase") == "start":
                start = rec
            elif data.get("phase") == "end":
                end = rec
        elif kind == NOTE_KIND:
            notes += 1
            status = data.get("status") or "ok"
            statuses[status] = statuses.get(status, 0) + 1
            kept += sum(
                1
                for o in data.get("observations") or []
                if o.get("status") in ("corroborated", "uncheckable")
            )
    sdata = start.get("data") or {}
    edata = (end or {}).get("data") or {}
    return {
        "analysis_id": path.stem,
        "video_id": sdata.get("video_id"),
        "model": sdata.get("model"),
        "started_wall_ms": start.get("wall_ms"),
        "ended_wall_ms": end.get("wall_ms") if end else None,
        "complete": end is not None and edata.get("status") == "complete",
        "status": edata.get("status") or ("running" if end is None else "unknown"),
        "moments_planned": sdata.get("moments_planned"),
        "notes": notes,
        "observations_kept": kept,
        "note_status": statuses,
        "error": edata.get("error"),
    }
