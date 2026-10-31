"""Prose fact-check tests: the figures a coach writes in text are checked, not only its gains.

The corners below are copied from the real Spa .ibt (lap 12 vs lap 6, the findings the Kimi
orchestrator was given). The failing plan is the one it actually wrote: it moved the T7 brake
point from 6540 m to ~6570 m, which is later, while the finding says the driver was already
32 m late and the reference brakes at about 6508 m. That plan used to score 1.0.
"""

from __future__ import annotations

import pytest
from evals.checks import cross_check

from rtv.coaching.agent.prose import check_prose, corner_facts

T7 = {
    "index": 6, "label": "T7", "distance": 6753.161, "sector": 3, "type": "Hairpin",
    "min_main": 55.594, "min_ref": 66.041, "net_dt": 2.851,
    "entry_m": 6605.397, "exit_m": 6924.137,
    "diags": [
        {"agent": "speed", "text": "Min speed -10 km/h vs reference", "distance": 6753.161,
         "time_loss": 1.259},
        {"agent": "brake", "text": "Brake point 32 m late", "distance": 6540.355,
         "time_loss": 1.284},
        {"agent": "gear", "text": "Apex in gear 1 - reference used 2", "distance": 6753.161,
         "time_loss": 0.157},
        {"agent": "grip", "text": "Peak 1.64g lateral - ref held 2.06g", "distance": 6753.161,
         "time_loss": 0.152},
    ],
}
T3 = {
    "index": 2, "label": "T3", "distance": 3024.287, "sector": 2, "type": "Hairpin",
    "min_main": 91.476, "min_ref": 92.425, "net_dt": 0.361,
    "entry_m": 2876.252, "exit_m": 3195.148,
    "diags": [
        {"agent": "throttle", "text": "Full throttle 14 m later than ref", "distance": 3102.905,
         "time_loss": 0.361},
        {"agent": "consistency", "text": "+-14 km/h min-speed spread over stint",
         "distance": 3024.287, "time_loss": 0.0, "variance": True},
    ],
}
FINDINGS = {
    "corners": [T3, T7],
    "sectors": [],
    "chief": {
        "lost": 4.230, "net_lap": 5.641, "time_spread": None,
        "top3": [{"label": "T7", "gain": 2.851}, {"label": "T3", "gain": 0.361}],
    },
}


def _result(*, focus="", why="", cues=(), diagnosis=""):
    return {
        "clean": False,
        "plan": {
            "headline": "T7 is where the lap goes.",
            "one_lap_focus": focus,
            "priorities": [
                {"corner": "T7", "why": why, "gain_s": 2.851},
                {"corner": "T3", "why": "Full throttle 14 m later than ref", "gain_s": 0.361},
            ],
        },
        "advices": [
            {"corner": "T7", "diagnosis": diagnosis, "cues": list(cues), "est_gain_s": 2.851}
        ],
    }


def test_reference_brake_point_is_derived_from_the_finding():
    f = corner_facts(T7)
    assert f.brake_main == pytest.approx(6540.355)
    assert f.brake_gap == 32
    assert f.brake_ref == pytest.approx(6508.355)
    assert f.gears == {1, 2}


def test_the_real_spa_t7_plan_is_no_longer_scored_perfect():
    result = _result(focus="At T7, move your brake point from 6540 m to ~6570 m.")
    score = cross_check(result, FINDINGS)
    assert score["claim_factuality"] < 1.0
    issues = " | ".join(score["prose_issues"])
    assert "tells the driver to brake later at T7" in issues
    assert "reference brakes at 6508 m" in issues
    assert "~6570 m at T7 matches no distance" in issues


def test_a_faithful_plan_on_the_same_corner_scores_perfect():
    result = _result(
        focus="At T7, move your brake point from 6540 m to 6508 m.",
        why="Brake point 32 m late, 10 km/h down at the apex.",
        diagnosis=(
            "You braked 32 m later than the reference and carried 55.6 km/h against "
            "66 km/h, in gear 1 where the reference used 2nd gear. That costs 2.85 s."
        ),
        cues=["Brake about 30 m earlier.", "Aim for 2.06g of lateral load like the reference."],
    )
    score = cross_check(result, FINDINGS)
    assert score["issues"] == []
    assert score["claim_factuality"] == 1.0
    assert score["prose_claims"] >= 9


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Brake later into T7.", "tells the driver to brake later at T7"),
        ("Try braking a bit later.", "tells the driver to brake later at T7"),
        ("You are braking too early.", "says the driver braked earlier at T7"),
        ("You braked 32 m earlier than the reference.", "says the driver braked earlier"),
    ],
)
def test_wrong_brake_direction_is_flagged(text, expected):
    rep = check_prose([("cue", "T7", text)], FINDINGS)
    assert any(expected in i for i in rep.issues), rep.issues


@pytest.mark.parametrize(
    "text",
    [
        "Brake earlier into T7.",
        "Brake sooner and get the car slowed in a straight line.",
        "You're braking late.",
        "Brake point 32 m late.",
        "Release the brake later and get on the throttle earlier.",  # not the brake point
    ],
)
def test_right_brake_direction_passes(text):
    rep = check_prose([("cue", "T7", text)], FINDINGS)
    assert rep.issues == []


def test_a_direction_where_the_findings_have_no_brake_point_is_flagged():
    rep = check_prose([("cue", "T3", "Brake 10 m earlier.")], FINDINGS)
    assert any("no brake-point difference" in i for i in rep.issues)
    assert any("10 m at T3 matches no distance" in i for i in rep.issues)


@pytest.mark.parametrize(
    ("text", "unit"),
    [
        ("You lose 18 km/h at the apex.", "speed"),
        ("That is worth 1.9 s.", "time"),
        ("Peak lateral was only 1.2g.", "lateral g"),
        ("The reference brakes at 6450 m.", "distance"),
        ("You brake 45 m late.", "distance"),
    ],
)
def test_figures_the_findings_do_not_carry_are_flagged(text, unit):
    rep = check_prose([("cue", "T7", text)], FINDINGS)
    assert any(f"matches no {unit}" in i for i in rep.issues), rep.issues


def test_a_wrong_gear_is_flagged():
    rep = check_prose([("cue", "T7", "Take it in third gear.")], FINDINGS)
    assert any("gear 3 at T7" in i for i in rep.issues)


def test_a_sentence_naming_another_corner_is_checked_against_that_corner():
    # Inside the T7 advice, but about T3: 14 m is T3's throttle gap, not a T7 figure.
    ok = check_prose([("cue", "T7", "At T3 you were on full throttle 14 m later.")], FINDINGS)
    assert ok.issues == []
    bad = check_prose([("cue", "T3", "Brake at 6508 m.")], FINDINGS)
    assert bad.issues


def test_a_hallucinated_corner_in_prose_is_flagged():
    rep = check_prose([("plan.headline", None, "T9 costs you 0.4 s.")], FINDINGS)
    assert rep.issues == ["plan.headline: mentions corner T9, which is not in the findings."]


def test_headline_without_a_corner_may_quote_any_corner_or_lap_figure():
    rep = check_prose(
        [("plan.headline", None, "4.23 s on the table, 2.85 s of it from a 32 m late brake.")],
        FINDINGS,
    )
    assert rep.issues == []
    assert rep.claims == 3


def test_numbers_that_are_not_figures_are_ignored():
    rep = check_prose([("cue", "T7", "Next lap, focus on one thing: the entry.")], FINDINGS)
    assert rep.claims == 0
