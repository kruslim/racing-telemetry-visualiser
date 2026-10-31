"""The desktop live view's Onboard panel: the pure view, and the widget against a fake API."""

from __future__ import annotations

import os

import pytest

from rtv.desktop.model.live_onboard import EMPTY_TEXT, frame_request, onboard_view

JPEG = b"\xff\xd8\xff\xe0not-a-real-jpeg\xff\xd9"

CAPTURE = {
    "mode": "live",
    "session_id": "s1",
    "source": {"kind": "capture", "video_id": "v1"},
    "capture": {"running": True, "session_id": "s1", "frames": 42, "error": None},
    "vlm": {
        "enabled": True, "calls": 2, "max_calls": 10, "pending": 1,
        "skipped": {"merged": 1, "over_cap": 2},
        "recent": [
            {"note_id": "a", "lap": 3, "trigger": "offtrack", "status": "ok",
             "kept": [{"kind": "kerb_use", "text": "Rode the exit kerb.", "confidence": "high"}]},
            {"note_id": "b", "lap": 4, "trigger": "lockup", "status": "trigger_not_seen",
             "kept": []},
        ],
    },
}  # fmt: skip

VIDEO = {
    "video_id": "v9", "session_id": "s1", "label": "spa onboard",
    "sync": {"synced": True, "rate": 1.0, "offset": 90.0},
    "session_time_start": 90.0, "session_time_end": 300.0,
}  # fmt: skip


def test_empty_view_says_how_to_get_video_and_the_vlm():
    view = onboard_view({"mode": "off", "capture": {"running": False}, "source": None,
                         "vlm": {"enabled": False}})
    assert view["mode"] == "empty" and view["source_line"] == EMPTY_TEXT
    assert "RTV_VISION_MODE=live" in view["vlm_line"]
    assert frame_request(view, 100.0) is None


def test_capture_view_counts_calls_and_lists_notes_newest_first():
    view = onboard_view(CAPTURE)
    assert view["mode"] == "capture" and view["capture_running"]
    assert view["source_line"] == "capturing the sim window · 42 frames stamped"
    assert view["vlm_line"] == "Live VLM on · 2 of 10 calls · 1 looking · 1 merged · 2 over the cap"
    assert [n["id"] for n in view["notes"]] == ["b", "a"]
    assert view["notes"][0]["head"] == "lap 4 · lock-up"
    assert "check the video sync" in view["notes"][0]["empty"]
    assert view["notes"][1]["lines"] == [
        {"kind": "kerb", "text": "Rode the exit kerb.", "confidence": "high"}
    ]
    assert frame_request(view, None) == ("/vision/live/frame", None)


def test_a_replayed_recording_is_framed_at_the_session_time_inside_its_span():
    view = onboard_view({"capture": {"running": False},
                         "source": {"kind": "video", "video_id": "v9", "video": VIDEO},
                         "vlm": {}})
    assert view["mode"] == "video" and view["source_line"] == "recording: spa onboard"
    assert frame_request(view, 123.456, width=320) == (
        "/sessions/s1/videos/v9/frame", {"session_time": 123.46, "width": 320}
    )
    assert frame_request(view, 50.0) is None
    assert frame_request(view, 400.0) is None
    assert frame_request(view, None) is None


def test_a_capture_error_is_the_source_line():
    view = onboard_view({"capture": {"running": False, "error": "ffmpeg exited with 1: no window"}})
    assert view["source_line"] == "capture failed: ffmpeg exited with 1: no window"


# ---- the widget (offscreen Qt) -----------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from rtv.desktop.theme.qss import stylesheet

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(stylesheet())
    return app


class FakeApi:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple] = []

    def _answer(self, method, path, body, params, ok, err):
        self.calls.append((method, path, body, params))
        answer = self.routes.get((method, path))
        if isinstance(answer, tuple):
            err and err(*answer)
        elif ok is not None:
            ok(answer)

    def get(self, path, params=None, ok=None, err=None, key=None):
        self._answer("GET", path, None, params, ok, err)

    def get_bytes(self, path, params=None, ok=None, err=None, key=None):
        self._answer("BYTES", path, None, params, ok, err)

    def post(self, path, body=None, params=None, ok=None, err=None, key=None):
        self._answer("POST", path, body, params, ok, err)


def _texts(widget) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [w.text() for w in widget.findChildren(QLabel)]


def test_panel_polls_shows_notes_fetches_frames_and_toggles_capture(qapp):
    from rtv.desktop.widgets.live_onboard import LiveOnboardPanel

    stopped = {**CAPTURE, "capture": {"running": False}, "source": None}
    api = FakeApi({
        ("GET", "/vision/live"): CAPTURE,
        ("BYTES", "/vision/live/frame"): JPEG,
        ("POST", "/vision/live/capture/stop"): {"running": False, "video": {"video_id": "v1"}},
    })  # fmt: skip
    notices: list[str] = []
    panel = LiveOnboardPanel()
    panel.bind(api, lambda: 120.0, on_notice=notices.append, start=False)
    panel.poll()
    texts = _texts(panel)
    assert "KERB · Rode the exit kerb.  (high)" in texts
    assert any("check the video sync" in x for x in texts)
    assert panel.toggle.text() == "■ Stop video"
    assert panel.meta.text().startswith("capturing the sim window")

    panel.fetch_frame()
    assert api.calls[-1] == ("BYTES", "/vision/live/frame", None, None)

    api.routes[("GET", "/vision/live")] = stopped
    panel.toggle.click()
    assert ("POST", "/vision/live/capture/stop", {}, None) in api.calls
    assert notices == ["Capture saved and synced to the session; the review can show it."]
    assert panel.toggle.text() == "● Capture video"
    assert panel.frame.text() == EMPTY_TEXT


def test_panel_follows_the_replay_clock_on_a_recording(qapp):
    from rtv.desktop.widgets.live_onboard import LiveOnboardPanel

    status = {"capture": {"running": False},
              "source": {"kind": "video", "video_id": "v9", "video": VIDEO}, "vlm": {}}
    api = FakeApi({("GET", "/vision/live"): status,
                   ("BYTES", "/sessions/s1/videos/v9/frame"): JPEG})
    now = [150.0]
    panel = LiveOnboardPanel()
    panel.bind(api, lambda: now[0], start=False)
    panel.poll()
    panel.fetch_frame()
    assert api.calls[-1][1:] == ("/sessions/s1/videos/v9/frame", None,
                                 {"session_time": 150.0, "width": 432})
    now[0] = 10.0
    n = len(api.calls)
    panel.fetch_frame()
    assert len(api.calls) == n  # outside the recording: nothing fetched
    assert panel.frame.text() == "The recording does not cover this moment."


def test_a_failed_capture_start_is_a_notice(qapp):
    from rtv.desktop.widgets.live_onboard import LiveOnboardPanel

    api = FakeApi({("GET", "/vision/live"): {"capture": {"running": False}},
                   ("POST", "/vision/live/capture/start"): (503, "'ffmpeg' was not found.")})
    notices: list[str] = []
    panel = LiveOnboardPanel()
    panel.bind(api, lambda: None, on_notice=notices.append, start=False)
    panel.poll()
    panel.toggle.click()
    assert notices == ["Capture start failed: 'ffmpeg' was not found."]
