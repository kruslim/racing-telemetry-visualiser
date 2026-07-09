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
