"""The desktop widgets render the real Spa payloads honestly, with no display needed.

Qt runs on the offscreen platform. Every panel is fed real responses from
``docs/ui-redesign/spa-example-payloads.zip`` and grabbed (painted) once, and the
brief's states are checked by their copy: ``n/a`` not zero, the no-field tower,
agents off, the quota prompt, a yellow flag band.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")  # also skips when Qt's system libraries are missing
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from rtv.desktop.model import activity, review  # noqa: E402
from rtv.desktop.model import view_model as vm  # noqa: E402
from tests import spa_payloads as spa  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    from rtv.desktop.theme.qss import stylesheet

    app.setStyleSheet(stylesheet())
    yield app
    # Delete the panels while Qt is still up: left to interpreter exit, Python frees
    # them after QApplication and the process segfaults (pytest exit 139).
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent

    for widget in app.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


def texts(widget) -> list[str]:
    return [lab.text() for lab in widget.findChildren(QLabel)]


def assert_plain(widget) -> None:
    """Server text must never be parsed as rich text (the desktop innerHTML rule)."""
    for lab in widget.findChildren(QLabel):
        assert lab.textFormat() == Qt.TextFormat.PlainText, lab.text()


def test_status_strip_and_flag_band_on_the_real_final_lap_state(qapp):
    from rtv.desktop.widgets.live import FlagBand, StatusStrip

    state = spa.load("racestate_running.json")
    view = vm.status_view(state, {"pitwall_status": spa.load("pitwall_status_idle.json")})
    strip = StatusStrip()
    strip.set_view(view, state)
    strip.set_ai(view["llm"])
    strip.grab()
    shown = texts(strip)
    assert "2:34" in shown and "P5" in shown and "2:20.618" in shown
    assert "AI OFF" in shown
    assert_plain(strip)
    band = FlagBand(reduced_motion=True)
    band.set_flag(view["flag"])
    assert band.height() > 40 and "CHEQUERED" in band.text.text()
    band.set_flag(vm.status_view({"flags": {"phase": "green"}})["flag"])
    assert band.height() == 3  # green is a hairline
    band.set_flag(
        vm.status_view({"flags": {"phase": "yellow", "active": ["yellow_waving"]}})["flag"]
    )
    assert band.height() > 40 and band.text.text().startswith("YELLOW")
    assert not band._pulse.isActive()  # reduced motion: no pulse


def test_strategy_panel_on_real_state_and_strategist_call(qapp):
    from rtv.desktop.widgets.live import StrategyPanel

    state = spa.load("racestate_running.json")
    panel = StrategyPanel()
    history = vm.FuelHistory()
    history.update(state)
    panel.set_view(vm.strategy_view(state), history.points())
    calls = spa.load("agents-on/pitwall-radio.json")["messages"]
    panel.set_recommendation(vm.recommendation_view(calls[12]))
    panel.grab()
    shown = texts(panel)
    assert "−0.71" in shown  # margin in laps, red band
    assert "BOX NOW" in shown and "16.2 L" in shown
    assert "n/a · no standings" in shown  # rejoin is not invented
    assert_plain(panel)


def test_strategy_panel_without_channels_says_why(qapp):
    from rtv.desktop.widgets.live import StrategyPanel

    panel = StrategyPanel()
    panel.set_view(vm.strategy_view(spa.load("racestate_idle.json")), [])
    panel.set_recommendation(None)
    panel.grab()
    shown = texts(panel)
    assert "no tyre channels" in shown and "0.0" not in " ".join(shown)


def test_tower_no_field_state_keeps_the_player_row(qapp):
    from rtv.desktop.widgets.live import TowerPanel

    state = spa.load("racestate_running.json")
    tower = TowerPanel()
    tower.set_view(vm.tower_view(state), state)
    tower.grab()
    shown = texts(tower)
    assert "No field data in this recording" in shown
    assert "P5" in shown and "2:20.722" in shown
    idle = spa.load("racestate_idle.json")
    tower.set_view(vm.tower_view(idle), idle)
    assert "waiting for telemetry" in texts(tower)


def test_radio_feed_variants_from_the_real_agents_on_run(qapp):
    from rtv.desktop.widgets.radio import EMPTY_TEXT, RadioFeed

    feed = RadioFeed()
    assert EMPTY_TEXT in texts(feed)
    calls = spa.load("agents-on/pitwall-radio.json")["messages"]
    feed.seed(
        calls
        + [
            {
                "agent": "driver",
                "spoken_text": "<b>fuel?</b>",
                "speak": False,
                "session_time": calls[-1]["session_time"] + 1,
            }
        ]
    )
    feed.grab()
    shown = texts(feed)
    assert "withheld · grounded refusal".upper() in shown
    assert "after your message" in shown
    assert "<b>fuel?</b>" in shown  # shown literally, never as markup
    assert any(s.startswith("could not back:") for s in shown)
    assert_plain(feed)


def test_review_panels_on_real_summary_findings_and_journal(qapp):
    from rtv.desktop.widgets.review import AuditPanel, FindingsPanel, LapTable, SummaryPanel

    summary = SummaryPanel()
    summary.set_summary(spa.load("summary.json"))
    assert "P10 → P5" in texts(summary) and "2:20.618" in texts(summary)
    findings = FindingsPanel()
    findings.set_codes(spa.load("coaching_agents.json")["agents"])
    findings.set_findings(spa.load("lap_findings.json"))
    findings.grab()
    assert "+0.381 s" in texts(findings)
    cell = findings.corners.item(1, 5).text()
    assert "SPD" in cell and "Brake point 9 m late" in cell
    laps = LapTable()
    rows = spa.load("laps.json")["laps"]
    laps.set_laps(rows, 9, 6)
    assert laps.item(0, 3).text() == "pre-start"
    assert laps.item(len(rows) - 1, 3).text() == "unfinished"
    audit = AuditPanel()
    records = spa.journal()
    audit.set_rows(review.audit_rows(records), 30, True)
    assert audit.table.rowCount() == 30
    audit.set_rows([], 0, False)
    assert "recorded with the agents off" in audit.empty.text()


def test_drawings_paint_real_data(qapp):
    from rtv.desktop.widgets.agents import FlowGraph, RunsWaterfall
    from rtv.desktop.widgets.charts import LaneTimeline, TraceStack, TrackMap

    model = activity.reduce(spa.load("agents-on/pitwall-activity.json")["records"])
    waterfall = RunsWaterfall()
    waterfall.resize(800, 300)
    waterfall.set_runs(activity.recent_runs(model, 10))
    waterfall.grab()
    flow = FlowGraph(reduced_motion=True)
    flow.resize(900, 180)
    for record in spa.load("agents-on/pitwall-activity.json")["records"][:20]:
        flow.pulse(activity.edges_for(record))
    flow.grab()
    track = TrackMap()
    track.resize(400, 300)
    track.set_points(spa.load("trackmap.json")["points"])
    track.set_pins(review.track_events(spa.journal()))
    track.grab()
    speed = spa.load("compare_Speed.json")
    traces = TraceStack()
    traces.resize(900, 500)
    delta = spa.load("delta.json")
    traces.set_data(
        [("SPEED", "km/h", speed["laps"]["9"], speed["laps"]["6"], 0, 90)],
        review.to_metres(speed["x_values"], 6929),
        (delta["distance_m"], delta["delta_s"]),
        6929,
    )
    traces.grab()
    lanes = review.lane_items(spa.journal(), spa.load("laps.json")["laps"])
    timeline = LaneTimeline(scrub=True)
    timeline.resize(900, 200)
    timeline.set_range(0, 2600)
    timeline.set_lanes([("coach", "#fff", lanes["coach"])])
    timeline.grab()


def test_quota_dialog_copy_and_ai_controls_off(qapp):
    from rtv.desktop.widgets.dialogs import QUOTA_TEXT, AiControls, QuotaDialog, agents_will_run

    status_on = spa.load("agents-on/pitwall-status.json")
    status_off = spa.load("pitwall_status_idle.json")
    assert agents_will_run(status_on) and not agents_will_run(status_off)
    dialog = QuotaDialog(status_on)
    assert QUOTA_TEXT in texts(dialog) and "75 / 400" in texts(dialog)
    controls = AiControls()
    controls.set_status(status_off)
    assert not controls.master.isEnabled()
    assert "not mounted" in controls.reason.text()
    controls.set_status(status_on)
    assert controls.master.isEnabled() and controls.agents["coach"].isChecked()


def test_ticker_keeps_its_items_when_the_events_have_not_changed(qapp):
    from rtv.desktop.widgets.live import TickerStrip

    ticker = TickerStrip()
    lines = [vm.event_line(e) for e in spa.load("racestate_events.json")["events"][:3]]
    ticker.set_events(lines, 3)
    before = ticker.findChildren(QLabel)
    ticker.set_events(list(lines), 3)  # the live view calls this on every 10 Hz render
    assert ticker.findChildren(QLabel) == before
    ticker.set_events(lines[:2], 4)
    assert "RACE EVENTS 4" in [lab.text() for lab in ticker.findChildren(QLabel)]
