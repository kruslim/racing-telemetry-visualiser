"""Engine-level tests driven by the scripted synthetic race (fully offline)."""

from __future__ import annotations

import pytest

from rtv.racestate import RaceStateEngine, replay_scenario
from rtv.racestate.models import EventType, FlagPhase
from rtv.racestate.scenario import ScenarioSpec, scenario_catalog, scenario_frames

#: The scripted milestones, in the order the scenario stages them.
EXPECTED_SEQUENCE = [
    "flag_change:yellow",
    "lockup",
    "pit_window_open",
    "pit_entry",
    "pit_exit",
]
TYRE_CHANNELS = tuple(
    f"{c}{suffix}" for c in ("LF", "RF", "LR", "RR") for suffix in ("tempCM", "pressure")
)


def run_scenario(spec: ScenarioSpec | None = None) -> RaceStateEngine:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, spec)
    return engine


def keys(engine: RaceStateEngine) -> list[str]:
    return [e.key for e in engine.bus.history()]


@pytest.fixture(scope="module")
def engine() -> RaceStateEngine:
    return run_scenario()


# --------------------------------------------------------------------------
# the scripted sequence
# --------------------------------------------------------------------------
def test_scenario_emits_the_expected_milestones_in_order(engine):
    milestones = [k for k in keys(engine) if k in set(EXPECTED_SEQUENCE)]
    assert milestones == EXPECTED_SEQUENCE


def test_scenario_detects_every_lap_and_the_yellow_period(engine):
    laps = [e for e in engine.bus.history() if e.event_type is EventType.LAP_COMPLETED]
    assert [e.payload["lap"] for e in laps] == [1, 2, 3, 4, 5]
    # Lap 3 carried the full-course yellow, so it is not a clean green lap.
    assert [e.payload["green"] for e in laps] == [True, True, False, True, False]


def test_engine_ends_green_after_the_yellow_clears(engine):
    assert engine.snapshot().flags.phase is FlagPhase.GREEN


def test_pit_stop_starts_a_new_stint(engine):
    player = engine.snapshot().player
    assert player.stint == 2
    assert player.stint_start_lap == 6


def test_self_timing_stays_far_inside_the_frame_budget(engine):
    metrics = engine.snapshot().metrics
    assert metrics.frames > 3000
    # The hot loop targets well under 16 ms/frame; assert an order of magnitude.
    assert metrics.avg_update_ms < 5.0
    assert metrics.max_update_ms < 16.0


# --------------------------------------------------------------------------
# fuel model
# --------------------------------------------------------------------------
def test_fuel_consumption_is_learned_from_green_laps(engine):
    fuel = engine.snapshot().fuel
    assert fuel.per_lap == pytest.approx(0.5, abs=1e-6)
    assert fuel.per_lap_std == pytest.approx(0.0, abs=1e-6)
    assert fuel.capacity == pytest.approx(4.0, abs=1e-6)


def test_pit_window_lands_on_the_derived_lap(engine):
    """4.0 L tank / 0.5 L per lap = 8 laps; a 12-lap race from 3.0 L opens at lap 5."""
    opens = [e for e in engine.bus.history() if e.event_type is EventType.PIT_WINDOW_OPEN]
    assert len(opens) == 1
    payload = opens[0].payload
    assert payload["lap"] == 5
    assert payload["earliest_lap"] == 5
    assert payload["latest_lap"] == 7
    assert payload["laps_remaining"] == pytest.approx(2.0, abs=1e-6)


def test_fuel_margin_is_consistent_with_the_tank_after_the_stop(engine):
    fuel = engine.snapshot().fuel
    # 7 laps left at 0.5 L each = 3.5 L needed against a nearly full 4.0 L tank.
    assert fuel.laps_to_finish == pytest.approx(7.0)
    assert fuel.fuel_to_finish == pytest.approx(3.5, abs=1e-6)
    assert fuel.margin_l == pytest.approx(fuel.level - 3.5, abs=1e-3)
    assert fuel.margin_laps > 0
    # A stop is no longer required, so the window must not read open.
    assert fuel.window_open is False


def test_fuel_critical_fires_once_before_the_stop(engine):
    critical = [e for e in engine.bus.history() if e.event_type is EventType.FUEL_CRITICAL]
    assert len(critical) == 1
    assert critical[0].payload["laps_remaining"] < 1.5


# --------------------------------------------------------------------------
# standings / gaps
# --------------------------------------------------------------------------
def test_gaps_follow_the_grid_offsets_and_the_known_lap_time():
    """Grid offsets are in laps; a 10 s lap turns 0.03 laps into exactly 0.3 s."""
    spec = ScenarioSpec()
    catalog = scenario_catalog(spec)
    engine = RaceStateEngine(source="replay")
    engine.bind_catalog(catalog, session_id="gaps")
    for frame in scenario_frames(spec):
        engine.on_frame(frame, catalog)
        if frame.lap == 4 and frame.values["LapDistPct"] == pytest.approx(0.5):
            break

    standings = engine.snapshot().standings
    assert standings.gap_basis == "lap_time_pct"
    assert standings.reference_lap_time == pytest.approx(10.0)

    order = [c.idx for c in standings.cars]
    assert order == [7, 5, 2, 1, 0, 3, 4, 6]  # sorted by grid offset, leader first

    player = next(c for c in standings.cars if c.is_player)
    assert player.position == 5
    assert player.gap_to_player == pytest.approx(0.0)
    assert player.gap_ahead == pytest.approx(0.3, abs=1e-3)
    assert player.gap_behind == pytest.approx(0.3, abs=1e-3)

    by_idx = {c.idx: c for c in standings.cars}
    assert by_idx[7].gap_to_player == pytest.approx(2.0, abs=1e-3)  # +0.20 laps
    assert by_idx[6].gap_to_player == pytest.approx(-1.2, abs=1e-3)  # -0.12 laps
    assert by_idx[7].gap_ahead is None  # nobody ahead of the leader
    assert by_idx[6].gap_behind is None  # nobody behind the tail


def test_gaps_are_none_before_any_lap_time_is_known():
    """No lap time and no track length => no defensible gap, so we report none."""
    spec = ScenarioSpec()
    catalog = scenario_catalog(spec)
    engine = RaceStateEngine(source="replay")
    engine.bind_catalog(catalog, session_id="early")
    for i, frame in enumerate(scenario_frames(spec)):
        engine.on_frame(frame, catalog)
        if i >= 60:  # still on lap 1, no lap time recorded yet
            break
    standings = engine.snapshot().standings
    assert standings.gap_basis is None
    assert all(c.gap_ahead is None and c.gap_to_player is None for c in standings.cars)


# --------------------------------------------------------------------------
# replay determinism
# --------------------------------------------------------------------------
def _signature(engine: RaceStateEngine) -> list[tuple]:
    return [
        (e.event_type, e.tick, e.session_time, e.lap, e.severity, e.state_version,
         tuple(sorted(e.payload.items(), key=lambda kv: kv[0])))
        for e in engine.bus.history()
    ]


def test_replaying_the_same_session_twice_gives_an_identical_event_log():
    assert _signature(run_scenario()) == _signature(run_scenario())


def test_replay_is_independent_of_wall_clock_speed():
    """Real-time pacing must change only *when* frames arrive, not the result."""
    fast = run_scenario()
    paced = RaceStateEngine(source="replay")
    replay_scenario(paced, ScenarioSpec(), speed=5000.0)
    assert _signature(fast) == _signature(paced)


def test_engine_reset_clears_state_and_replays_cleanly():
    engine = run_scenario()
    first = _signature(engine)
    engine.reset()
    assert engine.snapshot().version == 0
    assert engine.snapshot().player.stint == 1
    engine.bus.clear_history()
    replay_scenario(engine)
    assert _signature(engine) == first


# --------------------------------------------------------------------------
# missing-channel degradation
# --------------------------------------------------------------------------
def test_stripping_tyre_channels_degrades_without_crashing():
    engine = run_scenario(ScenarioSpec(exclude=TYRE_CHANNELS))
    state = engine.snapshot()

    assert state.capabilities.tyres is False
    assert state.tyres.temps == {}
    assert state.tyres.pressures == {}
    assert state.tyres.temp_trend == {}
    # The absent channels are named, so an operator can see *why* it is empty.
    assert set(TYRE_CHANNELS).issubset(set(state.capabilities.missing))
    # Everything else still works: the scripted milestones are unaffected.
    assert [k for k in keys(engine) if k in set(EXPECTED_SEQUENCE)] == EXPECTED_SEQUENCE


def test_stripping_fuel_channels_leaves_fuel_unknown_not_invented():
    engine = run_scenario(ScenarioSpec(exclude=("FuelLevel", "FuelLevelPct")))
    state = engine.snapshot()

    assert state.capabilities.fuel is False
    assert state.fuel.level is None
    assert state.fuel.per_lap is None
    assert state.fuel.laps_remaining is None
    assert state.fuel.window_open is False
    # No fuel data => no fuel advice, rather than a guessed window.
    assert "pit_window_open" not in keys(engine)
    assert "fuel_critical" not in keys(engine)


def test_stripping_wheel_channels_disables_only_the_slip_detectors():
    wheels = ("LFspeed", "RFspeed", "LRspeed", "RRspeed")
    engine = run_scenario(ScenarioSpec(exclude=wheels))
    state = engine.snapshot()

    assert state.capabilities.lockup is False
    assert state.capabilities.wheelspin is False
    assert "lockup" not in keys(engine)
    # The rest of the sequence survives.
    assert "pit_entry" in keys(engine) and "flag_change:yellow" in keys(engine)


def test_stripping_standings_channels_leaves_the_field_empty():
    engine = run_scenario(ScenarioSpec(exclude=("CarIdxLapDistPct",)))
    state = engine.snapshot()

    assert state.capabilities.standings is False
    assert state.standings.cars == []
    assert state.standings.gap_basis is None
    assert "CarIdxLapDistPct" in state.capabilities.missing


def test_session_info_supplies_track_length():
    engine = run_scenario()
    session = engine.snapshot().session
    assert session.lap_length_m == pytest.approx(4000.0)
    assert session.track_name == "Synthetic Circuit"
    assert session.source == "replay"
