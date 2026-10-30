"""Lap-marker derivation."""

from __future__ import annotations

from rtv.ingest.normalize import LapTracker
from tests.helpers import generate_frames


def test_lap_tracker_detects_laps():
    tracker = LapTracker("s1")
    for values, tick, t, _lap in generate_frames(laps=3, ticks_per_lap=120):
        tracker.update(values, tick=tick, session_time=t)
    tracker.finalize(tick=10_000, session_time=10_000.0)

    # 3 laps generated -> 3 markers after finalize.
    assert len(tracker.laps) == 3
    assert [lap.lap for lap in tracker.laps] == [1, 2, 3]
    # Completed laps (2 and 3) pick up LapLastLapTime.
    assert tracker.laps[1].lap_time == 90.0


def test_lap_marker_has_start_and_end_ticks():
    tracker = LapTracker("s1")
    last_tick = 0
    for values, tick, t, _lap in generate_frames(laps=2, ticks_per_lap=50):
        tracker.update(values, tick=tick, session_time=t)
        last_tick = tick
    tracker.finalize(tick=last_tick, session_time=float(last_tick))
    for lap in tracker.laps:
        assert lap.start_tick is not None
        assert lap.end_tick is not None
        assert lap.end_tick >= lap.start_tick


def _tick(lap, t, *, llt=0.0, pit=False):
    return {"Lap": lap, "LapLastLapTime": llt, "OnPitRoad": pit, "LapDistPct": 0.0}


def _feed(tracker, rows):
    """rows: (session_time, values); tick = index."""
    done = []
    for i, (t, v) in enumerate(rows):
        lap = tracker.update(v, tick=i, session_time=t)
        if lap is not None:
            done.append(lap)
    return done


def test_the_official_lap_time_is_taken_when_iracing_publishes_it():
    """At the line LapLastLapTime still holds the previous lap's time; the real
    one arrives ~1.5 s later. Reading it at the crossing shifted every lap."""
    tracker = LapTracker("s1")
    rows = [
        (100.0, _tick(2, 100.0, llt=149.2)),
        (243.9, _tick(3, 243.9, llt=149.2)),  # crossed: still lap 2's predecessor
        (244.5, _tick(3, 244.5, llt=149.2)),
        (245.4, _tick(3, 245.4, llt=143.965)),  # published
    ]
    done = _feed(tracker, rows)
    assert [(lap.lap, lap.lap_time) for lap in done] == [(2, 143.965)]
    assert tracker.current_lap == 3


def test_session_time_is_used_when_no_official_time_arrives():
    tracker = LapTracker("s1")
    rows = [(10.0, _tick(1, 10.0)), (100.0, _tick(2, 100.0)), (106.0, _tick(2, 106.0))]
    done = _feed(tracker, rows)
    assert [(lap.lap, lap.lap_time) for lap in done] == [(1, 90.0)]


def test_in_and_out_laps_come_from_pit_road_transitions():
    tracker = LapTracker("s1")
    rows = [
        (0.0, _tick(5, 0.0)),
        (100.0, _tick(5, 100.0, pit=True)),  # entered the pit lane on lap 5
        (110.0, _tick(6, 110.0, pit=True)),  # crossed the line in the pit lane
        (130.0, _tick(6, 130.0)),  # left the pit lane on lap 6
        (250.0, _tick(7, 250.0)),
    ]
    _feed(tracker, rows)
    tracker.finalize()
    flags = {lap.lap: (lap.is_in_lap, lap.is_out_lap) for lap in tracker.laps}
    assert flags[5] == (True, False)
    assert flags[6] == (False, True)
    assert flags[7] == (False, False)


def test_pre_start_and_unfinished_segments_are_not_valid_laps():
    tracker = LapTracker("s1")
    rows = [(0.0, _tick(0, 0.0)), (59.0, _tick(1, 59.0)), (200.0, _tick(2, 200.0)),
            (233.0, _tick(2, 233.0))]
    _feed(tracker, rows)
    tracker.finalize()
    valid = {lap.lap: lap.is_valid for lap in tracker.laps}
    assert valid == {0: False, 1: True, 2: False}
    # finalize() closes the last segment at the last tick seen, not at zero.
    assert tracker.laps[-1].lap_time == 33.0
