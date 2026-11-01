"""Push-to-talk questions (C2), offline: a mocked microphone and recogniser.

No audio device, no speech model and no network: the recorder and the recogniser
are fakes, the worker thread is replaced by an inline runner. The real
faster-whisper model would need a download, so it is not exercised here; the
user checks it on their PC (docs/DESKTOP.md, "Push-to-talk questions").
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")  # also skips when Qt's system libraries are missing
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
np = pytest.importorskip("numpy")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from rtv.desktop import voice  # noqa: E402
from rtv.desktop.voice import (  # noqa: E402
    SAMPLE_RATE,
    KeySpec,
    KeyWatcher,
    PushToTalk,
    inline_runner,
    parse_key,
    to_mono_16k,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent

    for widget in app.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


class FakeRecorder:
    """Records whatever ``speech`` holds; drops it while muted, like the real one."""

    def __init__(self, seconds: float = 1.5):
        self.available = True
        self.reason = ""
        self.muted = False
        self.mute_calls: list[bool] = []
        self.started = 0
        self.seconds = seconds
        self.heard_while_muted = 0.0

    def start(self) -> None:
        self.started += 1

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        self.mute_calls.append(muted)

    def stop(self):
        return np.zeros(int(self.seconds * SAMPLE_RATE), dtype=np.float32)


class FakeRecogniser:
    def __init__(self, text: str = "How many laps of fuel?", error: Exception | None = None):
        self.text = text
        self.error = error
        self.loaded = False
        self.calls = 0

    def ready(self) -> bool:
        return self.loaded

    def transcribe(self, audio) -> str:
        self.calls += 1
        self.loaded = True
        if self.error:
            raise self.error
        return self.text


def _ptt(recorder=None, recogniser=None, **kw) -> tuple[PushToTalk, list, list]:
    ptt = PushToTalk(
        recorder or FakeRecorder(), recogniser or FakeRecogniser(), runner=inline_runner, **kw
    )
    states: list[tuple[str, str]] = []
    asked: list[str] = []
    ptt.state_changed.connect(lambda s, d: states.append((s, d)))
    ptt.transcribed.connect(asked.append)
    return ptt, states, asked


# ---- the key ----------------------------------------------------------------


@pytest.mark.parametrize(
    "spec, vk, qt",
    [
        ("ScrollLock", 0x91, "ScrollLock"),
        ("scroll lock", 0x91, "ScrollLock"),
        ("Pause", 0x13, "Pause"),
        ("F13", 0x7C, "F13"),
        ("f1", 0x70, "F1"),
        ("F24", 0x87, "F24"),
        ("a", 0x41, "A"),
        ("7", 0x37, "7"),
        ("Numpad0", 0x60, None),
        ("RCtrl", 0xA3, None),
        ("Space", 0x20, "Space"),
    ],
)
def test_keys_parse_to_windows_and_qt_codes(spec, vk, qt):
    key = parse_key(spec)
    assert key.vk == vk and key.qt_name == qt and not key.is_joystick


@pytest.mark.parametrize(
    "spec, device, button",
    [("joy1:5", 0, 5), ("joy:5", 0, 5), ("joy5", 0, 5), ("JOY2:12", 1, 12), ("joy16:32", 15, 32)],
)
def test_wheel_buttons_parse_to_a_controller_and_button(spec, device, button):
    key = parse_key(spec)
    assert key.is_joystick and key.joystick == device and key.button == button
    assert key.label == f"controller {device + 1} button {button}"


@pytest.mark.parametrize("spec", ["", "F25", "joy0:1", "joy1:33", "Hyper", "joy1:x", "Numpad10"])
def test_an_unknown_key_says_what_is_accepted(spec):
    with pytest.raises(ValueError, match="joy<device>:<button>"):
        parse_key(spec)


def test_the_key_watcher_emits_one_press_and_one_release_per_hold(qapp):
    held = {"down": False}
    watcher = KeyWatcher(KeySpec(label="F13", vk=0x7C), probe=lambda: held["down"])
    events: list[str] = []
    watcher.pressed.connect(lambda: events.append("press"))
    watcher.released.connect(lambda: events.append("release"))
    for down in (False, True, True, True, False, False, True, False):
        held["down"] = down
        watcher.poll()
    assert events == ["press", "release", "press", "release"]


def test_a_controller_that_fails_reads_as_released(qapp):
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("unplugged")
        return True

    watcher = KeyWatcher(KeySpec(label="joy", joystick=0, button=1), probe=probe)
    events: list[str] = []
    watcher.pressed.connect(lambda: events.append("press"))
    watcher.released.connect(lambda: events.append("release"))
    watcher.poll()
    watcher.poll()
    assert events == ["press", "release"]


def test_off_windows_there_is_no_global_probe(monkeypatch):
    monkeypatch.setattr(voice.sys, "platform", "linux")
    assert voice.global_probe(parse_key("F13")) is None
    assert voice.joystick_buttons(0) is None


# ---- audio ------------------------------------------------------------------


def test_stereo_48k_int16_becomes_mono_16k_float():
    t = np.arange(48_000) / 48_000
    tone = (np.sin(2 * np.pi * 440 * t) * 16_000).astype(np.int16)
    stereo = np.stack([tone, tone], axis=1).reshape(-1)
    out = to_mono_16k(stereo.tobytes(), 48_000, 2, "int16")
    assert out.dtype == np.float32 and len(out) == SAMPLE_RATE
    assert abs(float(np.max(np.abs(out))) - 16_000 / 32768) < 0.01


@pytest.mark.parametrize(
    "fmt, dtype", [("uint8", np.uint8), ("int32", np.int32), ("float", np.float32)]
)
def test_every_qt_sample_format_converts(fmt, dtype):
    data = np.zeros(1600, dtype=dtype)
    if fmt == "uint8":
        data[:] = 128
    stray = b"" if fmt == "uint8" else b"\x01"  # a part sample at the end is ignored
    out = to_mono_16k(data.tobytes() + stray, 16_000, 1, fmt)
    assert len(out) == 1600 and float(np.max(np.abs(out))) == 0.0


# ---- the controller ---------------------------------------------------------


def test_hold_and_release_sends_the_words_once(qapp):
    ptt, states, asked = _ptt()
    ptt.press()
    assert states[-1][0] == "listening"
    ptt.release()
    assert asked == ["How many laps of fuel?"]
    kinds = [s for s, _ in states]
    assert kinds == ["listening", "loading", "heard"]  # the first use loads the model
    assert states[-1][1] == "How many laps of fuel?"  # shown before the answer arrives
    ptt.press()
    ptt.release()
    assert [s for s, _ in states][-2:] == ["transcribing", "heard"]
    assert len(asked) == 2


def test_the_mic_is_muted_while_the_radio_speaks(qapp):
    recorder = FakeRecorder()
    ptt, states, asked = _ptt(recorder)
    ptt.set_radio_speaking(True)
    assert recorder.muted is True
    ptt.press()
    assert states[-1][0] == "muted" and "mic muted" in states[-1][1]
    ptt.set_radio_speaking(False)  # the call ends mid-question: listening again
    assert recorder.muted is False and states[-1][0] == "listening"
    ptt.set_radio_speaking(True)
    assert recorder.muted is True and states[-1][0] == "muted"
    ptt.release()
    assert asked == ["How many laps of fuel?"]


def test_a_tap_is_not_a_question(qapp):
    recogniser = FakeRecogniser()
    ptt, states, asked = _ptt(FakeRecorder(seconds=0.1), recogniser)
    ptt.press()
    ptt.release()
    assert asked == [] and recogniser.calls == 0 and states[-1][0] == "nothing"


def test_silence_is_not_sent(qapp):
    ptt, states, asked = _ptt(recogniser=FakeRecogniser(text="   "))
    ptt.press()
    ptt.release()
    assert asked == [] and states[-1] == (
        "nothing",
        "Didn't catch that. Hold the key and ask again.",
    )


def test_a_recogniser_failure_is_shown_not_raised(qapp):
    ptt, states, asked = _ptt(recogniser=FakeRecogniser(error=RuntimeError("model corrupt")))
    ptt.press()
    ptt.release()
    assert asked == [] and states[-1][0] == "error" and "model corrupt" in states[-1][1]
    assert ptt.busy is False  # the next press works


def test_release_without_press_does_nothing(qapp):
    ptt, states, asked = _ptt()
    ptt.release()
    assert states == [] and asked == []


def test_without_the_extra_the_button_says_so(qapp):
    ptt = PushToTalk(None, None, key_label="ScrollLock", runner=inline_runner)
    assert ptt.available is False and ptt.state == "off"
    assert "[voice]" in ptt.reason and "faster-whisper" in ptt.reason
    ptt.press()  # nothing to do, nothing raised
    ptt.release()
    assert ptt.state == "off"


def test_without_a_microphone_the_button_says_so(qapp):
    recorder = FakeRecorder()
    recorder.available, recorder.reason = False, "No microphone found."
    ptt = PushToTalk(recorder, FakeRecogniser(), runner=inline_runner)
    assert ptt.available is False and ptt.reason == "No microphone found."


def test_a_downloaded_model_is_warmed_at_start_but_never_downloaded(qapp, tmp_path):
    rec = voice.WhisperRecogniser("base.en", tmp_path / "whisper")
    loads: list[int] = []
    rec.load = lambda: loads.append(1)
    ptt = PushToTalk(FakeRecorder(), rec, runner=inline_runner)
    ptt.warm()
    assert loads == []  # not on disk: the first press downloads it
    (tmp_path / "whisper" / "models--Systran--faster-whisper-base.en").mkdir(parents=True)
    ptt.warm()
    assert loads == [1]


def test_the_thread_runner_reports_back_on_the_gui_thread(qapp):
    import threading
    import time

    run = voice.thread_runner()
    got: list = []
    main = threading.get_ident()
    run(lambda: threading.get_ident(), lambda r, e: got.append((r, e, threading.get_ident())))
    deadline = time.monotonic() + 5
    while not got and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert got and got[0][1] is None
    worker, _err, delivered_on = got[0]
    assert worker != main and delivered_on == main


# ---- settings ---------------------------------------------------------------


class _Settings:
    def __init__(self, tmp_path, **kw):
        self.voice_enabled = kw.get("enabled", True)
        self.voice_key = kw.get("key", "ScrollLock")
        self.voice_model = "base.en"
        self.data_dir = tmp_path


def test_voice_is_off_unless_enabled(tmp_path):
    assert voice.build_push_to_talk(_Settings(tmp_path, enabled=False)) == (None, None)


def test_a_bad_key_turns_voice_off_with_the_reason(qapp, tmp_path):
    ptt, keys = voice.build_push_to_talk(_Settings(tmp_path, key="Hyper"))
    assert keys is None and ptt.state == "off" and "Hyper" in ptt.reason


def test_a_missing_extra_turns_voice_off_with_the_reason(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "whisper_installed", lambda: False)
    ptt, keys = voice.build_push_to_talk(_Settings(tmp_path))
    assert keys is None and ptt.reason == voice.UNAVAILABLE_EXTRA


def test_enabled_voice_wires_the_key_to_the_controller(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "whisper_installed", lambda: True)
    monkeypatch.setattr(voice, "MicRecorder", lambda parent=None: FakeRecorder())
    ptt, keys = voice.build_push_to_talk(_Settings(tmp_path, key="F13"))
    assert ptt.available and ptt.key_label == "F13"
    assert isinstance(ptt.recogniser, voice.WhisperRecogniser)
    assert ptt.recogniser.download_root == tmp_path / "models" / "whisper"
    ptt.runner = inline_runner
    ptt.recogniser = FakeRecogniser("gap behind?")
    asked: list[str] = []
    ptt.transcribed.connect(asked.append)
    keys.probe = lambda: True
    keys.poll()
    keys.probe = lambda: False
    keys.poll()
    assert asked == ["gap behind?"]


def test_the_settings_have_safe_defaults(monkeypatch):
    for var in ("RTV_VOICE_ENABLED", "RTV_VOICE_KEY", "RTV_VOICE_MODEL"):
        monkeypatch.delenv(var, raising=False)
    from rtv.config import Settings

    s = Settings(_env_file=None)
    assert s.voice_enabled is False and s.voice_model == "base.en"
    parse_key(s.voice_key)  # the default key parses


# ---- the hub and the radio feed ---------------------------------------------


def test_the_hub_sends_voice_questions_and_mutes_the_mic_on_air(qapp):
    from rtv.desktop.client.api import Api
    from rtv.desktop.views.hub import Hub

    hub = Hub(Api("http://127.0.0.1:9"), speak=False)
    posts: list[tuple[str, dict]] = []
    hub.api.post = lambda path, body=None, **kw: posts.append((path, body))
    recorder = FakeRecorder()
    ptt = PushToTalk(recorder, FakeRecogniser("what's the gap ahead"), runner=inline_runner)
    hub.attach_voice(ptt)
    ptt.press()
    ptt.release()
    assert posts == [
        ("/pitwall/driver-message", {"text": "what's the gap ahead", "source": "voice"})
    ]
    hub.on_air_changed.emit({"speaking": {"agent": "spotter", "spoken_text": "Car left."}})
    assert recorder.muted is True
    hub.on_air_changed.emit({"speaking": None})
    assert recorder.muted is False


def test_the_radio_feed_shows_the_words_heard_and_why_voice_is_off(qapp):
    from rtv.desktop.widgets.radio import RadioFeed

    feed = RadioFeed()
    assert feed.voice_row.isHidden()  # voice not enabled: no row at all
    feed.set_voice(False, "ScrollLock", voice.UNAVAILABLE_EXTRA)
    assert not feed.voice_row.isHidden() and not feed.talk.isEnabled()
    assert feed.voice_status.text() == voice.UNAVAILABLE_EXTRA
    feed.set_voice(True, "ScrollLock")
    assert feed.talk.isEnabled() and "ScrollLock" in feed.talk.text()
    feed.set_voice_state("heard", "How many laps of fuel?")
    assert feed.voice_status.text().startswith("Heard: “How many laps of fuel?”")
    feed.add(
        {
            "message_id": "drv-1",
            "agent": "driver",
            "spoken_text": "How many laps of fuel?",
            "speak": False,
            "session_time": 100.0,
            "data": {"source": "voice"},
        }
    )
    feed.grab()
    shown = [lab.text() for lab in feed.findChildren(QLabel)]
    assert "VOICE" in shown and "NOT SPOKEN" in shown


def test_the_live_view_binds_the_talk_button(qapp):
    from rtv.desktop.client.api import Api
    from rtv.desktop.views.hub import Hub
    from rtv.desktop.views.live_view import LiveView

    hub = Hub(Api("http://127.0.0.1:9"), speak=False)
    hub.api.post = lambda *a, **kw: None
    ptt = PushToTalk(FakeRecorder(), FakeRecogniser(), key_label="F13", runner=inline_runner)
    hub.attach_voice(ptt)
    view = LiveView(hub, reduced_motion=True)
    radio = view.radio
    assert not radio.voice_row.isHidden() and "F13" in radio.talk.text()
    radio.talk_pressed.emit()
    assert "Listening" in radio.voice_status.text()
    radio.talk_released.emit()
    assert "Heard" in radio.voice_status.text()
    hub.driver_unheard.emit("box this lap?")  # no pitwall: shown as not heard
    assert any(m.get("spoken_text") == "box this lap?" for m in radio.messages)
    view.close()


# ---- the server side: the transcript is journalled as voice -----------------


def test_a_voice_question_is_journalled_with_source_voice(tmp_path):
    from evals.pitwall_cases import _end_of_scenario_state

    from rtv.pitwall.orchestrator import PitwallOrchestrator
    from rtv.pitwall.provider import ScriptedProvider
    from rtv.pitwall.radio import RadioFeed
    from rtv.racestate import RaceStateEngine
    from rtv.store.journal import SessionRecorder, iter_records

    engine = RaceStateEngine(source="replay")
    engine._state = _end_of_scenario_state().model_copy(deep=True)  # noqa: SLF001
    feed = RadioFeed()
    orch = PitwallOrchestrator(
        engine, ScriptedProvider(lambda **_: None), [], feed=feed, clock=None
    )
    recorder = SessionRecorder(tmp_path)
    recorder.begin("voice-test", "replay")
    engine.bus.subscribe_callback(recorder.on_race_event)
    feed.subscribe_callback(recorder.on_radio)
    transcript = orch.ask("how many laps of fuel", source="voice")
    assert transcript.data["source"] == "voice"
    info = recorder.end()
    records = list(iter_records(info["path"]))
    radio = [r for r in records if r.get("kind") == "radio" and r["data"]["agent"] == "driver"]
    events = [
        r
        for r in records
        if r.get("kind") == "race_event" and r["data"].get("event_type") == "driver_question"
    ]
    assert radio and radio[0]["data"]["data"]["source"] == "voice"
    assert events and events[0]["data"]["payload"]["source"] == "voice"
