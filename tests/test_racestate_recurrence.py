"""Stage-3 deterministic aggregation: recurrence, tyre bands, health, traffic.

These are the events that wake the vehicle engineer, the spotter and the coach,
and the whole point of them is that they fire *instead of* a hundred singles. So
the tests are mostly about restraint: the same lockup at the same corner three
times is one event, a lockup and a different corner is none, and a race where
nothing is wrong produces nothing at all.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from rtv.racestate import RaceStateEngine
from rtv.racestate import detectors as det
from rtv.racestate.models import (
    CarHealthState,
    CarState,
    StandingsState,
    TyreState,
)
from rtv.racestate.scenario import (
    ScenarioSpec,
    scenario_catalog,
    scenario_frames,
    scenario_session_info,
)

CFG = det.DEFAULT_CONFIG


def run(spec: ScenarioSpec, config=CFG, *, session_info: bool = True) -> RaceStateEngine:
    catalog = scenario_catalog(spec)
    engine = RaceStateEngine(source="replay", config=config)
    engine.bind_catalog(catalog, session_id="recurrence")
    if session_info:
        engine.set_session_info(scenario_session_info(spec))
    for frame in scenario_frames(spec):
        engine.on_frame(frame, catalog)
    return engine


def keys(engine: RaceStateEngine) -> list[str]:
    return [e.key for e in engine.bus.history()]


# --------------------------------------------------------------------------
# corner labelling
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pct", "expected"),
    [(0.0, "C00"), (0.049, "C00"), (0.05, "C01"), (0.4033, "C08"), (0.999, "C19")],
)
def test_corner_label_buckets_the_lap(pct, expected):
    assert det.corner_label(pct, 20) == expected


def test_corner_label_wraps_and_refuses_unknown_distance():
    assert det.corner_label(1.05, 20) == det.corner_label(0.05, 20)
    assert det.corner_label(None, 20) is None


# --------------------------------------------------------------------------
# CornerRecurrence
# --------------------------------------------------------------------------
def _record(tracker, lap, pct=0.40, issue="lockup", slip=0.4):
    return tracker.record(
        issue,
        lap=lap,
        session_time=float(lap) * 10.0,
        lap_dist_pct=pct,
        payload={"slip": slip, "speed": 40.0},
    )


def test_the_third_repeat_at_one_corner_is_the_finding():
    tracker = det.CornerRecurrence(CFG)
    assert _record(tracker, 1) is None
    assert _record(tracker, 2) is None
    finding = _record(tracker, 3)
    assert finding is not None
    assert finding["issue"] == "lockup"
    assert finding["corner"] == "C08"
    assert finding["occurrences"] == 3
    assert finding["laps"] == [1, 2, 3]
    assert finding["worst_slip"] == 0.4
    assert finding["mean_speed"] == 40.0


def test_repeats_at_different_corners_are_not_a_pattern():
    tracker = det.CornerRecurrence(CFG)
    for lap, pct in ((1, 0.10), (2, 0.40), (3, 0.70)):
        assert _record(tracker, lap, pct) is None


def test_repeats_spread_beyond_the_window_are_not_a_pattern():
    tracker = det.CornerRecurrence(CFG)  # window is 5 laps
    assert _record(tracker, 1) is None
    assert _record(tracker, 2) is None
    assert _record(tracker, 20) is None


def test_different_issues_at_one_corner_are_counted_separately():
    tracker = det.CornerRecurrence(CFG)
    _record(tracker, 1, issue="lockup")
    _record(tracker, 1, issue="wheelspin")
    _record(tracker, 2, issue="lockup")
    _record(tracker, 2, issue="wheelspin")
    assert _record(tracker, 3, issue="lockup") is not None
    assert _record(tracker, 4, issue="wheelspin") is not None


def test_a_pattern_is_reported_once_not_once_per_lap():
    """A driver locking up every lap gets one call, then one more at six."""
    tracker = det.CornerRecurrence(CFG)
    fired = [bool(_record(tracker, lap)) for lap in range(1, 8)]
    assert fired == [False, False, True, False, False, True, False]


def test_recurrence_needs_a_lap_distance_to_mean_the_same_corner():
    tracker = det.CornerRecurrence(CFG)
    for lap in (1, 2, 3):
        assert tracker.record("lockup", lap=lap, session_time=0.0, lap_dist_pct=None) is None


def test_reset_clears_the_pattern():
    tracker = det.CornerRecurrence(CFG)
    _record(tracker, 1)
    _record(tracker, 2)
    tracker.reset()
    assert _record(tracker, 3) is None


# --------------------------------------------------------------------------
# tyre bands
# --------------------------------------------------------------------------
def _tyres(**kw) -> TyreState:
    base = {
        "temps": {"LF": 90.0, "RF": 91.0, "LR": 92.0, "RR": 93.0},
        "pressures": {"LF": 165.0, "RF": 165.0, "LR": 160.0, "RR": 160.0},
        "stint_laps": 5,
    }
    base.update(kw)
    return TyreState(**base)


def test_a_healthy_set_is_not_a_finding():
    assert det.detect_tyre_condition(_tyres(), CFG) is None


def test_absolute_tyre_bands_are_off_until_an_operator_sets_them():
    """A 'normal' tyre temperature is a per-car number we were never told."""
    assert det.detect_tyre_condition(_tyres(temps={"LF": 400.0}), CFG) is None
    hot = replace(CFG, tyre_temp_max_c=110.0)
    finding = det.detect_tyre_condition(_tyres(temps={"LF": 400.0}), hot)
    assert finding["measure"] == "tyre_temp"
    assert finding["corner"] == "LF"
    assert finding["threshold"] == 110.0


def test_a_sustained_temperature_climb_is_a_finding():
    finding = det.detect_tyre_condition(
        _tyres(temp_trend={"LF": 4.5, "RF": 1.0}), CFG
    )
    assert finding["measure"] == "tyre_temp_trend"
    assert finding["corner"] == "LF"
    assert finding["value"] == 4.5


def test_a_trend_over_too_few_stint_laps_is_not_yet_a_trend():
    assert (
        det.detect_tyre_condition(_tyres(temp_trend={"LF": 4.5}, stint_laps=1), CFG)
        is None
    )


def test_pressure_drift_is_reported_in_its_own_units():
    finding = det.detect_tyre_condition(
        _tyres(pressure_trend={"RR": -3.0}), CFG
    )
    assert finding["measure"] == "tyre_pressure_trend"
    assert finding["unit"] == "kPa/lap"


def test_axle_imbalance_needs_no_per_car_knowledge():
    finding = det.detect_tyre_condition(
        _tyres(temps={"LF": 120.0, "RF": 90.0, "LR": 92.0, "RR": 93.0}), CFG
    )
    assert finding["measure"] == "tyre_axle_imbalance"
    assert finding["axle"] == "front"
    assert finding["corner"] == "LF"
    assert finding["value"] == 30.0


# --------------------------------------------------------------------------
# car health
# --------------------------------------------------------------------------
def test_a_healthy_engine_is_not_a_finding():
    assert det.detect_car_health(CarHealthState(oil_temp=95.0, water_temp=88.0), CFG) is None


def test_the_limiter_bits_are_normal_driving_not_faults():
    health = CarHealthState(engine_warnings=["pit_speed_limiter", "rev_limiter_active"])
    assert det.detect_car_health(health, CFG) is None


def test_the_sims_own_warning_bit_outranks_our_thresholds():
    health = CarHealthState(oil_temp=200.0, engine_warnings=["oil_pressure_warning"])
    finding = det.detect_car_health(health, CFG)
    assert finding["measure"] == "engine_warning"
    assert finding["warnings"] == ["oil_pressure_warning"]
    assert finding["critical"] is True


def test_a_tow_is_reported_as_damage():
    finding = det.detect_car_health(CarHealthState(tow_time=42.0), CFG)
    assert finding["measure"] == "tow"
    assert finding["critical"] is True


def test_a_temperature_over_the_configured_limit_is_our_judgement_not_the_cars():
    finding = det.detect_car_health(CarHealthState(oil_temp=138.4), CFG)
    assert finding["measure"] == "oil_temp"
    assert finding["threshold"] == CFG.oil_temp_max_c
    assert finding["critical"] is False


# --------------------------------------------------------------------------
# closing traffic
# --------------------------------------------------------------------------
def _standings(gaps: dict[int, float], *, basis: str | None = "lap_time_pct", pit=()):
    cars = [CarState(idx=0, is_player=True, position=4)]
    cars += [
        CarState(idx=i, position=4 + i, gap_to_player=g, on_pit_road=i in pit)
        for i, g in gaps.items()
    ]
    return StandingsState(cars=cars, player_idx=0, gap_basis=basis)


def test_a_car_closing_fast_enough_is_reported():
    finding = det.detect_closing_traffic(
        _standings({1: -0.6}), {1: -1.0}, dt=1.0, cfg=CFG
    )
    assert finding["car_idx"] == 1
    assert finding["side"] == "behind"
    assert finding["gap"] == 0.6
    assert finding["closing_rate_s_per_s"] == pytest.approx(0.4)


def test_a_steady_gap_is_not_news():
    assert det.detect_closing_traffic(_standings({1: -0.6}), {1: -0.6}, 1.0, CFG) is None


def test_a_car_that_is_close_but_dropping_back_is_not_news():
    assert det.detect_closing_traffic(_standings({1: -0.9}), {1: -0.6}, 1.0, CFG) is None


def test_a_car_beyond_the_threshold_is_not_close():
    assert det.detect_closing_traffic(_standings({1: -4.0}), {1: -6.0}, 1.0, CFG) is None


def test_no_gap_basis_means_no_traffic_call():
    assert (
        det.detect_closing_traffic(_standings({1: -0.6}, basis=None), {1: -1.0}, 1.0, CFG)
        is None
    )


def test_a_car_in_the_pits_is_not_traffic():
    assert (
        det.detect_closing_traffic(_standings({1: -0.6}, pit=(1,)), {1: -1.0}, 1.0, CFG)
        is None
    )


# --------------------------------------------------------------------------
# the engine end to end
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def quiet() -> RaceStateEngine:
    """The ground-truth scenario: nothing is wrong, so nothing new is said."""
    return run(ScenarioSpec())


def test_a_clean_race_emits_none_of_the_stage_3_events(quiet):
    fired = set(keys(quiet))
    assert not fired & {
        "recurring_issue",
        "tyre_out_of_band",
        "car_health_warning",
        "traffic_close",
    }


def test_a_repeated_lockup_at_one_corner_becomes_exactly_one_finding():
    engine = run(ScenarioSpec(lockup_laps=(1, 2)))
    events = [e for e in engine.bus.history() if e.key == "recurring_issue"]
    assert len(keys(engine)) > 0
    assert len([k for k in keys(engine) if k == "lockup"]) == 3
    assert len(events) == 1
    payload = events[0].payload
    assert payload["issue"] == "lockup"
    assert payload["corner"] == "C08"
    assert payload["laps"] == [1, 2, 4]  # lap 3 is under caution, so it cannot


def test_the_aggregate_fires_after_the_singles_that_caused_it():
    engine = run(ScenarioSpec(lockup_laps=(1, 2)))
    history = engine.bus.history()
    singles = [i for i, e in enumerate(history) if e.key == "lockup"]
    aggregate = next(i for i, e in enumerate(history) if e.key == "recurring_issue")
    assert aggregate > singles[2]


def test_a_tyre_trend_out_of_band_fires_once_per_stint():
    engine = run(ScenarioSpec(), replace(CFG, tyre_temp_trend_c_per_lap=1.0))
    events = [e for e in engine.bus.history() if e.key == "tyre_out_of_band"]
    assert len(events) == 1
    assert events[0].payload["measure"] == "tyre_temp_trend"
    assert events[0].payload["threshold"] == 1.0


def test_a_gap_basis_change_is_not_a_car_closing():
    """Gaps all move at once when a lap time first becomes known. Nobody moved."""
    engine = run(ScenarioSpec())
    assert "traffic_close" not in keys(engine)


# --------------------------------------------------------------------------
# the setup snapshot
# --------------------------------------------------------------------------
def test_setup_snapshot_reads_the_session_info_document(quiet):
    setup = quiet.setup_snapshot()
    assert setup["available"] is True
    assert "Chassis" in setup["setup"]
    assert setup["car"]["DriverCarFuelMaxLtr"] == ScenarioSpec().fuel_capacity


def test_setup_snapshot_says_so_when_there_is_no_document():
    engine = run(ScenarioSpec(), session_info=False)
    setup = engine.setup_snapshot()
    assert setup["available"] is False
    assert "unavailable" in setup


def test_naming_the_session_does_not_reset_the_race():
    """The live path learns the session id mid-race; it must cost nothing."""
    engine = run(ScenarioSpec())
    before = engine.snapshot()
    engine.set_session_id("live-1234")
    after = engine.snapshot()
    assert after.session.session_id == "live-1234"
    assert after.fuel.per_lap == before.fuel.per_lap
    assert after.player.stint == before.player.stint
    assert len(engine.bus.history()) == len(keys(engine))


def test_the_session_id_survives_a_catalog_rebind():
    spec = ScenarioSpec()
    catalog = scenario_catalog(spec)
    engine = RaceStateEngine(source="replay")
    engine.set_session_id("live-1234")
    engine.bind_catalog(catalog, session_id=engine.snapshot().session.session_id)
    assert engine.snapshot().session.session_id == "live-1234"


def test_setup_snapshot_says_so_when_the_document_has_no_setup():
    engine = run(ScenarioSpec(), session_info=False)
    engine.set_session_info({"WeekendInfo": {"TrackLength": "4.00 km"}})
    setup = engine.setup_snapshot()
    assert setup["available"] is False
    assert "CarSetup" in setup["unavailable"]
