"""Stage 09 part B in RTV Desktop: the review's debrief panel and its view model.

The model is fed real debrief documents (the synthetic race replayed through the app and
debriefed with a scripted model, and with no key), so the panel paints what the service
actually stores. The n/a rule is checked: a figure the run did not record reads ``n/a``.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("fastapi")

from evals.debrief_cases import debrief_cases, faithful_answer, scripted_handler  # noqa: E402
from evals.synthetic_debrief import (  # noqa: E402
    DEBRIEF_SESSION_ID,
    debrief_source,
    seed_debrief_session,
)

from rtv.desktop.model import debrief as model  # noqa: E402
from rtv.desktop.model.format import NA  # noqa: E402
from rtv.pitwall.provider import ScriptedProvider  # noqa: E402

SID = DEBRIEF_SESSION_ID


@pytest.fixture(scope="module")
def docs(tmp_path_factory):
    """{"none": no-key doc, "faithful": all sections kept, "refused": one refused}."""
    mp = pytest.MonkeyPatch()
    mp.setenv("RTV_DATA_DIR", str(tmp_path_factory.mktemp("debrief-ui")))
    mp.setenv("RTV_AUTOSTART_LIVE", "false")
    mp.setenv("RTV_PITWALL", "true")
    mp.setenv("RTV_DEBRIEF_AUTO", "false")
    for name in ("KIMI_CODE_API_KEY", "KIMI_API_KEY"):
        mp.setenv(name, "")
    import rtv.llm

    mp.setattr(rtv.llm, "_from_env_file", lambda _name: None)
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    out = {}
    try:
        with TestClient(create_app()) as c:
            services = c.app.state.services
            seed_debrief_session(services.writer)
            services.replay.start(debrief_source())
            services.replay.join(timeout=60)
            assert services.on_session_end.wait(timeout=60)
            run = c.get(f"/api/v1/sessions/{SID}/pitwall-runs").json()["runs"][0]["run_id"]
            out["none"] = services.debrief.generate(SID, run)
            inputs = out["none"]["inputs"]
            cases = {case.name: case for case in debrief_cases(inputs)}
            picks = (("faithful", cases["faithful"]), ("refused", cases["unrecorded_lap"]))
            for name, case in picks:
                services.debrief.provider = ScriptedProvider(scripted_handler(case))
                out[name] = services.debrief.generate(SID, run)
            out["answer"] = faithful_answer(inputs)
    finally:
        mp.undo()
        get_settings.cache_clear()
    return out


def test_the_no_key_debrief_reads_deterministic_only_with_its_note(docs):
    doc = docs["none"]
    head = model.header(doc)
    assert "deterministic only" in head["meta"] and "analysis half is unavailable" in head["note"]
    rows = model.section_rows(doc)
    assert [r["status"] for r in rows] == ["unavailable"] * 6
    assert all(r["refs"] == [] and r["items"] == [] for r in rows)
    result = dict(model.result_rows(doc))
    assert result["Laps"] != NA and result["Best lap"].endswith(")")
    losses = model.loss_rows(doc)
    assert losses and all(r["corner"] and r["lap"] in doc["references"]["laps"] for r in losses)


def test_a_missing_figure_reads_na_never_zero():
    rows = dict(model.result_rows({"deterministic": {"result": {"laps": 0}, "fuel": {}}}))
    assert rows["Start"] == NA and rows["Fuel used"] == NA and rows["Best lap"] == NA
    assert rows["Laps"] == "0"  # a real zero is still printed
    assert model.section_rows(None) == [] and model.header(None)["note"] == ""


def test_kept_sections_carry_clickable_laps_and_corners(docs):
    doc = docs["faithful"]
    rows = {r["name"]: r for r in model.section_rows(doc)}
    assert {r["status"] for r in rows.values()} == {"ok"}
    assert "sections checked" in model.header(doc)["meta"]
    recorded = set(doc["references"]["laps"])
    item = rows["time_lost"]["items"][0]
    kinds = {r["kind"] for r in item["refs"]}
    assert kinds == {"lap", "corner"}
    lap_ref = next(r for r in item["refs"] if r["kind"] == "lap")
    assert lap_ref["lap"] in recorded and lap_ref["corner"] == docs["answer"].time_lost[0].corner
    assert rows["priorities"]["items"][0]["text"].endswith("(from recurring issues)")
    assert model.reference_lap(doc) == doc["references"]["reference_lap"]


def test_a_refused_section_shows_its_reason_and_no_claims(docs):
    rows = {r["name"]: r for r in model.section_rows(docs["refused"])}
    lost = rows["time_lost"]
    assert lost["status"] == "refused" and lost["badge"] == "refused"
    assert lost["body"].startswith("Refused:") and lost["items"] == [] and lost["refs"] == []


def test_refs_ignore_laps_that_were_not_recorded():
    refs = model.refs("Box on lap 40, as on laps 3 to 4.", [9], "T5 Pouhon", {3, 4, 9})
    assert [(r["kind"], r.get("lap"), r.get("corner")) for r in refs] == [
        ("lap", 9, "T5"),
        ("lap", 3, "T5"),
        ("lap", 4, "T5"),
        ("corner", 9, "T5"),
    ]


# ---------------------------------------------------------------- widgets
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from rtv.desktop.theme.qss import stylesheet

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(stylesheet())
    return app


def test_the_panel_paints_links_and_asks_before_calling_kimi(qapp, docs):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QPushButton

    from rtv.desktop.widgets.debrief import DebriefPanel

    asked: list[bool] = []
    answer = [False]
    panel = DebriefPanel(confirm=lambda: asked.append(True) or answer[0])
    regen: list[bool] = []
    clicked: list[dict] = []
    panel.regenerate_clicked.connect(lambda: regen.append(True))
    panel.ref_clicked.connect(clicked.append)

    panel.set_debrief(None, None)
    assert not panel.regen.isEnabled() and "No journal run" in panel.note.text()
    panel.set_debrief(None, "run-1")
    assert panel.regen.isEnabled() and "No debrief for this run" in panel.note.text()

    panel.set_debrief(docs["faithful"], docs["faithful"]["run_id"])
    panel.grab()
    for lab in panel.findChildren(QLabel):
        assert lab.textFormat() == Qt.TextFormat.PlainText
    links = [b for b in panel.findChildren(QPushButton) if b.property("role") == "link"]
    lap_link = next(b for b in links if b.text().startswith("Lap "))
    lap_link.click()
    assert clicked and clicked[-1]["kind"] == "lap"
    assert clicked[-1]["lap"] in docs["faithful"]["references"]["laps"]
    corner_link = next(b for b in links if b.text().startswith("T"))
    corner_link.click()
    assert clicked[-1]["corner"] == corner_link.text().split()[0]

    # Regenerate says it calls Kimi first, and does nothing when declined.
    assert "Kimi" in panel.regen.toolTip() and "Kimi" in panel.regen.accessibleName()
    panel.regen.click()
    assert asked == [True] and regen == []
    answer[0] = True
    panel.regen.click()
    assert regen == [True]
