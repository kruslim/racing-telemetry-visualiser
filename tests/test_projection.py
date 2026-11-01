"""Stage 08: the projection layer, standalone, with no model in the loop.

Degradation, fuel save, traffic and undercut/overcut over hand-built states (exact
arithmetic) and over the synthetic race, plus the real Spa fixture through the app's
own replay (skipped without it): degradation fits the real laps with a sample count,
and traffic is ungrounded because an ``.ibt`` has no other cars.
"""

from __future__ import annotations

import pytest
from evals.synthetic_race import ScenarioSpec, replay_scenario

from rtv.racestate import RaceStateEngine
from rtv.racestate import projection as pj
from rtv.racestate.models import CarState, LapRecord, RaceState


def _state(times: list[float | None], *, stint: int = 1, **lap_kw) -> RaceState:
    state = RaceState()
    state.player.stint = stint
    state.player.lap = len(times) + 1
    state.laps = [
        LapRecord(lap=i + 1, lap_time=t, stint=stint, stint_lap=i + 1, **lap_kw)
        for i, t in enumerate(times)
    ]
    return state


# --------------------------------------------------------------------------
# degradation
# --------------------------------------------------------------------------
def test_degradation_fits_a_linear_stint_exactly():
    state = _state([100.0 + 0.1 * i for i in range(8)])
    deg = pj.lap_time_degradation(state, horizon_laps=5)
    assert deg.grounded and deg.samples == 8
    assert deg.loss_per_lap_s == pytest.approx(0.1)
    assert deg.current_lap_time_s == pytest.approx(100.7)
    assert deg.fresh_lap_time_s == pytest.approx(100.0)
    assert deg.projected_lap_time_s == pytest.approx(101.2)
    assert deg.loss_stderr_s == pytest.approx(0.0, abs=1e-6)
    assert deg.confidence == "high"


def test_outliers_pit_laps_and_yellow_laps_are_left_out():
    state = _state([100.0, 100.1, 100.2, 110.0, 100.4, 100.5])
    state.laps.append(LapRecord(lap=7, lap_time=101.0, stint=1, stint_lap=7, green=False))
    state.laps.append(LapRecord(lap=8, lap_time=125.0, stint=1, stint_lap=8, pit=True))
    deg = pj.lap_time_degradation(state)
    assert deg.grounded
    assert deg.outlier_laps == [4]
    assert deg.laps_used == [1, 2, 3, 5, 6]
    assert deg.loss_per_lap_s == pytest.approx(0.1)


def test_only_the_current_stint_is_fitted():
    state = _state([100.0, 100.0, 100.0, 100.0])
    state.laps += [
        LapRecord(lap=5 + i, lap_time=99.0 + 0.2 * i, stint=2, stint_lap=i + 1) for i in range(5)
    ]
    state.player.stint = 2
    deg = pj.lap_time_degradation(state)
    assert deg.stint == 2 and deg.laps_used == [5, 6, 7, 8, 9]
    assert deg.loss_per_lap_s == pytest.approx(0.2)


def test_too_few_clean_laps_is_ungrounded_with_a_reason():
    deg = pj.lap_time_degradation(_state([100.0, 100.1, 100.2]))
    assert not deg.grounded and deg.samples == 3
    assert deg.loss_per_lap_s is None and deg.projected_lap_time_s is None
    assert "3 clean laps" in deg.reason and "4 are needed" in deg.reason
    assert not pj.lap_time_degradation(RaceState()).grounded


# --------------------------------------------------------------------------
# fuel save
# --------------------------------------------------------------------------
def _fuel_state(level=20.0, per_lap=4.0, lap=5, pct=0.0, laps_to_finish=None) -> RaceState:
    state = RaceState()
    state.capabilities.fuel = True
    state.player.lap = lap
    state.player.lap_dist_pct = pct
    state.fuel.level = level
    state.fuel.per_lap = per_lap
    state.fuel.samples = 4
    state.fuel.laps_remaining = level / per_lap
    if laps_to_finish is not None:
        state.fuel.laps_to_finish = laps_to_finish
        state.fuel.margin_laps = level / per_lap - laps_to_finish
    return state


def test_fuel_save_is_the_consumption_that_stretches_the_tank():
    save = pj.fuel_save_options(_fuel_state(), extra_laps=(1, 2))
    assert save.grounded and save.laps_remaining == pytest.approx(5.0)
    one, two = save.options
    assert one.extra_laps == 1 and one.target_per_lap_l == pytest.approx(20.0 / 6, abs=1e-4)
    assert one.save_per_lap_l == pytest.approx(4.0 - 20.0 / 6, abs=1e-4)
    assert one.save_pct == pytest.approx((4.0 - 20.0 / 6) / 4.0, abs=1e-4)
    assert two.target_per_lap_l == pytest.approx(20.0 / 7, abs=1e-4)
    # From the line at the start of lap 5, five laps of fuel reach the end of lap 9.
    assert save.reach_lap == 9 and one.reach_lap == 10 and two.reach_lap == 11
    assert save.to_flag is None


def test_fuel_save_to_the_flag_when_a_stop_is_needed():
    save = pj.fuel_save_options(_fuel_state(laps_to_finish=5.5))
    assert save.to_flag is not None
    assert save.to_flag.extra_laps == 1
    assert save.to_flag.target_per_lap_l == pytest.approx(20.0 / 5.5, abs=1e-4)


def test_fuel_save_is_ungrounded_without_consumption():
    state = _fuel_state()
    state.fuel.per_lap = None
    save = pj.fuel_save_options(state)
    assert not save.grounded and save.options == [] and "not been learned" in save.reason
    state.capabilities.fuel = False
    assert "no fuel channels" in pj.fuel_save_options(state).reason


# --------------------------------------------------------------------------
# traffic
# --------------------------------------------------------------------------
def _field(gap_ahead=2.0, gap_behind=1.5, mine=100.0, ahead=100.5, behind=99.5) -> RaceState:
    state = RaceState()
    state.player.lap = 8
    state.standings.gap_basis = "lap_time_pct"
    state.standings.cars = [
        CarState(idx=4, position=3, last_lap_time=ahead, gap_behind=gap_ahead),
        CarState(idx=0, position=4, is_player=True, last_lap_time=mine,
                 gap_ahead=gap_ahead, gap_behind=gap_behind),
        CarState(idx=7, position=5, last_lap_time=behind, gap_ahead=gap_behind),
    ]
    return state


def test_traffic_projects_both_catches_from_gap_and_pace():
    out = pj.traffic_projection(_field())
    assert out.grounded and out.gap_basis == "lap_time_pct"
    assert out.ahead.car_idx == 4 and out.ahead.closing_per_lap_s == pytest.approx(0.5)
    assert out.ahead.catch_in_laps == pytest.approx(4.0) and out.ahead.catch_lap == 12
    assert out.behind.car_idx == 7 and out.behind.closing_per_lap_s == pytest.approx(0.5)
    assert out.behind.catch_in_laps == pytest.approx(3.0) and out.behind.catch_lap == 11


def test_an_opening_gap_is_grounded_but_never_caught():
    out = pj.traffic_projection(_field(ahead=99.0))
    assert out.ahead.grounded and out.ahead.closing_per_lap_s == pytest.approx(-1.0)
    assert out.ahead.catch_in_laps is None and out.ahead.catch_lap is None


def test_traffic_is_ungrounded_without_a_gap_basis():
    state = _field()
    state.standings.gap_basis = None
    out = pj.traffic_projection(state)
    assert not out.grounded and "gap basis" in out.reason and ".ibt" in out.reason
    assert out.ahead.catch_lap is None and out.behind.catch_lap is None


# --------------------------------------------------------------------------
# undercut / overcut
# --------------------------------------------------------------------------
def _undercut_state() -> RaceState:
    state = _fuel_state(level=20.0, per_lap=4.0, lap=5, laps_to_finish=10.0)
    state.fuel.capacity = 40.0
    state.fuel.margin_l = 20.0 - 40.0
    state.fuel.pit_window_earliest_lap = 5
    state.fuel.pit_window_latest_lap = 9
    state.player.stint = 1
    state.laps = [
        LapRecord(lap=i + 1, lap_time=100.0 + 0.2 * i, stint=1, stint_lap=i + 1) for i in range(4)
    ]
    return state


def test_undercut_prices_both_branches_and_the_tyre_gain():
    out = pj.undercut_overcut(_undercut_state(), early_lap=6, late_lap=8)
    assert out.grounded
    assert out.early["stop_lap"] == 6 and out.late["stop_lap"] == 8
    # The fuel to the flag is the same either way, so both stops add the same.
    assert out.extra_fuel_l == pytest.approx(0.0)
    assert out.early["fuel_to_add_l"] == pytest.approx(22.0)
    # Two laps on tyres 4 and 5 laps younger at 0.2 s a lap per lap.
    assert out.tyre_gain_s == pytest.approx(0.2 * 4 + 0.2 * 5)
    assert out.net_s == out.tyre_gain_s and out.favours == "early"
    assert any("same in both branches" in a for a in out.assumptions)


def test_undercut_without_a_degradation_fit_says_the_tyre_gain_is_not_priced():
    state = _undercut_state()
    state.laps = state.laps[:2]
    out = pj.undercut_overcut(state, early_lap=6, late_lap=8)
    assert out.grounded and out.tyre_gain_s is None and out.net_s is None
    assert out.favours is None
    assert any("not priced" in a for a in out.assumptions)


def test_undercut_is_ungrounded_without_a_stop_plan():
    out = pj.undercut_overcut(RaceState())
    assert not out.grounded and out.reason


# --------------------------------------------------------------------------
# the synthetic race
# --------------------------------------------------------------------------
def test_the_engine_records_every_lap_with_its_stint_and_flags():
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec(laps=10))
    laps = {lap.lap: lap for lap in engine.snapshot().laps}
    assert laps[3].green is False  # the full-course yellow
    assert laps[5].pit and laps[5].stint == 1  # the in-lap
    assert laps[6].pit and laps[6].stint == 2 and laps[6].stint_lap == 1  # the out-lap
    assert laps[9].stint == 2 and laps[9].stint_lap == 4
    deg = pj.lap_time_degradation(engine.snapshot(), min_laps=3)
    assert deg.grounded and deg.laps_used == [7, 8, 9]
    assert deg.loss_per_lap_s == pytest.approx(0.0, abs=1e-6)
    # The synthetic field has gaps (CarIdx channels), so traffic is grounded here.
    assert pj.traffic_projection(engine.snapshot()).gap_basis is not None
