"""Read an onboard video with ffprobe and ffmpeg: its metadata and single frames.

ffmpeg is the one external tool vision needs, and it is a command-line binary, not
a Python package, so nothing here imports at module load and every caller gets a
:class:`VideoToolError` with a plain message when it is missing. Paths come from
``RTV_VISION_FFMPEG`` / ``RTV_VISION_FFPROBE``.

Frames are pulled one at a time with an input-side seek (``-ss`` before ``-i``),
which is fast on any keyframed MP4 and accurate to the frame since ffmpeg 2.1.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: Container extensions accepted for registration and upload.
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")


class VideoToolError(RuntimeError):
    """ffmpeg/ffprobe is missing, or could not read the file."""


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float | None
    fps: float | None
    width: int | None
    height: int | None
    codec: str | None


class FrameSource(Protocol):  # pragma: no cover - structural type
    def frame(self, path: Path, video_time: float, *, width: int) -> bytes:
        """One JPEG frame at ``video_time`` seconds, scaled to ``width`` pixels."""
        ...


def _binary(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise VideoToolError(
            f"'{name}' was not found. Install ffmpeg (it ships ffprobe too) and put it on "
            "PATH, or point RTV_VISION_FFMPEG / RTV_VISION_FFPROBE at the binaries."
        )
    return found


def available(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> dict[str, bool]:
    return {
        "ffmpeg": shutil.which(ffmpeg) is not None,
        "ffprobe": shutil.which(ffprobe) is not None,
    }


def _rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "0"}:
        return None
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            return float(num) / float(den) if float(den) else None
        return float(value)
    except ValueError:
        return None


def probe(path: Path, *, ffprobe: str = "ffprobe", timeout_s: float = 30.0) -> ProbeResult:
    """Duration, frame rate, size and codec of the first video stream."""
    cmd = [
        _binary(ffprobe),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,duration",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]  # fmt: skip
    try:
        out = subprocess.run(cmd, capture_output=True, check=True, timeout=timeout_s)
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode("utf-8", "replace").strip()[:300]
        raise VideoToolError(f"ffprobe could not read {path.name}: {msg}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoToolError(f"ffprobe timed out on {path.name}") from exc
    data = json.loads(out.stdout or b"{}")
    streams = data.get("streams") or []
    if not streams:
        raise VideoToolError(f"{path.name} has no video stream.")
    s = streams[0]
    duration = s.get("duration") or (data.get("format") or {}).get("duration")
    return ProbeResult(
        duration_s=float(duration) if duration not in (None, "N/A") else None,
        fps=_rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate")),
        width=s.get("width"),
        height=s.get("height"),
        codec=s.get("codec_name"),
    )


class FfmpegFrames:
    """The real :class:`FrameSource`: one ffmpeg process per frame."""

    def __init__(self, ffmpeg: str = "ffmpeg", *, quality: int = 4, timeout_s: float = 30.0):
        self._ffmpeg = ffmpeg
        self._quality = quality
        self._timeout_s = timeout_s

    def frame(self, path: Path, video_time: float, *, width: int) -> bytes:
        cmd = [
            _binary(self._ffmpeg),
            "-nostdin", "-loglevel", "error",
            "-ss", f"{max(0.0, video_time):.3f}",
            "-i", str(path),
            "-frames:v", "1",
            # Never upscale a small capture; -2 keeps the height even for MJPEG.
            "-vf", f"scale='min({int(width)},iw)':-2",
            "-q:v", str(self._quality),
            "-f", "image2pipe", "-vcodec", "mjpeg", "-",
        ]  # fmt: skip
        try:
            out = subprocess.run(cmd, capture_output=True, check=True, timeout=self._timeout_s)
        except subprocess.CalledProcessError as exc:
            msg = exc.stderr.decode("utf-8", "replace").strip()[:300]
            raise VideoToolError(
                f"ffmpeg could not grab a frame at {video_time:.2f}s: {msg}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise VideoToolError(f"ffmpeg timed out grabbing a frame at {video_time:.2f}s") from exc
        if not out.stdout:
            raise VideoToolError(f"No frame at {video_time:.2f}s (past the end of the video?)")
        return out.stdout
