"""Unit tests for the pure detectors and the event bus (no store, no iRacing)."""

from __future__ import annotations

import pytest

from rtv.racestate import detectors as det
from rtv.racestate.models import (
    CarState,
    EventType,
    FlagPhase,
    RaceEvent,
    StandingsState,
)

BASE = {
    "Speed": 40.0,
    "Brake": 0.0,
    "Throttle": 0.0,
    "LFspeed": 40.0,
    "RFspeed": 40.0,
    "LRspeed": 40.0,
    "RRspeed": 40.0,
    "LapDistPct": 0.5,
    "Lap": 3,
    "OnPitRoad": False,
    "PlayerTrackSurface": 3,  # on_track
    "PlayerCarMyIncidentCount": 0,
    "LapLastLapTime": 0.0,
}


def frame(**overrides) -> dict:
    return {**BASE, **overrides}


def window(*frames: dict) -> list[dict]:
    return list(frames)


# --------------------------------------------------------------------------
# lock-up
# --------------------------------------------------------------------------
def _braking(front_ratio: float, brake: float = 0.9, speed: float = 40.0) -> list[dict]:
    f = frame(Speed=speed, Brake=brake, LFspeed=speed * front_ratio,
              RFspeed=speed * front_ratio, LRspeed=speed, RRspeed=speed)
    return window(f, f, f)


def test_lockup_fires_when_front_wheel_collapses_under_braking():
    hit = det.detect_lockup(_braking(0.60))
    assert hit is not None
    assert hit["axle"] == "front"
    assert hit["slip"] == pytest.approx(0.40)
    assert hit["wheel_speed"] == pytest.approx(24.0)


def test_lockup_near_miss_does_not_fire():
    # 10% slip, just inside the 15% threshold.
    assert det.detect_lockup(_braking(0.90)) is None


def test_lockup_needs_the_brake_applied():
    assert det.detect_lockup(_braking(0.60, brake=0.10)) is None


def test_lockup_ignored_below_the_minimum_speed():
    assert det.detect_lockup(_braking(0.60, speed=5.0)) is None


def test_lockup_needs_a_sustained_condition():
    locked = frame(Brake=0.9, LFspeed=24.0, RFspeed=24.0)
    clean = frame(Brake=0.9)
    # Only the newest frame is locked -> not sustained across the 3-frame window.
    assert det.detect_lockup(window(clean, clean, locked)) is None


def test_lockup_without_wheel_channels_returns_none():
    bare = {"Speed": 40.0, "Brake": 0.9}
    assert det.detect_lockup(window(bare, bare, bare)) is None


# --------------------------------------------------------------------------
# wheelspin
# --------------------------------------------------------------------------
def _driving(rear_ratio: float, throttle: float = 0.8) -> list[dict]:
    f = frame(Throttle=throttle, LRspeed=40.0 * rear_ratio, RRspeed=40.0 * rear_ratio)
    return window(f, f, f)


def test_wheelspin_fires_when_driven_wheels_outrun_the_car():
    hit = det.detect_wheelspin(_driving(1.25))
    assert hit is not None
    assert hit["axle"] == "rear"
    assert hit["slip"] == pytest.approx(0.25)


def test_wheelspin_near_miss_does_not_fire():
    assert det.detect_wheelspin(_driving(1.05)) is None


def test_wheelspin_needs_throttle():
    assert det.detect_wheelspin(_driving(1.25, throttle=0.05)) is None


# --------------------------------------------------------------------------
# edge detectors
# --------------------------------------------------------------------------
def test_offtrack_fires_on_the_rising_edge_only():
    on, off = frame(PlayerTrackSurface=3), frame(PlayerTrackSurface=0)
    assert det.detect_offtrack(window(on, off)) is not None
    assert det.detect_offtrack(window(off, off)) is None
    assert det.detect_offtrack(window(off, on)) is None


def test_incident_fires_on_counter_increase():
    a, b = frame(PlayerCarMyIncidentCount=2), frame(PlayerCarMyIncidentCount=6)
    hit = det.detect_incident(window(a, b))
    assert hit == {"incidents": 6, "delta": 4, "lap_dist_pct": 0.5}
    assert det.detect_incident(window(b, b)) is None


def test_pit_transitions():
    out_, in_ = frame(OnPitRoad=False), frame(OnPitRoad=True)
    assert det.detect_pit_transition(window(out_, in_)) == "entry"
    assert det.detect_pit_transition(window(in_, out_)) == "exit"
    assert det.detect_pit_transition(window(out_, out_)) is None


def test_lap_completion_reports_the_lap_that_ended():
    a = frame(Lap=3)
    b = frame(Lap=4, LapLastLapTime=88.5)
    assert det.detect_lap_completion(window(a, b)) == {
        "lap": 3,
        "new_lap": 4,
        "lap_time": 88.5,
    }
    assert det.detect_lap_completion(window(a, a)) is None


def test_lap_completion_drops_a_zero_lap_time():
    a, b = frame(Lap=1), frame(Lap=2, LapLastLapTime=0.0)
    assert det.detect_lap_completion(window(a, b))["lap_time"] is None


# --------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------
def test_flag_phase_priority():
    assert det.flag_phase(0x00000004)[0] is FlagPhase.GREEN
    assert det.flag_phase(0x00000008)[0] is FlagPhase.YELLOW
    assert det.flag_phase(0x00004000)[0] is FlagPhase.YELLOW  # caution
    assert det.flag_phase(0x00000010)[0] is FlagPhase.RED
    # White (last lap) is normally set alongside green; the more specific wins.
    assert det.flag_phase(0x00000002 | 0x00000004)[0] is FlagPhase.WHITE
    # ...but a yellow on the last lap outranks the white flag.
    assert det.flag_phase(0x00000002 | 0x00000008)[0] is FlagPhase.YELLOW
    assert det.flag_phase(0x00000001 | 0x00000004)[0] is FlagPhase.CHECKERED
    assert det.flag_phase(None) == (FlagPhase.UNKNOWN, [])


def test_flag_phase_lists_active_names():
    _, active = det.flag_phase(0x00000008 | 0x00004000)
    assert set(active) == {"yellow", "caution"}


# --------------------------------------------------------------------------
# blue flag (standings-derived)
# --------------------------------------------------------------------------
def _standings(*cars: CarState) -> StandingsState:
    return StandingsState(cars=list(cars), player_idx=0, gap_basis="lap_time_pct")


def test_blue_flag_fires_for_a_lapping_car_closing_from_behind():
    player = CarState(idx=0, is_player=True, lap_dist_pct=0.50, laps_completed=10.50)
    leader = CarState(idx=5, lap_dist_pct=0.48, laps_completed=11.48, gap_to_player=-1.2,
                      position=1)
    hit = det.detect_blue_flag(_standings(player, leader))
    assert hit is not None
    assert hit["car_idx"] == 5
    assert hit["gap"] == pytest.approx(1.2)
    assert hit["laps_ahead"] == pytest.approx(0.98)


def test_blue_flag_ignores_a_car_on_the_same_lap():
    player = CarState(idx=0, is_player=True, lap_dist_pct=0.50, laps_completed=10.50)
    rival = CarState(idx=5, lap_dist_pct=0.48, laps_completed=10.48, gap_to_player=-0.2)
    assert det.detect_blue_flag(_standings(player, rival)) is None


def test_blue_flag_ignores_a_lapping_car_that_is_already_past():
    player = CarState(idx=0, is_player=True, lap_dist_pct=0.50, laps_completed=10.50)
    ahead = CarState(idx=5, lap_dist_pct=0.55, laps_completed=11.55, gap_to_player=+0.5)
    assert det.detect_blue_flag(_standings(player, ahead)) is None


def test_blue_flag_ignores_a_car_still_far_behind():
    player = CarState(idx=0, is_player=True, lap_dist_pct=0.50, laps_completed=10.50)
    far = CarState(idx=5, lap_dist_pct=0.10, laps_completed=11.10, gap_to_player=-8.0)
    assert det.detect_blue_flag(_standings(player, far)) is None


def test_blue_flag_refuses_without_a_gap_basis():
    player = CarState(idx=0, is_player=True, lap_dist_pct=0.50, laps_completed=10.50)
    leader = CarState(idx=5, lap_dist_pct=0.48, laps_completed=11.48, gap_to_player=-1.2)
    unknown = StandingsState(cars=[player, leader], player_idx=0, gap_basis=None)
    assert det.detect_blue_flag(unknown) is None


# --------------------------------------------------------------------------
# accessors degrade rather than invent
# --------------------------------------------------------------------------
def test_accessors_return_none_for_missing_or_bad_values():
    assert det.as_float({}, "Nope") is None
    assert det.as_float({"x": "abc"}, "x") is None
    assert det.as_float({"x": float("nan")}, "x") is None
    assert det.as_int({"x": None}, "x") is None
    assert det.as_bool({}, "x") is None
    assert det.as_bool({"x": 0}, "x") is False


# --------------------------------------------------------------------------
# event bus
# --------------------------------------------------------------------------
def _event(n: int) -> RaceEvent:
    return RaceEvent(event_type=EventType.LAP_COMPLETED, tick=n, session_time=float(n))


def test_bus_delivers_to_every_subscriber():
    from rtv.racestate.bus import EventBus

    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    bus.publish(_event(1))
    assert [e.tick for e in a.drain()] == [1]
    assert [e.tick for e in b.drain()] == [1]


def test_bus_bounded_queue_drops_oldest_and_counts():
    from rtv.racestate.bus import EventBus

    bus = EventBus()
    sub = bus.subscribe(maxsize=3)
    for i in range(5):
        bus.publish(_event(i))
    # Latest-wins: the newest 3 survive, the 2 oldest are dropped and counted.
    assert [e.tick for e in sub.drain()] == [2, 3, 4]
    assert sub.dropped == 2


def test_bus_callbacks_run_and_a_bad_one_does_not_break_the_loop():
    from rtv.racestate.bus import EventBus

    bus = EventBus()
    seen: list[int] = []
    bus.subscribe_callback(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe_callback(lambda e: seen.append(e.tick))
    bus.publish(_event(7))
    assert seen == [7]


def test_bus_history_is_bounded_and_ordered():
    from rtv.racestate.bus import EventBus

    bus = EventBus(history=3)
    for i in range(5):
        bus.publish(_event(i))
    assert [e.tick for e in bus.history()] == [2, 3, 4]
    assert [e.tick for e in bus.history(2)] == [3, 4]


def test_unsubscribe_stops_delivery():
    from rtv.racestate.bus import EventBus

    bus = EventBus()
    sub = bus.subscribe()
    sub.close()
    bus.publish(_event(1))
    assert sub.drain() == []
    assert bus.subscriber_count == 0


async def test_bus_async_next_events_wakes_on_publish():
    import asyncio

    from rtv.racestate.bus import EventBus

    bus = EventBus()
    sub = bus.subscribe()

    async def publish_soon():
        await asyncio.sleep(0.02)
        bus.publish(_event(42))

    task = asyncio.create_task(publish_soon())
    events = await sub.next_events(timeout=2.0)
    await task
    assert [e.tick for e in events] == [42]


async def test_bus_async_next_events_times_out_empty():
    from rtv.racestate.bus import EventBus

    sub = EventBus().subscribe()
    assert await sub.next_events(timeout=0.01) == []


async def test_bus_async_next_events_returns_backlog_immediately():
    from rtv.racestate.bus import EventBus

    bus = EventBus()
    sub = bus.subscribe()
    bus.publish(_event(1))
    bus.publish(_event(2))
    assert [e.tick for e in await sub.next_events(timeout=0.01)] == [1, 2]
