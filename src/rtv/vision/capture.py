"""Live onboard capture: ffmpeg grabs the sim's window into a frame ring and an MP4.

One ffmpeg process, two outputs:

* an H.264 MP4 in the session's vision folder, registered on the session when the
  capture stops, already synced (no lap-start step in the review);
* a low-rate MJPEG stream on stdout, split into JPEGs and kept in a
  :class:`FrameRing` of the last few seconds, each frame stamped with the live
  session time. The live view shows the newest one, and the live VLM worker
  (:mod:`rtv.vision.live_worker`) takes the frames around a flagged moment from it.

The input is plain ffmpeg arguments (``RTV_VISION_CAPTURE_INPUT``), by default the
iRacing window through ``gdigrab`` on Windows, so OBS is not needed. Tests use a
``lavfi`` test source or a scripted byte stream; nothing here needs iRacing.

Stamping: the session clock is the live telemetry's ``SessionTime`` (60 Hz),
extrapolated by wall time since its last frame. A frame is stamped when its last
byte arrives, so the stamp lags the screen by the pipe's latency (tens of ms);
``RTV_VISION_CAPTURE_LAG_S`` takes it off.
"""

from __future__ import annotations

import bisect
import shlex
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from rtv.logging import get_logger
from rtv.vision.models import SyncAnchor, SyncMap
from rtv.vision.video import VideoToolError, _binary

log = get_logger("vision.capture")

SOI, EOI = b"\xff\xd8", b"\xff\xd9"

#: Anchor the recording's sync every this many seconds of video.
ANCHOR_EVERY_S = 10.0


@dataclass(frozen=True)
class LiveFrame:
    seq: int
    session_time: float
    #: Seconds from the recording's first frame (the preview stream's own clock).
    video_time: float
    wall_ms: int
    jpeg: bytes


class FrameRing:
    """The last ``seconds`` of frames, in session-time order. Thread-safe."""

    def __init__(self, seconds: float = 20.0) -> None:
        self.seconds = seconds
        self._frames: deque[LiveFrame] = deque()
        self._lock = threading.Lock()
        self._seq = 0

    def push(self, session_time: float, video_time: float, jpeg: bytes) -> LiveFrame:
        with self._lock:
            self._seq += 1
            frame = LiveFrame(
                self._seq, float(session_time), float(video_time), int(time.time() * 1000), jpeg
            )
            # A session-time step backwards (a new session, an iRacing replay)
            # makes the old frames meaningless: start over rather than mix clocks.
            if self._frames and frame.session_time < self._frames[-1].session_time:
                self._frames.clear()
            self._frames.append(frame)
            while self._frames and frame.session_time - self._frames[0].session_time > self.seconds:
                self._frames.popleft()
            return frame

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def last_seq(self) -> int:
        return self._seq

    def latest(self) -> LiveFrame | None:
        with self._lock:
            return self._frames[-1] if self._frames else None

    def span(self) -> tuple[float, float] | None:
        with self._lock:
            if not self._frames:
                return None
            return self._frames[0].session_time, self._frames[-1].session_time

    def nearest(self, session_time: float, *, tolerance_s: float = 0.5) -> LiveFrame | None:
        """The frame closest to ``session_time``, if one is within ``tolerance_s``."""
        with self._lock:
            if not self._frames:
                return None
            times = [f.session_time for f in self._frames]
            i = bisect.bisect_left(times, session_time)
            best = min(
                (self._frames[j] for j in (i - 1, i) if 0 <= j < len(times)),
                key=lambda f: abs(f.session_time - session_time),
            )
        return best if abs(best.session_time - session_time) <= tolerance_s else None


def split_jpegs(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Cut a concatenated MJPEG byte stream (ffmpeg ``image2pipe``) into JPEGs.

    ffmpeg's MJPEG encoder writes baseline JPEGs with no embedded thumbnail, so
    the first end-of-image marker after a start-of-image ends the frame.
    """
    buf = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        while True:
            start = buf.find(SOI)
            if start < 0:
                # Keep a trailing 0xff: it may be the first half of the next SOI.
                del buf[: max(0, len(buf) - 1)]
                break
            end = buf.find(EOI, start + 2)
            if end < 0:
                if start:
                    del buf[:start]
                break
            yield bytes(buf[start : end + 2])
            del buf[: end + 2]


class SessionClock:
    """Live session time: the last telemetry ``SessionTime`` plus wall time since."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._mono = monotonic
        self._t: float | None = None
        self._at = 0.0
        self.session_id: str | None = None

    def observe(self, session_time: float, session_id: str | None = None) -> None:
        self._t, self._at = float(session_time), self._mono()
        if session_id:
            self.session_id = session_id

    def reset(self) -> None:
        self._t, self.session_id = None, None

    def now(self, *, max_stale_s: float = 2.0) -> float | None:
        """None when telemetry has not arrived for ``max_stale_s`` (sim paused or gone)."""
        if self._t is None:
            return None
        dt = self._mono() - self._at
        if dt > max_stale_s:
            return None
        return self._t + dt


def capture_command(
    ffmpeg: str,
    input_args: str,
    *,
    record_path: Path | None,
    preview_fps: float,
    preview_width: int,
    quality: int = 5,
) -> list[str]:
    """The ffmpeg command line: the input, then the MP4 output (if any), then the pipe."""
    cmd = [_binary(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error"]
    cmd += shlex.split(input_args, posix=True)
    if record_path is not None:
        cmd += [
            "-map", "0:v:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            # Fragmented MP4: a crash or a kill leaves a playable file.
            "-movflags", "+frag_keyframe+empty_moov",
            "-y", str(record_path),
        ]  # fmt: skip
    cmd += [
        "-map", "0:v:0",
        "-vf", f"fps={preview_fps:g},scale='min({int(preview_width)},iw)':-2",
        "-c:v", "mjpeg", "-q:v", str(quality),
        "-f", "image2pipe", "pipe:1",
    ]  # fmt: skip
    return cmd


class LiveCapture:
    """One capture run for one session: ffmpeg, the reader thread, the sync anchors."""

    def __init__(
        self,
        command: list[str],
        ring: FrameRing,
        clock: Callable[[], float | None],
        *,
        session_id: str,
        record_path: Path | None,
        preview_fps: float,
        lag_s: float = 0.0,
        popen: Callable[..., Any] = subprocess.Popen,
        on_frame: Callable[[LiveFrame], None] | None = None,
    ) -> None:
        self.command = command
        self.ring = ring
        self._clock = clock
        self.session_id = session_id
        self.record_path = record_path
        self.preview_fps = preview_fps
        self.lag_s = lag_s
        self._popen = popen
        self._on_frame = on_frame
        self._proc: Any = None
        self._thread: threading.Thread | None = None
        self.frames = 0
        self.unstamped = 0
        self.anchors: list[SyncAnchor] = []
        self.started_wall_ms: int | None = None
        self.ended_wall_ms: int | None = None
        self.error: str | None = None
        #: The newest frame, stamped or not (what the live view shows).
        self.last_jpeg: bytes | None = None
        self._stderr_tail = b""
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.record_path is not None:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._proc = self._popen(
                self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0,
            )  # fmt: skip
        except OSError as exc:
            raise VideoToolError(f"Could not start ffmpeg: {exc}") from exc
        self.started_wall_ms = int(time.time() * 1000)
        self._thread = threading.Thread(target=self._read, name="vision-capture", daemon=True)
        self._thread.start()
        # Drain stderr so a chatty ffmpeg can never block on a full pipe.
        threading.Thread(target=self._drain, name="vision-capture-err", daemon=True).start()

    def _drain(self) -> None:
        stream = getattr(self._proc, "stderr", None)
        if stream is None:
            return
        try:
            for chunk in self._chunks(stream):
                self._stderr_tail = (self._stderr_tail + chunk)[-2000:]
        except (OSError, ValueError):  # pragma: no cover - closed under us
            pass

    def _chunks(self, stream: IO[bytes]) -> Iterator[bytes]:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            yield chunk

    def _read(self) -> None:
        index = 0
        next_anchor = 0.0
        try:
            for jpeg in split_jpegs(self._chunks(self._proc.stdout)):
                video_time = index / self.preview_fps
                index += 1
                self.last_jpeg = jpeg
                now = self._clock()
                if now is None:
                    # No live telemetry: the frame cannot be placed on the
                    # session clock, so it is shown nowhere and never analysed.
                    self.unstamped += 1
                    continue
                st = now - self.lag_s
                frame = self.ring.push(st, video_time, jpeg)
                self.frames += 1
                if video_time >= next_anchor:
                    self.anchors.append(
                        SyncAnchor(video_time=round(video_time, 3), session_time=round(st, 3),
                                   source="capture")
                    )  # fmt: skip
                    next_anchor = video_time + ANCHOR_EVERY_S
                if self._on_frame is not None:
                    self._on_frame(frame)
        except Exception as exc:  # pragma: no cover - surfaced in status
            log.exception("Vision capture reader failed")
            self.error = str(exc)[:300]
        finally:
            code = self._proc.wait()
            self.ended_wall_ms = int(time.time() * 1000)
            if code not in (0, None) and self.error is None and not self._stopping:
                time.sleep(0.1)  # let the drain thread catch the last words
                err = self._stderr_tail.decode("utf-8", "replace").strip()[-300:]
                self.error = f"ffmpeg exited with {code}: {err}"

    def stop(self, timeout_s: float = 10.0) -> None:
        """Ask ffmpeg to finish (``q`` is not possible with stdin closed, so terminate)."""
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
        if self._thread is not None:
            self._thread.join(timeout_s)

    def sync_map(self) -> SyncMap | None:
        """The recording's sync from the anchors (a single anchor if a fit is refused)."""
        if not self.anchors:
            return None
        try:
            return SyncMap(anchors=list(self.anchors))
        except ValueError:
            log.warning("Capture anchors did not fit; using the first one only")
            return SyncMap(anchors=[self.anchors[0]])

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "session_id": self.session_id,
            "frames": self.frames,
            "unstamped": self.unstamped,
            "recording": str(self.record_path) if self.record_path else None,
            "started_wall_ms": self.started_wall_ms,
            "ended_wall_ms": self.ended_wall_ms,
            "error": self.error,
        }
