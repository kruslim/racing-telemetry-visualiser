"""Stage 08 in RTV Desktop: the live race-plan panel and the review's plan lane and audit.

The plan model is fed real engine output (the synthetic race replayed through the
engine, plus a proposal accepted and one rejected), so the panel paints what the engine
actually serialises. The n/a rule is checked: an element with no grounding reads
``n/a`` with the plan's reason, never zero.
"""

from __future__ import annotations

import os

import pytest
from evals.synthetic_race import ScenarioSpec, replay_scenario

from rtv.desktop.model import plan as plan_model
from rtv.desktop.model.format import NA
from rtv.racestate import RaceStateEngine

RACE = ScenarioSpec(session_num=0, sessions=("Race",), laps=4, end_pct=0.5)


def _race_state(propose: bool = True) -> dict:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, RACE)
    if propose:
        engine.propose_plan_revision(
            {"next_stop_lap": 9, "rationale": "Stretch it."}, agent="strategist"
        )
        engine.propose_plan_revision(
            {"next_stop_lap": 4, "rationale": "Take the window early."}, agent="strategist"
        )
    return engine.snapshot().model_dump(mode="json")


def test_plan_card_shows_every_element_with_provenance():
    state = _race_state()
    card = plan_model.plan_card(state)
    assert card["available"] and card["version"].startswith("v")
    rows = {r["key"]: r for r in card["rows"]}
    assert list(rows) == list(plan_model.LABELS)
    assert rows["next_stop_lap"]["value"] == "lap 4"
    assert rows["next_stop_lap"]["source"] == "agent_proposed"
    assert rows["next_stop_lap"]["source_label"] == "strategist"
    assert rows["fuel_to_add_l"]["value"].endswith(" L")


def test_a_missing_element_reads_na_with_its_reason_never_zero():
    state = _race_state(propose=False)
    state["plan"]["pace_target_s"] = None
    state["plan"]["missing"]["pace_target_s"] = "No clean lap yet."
    row = next(r for r in plan_model.plan_card(state)["rows"] if r["key"] == "pace_target_s")
    assert row["value"] == NA and not row["grounded"] and row["note"] == "No clean lap yet."
    assert plan_model.element_text("stop_count", None) == NA
    assert plan_model.element_text("stop_count", 0) == "0"


def test_no_plan_outside_a_race_says_why():
    card = plan_model.plan_card({"plan": None, "lifecycle": {"session_type": "practice"}})
    assert not card["available"] and "only in a race" in card["reason"]
    assert not plan_model.plan_card(None)["available"]


def test_revision_rows_are_newest_first_with_the_rejection():
    rows = plan_model.revision_rows(_race_state()["plan"])
    assert rows[0]["accepted"] and "lap 5 → lap 4" in rows[0]["changes"]
    assert not rows[1]["accepted"] and rows[1]["title"].startswith("rejected · strategist")
    assert "run-dry" in rows[1]["why"]


def _records() -> list[dict]:
    return [
        {
            "kind": "race_event",
            "session_time": 12.0,
            "data": {
                "event_type": "plan_revised",
                "payload": {
                    "new_version": 2,
                    "provenance": "agent_proposed",
                    "agent": "strategist",
                    "changes": {"next_stop_lap": {"from": 5, "to": 4}},
                },
            },
        },
        {
            "kind": "plan_proposal_rejected",
            "agent": "strategist",
            "session_time": 11.0,
            "data": {"reason": "next_stop_lap is after the run-dry lap 6."},
        },
        {
            "kind": "tool_call",
            "run_id": "r1",
            "session_time": 11.0,
            "data": {
                "tool": "propose_plan_revision",
                "result_full": {"accepted": False, "reason": "too late"},
            },
        },
        {
            "kind": "tool_call",
            "run_id": "r1",
            "session_time": 12.0,
            "data": {
                "tool": "propose_plan_revision",
                "result_full": {"accepted": True, "plan_version": 2},
            },
        },
        {"kind": "tool_call", "run_id": "r2", "data": {"tool": "get_fuel_state"}},
    ]


def test_timeline_items_and_audit_notes():
    items = plan_model.timeline_items(_records())
    assert [i["accepted"] for i in items] == [True, False]
    assert "plan v2 (strategist): next stop lap 5 → lap 4" == items[0]["label"]
    assert items[1]["label"].startswith("plan proposal rejected (strategist)")
    assert plan_model.is_plan_item({"record": _records()[0]})
    assert not plan_model.is_plan_item({"record": _records()[1]})
    assert plan_model.audit_notes(_records()) == {"r1": "rejected: too late; v2 accepted"}


# ---------------------------------------------------------------- widgets
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent

    for widget in app.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


def test_plan_panel_paints_the_plan_and_the_empty_state(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    from rtv.desktop.widgets.plan import PlanPanel

    panel = PlanPanel()
    panel.set_view(_race_state())
    panel.grab()
    texts = [lab.text() for lab in panel.findChildren(QLabel)]
    assert "lap 4" in texts and any(t.startswith("strategist · v") for t in texts)
    assert any(t.startswith("rejected · strategist") for t in texts)
    for lab in panel.findChildren(QLabel):
        assert lab.textFormat() == Qt.TextFormat.PlainText
    panel.set_view({"plan": None, "lifecycle": {"session_type": "qualify"}})
    assert "only in a race" in panel.reason.text() and panel.grid_widget.isHidden()


def test_audit_panel_has_a_plan_column(qapp):
    from rtv.desktop.model import review
    from rtv.desktop.widgets.review import AuditPanel

    records = [
        {
            "kind": "run_start",
            "run_id": "r1",
            "agent": "strategist",
            "session_time": 10.0,
            "data": {},
        },
        *_records(),
    ]
    audit = AuditPanel()
    rows = review.audit_rows(records)
    audit.set_rows(rows, len(rows), True, plan_model.audit_notes(records))
    headers = [audit.table.horizontalHeaderItem(c).text() for c in range(audit.table.columnCount())]
    col = headers.index("Plan")
    r1 = next(i for i, row in enumerate(rows) if row["run_id"] == "r1")
    assert audit.table.item(r1, col).text() == "rejected: too late; v2 accepted"
