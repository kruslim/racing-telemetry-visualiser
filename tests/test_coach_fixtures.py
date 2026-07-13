"""Fixture builder tests — every registered fixture builds and behaves as the seed cases expect.

Also exercises the one store-backed integration path: synthetic laps through the REAL feature
pipeline, read back and coached end to end.
"""

from __future__ import annotations

import pytest
from evals.fixtures import FIXTURES, build_fixture


@pytest.mark.parametrize("name", FIXTURES)
def test_every_fixture_builds(name):
    provider = build_fixture(name)
    if name == "missing_session":
        with pytest.raises(KeyError):
            provider.available_channels("synthetic")
        with pytest.raises(KeyError):
            provider.lap_findings("synthetic", 5, 3)
    else:
        findings = provider.lap_findings("synthetic", 5, 3)
        assert "corners" in findings and "chief" in findings
        assert isinstance(provider.available_channels("synthetic"), list)


def test_unknown_fixture_raises_with_known_keys():
    with pytest.raises(KeyError) as exc:
        build_fixture("nope")
    assert "clean_lap" in str(exc.value)


def test_no_tyre_temp_omits_tyre_channels():
    provider = build_fixture("no_tyre_temp")
    channels = provider.available_channels("synthetic")
    assert not any("Tyre" in c or "Temp" in c for c in channels)


def test_t4_brake_loss_top_priority_is_t4():
    findings = build_fixture("t4_brake_loss").lap_findings("synthetic", 5, 3)
    assert findings["chief"]["top3"][0]["label"] == "T4"


def test_clean_lap_has_no_losing_corners():
    findings = build_fixture("clean_lap").lap_findings("synthetic", 5, 3)
    assert findings["chief"]["losing_n"] == 0
    assert findings["chief"]["top3"] == []


# ── Store-backed integration path (real features.py pipeline) ───────────────────────────────

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("numpy")


def test_store_fixture_flows_real_findings_through_the_coach(tmp_path, make_model, ai):
    pytest.importorskip("langgraph")
    from evals.fixtures import build_store_fixture

    from rtv.coaching.agent import run_coach

    provider, session_id, main_lap, ref_lap = build_store_fixture(tmp_path)

    # The real pipeline produces a losing corner near the synthetic apex; find its label so the
    # scripted answer cites a corner that genuinely exists in ground truth.
    findings = provider.lap_findings(session_id, main_lap, ref_lap)
    assert findings["chief"]["losing_n"] >= 1
    top = findings["chief"]["top3"][0]
    net_dt = next(c["net_dt"] for c in findings["corners"] if c["label"] == top["label"])

    answer = {
        "headline": f"{top['label']} is your biggest loss.",
        "priorities": [{"corner": top["label"], "why": "time available", "gain_s": net_dt}],
        "one_lap_focus": f"Focus on {top['label']}.",
        "claims": [
            {
                "statement": f"{top['label']} loses time vs the reference.",
                "corner": top["label"],
                "citations": [
                    {"corner": top["label"], "figure": "net_dt", "value": net_dt, "unit": "s"}
                ],
                "confidence": "high",
            }
        ],
        "corners_examined": [c["label"] for c in findings["corners"]],
        "could_not_determine": [],
    }
    find_args = {"session_id": session_id, "main_lap": main_lap, "ref_lap": ref_lap}
    model = make_model(
        [
            ai("get_lap_findings", find_args),
            ai("submit_coaching", answer),
        ]
    )
    state = run_coach("Where am I losing time?", provider, model)

    assert state.answer is not None
    assert state.answer.priorities[0].corner == top["label"]
