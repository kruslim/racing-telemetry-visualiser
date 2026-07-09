"""Eval cross-check tests — prove the ground-truth checker catches hallucinations.

These run fully offline (no LLM): the point is that because the findings are ground truth,
a coach output with invented figures is caught deterministically.
"""

from __future__ import annotations

from evals.checks import aggregate, cross_check

FINDINGS = {
    "corners": [
        {"label": "T1", "net_dt": 0.30},
        {"label": "T2", "net_dt": 0.10},
        {"label": "T3", "net_dt": -0.02},
    ],
    "chief": {"top3": [
        {"label": "T1", "gain": 0.30},
        {"label": "T2", "gain": 0.10},
    ]},
}


def _result(priorities, advices):
    return {
        "clean": False,
        "plan": {"headline": "h", "one_lap_focus": "f", "priorities": priorities},
        "advices": advices,
    }


def test_faithful_coach_scores_perfect():
    result = _result(
        priorities=[{"corner": "T1", "why": "x", "gain_s": 0.30},
                    {"corner": "T2", "why": "y", "gain_s": 0.10}],
        advices=[{"corner": "T1", "diagnosis": "d", "cues": ["c"], "est_gain_s": 0.30}],
    )
    score = cross_check(result, FINDINGS)
    assert score["outcome_accuracy"] is True
    assert score["claim_factuality"] == 1.0
    assert score["issues"] == []


def test_wrong_gain_is_flagged():
    result = _result(
        priorities=[{"corner": "T1", "why": "x", "gain_s": 0.90}],  # finding says 0.30
        advices=[],
    )
    score = cross_check(result, FINDINGS)
    assert score["claim_factuality"] < 1.0
    assert any("net_dt" in i for i in score["issues"])


def test_hallucinated_corner_is_flagged():
    result = _result(
        priorities=[{"corner": "T1", "why": "x", "gain_s": 0.30}],
        advices=[{"corner": "T9", "diagnosis": "d", "cues": ["c"], "est_gain_s": 0.5}],
    )
    score = cross_check(result, FINDINGS)
    assert any("hallucinated" in i for i in score["issues"])
    assert score["supported"] < score["claims"]


def test_wrong_top_priority_fails_outcome():
    result = _result(
        priorities=[{"corner": "T2", "why": "x", "gain_s": 0.10}],  # T1 is the real biggest loss
        advices=[],
    )
    score = cross_check(result, FINDINGS)
    assert score["outcome_accuracy"] is False


def test_aggregate_rolls_up():
    good = cross_check(
        _result([{"corner": "T1", "why": "x", "gain_s": 0.30}], []), FINDINGS
    )
    bad = cross_check(
        _result([{"corner": "T9", "why": "x", "gain_s": 9.9}], []), FINDINGS
    )
    agg = aggregate([good, bad])
    assert agg["cases"] == 2
    assert 0.0 <= agg["mean_claim_factuality"] <= 1.0
    assert agg["total_hallucinations"] >= 1
