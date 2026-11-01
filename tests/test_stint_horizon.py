"""Task R5: the engineer's stint length is a fact, and its trends are not multiplied out.

R1's Spa replay refused the engineer's "4.184 kPa/lap ... roughly 42 kPa over a 10-lap
stint": it had no fact for the stint length, and the trend it multiplied was the steep
warm-up of a set that levelled off (the RF read 186.8 kPa on lap 12, not 219). The
engineer now gets the laps left on the set as a count, and its prompt says to quote a
trend as a rate. Offline, no model.
"""

from __future__ import annotations

from evals.synthetic_race import ScenarioSpec, replay_scenario

from rtv.grounding.units import field_unit
from rtv.pitwall.agents.vehicle_engineer import VEHICLE_ENGINEER_PROMPT, vehicle_engineer_slice
from rtv.pitwall.validator import FactSet, validate_output
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import PlanElement, RacePlan, RaceState
from rtv.racestate.plan import stint_horizon

RACE = ScenarioSpec(session_num=0, sessions=("Race",))

# R1's get_tyre_trend result for vehicle_engineer-2 (lap 3, stint_laps 3), trimmed.
R1_TYRE_TREND = {
    "pressures_kpa": {"LF": 175.65, "RF": 176.784, "LR": 174.8796, "RR": 175.7211},
    "pressure_trend_per_lap": {"LF": 4.0663, "RF": 4.184, "LR": 3.9214, "RR": 3.9583},
    "stint_laps": 3,
    "since_stint_start": {"pressure_change_kpa": {"LF": 8.13, "RF": 8.37}},
    "vs_previous_lap": {"pressure_change_kpa": {"LF": 6.01, "RF": 6.46}},
}


def _state(laps: int) -> RaceState:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec(**{**RACE.__dict__, "laps": laps, "end_pct": 0.5}))
    return engine.snapshot()


def _element(value: object, unit: str | None = None) -> PlanElement:
    return PlanElement(value=value, unit=unit, provenance="derived", version=1, set_by="engine")


def test_the_stint_runs_to_the_planned_stop_while_one_is_planned():
    state = _state(2)  # the plan's stop is lap 5
    assert state.plan.next_stop_lap.value == 5
    assert stint_horizon(state) == {
        "available": True,
        "basis": "planned_stop",
        "laps_to_stint_end": 3,
    }
    assert stint_horizon(_state(5))["laps_to_stint_end"] == 0


def test_after_the_last_stop_the_stint_runs_to_the_flag():
    state = _state(8)  # stopped on lap 5, no stop left; lap 8 of 12
    assert state.plan.stop_count.value == 0
    assert stint_horizon(state) == {"available": True, "basis": "flag", "laps_to_stint_end": 4}


def test_no_plan_or_an_unplaced_stop_grounds_nothing():
    bare = RaceState()
    assert stint_horizon(bare)["available"] is False
    bare.player.lap = 3
    assert "No race plan" in stint_horizon(bare)["unavailable"]
    bare.plan = RacePlan(stop_count=_element(1))
    view = stint_horizon(bare)
    assert view["available"] is False and "laps_to_stint_end" not in view


def test_the_slice_carries_the_count_but_not_the_stop_lap():
    view = vehicle_engineer_slice(_state(2))
    assert view["stint"]["laps_to_stint_end"] == 3
    assert "next_stop_lap" not in view["plan"]
    assert field_unit("race_state.stint.laps_to_stint_end") == ("laps", "laps")


def test_r1_engineer_call_now_grounds_the_laps_but_not_the_projection():
    state = _state(2)
    facts = FactSet(typed=True)
    facts.add("race_state", vehicle_engineer_slice(state))
    facts.add("get_tyre_trend", R1_TYRE_TREND)
    # The stint length is a fact now...
    assert validate_output("Three laps left on this set, pressures holding.", "", facts).ok
    # ...the tool's per-lap trend grounds the rate in kPa...
    assert field_unit("get_tyre_trend.pressure_trend_per_lap.RF") == ("pressure", "kpa")
    # ...and the multiplied-out trend is still not one.
    report = validate_output(
        "RF climbing 4.2 kPa a lap.",
        "At 4.184 kPa/lap the RF gains roughly 42 kPa over the stint.",
        facts,
    )
    assert report.unsupported == ["42"]


def test_the_prompt_asks_for_a_rate_not_a_multiplied_trend():
    prompt = " ".join(VEHICLE_ENGINEER_PROMPT.split())
    assert "multiply it out before" not in prompt
    assert "never multiply it out" in prompt
    assert "laps_to_stint_end" in prompt
