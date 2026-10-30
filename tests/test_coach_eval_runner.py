"""Regression-runner tests — the seed set passes a well-behaved coach, and the baseline diff
catches a regression. Hermetic: each case is driven by a per-case scripted model.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from evals.cases import SEED_CASES, SEED_CASES_BY_ID  # noqa: E402
from evals.runner import run_regression  # noqa: E402

FIND_ARGS = {"session_id": "synthetic", "main_lap": 5, "ref_lap": 3}


def _answer(corner: str, gain: float, examined: list[str]) -> dict:
    return {
        "headline": f"{corner} is your biggest opportunity.",
        "priorities": [{"corner": corner, "why": "time available", "gain_s": gain}],
        "one_lap_focus": f"Focus on {corner}.",
        "claims": [
            {
                "statement": f"{corner} loses time vs the reference.",
                "corner": corner,
                "citations": [{"corner": corner, "figure": "net_dt", "value": gain, "unit": "s"}],
                "confidence": "high",
            }
        ],
        "corners_examined": examined,
        "could_not_determine": [],
    }


# The correct scripted behaviour for each seed case, by case_id.
def _good_scripts(ai) -> dict[str, list]:
    t4 = ["T1", "T4", "T7"]
    multi = ["T1", "T2", "T3", "T4"]
    return {
        "seed_tyre_temp_refusal": [
            ai("list_available_channels", {"session_id": "synthetic"}),
            ai("refuse", {"reason": "channel_not_captured", "channels_required": ["TyreTemp"]}),
        ],
        "seed_missing_session_refusal": [
            ai("get_lap_findings", FIND_ARGS),
            ai("refuse", {"reason": "session_not_found", "channels_required": []}),
        ],
        "seed_t4_brake_priority": [
            ai("get_lap_findings", FIND_ARGS),
            ai("submit_coaching", _answer("T4", 0.30, t4)),
        ],
        "seed_multi_corner_priority": [
            ai("get_lap_findings", FIND_ARGS),
            ai("submit_coaching", _answer("T3", 0.40, multi)),
        ],
        "seed_nonexistent_corner_in_question": [
            ai("get_lap_findings", FIND_ARGS),
            ai("submit_coaching", _answer("T4", 0.30, t4)),
        ],
    }


def test_seed_cases_all_pass_a_well_behaved_coach(make_model, ai):
    scripts = _good_scripts(ai)
    report = run_regression(SEED_CASES, lambda case: make_model(scripts[case.case_id]))

    assert report.n_total == len(SEED_CASES)
    assert report.pass_rate == 1.0, report.render()
    assert report.regressions(report.baseline_map()) == []


def test_baseline_diff_flags_a_regression(make_model, ai):
    good = _good_scripts(ai)
    baseline = run_regression(
        SEED_CASES, lambda case: make_model(good[case.case_id])
    ).baseline_map()

    # Break one case: coach the wrong corner as top priority for the multi-corner plan.
    broken = _good_scripts(ai)
    broken["seed_multi_corner_priority"] = [
        ai("get_lap_findings", FIND_ARGS),
        ai("submit_coaching", _answer("T1", 0.12, ["T1", "T2", "T3", "T4"])),  # T3 is the real top
    ]
    report = run_regression(SEED_CASES, lambda case: make_model(broken[case.case_id]))

    regressions = report.regressions(baseline)
    assert "seed_multi_corner_priority" in regressions
    assert report.pass_rate < 1.0


def test_newly_fixed_is_reported(make_model, ai):
    scripts = _good_scripts(ai)
    # A baseline in which the t4 case was failing; a good run should report it newly fixed.
    baseline = {case.case_id: True for case in SEED_CASES}
    baseline["seed_t4_brake_priority"] = False

    report = run_regression(
        [SEED_CASES_BY_ID["seed_t4_brake_priority"]],
        lambda case: make_model(scripts[case.case_id]),
    )
    assert report.newly_fixed(baseline) == ["seed_t4_brake_priority"]
