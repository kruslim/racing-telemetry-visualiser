"""RTV Desktop's onboard video panel: the pure view model, the widget, and the review flow.

The review flow runs against a fake API that answers in the shapes ``docs/VISION.md``
documents, so nothing here needs ffmpeg, a video, a key or the network. The spend path
is checked to stop at the confirm dialog unless it is accepted.
"""

from __future__ import annotations

import os

import pytest

from rtv.desktop.model import vision

SYNCED = {
    "video_id": "9e4e6c3132",
    "label": "spa onboard",
    "duration_s": 210.0,
    "fps": 60.0,
    "width": 1920,
    "height": 1080,
    "registered_wall_ms": 2,
    "sync": {
        "anchors": [
            {"video_time": 7.2, "session_time": 97.2, "source": "lap_start:2"},
            {"video_time": 106.5, "session_time": 196.47, "source": "lap_start:3"},
        ],
        "rate": 1.0,
        "offset": 90.0,
        "synced": True,
    },
    "session_time_start": 90.0,
    "session_time_end": 300.0,
}
UNSYNCED = {
    "video_id": "aaaaaaaaaa",
    "label": "raw",
    "registered_wall_ms": 3,
    "sync": {"anchors": [], "synced": False},
}

KEPT = {
    "kind": "kerb_use",
    "text": "Rode the inside kerb hard at the apex and the car skipped wide.",
    "confidence": "medium",
    "status": "uncheckable",
    "reason": None,
}
DROPPED = {
    "kind": "traffic",
    "text": "A car was about 2 lengths behind.",
    "confidence": "low",
    "status": "dropped",
    "reason": "Number-free rule: contains a number.",
}


def note(nid, t, *, lap=2, corner="T3", kinds=("finding",), kept=(KEPT,), status="ok"):
    return {
        "note_id": nid,
        "analysis_id": "A1",
        "moment": {
            "session_time": t,
            "t_start": t - 2,
            "t_end": t + 2,
            "lap": lap,
            "corner": corner,
            "triggers": [{"kind": k, "corner": corner} for k in kinds],
        },
        "frames": [{"name": f"{nid}-{i}.jpg"} for i in range(3)],
        "status": status,
        "observations": [*kept, DROPPED],
        "kept": list(kept),
    }


NOTES = [
    note("n2", 150.0, lap=3, corner=None, kinds=("incident",), kept=(), status="trigger_not_seen"),
    note("n1", 118.4, kinds=("finding", "offtrack")),
]


# ---- pure model ------------------------------------------------------------
def test_pick_video_prefers_the_choice_then_the_newest_synced():
    assert vision.pick_video([], None) is None
    assert vision.pick_video([SYNCED, UNSYNCED])["video_id"] == SYNCED["video_id"]
    assert vision.pick_video([SYNCED, UNSYNCED], "aaaaaaaaaa")["video_id"] == "aaaaaaaaaa"
    assert vision.pick_video([UNSYNCED])["video_id"] == "aaaaaaaaaa"


def test_to_video_uses_the_servers_rate_and_offset_and_stays_inside_the_clip():
    assert vision.to_video(SYNCED, 97.2) == pytest.approx(7.2)
    assert vision.to_video(SYNCED, 80.0) is None  # before the clip
    assert vision.to_video(SYNCED, 400.0) is None  # past its end
    assert vision.to_video(UNSYNCED, 100.0) is None
    assert vision.to_video(SYNCED, None) is None


def test_header_lines():
    assert vision.sync_text(SYNCED) == "synced from lap 2, 3 start · covers 1:30 → 5:00"
    assert vision.sync_text(UNSYNCED) == "not synced yet"
    assert vision.video_title(SYNCED) == "spa onboard · 1920×1080 · 60 fps · 3:30"


def test_note_rows_keep_only_kept_lines_in_race_order_with_hints():
    rows = vision.note_rows(NOTES)
    assert [r["note_id"] for r in rows] == ["n1", "n2"]
    first, second = rows
    assert first["head"] == "L2 · T3 · 1:58 · off track + time lost"
    assert first["severity"] == "advisory"
    assert first["lines"] == [
        {"tag": "kerb", "text": KEPT["text"], "confidence": "medium", "checked": False}
    ]
    assert first["dropped"] == 1 and first["frame"] == "n1-1.jpg"
    assert second["severity"] == "critical" and second["lines"] == []
    assert "check the video sync" in second["hint"]
    # every kept line is the server's text as is: no numbers added on the client side
    assert all(line["text"] == KEPT["text"] for r in rows for line in r["lines"])


def test_corner_filter_and_timeline_and_note_at():
    assert [r["note_id"] for r in vision.note_rows(NOTES, corner="T3")] == ["n1"]
    assert vision.note_rows(NOTES, corner="T9") == []
    items = vision.timeline_items(NOTES)
    assert [(i["note_id"], i["t0"], i["t1"], i["quiet"]) for i in items] == [
        ("n1", 116.4, 120.4, False),
        ("n2", 148.0, 152.0, True),  # nothing kept: drawn dashed
    ]
    assert vision.note_at(NOTES, 119.0)["note_id"] == "n1"
    assert vision.note_at(NOTES, 130.0) is None


def test_plan_analysis_and_blocker_text():
    plan = {"count": 5, "frames": 20, "model": "kimi-k3", "skipped_over_budget": 2}
    assert vision.plan_text(plan) == (
        "5 moments · 20 frames · 5 kimi-k3 calls, 2 more over the frame budget"
    )
    assert vision.plan_text({"count": 0, "notes": ["No race-journal run yet."]}) == (
        "No moments to analyse: No race-journal run yet."
    )
    running = {"status": "running", "progress": {"done": 2, "total": 5}}
    assert vision.analysis_text(running) == "Analysing… 2 / 5 moments"
    done = {"status": "complete", "notes": 5, "observations_kept": 7}
    assert vision.analysis_text(done) == "Analysed · 5 moments · 7 observations kept"
    ready = {"analysis_enabled": True, "key_present": True, "tools": {"ffmpeg": True}}
    assert vision.vision_blocker(ready) is None
    assert "RTV_VISION_MODE" in vision.vision_blocker({**ready, "analysis_enabled": False})
    assert "KIMI_CODE_API_KEY" in vision.vision_blocker({**ready, "key_present": False})
    assert "ffmpeg" in vision.vision_blocker({**ready, "tools": {"ffmpeg": False}})


# ---- widgets ---------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from rtv.desktop.theme.qss import stylesheet

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(stylesheet())
    return app


def _texts(widget) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [lab.text() for lab in widget.findChildren(QLabel)]


def _assert_plain(widget) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    for lab in widget.findChildren(QLabel):
        assert lab.textFormat() == Qt.TextFormat.PlainText, lab.text()


def test_onboard_panel_states(qapp):
    from rtv.desktop.widgets.vision import EMPTY_TEXT, OnboardPanel

    panel = OnboardPanel()
    assert not panel.empty.isHidden() and panel.main.isHidden()
    assert EMPTY_TEXT in _texts(panel)

    laps = [{"lap": 2, "start_session_time": 97.2}, {"lap": 3, "start_session_time": 196.47}]
    panel.set_videos([UNSYNCED], UNSYNCED, laps)
    assert not panel.sync_box.isHidden() and not panel.analyse.isEnabled()
    assert [panel.sync_lap.itemData(i) for i in range(panel.sync_lap.count())] == [2, 3]
    got = []
    panel.sync_requested.connect(lambda lap, vt: got.append((lap, vt)))
    panel.sync_lap.setCurrentIndex(1)
    panel.sync_time.setValue(106.5)
    panel.sync_go.click()
    assert got == [(3, 106.5)]

    panel.set_videos([SYNCED, UNSYNCED], SYNCED, laps)
    assert panel.sync_box.isHidden() and panel.analyse.isEnabled()
    assert not panel.video_pick.isHidden()  # two videos: a picker
    panel.set_blocker("Vision analyses are off. Set RTV_VISION_MODE=post to allow them.")
    assert not panel.analyse.isEnabled() and panel.plan.isEnabled()  # the plan is free

    rows = vision.note_rows(NOTES)
    panel.set_notes(rows, None, len(NOTES))
    shown = " ".join(_texts(panel))
    assert KEPT["text"] in shown and DROPPED["text"] not in shown
    assert "check the video sync" in shown
    picked = []
    panel.note_picked.connect(picked.append)
    from rtv.desktop.widgets.vision import NoteCard

    panel.findChildren(NoteCard)[0].clicked.emit(rows[0])
    assert picked[0]["note_id"] == "n1"

    panel.set_notes(vision.note_rows(NOTES, corner="T9"), "T9", len(NOTES))
    assert "No visual notes at T9." in _texts(panel) and not panel.filter_row.isHidden()
    panel.set_frame(b"not a jpeg", "cursor")
    assert panel.frame.text() == "cursor"
    _assert_plain(panel)


def test_spend_dialog_defaults_to_cancel(qapp):
    from PySide6.QtWidgets import QPushButton

    from rtv.desktop.widgets.vision import VisionSpendDialog

    dlg = VisionSpendDialog("5 moments · 20 frames · 5 kimi-k3 calls")
    default = [b for b in dlg.findChildren(QPushButton) if b.isDefault()]
    assert [b.text() for b in default] == ["Cancel"]
    assert "5 moments · 20 frames · 5 kimi-k3 calls" in _texts(dlg)
    _assert_plain(dlg)


# ---- the review flow against a fake API --------------------------------------
class FakeApi:
    """Answers synchronously from a table of ``(method, path) -> body``; logs every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple] = []

    def _answer(self, method, path, body, params, ok, err):
        self.calls.append((method, path, body, params))
        if (method, path) in self.routes:
            reply = self.routes[(method, path)]
            reply = reply(body, params) if callable(reply) else reply
            if ok:
                ok(reply)
        elif err:
            err(404, "not found")

    def get(self, path, params=None, ok=None, err=None, key=None):
        self._answer("GET", path, None, params, ok, err)

    def get_bytes(self, path, params=None, ok=None, err=None, key=None):
        self._answer("BYTES", path, None, params, ok, err)

    def post(self, path, body=None, params=None, ok=None, err=None, key=None):
        self._answer("POST", path, body, params, ok, err)

    def put(self, path, body=None, params=None, ok=None, err=None, key=None):
        self._answer("PUT", path, body, params, ok, err)

    def upload(self, path, file_path, params=None, ok=None, err=None, progress=None):
        self._answer("UPLOAD", path, file_path, params, ok, err)


def _hub(api):
    from PySide6.QtCore import QObject, Signal

    class FakeHub(QObject):
        sessions_changed = Signal()

        def __init__(self):
            super().__init__()
            self.api = api
            self.sessions = [{"session_id": "S"}]
            self.status = {}
            self.state = {}

        def load_sessions(self):
            pass

    return FakeHub()


S = "/sessions/S"
LAPS = [
    {"lap": 1, "start_session_time": 0.0, "lap_time": 97.2, "is_valid": True},
    {"lap": 2, "start_session_time": 97.2, "lap_time": 99.3, "is_valid": True},
    {"lap": 3, "start_session_time": 196.47, "lap_time": 99.0, "is_valid": True},
]


def _routes(videos, extra=None):
    status = {"analysis_enabled": True, "key_present": True, "tools": {"ffmpeg": True}}
    routes = {
        ("GET", f"{S}/laps"): {"laps": LAPS},
        ("GET", f"{S}/pitwall-runs"): {"runs": [], "default_run": None},
        ("GET", "/vision/status"): status,
        ("GET", f"{S}/videos"): {"videos": videos},
        ("GET", f"{S}/vision/analyses"): {
            "default_analysis": "A1",
            "analyses": [{"analysis_id": "A1", "status": "complete", "notes": 2}],
        },
        ("GET", f"{S}/vision/notes"): {"items": NOTES},
        ("BYTES", f"{S}/videos/{SYNCED['video_id']}/frame"): b"\xff\xd8 fake",
        ("GET", "/coaching/agents"): {"agents": []},
    }
    routes.update(extra or {})
    return routes


def test_review_loads_video_notes_and_the_camera_lane(qapp):
    from rtv.desktop.views.review_view import ReviewView

    api = FakeApi(_routes([SYNCED]))
    view = ReviewView(_hub(api), embedded=True)
    assert view.video["video_id"] == SYNCED["video_id"]
    assert [n["note_id"] for n in view.vision_notes] == ["n2", "n1"]
    assert ("GET", f"{S}/vision/notes", None, {"analysis": "A1"}) in api.calls
    assert view.onboard.main.isVisible() or not view.onboard.main.isHidden()

    # a note click moves the cursor and asks for the frame at the matching video time
    view._note_picked(vision.note_rows(view.vision_notes)[0])
    frame = [c for c in api.calls if c[0] == "BYTES"][-1]
    assert frame[3]["video_time"] == pytest.approx(118.4 - 90.0)

    # a corner picked in the findings narrows the notes
    view._corner({"label": "T3", "distance": 760.0})
    assert view._vision_corner == "T3"
    view.onboard.filter_clear.click()
    assert view._vision_corner is None

    # the camera lane joins the replay once a run gives the timeline its bounds
    view.bounds = (0.0, 300.0)
    lanes = []
    view.timeline.set_lanes = lanes.append
    view._render_replay()
    titles = [title for title, _c, _i in lanes[-1]]
    assert "camera" in titles
    _assert_plain(view.onboard)


def test_sync_puts_lap_starts(qapp):
    from rtv.desktop.views.review_view import ReviewView

    synced = {**UNSYNCED, **{k: SYNCED[k] for k in ("sync", "session_time_start")}}
    api = FakeApi(
        _routes(
            [UNSYNCED],
            {("PUT", f"{S}/videos/{UNSYNCED['video_id']}/sync"): lambda body, _p: synced},
        )
    )
    view = ReviewView(_hub(api), embedded=True)
    assert not view.onboard.sync_box.isHidden()
    view._sync_video(2, 7.2)
    put = [c for c in api.calls if c[0] == "PUT"][-1]
    assert put[2] == {"lap_starts": [{"lap": 2, "video_time": 7.2}]}
    assert vision.is_synced(view.video) and view.onboard.sync_box.isHidden()


def test_analyse_stops_at_the_confirm_unless_accepted(qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from rtv.desktop.views import review_view
    from rtv.desktop.views.review_view import ReviewView

    plan = {"count": 2, "frames": 8, "model": "kimi-k3"}
    running = {"analysis_id": "A2", "status": "running", "done": 0, "total": 2}
    api = FakeApi(
        _routes(
            [SYNCED],
            {
                ("POST", f"{S}/vision/plan"): plan,
                ("POST", f"{S}/vision/analyses"): running,
                ("GET", f"{S}/vision/analyses/A2"): {
                    "status": "complete",
                    "notes": 2,
                    "observations_kept": 1,
                    "items": NOTES,
                },
            },
        )
    )
    view = ReviewView(_hub(api), embedded=True)

    view.onboard.plan.click()  # the free dry run never opens the dialog
    assert "2 moments · 8 frames · 2 kimi-k3 calls" in view.onboard.plan_line.text()

    answers = [QDialog.DialogCode.Rejected]
    monkeypatch.setattr(review_view.VisionSpendDialog, "exec", lambda self: answers[0])
    view.onboard.analyse.click()
    assert not [c for c in api.calls if c[:2] == ("POST", f"{S}/vision/analyses")]

    answers[0] = QDialog.DialogCode.Accepted
    view.onboard.analyse.click()
    posts = [c for c in api.calls if c[:2] == ("POST", f"{S}/vision/analyses")]
    assert posts and posts[0][2] == {"video_id": SYNCED["video_id"]}
    assert view._analysis == "A2" and view._vision_timer.isActive()
    view._poll_analysis()
    assert not view._vision_timer.isActive()
    assert view.onboard.analysis.text() == "Analysed · 2 moments · 1 observation kept"
