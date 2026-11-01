"""Desktop: the session lifecycle and the agents' arming (stage 07), model and widgets."""

from __future__ import annotations

import os

import pytest

from rtv.desktop.model.session import agents_view, lifecycle_view, run_label


def _state(**lc):
    return {
        "capabilities": {"session": True, "lifecycle": True, "missing": []},
        "lifecycle": {"state": "idle", "segment": 0, **lc},
    }


def _status(**kw):
    return {"available": True, "enabled": True, "suspended": False, "mode": "on", **kw}


def test_the_lifecycle_pill_names_the_session_and_its_phase():
    view = lifecycle_view(_state(state="green", session_type="race", segment=3), _status())
    assert view["text"] == "Race · green" and view["segment"] == 3
    assert lifecycle_view(_state(state="pre_green", session_type="qualify"), None)["text"] == (
        "Qualifying · pre-green"
    )
    assert lifecycle_view(_state(), None)["text"] == "No session yet"
    unknown = lifecycle_view(_state(state="green", session_type=None), None)
    assert unknown["session_label"] == "Session"


def test_missing_lifecycle_channels_are_said_not_guessed():
    state = _state(state="green", notes=["SessionNum is not in this catalog."])
    state["capabilities"].update(lifecycle=False, missing=["CarIdxLap", "SessionNum"])
    view = lifecycle_view(state, None)
    assert "SessionNum is not in this catalog." in view["detail"]
    assert "No SessionNum in this telemetry." in view["detail"]
    assert "CarIdxLap" not in view["detail"]


@pytest.mark.parametrize(
    ("status", "agents", "label", "detail"),
    [
        (None, "unmounted", "AGENTS OFF", "agent layer not mounted"),
        ({"available": False, "reason": "no key"}, "unmounted", "AGENTS OFF", "no key"),
        (_status(enabled=False, mode="off"), "off", "AGENTS OFF", "kill switch"),
        (
            _status(mode="standby", standby_reason="waiting for the green flag"),
            "standby",
            "STANDBY",
            "waiting for the green flag",
        ),
        (_status(mode="armed"), "armed", "ARMED", "every agent"),
        (_status(mode="armed", armed_roster=["coach"]), "armed", "ARMED", "roster: coach"),
        (_status(mode="suspended", suspend_reason="gone"), "suspended", "SUSPENDED", "gone"),
        (_status(mode="on"), "on", "AGENTS ON", "autonomy off"),
        # A server from before stage 07 has no mode field.
        ({"available": True, "enabled": False}, "off", "AGENTS OFF", "kill switch"),
    ],
)
def test_agents_are_armed_in_standby_or_off_and_say_why(status, agents, label, detail):
    view = agents_view(status)
    assert view["agents"] == agents and view["agents_label"] == label
    assert detail in view["agents_detail"]


def test_review_runs_are_labelled_with_their_session_type():
    run = {
        "source": "live",
        "session_type": "qualify",
        "session_num": 1,
        "has_agents": True,
        "complete": True,
        "started_wall_ms": None,
    }
    assert run_label(run) == "Qualifying · live · ? · agents on · complete"
    old = {"source": "replay", "has_agents": False, "complete": False}
    assert run_label(old) == "replay · ? · agents off · incomplete"


def test_the_status_strip_and_run_picker_show_it():
    pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QLabel

    app = QApplication.instance() or QApplication([])
    from rtv.desktop.widgets.live import StatusStrip
    from rtv.desktop.widgets.review import SessionBar

    strip = StatusStrip()
    strip.set_lifecycle(
        lifecycle_view(
            _state(state="green", session_type="race"),
            _status(mode="standby", standby_reason="no agents are on the race roster"),
        )
    )
    shown = [lab.text() for lab in strip.findChildren(QLabel)]
    assert "RACE · GREEN" in shown and "STANDBY" in shown
    assert any("race roster" in s for s in shown)
    bar = SessionBar()
    bar.set_runs(
        [
            {"run_id": "a", "source": "live", "session_type": "practice", "session_num": 0},
            {"run_id": "b", "source": "live", "session_type": "race", "session_num": 2},
        ],
        "b",
    )
    assert bar.runs.itemText(0).startswith("Practice · live")
    assert bar.runs.currentText().startswith("Race · live")
    from PySide6.QtCore import QCoreApplication, QEvent

    for widget in (strip, bar):
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert app is not None
