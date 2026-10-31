"""Live onboard: the frame ring, MJPEG splitting, capture, the live VLM worker, the API.

Offline. The VLM is a ScriptedProvider, ffmpeg is a fake process except in the one
test that captures a lavfi test pattern (skipped without ffmpeg), and the race
events are published by hand on a replay engine's bus.
"""

from __future__ import annotations

import io
import shutil
import threading
import time
from pathlib import Path

import pytest

from rtv.pitwall.provider import ScriptedProvider
from rtv.racestate.engine import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.vision.analyzer import VisionAnalyzer
from rtv.vision.capture import (
    FrameRing,
    LiveCapture,
    SessionClock,
    capture_command,
    split_jpegs,
)
from rtv.vision.live_worker import LiveSource, LiveVisionWorker
from rtv.vision.models import VisionAnswer
from rtv.vision.store import VisionStore

JPEG_A = b"\xff\xd8\xff\xe0frame-a\xff\xd9"
JPEG_B = b"\xff\xd8\xff\xe0frame-b\xff\xd9"


def _until(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


# ---- MJPEG splitting, the ring, the clock ----------------------------------


def test_split_jpegs_handles_frames_cut_across_chunks():
    stream = JPEG_A + JPEG_B + JPEG_A
    chunks = [stream[i : i + 5] for i in range(0, len(stream), 5)]
    assert list(split_jpegs(iter(chunks))) == [JPEG_A, JPEG_B, JPEG_A]


def test_split_jpegs_skips_garbage_before_a_frame():
    assert list(split_jpegs(iter([b"junk\xff", b"\xd8x\xff\xd9tail"]))) == [b"\xff\xd8x\xff\xd9"]


def test_ring_keeps_a_window_and_finds_the_nearest_frame():
    ring = FrameRing(seconds=5.0)
    for i in range(100):
        ring.push(100.0 + i * 0.1, i * 0.1, JPEG_A)
    span = ring.span()
    assert span[1] == pytest.approx(109.9)
    assert span[1] - span[0] <= 5.0 + 1e-9
    near = ring.nearest(108.04)
    assert near.session_time == pytest.approx(108.0)
    assert ring.nearest(200.0) is None
    assert ring.nearest(100.0) is None  # evicted


def test_ring_starts_over_when_session_time_goes_back():
    ring = FrameRing()
    ring.push(500.0, 0.0, JPEG_A)
    ring.push(10.0, 0.1, JPEG_B)
    assert len(ring) == 1 and ring.latest().jpeg == JPEG_B


def test_session_clock_extrapolates_and_goes_stale():
    now = [0.0]
    clock = SessionClock(monotonic=lambda: now[0])
    assert clock.now() is None
    clock.observe(120.0, "s1")
    now[0] = 0.5
    assert clock.now() == pytest.approx(120.5)
    assert clock.session_id == "s1"
    now[0] = 3.0
    assert clock.now() is None


def test_capture_command_records_and_pipes(monkeypatch):
    monkeypatch.setattr("rtv.vision.capture._binary", lambda name: name)
    cmd = capture_command(
        "ffmpeg", '-f gdigrab -framerate 30 -i "title=iRacing.com Simulator"',
        record_path=Path("out.mp4"), preview_fps=10, preview_width=768,
    )  # fmt: skip
    assert cmd[cmd.index("-i") + 1] == "title=iRacing.com Simulator"
    assert "out.mp4" in cmd and cmd[-1] == "pipe:1"
    assert "fps=10,scale='min(768,iw)':-2" in cmd
    no_rec = capture_command("ffmpeg", "-f lavfi -i testsrc", record_path=None,
                             preview_fps=5, preview_width=320)
    assert "libx264" not in no_rec


class FakeProc:
    def __init__(self, data: bytes) -> None:
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(b"")
        self.terminated = False

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        self.terminated = True


def test_live_capture_stamps_frames_and_anchors_the_recording(tmp_path):
    ring = FrameRing()
    times = iter([None, 50.0, 50.1, 50.2])
    cap = LiveCapture(
        ["ffmpeg"], ring, lambda: next(times), session_id="s1",
        record_path=tmp_path / "rec.mp4", preview_fps=10.0, lag_s=0.05,
        popen=lambda *a, **k: FakeProc(JPEG_A * 4),
    )  # fmt: skip
    cap.start()
    _until(lambda: not cap.running)
    # The first frame came before telemetry: shown, never kept.
    assert cap.unstamped == 1 and cap.frames == 3 and cap.last_jpeg == JPEG_A
    assert [round(f, 2) for f in (ring.span() or ())] == [49.95, 50.15]
    # video_time counts every frame, stamped or not: frame 1 is at 0.1 s.
    sync = cap.sync_map()
    assert sync is not None and sync.to_session(0.1) == pytest.approx(49.95)
    assert cap.status()["error"] is None


# ---- the live VLM worker ------------------------------------------------------


class RingSource(LiveSource):
    kind = "capture"
    video_id = "cap1"

    def __init__(self, have_frames: bool = True) -> None:
        self.have_frames = have_frames
        self.grabbed: list[float] = []

    async def ready(self, t_end, timeout_s):
        return self.have_frames

    def grab(self):
        async def g(st):
            self.grabbed.append(st)
            return JPEG_A, st - 90.0

        return g


def _answer(**kw):
    return VisionAnswer.model_validate({
        "trigger_visible": "yes",
        "observations": [
            {"kind": "line", "text": "Ran wide over the exit kerb onto the grass.",
             "confidence": "high"},
            {"kind": "traffic", "text": "A car was 2 lengths behind.", "confidence": "low"},
        ],
    })


def _event(t: float, etype: EventType = EventType.OFFTRACK) -> RaceEvent:
    return RaceEvent(event_type=etype, tick=int(t * 60), session_time=t, lap=3,
                     severity=Severity.ADVISORY, payload={"lap_dist_pct": 0.42})


def _worker(tmp_path, source, *, enabled=lambda: True, max_calls=3, calls=None):
    engine = RaceStateEngine(source="replay")
    engine.set_session_id("s1")
    store = VisionStore(tmp_path / "sessions")

    def handler(**kw):
        if calls is not None:
            calls.append(kw)
        return _answer()

    worker = LiveVisionWorker(
        engine=engine, store=store,
        analyzer_factory=lambda: VisionAnalyzer(ScriptedProvider(handler), None, store,
                                                model="kimi-k3", frames_per_moment=3),
        source_for=lambda sid: source if sid == "s1" else None,
        enabled=enabled,
        triggers=frozenset({"offtrack", "incident", "not_a_trigger"}),
        max_calls=max_calls, min_gap_s=5.0,
    )  # fmt: skip
    worker.start()
    return engine, store, worker


def test_a_live_offtrack_becomes_a_grounded_note_and_an_event(tmp_path):
    source, calls = RingSource(), []
    engine, store, worker = _worker(tmp_path, source, calls=calls)
    try:
        engine.bus.publish(_event(100.0))
        _until(lambda: worker.status()["calls"] == 1)
        # Frames from 2 s before to 1 s after the event, three of them.
        assert source.grabbed == [98.0, 99.5, 101.0]
        content = calls[0]["messages"][0]["content"]
        assert content[0]["text"].startswith("The telemetry flagged this moment: the car went")
        assert sum(1 for c in content if c["type"] == "image") == 3
        published = [e for e in engine.bus.history()
                     if e.event_type is EventType.VISUAL_OBSERVATION]
        # The number-bearing observation was dropped before it could air.
        assert [e.payload["text"] for e in published] == [
            "Ran wide over the exit kerb onto the grass."
        ]
        assert published[0].severity is Severity.INFO
        status = worker.status()
        assert status["recent"][0]["kept"][0]["kind"] == "line"
        assert status["triggers"] == ["incident", "offtrack"]
        aid = status["analysis_id"]
        assert "live-cap1" in aid
    finally:
        worker.stop()
    notes = store.notes("s1", aid)
    assert len(notes) == 1 and notes[0].moment.triggers[0].detail["live"] is True
    run = store.list_runs("s1")[0]
    assert run["complete"] and run["notes"] == 1


def test_live_events_are_merged_capped_and_filtered(tmp_path):
    source, calls = RingSource(), []
    engine, _store, worker = _worker(tmp_path, source, max_calls=2, calls=calls)
    try:
        engine.bus.publish(_event(100.0))
        engine.bus.publish(_event(102.0, EventType.INCIDENT))  # merged into the first
        engine.bus.publish(_event(103.0, EventType.LOCKUP))  # not a configured trigger
        engine.bus.publish(_event(120.0))
        engine.bus.publish(_event(140.0))  # over the cap
        _until(lambda: worker.status()["calls"] == 2 and worker.pending == 0)
        assert worker.status()["skipped"] == {"merged": 1, "over_cap": 1}
        assert len(calls) == 2
    finally:
        worker.stop()


def test_no_frames_costs_no_call(tmp_path):
    calls = []
    engine, _store, worker = _worker(tmp_path, RingSource(have_frames=False), calls=calls)
    try:
        engine.bus.publish(_event(100.0))
        _until(lambda: worker.pending == 0 and worker.status()["skipped"])
        assert worker.status()["skipped"] == {"no_frames": 1}
        assert worker.status()["calls"] == 0 and calls == []
    finally:
        worker.stop()


def test_the_worker_is_silent_when_disabled_or_for_another_session(tmp_path):
    calls, on = [], [False]
    engine, _store, worker = _worker(tmp_path, RingSource(), enabled=lambda: on[0], calls=calls)
    try:
        engine.bus.publish(_event(100.0))
        engine.set_session_id("other")
        on[0] = True
        engine.bus.publish(_event(200.0))
        time.sleep(0.2)
        assert calls == [] and worker.status("s1")["analysis_id"] is None
    finally:
        worker.stop()


# ---- service + API ---------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from evals.synthetic_session import seed_synthetic_session
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    with TestClient(create_app()) as c:
        sid = seed_synthetic_session(c.app.state.services.writer)
        yield c, sid
    get_settings.cache_clear()


def test_live_api_status_frame_and_mjpeg(client, monkeypatch):
    c, sid = client
    vision = c.app.state.services.vision
    body = c.get("/api/v1/vision/live").json()
    assert body["mode"] == "off" and body["source"] is None
    assert body["capture"]["running"] is False
    assert body["vlm"]["enabled"] is False
    assert c.get("/api/v1/vision/live/frame").status_code == 204

    vision.ring.push(100.0, 0.0, JPEG_A)
    r = c.get("/api/v1/vision/live/frame")
    assert r.status_code == 200 and r.content == JPEG_A
    assert r.headers["content-type"] == "image/jpeg"
    r = c.get("/api/v1/vision/live/mjpeg?max_frames=1")
    assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert JPEG_A in r.content and b"--rtvframe" in r.content

    assert c.get("/api/v1/vision/status").json()["live_vlm"] is False
    monkeypatch.setattr(vision.settings, "vision_mode", "live")
    monkeypatch.setattr("rtv.vision.service.llm.is_configured", lambda s=None: True)
    assert c.get("/api/v1/vision/live").json()["vlm"]["enabled"] is True


def test_a_replayed_session_with_a_synced_video_is_a_live_source(client, monkeypatch):
    c, sid = client
    vision = c.app.state.services.vision
    from rtv.vision import service as vision_service
    from rtv.vision.video import ProbeResult

    monkeypatch.setattr(
        vision_service, "probe",
        lambda path, **kw: ProbeResult(duration_s=60.0, fps=60.0, width=1280, height=720,
                                       codec="h264"),
    )  # fmt: skip
    clip = Path(vision.store.root) / "clip.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"not really a video")
    assert vision.live_source(sid) is None
    r = c.post(f"/api/v1/sessions/{sid}/videos",
               json={"path": str(clip), "sync": {"offset_s": 30.0}})
    assert r.status_code == 201
    source = vision.live_source(sid)
    assert source.kind == "video" and source.video_id == r.json()["video_id"]


def test_capture_start_twice_is_refused(client):
    c, _sid = client
    vision = c.app.state.services.vision

    class SlowProc(FakeProc):
        def __init__(self):
            super().__init__(b"")
            self.stdout = _Blocking()

        def poll(self):
            return None if not self.terminated else 0

        def terminate(self):
            self.terminated = True
            self.stdout.closed_flag = True

    vision._popen = lambda *a, **k: SlowProc()
    vision.settings.vision_capture_record = False
    vision.settings.vision_capture_input = "-f lavfi -i testsrc"
    import rtv.vision.capture as capture_mod

    real_binary = capture_mod._binary
    capture_mod._binary = lambda name: name
    try:
        assert c.post("/api/v1/vision/live/capture/start", json={}).json()["running"] is True
        assert c.post("/api/v1/vision/live/capture/start", json={}).status_code == 409
        stopped = c.post("/api/v1/vision/live/capture/stop").json()
        assert stopped["running"] is False and stopped["video"] is None
    finally:
        capture_mod._binary = real_binary


class _Blocking:
    """A stdout that yields nothing until the process is terminated."""

    closed_flag = False

    def read(self, n):
        while not self.closed_flag:
            time.sleep(0.01)
        return b""


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg not installed")
def test_real_ffmpeg_capture_is_registered_synced(client):
    c, sid = client
    vision = c.app.state.services.vision
    vision.settings.vision_capture_input = (
        "-re -f lavfi -i testsrc=size=320x180:rate=30:duration=1.5"
    )
    vision.settings.vision_capture_preview_fps = 10.0

    # Stand in for the live poller: telemetry keeps arriving while ffmpeg runs.
    t0, done = time.monotonic(), threading.Event()

    def feed():
        while not done.is_set():
            vision.observe_frame(200.0 + time.monotonic() - t0, sid)
            time.sleep(0.05)

    threading.Thread(target=feed, daemon=True).start()
    started = c.post("/api/v1/vision/live/capture/start", json={"session_id": sid}).json()
    assert started["running"] is True and started["session_id"] == sid
    _until(lambda: not vision.capture.running, timeout=15.0)
    done.set()
    assert vision.capture.error is None, vision.capture.error
    assert vision.capture.frames >= 5
    frame = c.get("/api/v1/vision/live/frame")
    assert frame.status_code == 200 and frame.content[:2] == b"\xff\xd8"
    stopped = c.post("/api/v1/vision/live/capture/stop").json()
    video = stopped["video"]
    assert video is not None, stopped
    assert video["sync"]["synced"] and video["label"] == "Live capture"
    assert video["sync"]["anchors"][0]["source"] == "capture"
    # The first preview frame was stamped right after the clock was set at 200 s.
    assert 199.5 < video["session_time_start"] < 202.0
    assert video["duration_s"] == pytest.approx(1.5, abs=0.3)
    listed = c.get(f"/api/v1/sessions/{sid}/videos").json()["videos"]
    assert [v["video_id"] for v in listed] == [video["video_id"]]
