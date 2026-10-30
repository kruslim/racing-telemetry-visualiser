"""Strategist eval tests — prove the ground-truth checker catches bad calls.

Fully offline, no LLM. The point mirrors ``tests/test_evals.py``: because the race
state *is* the ground truth, a strategist that invents a rejoin position, names a
lap outside the fuel window, or makes a confident call on absent data is caught by
arithmetic rather than by judgement.
"""

from __future__ import annotations

import pytest
from evals.pitwall_cases import build_cases, degraded_cases
from evals.pitwall_checks import aggregate, cross_check, expected_calls

from rtv.pitwall.agents.strategist import STRATEGIST
from rtv.pitwall.framework import AgentRuntime
from rtv.racestate.strategy_math import simulate_pit_outcome


def _result(state, **data):
    """A strategist answer shaped as the radio message the runtime would emit."""
    outcome = simulate_pit_outcome(state)
    payload = {
        "recommendation": "box_now",
        "target_lap": state.fuel.pit_window_earliest_lap,
        "rejoin_position": outcome.rejoin_position,
        "fuel_to_add_l": outcome.fuel_to_add_l,
        "confidence": "high",
        "rationale": "grounded",
        "risks": [],
    }
    payload.update(data)
    return {
        "spoken_text": data.pop("spoken_text", "Box this lap, fuel only."),
        "detail_text": data.pop("detail_text", "Window is open."),
        "data": payload,
    }


@pytest.fixture(scope="module")
def cases():
    return build_cases()


@pytest.fixture(scope="module")
def open_window(cases):
    return next(c for c in cases if c.event.key == "pit_window_open" and c.state.fuel.per_lap)


# --------------------------------------------------------------------------
# the golden set itself
# --------------------------------------------------------------------------
def test_the_golden_set_covers_every_strategist_trigger(cases):
    covered = {c.event.event_type.value for c in cases}
    declared = {t.event_type for t in STRATEGIST.triggers}
    assert declared <= covered, f"no eval case for {sorted(declared - covered)}"


def test_the_golden_set_is_reproducible():
    """A generated eval set is worthless if it drifts between runs."""
    first = [(c.name, c.event.tick, c.state.version) for c in build_cases()]
    second = [(c.name, c.event.tick, c.state.version) for c in build_cases()]
    assert first == second


def test_every_case_carries_the_state_that_was_current_when_it_fired(cases):
    for case in cases:
        if case.note != "scripted race":
            continue
        assert case.event.state_version == case.state.version, case.name


def test_the_cases_the_engine_produced_are_the_ones_a_live_agent_would_see(cases):
    """Cases come from the same match() the orchestrator uses, not a parallel list."""
    runtime = AgentRuntime(STRATEGIST, provider=None)
    scripted = [c for c in cases if c.note == "scripted race"]
    assert scripted
    for case in scripted:
        assert runtime.match(case.event, case.state) is not None, case.name
        runtime.reset()


# --------------------------------------------------------------------------
# the defensible band
# --------------------------------------------------------------------------
def test_an_open_window_demands_a_stop(open_window):
    assert expected_calls(open_window.state) <= {"box_now", "box_next_lap", "pit_under_yellow"}


def test_no_fuel_model_leaves_hold_as_the_only_honest_answer():
    case = next(c for c in degraded_cases() if c.name == "degraded-no-fuel-model")
    assert expected_calls(case.state) == {"hold"}


def test_a_caution_with_a_stop_owed_admits_the_opportunistic_stop():
    case = next(c for c in degraded_cases() if c.name == "degraded-yellow-opportunity")
    assert "pit_under_yellow" in expected_calls(case.state)


def test_a_comfortable_fuel_margin_does_not_justify_covering_a_rival():
    case = next(c for c in degraded_cases() if c.name == "degraded-rival-pitted-no-need")
    allowed = expected_calls(case.state)
    assert "box_now" not in allowed and "stay_out" in allowed


# --------------------------------------------------------------------------
# catching bad calls
# --------------------------------------------------------------------------
def test_a_faithful_call_scores_clean(open_window):
    score = cross_check(_result(open_window.state), open_window.state)
    assert score["issues"] == []
    assert score["score"] == 1.0


def test_an_invented_rejoin_position_is_flagged(open_window):
    score = cross_check(
        _result(open_window.state, rejoin_position=1), open_window.state
    )
    assert score["rejoin_ok"] is False
    assert any("projection says" in i for i in score["issues"])


def test_a_stop_lap_past_the_run_dry_bound_is_flagged(open_window):
    latest = open_window.state.fuel.pit_window_latest_lap
    score = cross_check(
        _result(open_window.state, target_lap=latest + 5), open_window.state
    )
    assert score["window_ok"] is False
    assert any("past the run-dry lap" in i for i in score["issues"])


def test_staying_out_on_an_open_window_is_outside_the_defensible_band(open_window):
    score = cross_check(
        _result(open_window.state, recommendation="stay_out"), open_window.state
    )
    assert score["decision_ok"] is False
    assert any("outside the defensible set" in i for i in score["issues"])


def test_an_ungrounded_figure_in_the_spoken_call_is_flagged(open_window):
    result = _result(open_window.state)
    result["spoken_text"] = "Box now, take 41.7 litres."
    score = cross_check(result, open_window.state)
    assert score["grounding_ok"] is False
    assert "41.7" in score["ungrounded"]


def test_a_speech_length_that_is_not_radio_is_flagged(open_window):
    result = _result(open_window.state)
    result["spoken_text"] = " ".join(["standby"] * 40)
    score = cross_check(result, open_window.state)
    assert score["radio_ok"] is False


def test_a_confident_call_on_missing_data_fails_the_refusal_check():
    case = next(c for c in degraded_cases() if c.name == "degraded-no-fuel-model")
    score = cross_check(_result(case.state, recommendation="box_now"), case.state)
    assert score["refusal_ok"] is False
    assert score["decision_ok"] is False


def test_holding_on_missing_data_passes(open_window):
    case = next(c for c in degraded_cases() if c.name == "degraded-no-fuel-model")
    result = {
        "spoken_text": "Standby, I have no fuel numbers yet.",
        "detail_text": "The catalog has no fuel channels for this session.",
        "data": {"recommendation": "hold", "confidence": "low", "rationale": "no data"},
    }
    score = cross_check(result, case.state)
    assert score["refusal_ok"] is True
    assert score["decision_ok"] is True
    assert score["issues"] == []


def test_claiming_a_rejoin_without_a_gap_basis_is_flagged():
    case = next(c for c in degraded_cases() if c.name == "degraded-no-gap-basis")
    result = _result(case.state, rejoin_position=4)
    score = cross_check(result, case.state)
    assert score["rejoin_ok"] is False
    assert any("not grounded" in i for i in score["issues"])


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------
def test_aggregate_rolls_up(open_window):
    good = cross_check(_result(open_window.state), open_window.state)
    bad = cross_check(
        _result(open_window.state, recommendation="stay_out", rejoin_position=1),
        open_window.state,
    )
    agg = aggregate([good, bad])
    assert agg["cases"] == 2
    assert agg["decision_rate"] == 0.5
    assert agg["rejoin_rate"] == 0.5
    assert 0.0 <= agg["mean_score"] <= 1.0
    assert agg["clean_cases"] == 1


def test_aggregate_of_nothing_is_not_a_crash():
    assert aggregate([]) == {"cases": 0}
