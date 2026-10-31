"""Vision: sync, grounding, moment planning, the analyzer, the live seam and the API.

Fully offline. The VLM is a ScriptedProvider and frames come from a fake source,
except in the one ffmpeg test, which makes its own tiny video and skips when
ffmpeg is not installed.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from rtv.pitwall.provider import ProviderResponse, ScriptedProvider
from rtv.racestate.models import EventType
from rtv.vision.grounding import ground, numeric_reason
from rtv.vision.live import publish_note, visual_events
from rtv.vision.models import (
    Moment,
    SyncAnchor,
    SyncMap,
    TriggerKind,
    TriggerRef,
    VideoInfo,
    VisionAnswer,
    VisionObservationOut,
    VisualNote,
)
from rtv.vision.store import VisionStore

JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


class FakeFrames:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def frame(self, path: Path, video_time: float, *, width: int) -> bytes:
        self.calls.append(video_time)
        return JPEG


def _moment(**kw) -> Moment:
    trig = TriggerRef(kind=kw.pop("kind", TriggerKind.OFFTRACK), session_time=100.0, lap=2,
                      label="the car went off track", corner=kw.pop("trig_corner", None))
    return Moment(moment_id="m2-offtrack-1000", session_time=100.0, t_start=98.0, t_end=101.0,
                  lap=2, triggers=[trig], **kw)


def _obs(kind: str, text: str, confidence: str = "high") -> VisionObservationOut:
    return VisionObservationOut(kind=kind, text=text, confidence=confidence)


# ---- sync ------------------------------------------------------------------


def test_sync_one_anchor_is_an_offset():
    sync = SyncMap(anchors=[SyncAnchor(video_time=10.0, session_time=110.0)])
    assert sync.rate == 1.0 and sync.offset == 100.0
    assert sync.to_session(0.0) == 100.0
    assert sync.to_video(150.0) == 50.0


def test_sync_two_anchors_fit_drift():
    # A 59.94 fps capture of a 60 Hz sim: the video clock runs slow by 0.1 %.
    sync = SyncMap(anchors=[
        SyncAnchor(video_time=0.0, session_time=50.0),
        SyncAnchor(video_time=1000.0, session_time=1051.0),
    ])
    assert sync.rate == pytest.approx(1.001)
    assert sync.to_session(500.0) == pytest.approx(550.5)
    assert sync.to_video(sync.to_session(123.4)) == pytest.approx(123.4)
    assert sync.residuals() == [0.0, 0.0]


def test_sync_rejects_an_implausible_rate():
    with pytest.raises(ValueError, match="sim clock"):
        SyncMap(anchors=[
            SyncAnchor(video_time=0.0, session_time=0.0),
            SyncAnchor(video_time=10.0, session_time=100.0),
        ])


def test_sync_survives_a_round_trip():
    sync = SyncMap(anchors=[SyncAnchor(video_time=3.0, session_time=93.0)])
    again = SyncMap.model_validate(json.loads(sync.model_dump_json()))
    assert again.offset == 90.0 and again.synced


# ---- grounding -----------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Braked 20 metres late",
    "Turned in half a second early",
    "Ran two car lengths wide at the exit",
    "Was in third gear",
])
def test_numbers_are_dropped(text):
    assert numeric_reason(text) is not None


def test_the_moments_corner_label_is_allowed():
    assert numeric_reason("Ran wide onto the exit kerb at T7", ("T7",)) is None
    assert numeric_reason("Ran wide at T8", ("T7",)) == "contains a number"


def test_ground_keeps_words_and_drops_figures():
    answer = VisionAnswer(trigger_visible="yes", observations=[
        _obs("line", "The car ran wide onto the grass on exit."),
        _obs("traffic", "A car was 3 lengths behind."),
    ])
    status, obs = ground(answer, _moment())
    assert status == "ok"
    assert [o.status for o in obs] == ["uncheckable", "dropped"]
    assert obs[1].reason.startswith("Number-free rule")


def test_kerb_claims_are_checked_against_vertical_acceleration():
    answer = VisionAnswer(trigger_visible="yes", observations=[
        _obs("kerb_use", "The car rode over the apex kerb."),
        _obs("line", "It stayed off the exit kerb."),
    ])
    _, flat = ground(answer, _moment(facts={"kerb_signal": False}))
    assert [o.status for o in flat] == ["contradicted", "corroborated"]
    assert not flat[0].kept

    _, spiky = ground(answer, _moment(facts={"kerb_signal": True}))
    assert [o.status for o in spiky] == ["corroborated", "contradicted"]

    _, unknown = ground(answer, _moment(facts={"kerb_signal": None}))
    assert [o.status for o in unknown] == ["uncheckable", "uncheckable"]


def test_trigger_not_seen_keeps_telemetry_and_lowers_confidence():
    answer = VisionAnswer(trigger_visible="no", observations=[
        _obs("track_state", "The frames show the pit lane.", "high"),
    ])
    status, obs = ground(answer, _moment())
    assert status == "trigger_not_seen"
    assert obs[0].confidence == "low"
    assert "sync" in obs[0].reason


# ---- store -----------------------------------------------------------------


def _video(tmp_path: Path, session_id: str, *, offset: float = 90.0, duration: float = 210.0):
    path = tmp_path / "onboard.mp4"
    path.write_bytes(b"not really a video")
    return VideoInfo(
        video_id="vid1", session_id=session_id, path=str(path), duration_s=duration, fps=60.0,
        sync=SyncMap(anchors=[SyncAnchor(video_time=0.0, session_time=offset, source="offset")]),
    )


def test_store_registry_round_trip(tmp_path):
    store = VisionStore(tmp_path / "sessions")
    video = _video(tmp_path, "s1")
    store.save_video(video)
    got = store.get_video("s1", "vid1")
    assert got is not None and got.session_span() == (90.0, 300.0)
    assert got.to_api()["session_time_end"] == 300.0
    # A registered (not uploaded) file is the user's: unregistering leaves it alone.
    assert store.delete_video("s1", "vid1") is not None
    assert Path(video.path).is_file()
    assert store.list_videos("s1") == []


def test_store_refuses_path_escapes(tmp_path):
    store = VisionStore(tmp_path / "sessions")
    with pytest.raises(ValueError):
        store.list_videos("../etc")
    with pytest.raises(ValueError):
        store.run_path("s1", "../../x")


# ---- moments, on the synthetic session -------------------------------------


@pytest.fixture()
def seeded(tmp_path):
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from evals.synthetic_session import SYNTHETIC_SESSION_ID, seed_synthetic_session

    from rtv.coaching.features import CoachingService
    from rtv.store.duck import Database
    from rtv.store.repository import Repository
    from rtv.store.writer import TelemetryWriter

    db = Database(tmp_path / "t.duckdb")
    seed_synthetic_session(TelemetryWriter(db, tmp_path / "parquet"))
    repo = Repository(db, tmp_path / "parquet")
    yield repo, CoachingService(repo), SYNTHETIC_SESSION_ID
    db.close()


def _write_journal(root: Path, session_id: str, events: list[dict]) -> str:
    run_dir = root / session_id / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = "20261031T000000Z-01-replay"
    lines = [{"seq": 1, "kind": "lifecycle", "session_time": 0.0,
              "data": {"phase": "start", "source": "replay"}}]
    for i, ev in enumerate(events, start=2):
        lines.append({"seq": i, "kind": "race_event", "session_time": ev["session_time"],
                      "data": {"severity": "advisory", "payload": {}, **ev}})
    (run_dir / f"{run_id}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return run_id


def test_plan_merges_events_and_findings_inside_the_video(seeded, tmp_path):
    from rtv.vision.moments import plan_moments

    repo, coaching, sid = seeded
    root = tmp_path / "sessions"
    run_id = _write_journal(root, sid, [
        {"event_type": "offtrack", "session_time": 150.0, "payload": {"lap_dist_pct": 0.5}},
        {"event_type": "incident", "session_time": 150.5},
        {"event_type": "lockup", "session_time": 20.0},  # before the video starts
        {"event_type": "lap_completed", "session_time": 196.4},  # not a trigger
    ])
    plan = plan_moments(repo=repo, coaching=coaching, journal_root=root, session_id=sid,
                        video=_video(tmp_path, sid), max_moments=50)
    assert plan.event_run == run_id
    kinds = [m.primary.kind for m in plan.moments]
    # The off and the incident half a second later are one look, led by the incident.
    merged = [m for m in plan.moments if TriggerKind.INCIDENT in {t.kind for t in m.triggers}]
    assert len(merged) == 1
    assert {t.kind for t in merged[0].triggers} >= {TriggerKind.INCIDENT, TriggerKind.OFFTRACK}
    assert merged[0].lap == 2
    assert TriggerKind.LOCKUP not in kinds
    assert plan.skipped_outside_video >= 1
    # Layer-1 findings against the fastest clean lap give corner moments too.
    findings = [m for m in plan.moments if m.primary.kind is TriggerKind.FINDING]
    assert findings and all(m.corner and m.corner.startswith("T") for m in findings)
    assert all(t.ref_lap == 6 for m in findings for t in m.triggers if t.ref_lap)
    assert all(90.0 <= m.t_start and m.t_end <= 300.0 for m in plan.moments)
    assert plan.moments == sorted(plan.moments, key=lambda m: m.session_time)
    assert all(m.facts["kerb_signal"] is None for m in plan.moments)  # no VertAccel here


def test_plan_respects_the_budget_by_rank(seeded, tmp_path):
    from rtv.vision.moments import plan_moments

    repo, coaching, sid = seeded
    root = tmp_path / "sessions"
    _write_journal(root, sid, [{"event_type": "incident", "session_time": 250.0}])
    plan = plan_moments(repo=repo, coaching=coaching, journal_root=root, session_id=sid,
                        video=_video(tmp_path, sid), max_moments=1)
    assert len(plan.moments) == 1
    assert plan.moments[0].primary.kind is TriggerKind.INCIDENT
    assert plan.skipped_over_budget >= 1


def test_plan_without_a_journal_says_how_to_get_one(seeded, tmp_path):
    from rtv.vision.moments import plan_moments

    repo, coaching, sid = seeded
    plan = plan_moments(repo=repo, coaching=coaching, journal_root=tmp_path / "none",
                        session_id=sid, video=_video(tmp_path, sid), sources=("events",))
    assert plan.moments == [] and "replay" in plan.notes[0]


def test_plan_needs_a_synced_video(seeded, tmp_path):
    from rtv.vision.moments import plan_moments

    repo, coaching, sid = seeded
    video = _video(tmp_path, sid).model_copy(update={"sync": SyncMap()})
    with pytest.raises(ValueError, match="not synced"):
        plan_moments(repo=repo, coaching=coaching, journal_root=tmp_path, session_id=sid,
                     video=video)


# ---- analyzer ----------------------------------------------------------------


def _scripted(answer: dict | None):
    def handler(**kw):
        return ProviderResponse() if answer is None else VisionAnswer.model_validate(answer)

    return ScriptedProvider(handler)


def test_analyzer_sends_frames_and_grounds_the_answer(tmp_path):
    from rtv.vision.analyzer import VisionAnalyzer

    store = VisionStore(tmp_path / "sessions")
    frames = FakeFrames()
    provider = _scripted({"trigger_visible": "yes", "observations": [
        {"kind": "line", "text": "Dropped two wheels onto the grass at the exit.",
         "confidence": "high"},
        {"kind": "traffic", "text": "A car was about 5 metres behind.", "confidence": "low"},
    ]})
    video = _video(tmp_path, "s1")
    analyzer = VisionAnalyzer(provider, frames, store, model="kimi-k3", frames_per_moment=3)
    writer = store.open_run("s1", "a1")
    notes = asyncio.run(analyzer.run(video, [_moment(corner="T7")], writer))
    writer.close()

    (note,) = notes
    assert note.status == "ok" and note.trigger_visible == "yes"
    assert [o.text for o in note.kept] == ["Dropped two wheels onto the grass at the exit."]
    # Frames span the window and are mapped through the sync (offset 90 s).
    assert frames.calls == pytest.approx([8.0, 9.5, 11.0])
    assert [f.name for f in note.frames] == [f"m2-offtrack-1000-{i}.jpg" for i in range(3)]
    assert (store.frames_dir("s1", "a1") / "m2-offtrack-1000-0.jpg").read_bytes() == JPEG

    (call,) = provider.calls
    assert call["agent"] == "vision" and call["output_model"] == "VisionAnswer"
    assert call["tools"] == []
    content = call["messages"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 3 and images[0]["source"]["type"] == "base64"
    brief = content[0]["text"]
    assert "off track" in brief and "T7" in brief
    # The model is told nothing numeric about the moment.
    assert numeric_reason(brief, ("T7",)) is None

    stored = store.notes("s1", "a1")
    assert stored[0].note_id == note.note_id
    rec = json.loads(store.run_path("s1", "a1").read_text().splitlines()[0])
    assert rec["kind"] == "visual_note" and rec["agent"] == "vision"
    assert rec["session_time"] == 100.0 and rec["event_key"] == "offtrack"


def test_analyzer_retries_once_then_records_no_answer(tmp_path):
    from rtv.vision.analyzer import VisionAnalyzer

    store = VisionStore(tmp_path / "sessions")
    provider = _scripted(None)
    analyzer = VisionAnalyzer(provider, FakeFrames(), store, model="kimi-k3")
    note = asyncio.run(analyzer.analyze_moment(_video(tmp_path, "s1"), _moment(), "a1"))
    assert note.status == "no_answer" and len(provider.calls) == 2


def test_analyzer_survives_a_bad_frame(tmp_path):
    from rtv.vision.analyzer import VisionAnalyzer
    from rtv.vision.video import VideoToolError

    class Broken:
        def frame(self, *a, **k):
            raise VideoToolError("past the end")

    provider = _scripted({"trigger_visible": "yes"})
    analyzer = VisionAnalyzer(provider, Broken(), VisionStore(tmp_path), model="kimi-k3")
    note = asyncio.run(analyzer.analyze_moment(_video(tmp_path, "s1"), _moment(), "a1"))
    assert note.status == "error" and "past the end" in note.error
    assert provider.calls == []


# ---- live seam -----------------------------------------------------------------


def _note(status: str = "ok") -> VisualNote:
    answer = VisionAnswer(trigger_visible="yes", observations=[
        _obs("traffic", "The car behind dived up the inside."),
        _obs("line", "Braked at the 100 board."),
    ])
    _, obs = ground(answer, _moment())
    return VisualNote(note_id="a1:m", analysis_id="a1", video_id="v", moment=_moment(),
                      status=status, observations=obs)


def test_visual_events_carry_only_kept_words():
    events = visual_events(_note(), tick=7)
    assert len(events) == 1
    (ev,) = events
    assert ev.event_type is EventType.VISUAL_OBSERVATION
    assert ev.severity.value == "info"
    assert ev.payload["source"] == "vision" and ev.payload["kind"] == "traffic"
    assert ev.payload["trigger"]["kind"] == "offtrack"
    assert visual_events(_note("trigger_not_seen"), tick=7) == []


def test_publish_note_goes_to_the_bus_of_the_same_session_only():
    from rtv.racestate.engine import RaceStateEngine

    engine = RaceStateEngine(source="replay")
    engine.set_session_id("s1")
    before = engine.snapshot().model_dump()
    assert publish_note(engine, "other", _note()) == 0
    assert publish_note(engine, "s1", _note()) == 1
    assert [e.event_type for e in engine.bus.history()] == [EventType.VISUAL_OBSERVATION]
    # The engine's state is untouched: vision never feeds RaceState.
    assert engine.snapshot().model_dump() == before
    assert publish_note(None, "s1", _note()) == 0


# ---- coach tool --------------------------------------------------------------


def test_coach_tool_degrades_without_vision():
    from rtv.coaching.agent.registry import TOOLS_BY_NAME, GetVisualNotesInput

    spec = TOOLS_BY_NAME["get_visual_notes"]
    out = spec.invoke(object(), GetVisualNotesInput(session_id="s1"))
    assert out == {"session_id": "s1", "available": False, "notes": []}


# ---- ffmpeg ------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg not installed")
def test_ffmpeg_probe_and_frame(tmp_path):
    from rtv.vision.video import FfmpegFrames, VideoToolError, probe

    path = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
         "-t", "3", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True, timeout=60,
    )
    meta = probe(path)
    assert meta.width == 320 and meta.height == 180
    assert meta.fps == pytest.approx(30.0)
    assert meta.duration_s == pytest.approx(3.0, abs=0.1)
    jpeg = FfmpegFrames().frame(path, 1.5, width=160)
    assert jpeg[:2] == b"\xff\xd8"
    with pytest.raises(VideoToolError):
        FfmpegFrames().frame(path, 30.0, width=160)
    with pytest.raises(VideoToolError):
        probe(tmp_path / "missing.mp4")


# ---- API ---------------------------------------------------------------------


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
    from rtv.vision import service as vision_service
    from rtv.vision.video import ProbeResult

    monkeypatch.setattr(
        vision_service, "probe",
        lambda path, **kw: ProbeResult(duration_s=210.0, fps=60.0, width=1280, height=720,
                                       codec="h264"),
    )
    with TestClient(create_app()) as c:
        services = c.app.state.services
        sid = seed_synthetic_session(services.writer)
        services.vision.frames = FakeFrames()
        services.vision.provider_factory = lambda: _scripted({
            "trigger_visible": "yes",
            "observations": [
                {"kind": "line", "text": "Turned in early and clipped the inside kerb.",
                 "confidence": "medium"},
                {"kind": "other", "text": "Lost 2 tenths here.", "confidence": "high"},
            ],
        })
        yield c, sid, tmp_path
    get_settings.cache_clear()


def _wait(c, sid: str, aid: str) -> dict:
    for _ in range(200):
        body = c.get(f"/api/v1/sessions/{sid}/vision/analyses/{aid}").json()
        if body["status"] not in ("running",):
            return body
        time.sleep(0.02)
    raise AssertionError("analysis did not finish")


def test_api_end_to_end(client, monkeypatch):
    c, sid, tmp_path = client
    base = f"/api/v1/sessions/{sid}"
    clip = tmp_path / "onboard.mp4"
    clip.write_bytes(b"x")

    status = c.get("/api/v1/vision/status").json()
    assert status["mode"] == "off" and status["analysis_enabled"] is False
    assert status["budget"]["max_moments"] == 15

    assert c.post(base + "/videos", json={"path": str(tmp_path / "nope.mp4")}).status_code == 404
    assert c.post(base + "/videos", json={"path": str(tmp_path)}).status_code == 404
    r = c.post(base + "/videos", json={"path": str(clip), "label": "Onboard"})
    assert r.status_code == 201, r.text
    vid = r.json()["video_id"]
    assert r.json()["sync"]["synced"] is False

    # Not synced yet: no plan, no frame by session time.
    assert c.post(base + "/vision/plan", json={"video_id": vid}).status_code == 400
    assert c.get(base + f"/videos/{vid}/time", params={"video_time": 1}).status_code == 409

    # Sync by a line crossing: lap 2 starts 7.2 s into the video.
    r = c.put(base + f"/videos/{vid}/sync", json={"lap_starts": [{"lap": 2, "video_time": 7.2}]})
    assert r.status_code == 200, r.text
    video = r.json()
    assert video["sync"]["anchors"][0]["source"] == "lap_start:2"
    assert video["session_time_start"] == pytest.approx(90.0, abs=0.01)
    assert c.put(base + f"/videos/{vid}/sync", json={}).status_code == 400
    bad = {"lap_starts": [{"lap": 99, "video_time": 1}]}
    assert c.put(base + f"/videos/{vid}/sync", json=bad).status_code == 404

    t = c.get(base + f"/videos/{vid}/time", params={"session_time": 100.0}).json()
    assert t["video_time"] == pytest.approx(10.0, abs=0.01) and t["in_video"]
    r = c.get(base + f"/videos/{vid}/frame", params={"session_time": 100.0})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert c.get(base + f"/videos/{vid}/frame", params={"video_time": 999}).status_code == 416
    r = c.get(base + f"/videos/{vid}/file")
    assert r.status_code == 200 and r.content == b"x"

    plan = c.post(base + "/vision/plan", json={"video_id": vid, "sources": ["findings"]}).json()
    assert plan["count"] > 0 and plan["frames"] == plan["count"] * 4
    bad_source = {"video_id": vid, "sources": ["x"]}
    assert c.post(base + "/vision/plan", json=bad_source).status_code == 400

    # Paid analyses are refused while the mode is off, then without a key.
    body = {"video_id": vid, "sources": ["findings"], "max_moments": 2}
    assert c.post(base + "/vision/analyses", json=body).status_code == 409
    c.app.state.services.settings.vision_mode = "post"
    assert c.post(base + "/vision/analyses", json=body).status_code == 503
    monkeypatch.setenv("KIMI_CODE_API_KEY", "test-key")  # the provider is scripted

    r = c.post(base + "/vision/analyses", json=body)
    assert r.status_code == 202, r.text
    aid = r.json()["analysis_id"]
    done = _wait(c, sid, aid)
    assert done["status"] == "complete" and done["notes"] == 2
    assert done["observations_kept"] == 2
    item = done["items"][0]
    assert [o["status"] for o in item["observations"]] == ["uncheckable", "dropped"]
    assert c.get(base + f"/vision/frames/{aid}/{item['frames'][0]['name']}").content == JPEG
    assert c.get(base + f"/vision/frames/{aid}/..").status_code in (400, 404)

    runs = c.get(base + "/vision/analyses").json()
    assert runs["default_analysis"] == aid and runs["analyses"][0]["complete"]

    notes = c.get(base + "/vision/notes").json()
    assert notes["analysis_id"] == aid and notes["count"] == 2
    lap = notes["items"][0]["moment"]["lap"]
    corner = notes["items"][0]["moment"]["corner"]
    assert c.get(base + "/vision/notes", params={"corner": corner}).json()["count"] >= 1
    assert c.get(base + "/vision/notes", params={"lap": 99}).json()["count"] == 0

    coach = c.get(base + "/vision/coach-notes", params={"lap": lap}).json()
    assert coach["available"] and coach["notes"][0]["observations"] == [
        {"kind": "line", "text": "Turned in early and clipped the inside kerb.",
         "confidence": "medium", "status": "uncheckable"},
    ]
    findings = c.get("/api/v1/coaching/lap-findings", params={
        "session_id": sid, "main_lap": lap, "ref_lap": 6, "include_visual": True}).json()
    assert findings["visual_notes"]["notes"]
    plain = c.get("/api/v1/coaching/lap-findings",
                  params={"session_id": sid, "main_lap": lap, "ref_lap": 6}).json()
    assert "visual_notes" not in plain

    summary = c.get(base + "/summary").json()
    assert summary["vision"]["analysis_id"] == aid
    assert summary["vision"]["observations_kept"] == 2

    r = c.delete(base + f"/videos/{vid}")
    assert r.status_code == 200 and r.json()["file_deleted"] is False
    assert clip.is_file()


def test_api_upload_and_unknown_session(client):
    c, sid, _ = client
    r = c.post(f"/api/v1/sessions/{sid}/videos/upload", params={"filename": "lap.mp4"},
               content=b"video-bytes")
    assert r.status_code == 201, r.text
    video = r.json()
    assert video["uploaded"] is True and Path(video["path"]).is_file()
    r = c.delete(f"/api/v1/sessions/{sid}/videos/{video['video_id']}")
    assert r.json()["file_deleted"] is True and not Path(video["path"]).exists()

    bad = c.post(f"/api/v1/sessions/{sid}/videos/upload", params={"filename": "x.exe"},
                 content=b"x")
    assert bad.status_code == 400
    assert c.post("/api/v1/sessions/ghost/videos/upload", params={"filename": "a.mp4"},
                  content=b"x").status_code == 404
    assert c.get("/api/v1/sessions/ghost/vision/coach-notes").status_code == 404
    assert c.get(f"/api/v1/sessions/{sid}/vision/notes").json() == {
        "session_id": sid, "analysis_id": None, "count": 0, "items": []}
