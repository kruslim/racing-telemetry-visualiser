"""Deterministic pit-strategy arithmetic, tested independently of any agent.

The strategist is only as good as this module: everything it is allowed to say
about a stop comes out of here, so these are the numbers that have to be right.
"""

from __future__ import annotations

import pytest

from rtv.racestate import RaceStateEngine, replay_scenario
from rtv.racestate.models import (
    CarState,
    FlagPhase,
    FuelState,
    PlayerState,
    RaceState,
    StandingsState,
)
from rtv.racestate.scenario import ScenarioSpec
from rtv.racestate.strategy_math import (
    YELLOW_PIT_DISCOUNT,
    fuel_projection,
    simulate_pit_outcome,
    standings_around_player,
    stint_history,
    tyre_trend,
)


def _state(
    *,
    lap: int = 10,
    position: int = 4,
    gaps_behind: tuple[float, ...] = (2.0, 6.0, 30.0),
    gap_ahead: float | None = 5.0,
    level: float = 20.0,
    per_lap: float = 2.5,
    capacity: float = 60.0,
    laps_to_finish: float | None = 20.0,
    gap_basis: str | None = "lap_time_pct",
    phase: FlagPhase = FlagPhase.GREEN,
) -> RaceState:
    """A hand-built race state with exactly known gaps, for exact assertions."""
    cars = [
        CarState(idx=0, position=position - 1, gap_ahead=None, gap_behind=gap_ahead),
        CarState(idx=1, is_player=True, position=position, gap_ahead=gap_ahead),
    ]
    previous = 0.0
    for i, cumulative in enumerate(gaps_behind, start=2):
        cars.append(
            CarState(idx=i, position=position + i - 1, gap_ahead=cumulative - previous)
        )
        previous = cumulative
    state = RaceState(
        player=PlayerState(lap=lap, position=position),
        standings=StandingsState(cars=cars, player_idx=1, gap_basis=gap_basis),
        fuel=FuelState(
            level=level,
            per_lap=per_lap,
            capacity=capacity,
            laps_to_finish=laps_to_finish,
        ),
    )
    state.flags.phase = phase
    return state


# --------------------------------------------------------------------------
# rejoin position
# --------------------------------------------------------------------------
def test_cars_within_the_pit_loss_come_out_ahead():
    """25 s lost; the cars 2 s and 6 s back clear us, the one 30 s back does not."""
    outcome = simulate_pit_outcome(_state(), pit_lane_loss_s=25.0)
    assert outcome.grounded is True
    assert outcome.position_before == 4
    assert outcome.cars_clearing == [2, 3]
    assert outcome.positions_lost == 2
    assert outcome.rejoin_position == 6
    # We rejoin 25 s after our old track position, 6 s of which the last car
    # ahead had already used: 19 s behind them.
    assert outcome.gap_to_car_ahead_after == pytest.approx(19.0)


def test_a_stop_that_loses_nobody_keeps_the_position():
    outcome = simulate_pit_outcome(_state(gaps_behind=(40.0, 55.0)), pit_lane_loss_s=25.0)
    assert outcome.cars_clearing == []
    assert outcome.rejoin_position == 4
    # Nobody cleared us, so the car ahead is the same one, now 25 s further up.
    assert outcome.gap_to_car_ahead_after == pytest.approx(30.0)


def test_a_yellow_makes_the_same_stop_cheaper():
    """At 25 s three cars clear us; at the discounted 11.25 s only two do."""
    gaps = (2.0, 6.0, 15.0, 30.0)
    green = simulate_pit_outcome(_state(gaps_behind=gaps), pit_lane_loss_s=25.0)
    yellow = simulate_pit_outcome(
        _state(gaps_behind=gaps), pit_lane_loss_s=25.0, under_yellow=True
    )
    assert yellow.effective_loss_s == pytest.approx(25.0 * YELLOW_PIT_DISCOUNT)
    assert green.cars_clearing == [2, 3, 4]
    assert yellow.cars_clearing == [2, 3]
    assert yellow.rejoin_position == green.rejoin_position - 1
    assert any("yellow" in a for a in yellow.assumptions)


def test_the_flag_state_supplies_under_yellow_when_it_is_not_given():
    outcome = simulate_pit_outcome(_state(phase=FlagPhase.YELLOW), pit_lane_loss_s=25.0)
    assert outcome.under_yellow is True
    assert outcome.effective_loss_s == pytest.approx(25.0 * YELLOW_PIT_DISCOUNT)


def test_an_incomplete_gap_chain_stops_counting_and_says_so():
    """A hole in the gap chain cannot be summed past, so we stop and admit it."""
    state = _state(gaps_behind=(2.0, 6.0, 30.0))
    state.standings.cars[3].gap_ahead = None  # unknown gap two cars back
    outcome = simulate_pit_outcome(state, pit_lane_loss_s=25.0)
    assert outcome.cars_clearing == [2]
    assert any("incomplete" in a for a in outcome.assumptions)


# --------------------------------------------------------------------------
# grounded refusal
# --------------------------------------------------------------------------
def test_no_gap_basis_means_no_rejoin_position_is_projected():
    outcome = simulate_pit_outcome(_state(gap_basis=None), pit_lane_loss_s=25.0)
    assert outcome.rejoin_position is None
    assert outcome.cars_clearing == []
    # Fuel is still known, so the outcome is usable -- just not for position.
    assert outcome.fuel_to_add_l is not None
    assert any("no rejoin position" in a for a in outcome.assumptions)


def test_no_fuel_model_means_no_refuel_target():
    outcome = simulate_pit_outcome(_state(per_lap=0.0), pit_lane_loss_s=25.0)
    assert outcome.fuel_to_add_l is None
    assert outcome.reason == "Fuel consumption has not been learned yet."
    # Gaps are still known, so a rejoin estimate survives.
    assert outcome.rejoin_position == 6


def test_nothing_known_is_reported_as_ungrounded_not_as_zero():
    outcome = simulate_pit_outcome(
        _state(per_lap=0.0, gap_basis=None), pit_lane_loss_s=25.0
    )
    assert outcome.grounded is False
    assert outcome.reason
    assert outcome.rejoin_position is None and outcome.fuel_to_add_l is None


def test_no_lap_counter_refuses_outright():
    outcome = simulate_pit_outcome(_state(lap=None), pit_lane_loss_s=25.0)  # type: ignore[arg-type]
    assert outcome.grounded is False
    assert "lap counter" in (outcome.reason or "")


# --------------------------------------------------------------------------
# fuel arithmetic
# --------------------------------------------------------------------------
def test_refuel_target_covers_the_remaining_laps_plus_a_reserve():
    """20 laps at 2.5 L plus half a lap reserve = 51.25 L, against 20 L aboard."""
    outcome = simulate_pit_outcome(_state(), pit_lane_loss_s=25.0)
    assert outcome.laps_after_stop == pytest.approx(20.0)
    assert outcome.fuel_to_add_l == pytest.approx(51.25 - 20.0)
    assert outcome.covers_to_finish is True


def test_the_refuel_target_is_capped_at_tank_capacity():
    outcome = simulate_pit_outcome(
        _state(capacity=40.0, laps_to_finish=30.0), pit_lane_loss_s=25.0
    )
    assert outcome.fuel_to_add_l == pytest.approx(20.0)  # 40 L tank, 20 L aboard
    # 40 L at 2.5 L/lap covers 16 laps of the 30 still to run: a second stop.
    assert outcome.covers_to_finish is False
    assert any("capped at tank capacity" in a for a in outcome.assumptions)


def test_a_later_stop_burns_fuel_before_it_takes_any_on():
    """Delaying a stop moves fuel from the tank to the pump, not off the bill.

    With an uncapped tank the litres added are *identical*: every lap you wait
    burns exactly the lap you would otherwise have carried. The stop only gets
    cheaper once the tank cap stops binding, which is the case below.
    """
    now = simulate_pit_outcome(_state(), stop_lap=10, pit_lane_loss_s=25.0)
    later = simulate_pit_outcome(_state(), stop_lap=14, pit_lane_loss_s=25.0)
    assert later.laps_before_stop == 4.0
    assert later.fuel_at_stop_l == pytest.approx(20.0 - 4 * 2.5)
    assert later.laps_after_stop == pytest.approx(16.0)
    assert later.fuel_to_add_l == pytest.approx(now.fuel_to_add_l)

    # A tank too small to reach the flag *does* reward waiting: the same full
    # tank now has fewer laps left to cover.
    small = dict(capacity=40.0, laps_to_finish=18.0)
    early = simulate_pit_outcome(_state(**small), stop_lap=10, pit_lane_loss_s=25.0)
    late = simulate_pit_outcome(_state(**small), stop_lap=14, pit_lane_loss_s=25.0)
    # A full 40 L tank covers 16 laps: not the 18 left at lap 10, but easily the
    # 14 left at lap 14. Waiting turns a two-stopper into a one-stopper.
    assert early.covers_to_finish is False
    assert late.covers_to_finish is True
    assert late.fuel_to_add_l > early.fuel_to_add_l  # topping up a barer tank


def test_an_unknown_race_distance_gives_no_refuel_target():
    outcome = simulate_pit_outcome(_state(laps_to_finish=None), pit_lane_loss_s=25.0)
    assert outcome.fuel_to_add_l is None
    assert outcome.fuel_at_stop_l is not None  # what is aboard is still known
    assert any("Race distance unknown" in a for a in outcome.assumptions)


# --------------------------------------------------------------------------
# the projection helpers the tools wrap
# --------------------------------------------------------------------------
def test_projections_say_unavailable_rather_than_returning_zeros():
    state = RaceState()
    assert "unavailable" in fuel_projection(state)
    assert "unavailable" in tyre_trend(state)
    assert "unavailable" in standings_around_player(state)


def test_standings_window_is_centred_on_the_player():
    state = _state(gaps_behind=(2.0, 6.0, 30.0))
    out = standings_around_player(state, window=1)
    assert [c["idx"] for c in out["cars"]] == [0, 1, 2]
    assert next(c for c in out["cars"] if c["is_player"])["idx"] == 1


def test_stint_history_is_rebuilt_from_the_event_log():
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    history = stint_history(engine.snapshot(), engine.bus.history())

    assert history["current_stint"] == 2
    assert len(history["stints"]) == 2
    first_laps = [lap["lap"] for lap in history["stints"][0]["laps"]]
    assert first_laps == [1, 2, 3, 4, 5]
    # The stop is where the scenario scripts it.
    assert history["stints"][0]["end_lap"] == 5
    assert history["stints"][1]["start_lap"] == 6


def test_projections_over_the_scripted_race_match_the_engine():
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    state = engine.snapshot()
    projection = fuel_projection(state)

    assert projection["per_lap_l"] == pytest.approx(0.5, abs=1e-6)
    assert projection["capacity_l"] == pytest.approx(4.0, abs=1e-6)
    assert projection["laps_to_finish"] == pytest.approx(7.0)
    assert "unavailable" not in projection
