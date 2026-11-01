"""Air-time validity (task B1): late calls air tagged, calls that stopped being true drop.

``RadioFeed.pump`` on a fake session clock covers each verdict (fresh, late, expired by
lap, by pit entry, by time, by a car that has gone), and a replay of the scripted race
covers the case the review found: a box call computed on lap N airs tagged late while
the car is still short of the pit entry, and is dropped once the car is past it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from evals.synthetic_race import ScenarioSpec, replay_scenario, scenario_source

from rtv.config import Settings
from rtv.pitwall import validity
from rtv.pitwall.activity import ActivityLog
from rtv.pitwall.agents import build_agents
from rtv.pitwall.agents.spotter import spotter_validity
from rtv.pitwall.agents.strategist import STRATEGIST, PitCall, strategist_validity
from rtv.pitwall.callers import spotter_max_age
from rtv.pitwall.framework import (
    LATE_PREFIX,
    EventRef,
    RadioMessage,
    RadioPriority,
    agent_max_age,
)
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ScriptedProvider
from rtv.pitwall.radio import RadioFeed
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import (
    Capabilities,
    CarState,
    EventType,
    PlayerState,
    RaceEvent,
    RaceState,
    Severity,
    StandingsState,
)


def _message(text="Box this lap.", *, agent="strategist", computed=100.0, **rules) -> RadioMessage:
    return RadioMessage(
        agent=agent,
        priority=rules.pop("priority", RadioPriority.ADVISORY),
        spoken_text=text,
        detail_text=text,
        event_ref=EventRef(event_type="pit_window_open", key=f"k{computed}", tick=1,
                           session_time=computed),
        session_time=computed,
        subject=rules.pop("subject", "pit_stop"),
        computed_at_session_time=computed,
        **rules,
    )


def _state(t=100.0, *, lap=5, pct=0.5, pit=False, cars=None) -> RaceState:
    state = RaceState(
        session_time=t,
        player=PlayerState(lap=lap, lap_dist_pct=pct, on_pit_road=pit),
    )
    if cars is not None:
        state.standings = StandingsState(cars=cars)
        state.capabilities = Capabilities(standings=True)
    return state


class _Clock:
    """A mutable race state the feed reads at air time."""

    def __init__(self, state: RaceState) -> None:
        self.state = state

    def __call__(self) -> RaceState:
        return self.state


def _feed(state: RaceState | None = None):
    activity = ActivityLog(clock=lambda: (0.0, 0))
    records: list[dict] = []
    activity.subscribe_callback(records.append)
    clock = _Clock(state or _state())
    feed = RadioFeed(tracer=activity, state_source=clock)
    heard: list[RadioMessage] = []
    feed.subscribe_callback(heard.append)
    return feed, clock, records, heard


def _kinds(records):
    return [r["kind"] for r in records]


# ---- pump on a fake clock -------------------------------------------------------
def test_a_fresh_call_airs_untouched():
    feed, clock, records, heard = _feed()
    msg = _message(max_age_s=20.0)
    feed.publish(msg)
    clock.state = _state(110.0)
    assert feed.pump(110.0) == [msg]
    assert not msg.late and msg.spoken_text == "Box this lap."
    assert msg.air_session_time == 110.0
    assert "radio_late" not in _kinds(records) and "radio_expired" not in _kinds(records)


def test_an_old_but_true_call_airs_tagged_late_with_a_stable_id():
    feed, clock, records, heard = _feed()
    msg = _message("Left rear is four degrees over.", agent="vehicle_engineer", max_age_s=20.0)
    queued_id = msg.message_id
    feed.publish(msg)
    clock.state = _state(127.5)
    assert feed.pump(127.5) == [msg]
    assert msg.late and msg.late_by_s == 27.5
    assert msg.spoken_text == LATE_PREFIX + "Left rear is four degrees over."
    assert msg.spoken_text.startswith("Late call:")
    # The audio URL and the radio_queued record were keyed on the queued id.
    assert msg.message_id == queued_id
    late = next(r for r in records if r["kind"] == "radio_late")
    assert late["session_time"] == 127.5
    assert late["data"]["message_id"] == queued_id
    assert late["data"]["late_by_s"] == 27.5 and late["data"]["max_age_s"] == 20.0
    aired = next(r for r in records if r["kind"] == "radio_aired")
    assert aired["data"]["late"] is True
    assert feed.stats()["late"] == 1 and heard == [msg]


def test_a_silent_late_message_is_tagged_but_not_reworded():
    feed, clock, _, _ = _feed()
    msg = _message("Call withheld.", max_age_s=1.0, speak=False)
    feed.publish(msg)
    feed.pump(150.0)
    assert msg.late and msg.spoken_text == "Call withheld."


def test_expired_by_lap():
    feed, clock, records, heard = _feed()
    msg = _message(max_age_s=30.0, valid_until_lap=5)
    feed.publish(msg)
    clock.state = _state(112.0, lap=6, pct=0.05)
    assert feed.pump(112.0) == []
    assert msg.expired and msg.speak is False
    assert msg.expired_reason == "lap 5 has passed (now lap 6)"
    expired = next(r for r in records if r["kind"] == "radio_expired")
    assert expired["data"]["reason"] == msg.expired_reason
    assert expired["data"]["spoken_text"] == "Box this lap."
    assert expired["session_time"] == 112.0
    assert "radio_aired" not in _kinds(records)
    # Consumers are told (so the journal and the lists can show it); the history,
    # which agents read as recent radio, is not.
    assert heard == [msg] and feed.history() == []
    assert feed.stats()["expired"] == 1 and feed.stats()["emitted"] == 0


def test_expired_by_pit_entry():
    feed, clock, _, _ = _feed()
    msg = _message(valid_until_lap=5, invalid_if_lap_dist_past=0.85, invalid_if_on_pit_road=True)
    feed.publish(msg)
    clock.state = _state(108.0, lap=5, pct=0.9)
    assert feed.pump(108.0) == []
    assert msg.expired_reason == "past the pit entry on lap 5 without stopping"


def test_short_of_the_pit_entry_it_is_still_valid():
    feed, clock, _, _ = _feed()
    msg = _message(valid_until_lap=5, invalid_if_lap_dist_past=0.85, invalid_if_on_pit_road=True)
    feed.publish(msg)
    clock.state = _state(108.0, lap=5, pct=0.8)
    assert feed.pump(108.0) == [msg] and not msg.expired


def test_a_box_call_is_dropped_once_the_car_is_in_the_pit_lane():
    feed, clock, _, _ = _feed()
    msg = _message(valid_until_lap=5, invalid_if_on_pit_road=True)
    feed.publish(msg)
    clock.state = _state(108.0, lap=5, pct=0.9, pit=True)
    assert feed.pump(108.0) == []
    assert msg.expired_reason == "the car is already in the pit lane"


def test_expired_by_time_even_without_a_state():
    feed = RadioFeed()
    msg = _message(expires_at_session_time=105.0)
    feed.publish(msg)
    assert feed.pump(106.0) == []
    assert msg.expired and msg.expired_reason == "expired at session time 105.0s"


def test_a_call_about_a_car_that_has_gone_is_dropped():
    cars = [CarState(idx=0, is_player=True), CarState(idx=7, on_pit_road=False)]
    feed, clock, _, _ = _feed(_state(cars=cars))
    stays = _message("Car behind, closing.", agent="spotter", subject="a", subject_car_idx=7)
    feed.publish(stays)
    assert feed.pump(101.0) == [stays]

    gone = _message("Car behind, closing.", agent="spotter", subject="b", subject_car_idx=7,
                    computed=101.0)
    feed.publish(gone)
    clock.state = _state(103.0, cars=[CarState(idx=0, is_player=True)])
    assert feed.pump(200.0) == []
    assert gone.expired_reason == "car 7 is no longer in the running order"


def test_a_critical_call_that_is_no_longer_true_is_dropped_too():
    feed, clock, _, _ = _feed(_state(lap=6))
    msg = _message(priority=RadioPriority.CRITICAL, valid_until_lap=5)
    feed.publish(msg)
    assert feed.pump(101.0) == [] and msg.expired


def test_an_expired_call_does_not_hold_the_channel():
    feed, clock, _, _ = _feed(_state(lap=6))
    dead = _message("Box this lap.", valid_until_lap=5, priority=RadioPriority.CRITICAL)
    alive = _message("Tyres look fine.", agent="vehicle_engineer", subject="tyres")
    feed.publish(dead)
    feed.publish(alive)
    assert feed.pump(101.0) == [alive]


def test_messages_without_a_computed_time_are_never_judged():
    feed, clock, _, _ = _feed(_state(lap=9))
    driver = _message("Copy.", agent="driver", valid_until_lap=1)
    driver.computed_at_session_time = None
    feed.publish(driver)
    assert feed.pump(900.0) == [driver] and not driver.late and not driver.expired


def test_a_stale_state_judges_on_time_only():
    stale = _state(lap=9)
    stale.stale = True
    feed, clock, _, _ = _feed(stale)
    msg = _message(valid_until_lap=5)
    feed.publish(msg)
    assert feed.pump(101.0) == [msg]


# ---- the rules the agents stamp -------------------------------------------------
def _event(**payload) -> RaceEvent:
    return RaceEvent(event_type=EventType.PIT_WINDOW_OPEN, tick=1, session_time=1.0,
                     severity=Severity.ADVISORY, payload=payload)


@pytest.mark.parametrize(
    ("data", "entry", "expected"),
    [
        ({"recommendation": "box_now"}, None,
         {"valid_until_lap": 5, "invalid_if_on_pit_road": True}),
        ({"recommendation": "box_now"}, 0.92,
         {"valid_until_lap": 5, "invalid_if_on_pit_road": True, "invalid_if_lap_dist_past": 0.92}),
        # An entry after the line: this lap's stop is entered early on the next lap.
        ({"recommendation": "box_now"}, 0.03,
         {"valid_until_lap": 6, "invalid_if_on_pit_road": True, "invalid_if_lap_dist_past": 0.03}),
        ({"recommendation": "box_next_lap"}, None,
         {"valid_until_lap": 6, "invalid_if_on_pit_road": True}),
        ({"recommendation": "plan_stop", "target_lap": 12}, None,
         {"valid_until_lap": 12, "invalid_if_on_pit_road": True}),
        ({"recommendation": "stay_out"}, 0.9, {}),
        ({"recommendation": "hold"}, None, {}),
    ],
)
def test_strategist_rules_come_from_the_structured_call(data, entry, expected):
    msg = _message(data=data)
    assert strategist_validity(msg, _state(lap=5), _event(), entry) == expected


def test_spotter_rule_names_the_car_from_the_event():
    msg = _message(agent="spotter")
    assert spotter_validity(msg, _state(), _event(car_idx=4), None) == {"subject_car_idx": 4}
    assert spotter_validity(msg, _state(), _event(), None) == {}


def test_stamp_records_the_snapshot_and_survives_a_broken_rule():
    msg = _message(computed=0.0, data={"recommendation": "box_now"})
    state = _state(321.0, lap=5)
    state.version = 77
    validity.stamp(msg, STRATEGIST, state, _event(), max_age_s=30.0, pit_entry_pct=0.9)
    assert (msg.state_version, msg.computed_at_session_time, msg.max_age_s) == (77, 321.0, 30.0)
    assert msg.valid_until_lap == 5 and msg.invalid_if_lap_dist_past == 0.9

    def broken(*_):
        raise RuntimeError("boom")

    other = _message(computed=0.0)
    validity.stamp(other, replace(STRATEGIST, validity=broken), state, _event(), max_age_s=None)
    assert other.computed_at_session_time == 321.0 and other.valid_until_lap is None


def test_default_max_ages_and_the_settings_override():
    ages = {spec.name: spec.max_age_s for spec in build_agents()}
    # B2: the LLM spotter only gives slow summaries, which are never tagged late.
    assert ages == {"strategist": 30.0, "vehicle_engineer": 20.0, "spotter": None, "coach": 30.0}
    engineer = next(s for s in build_agents() if s.name == "vehicle_engineer")
    assert agent_max_age(engineer, Settings(_env_file=None)) == 20.0
    over = Settings(_env_file=None, pitwall_vehicle_engineer_max_age_s=5)
    assert agent_max_age(engineer, over) == 5.0
    off = Settings(_env_file=None, pitwall_vehicle_engineer_max_age_s=0)
    assert agent_max_age(engineer, off) is None
    # The spotter's 3 s limit belongs to its template calls now.
    assert spotter_max_age(Settings(_env_file=None)) == 3.0
    assert spotter_max_age(Settings(_env_file=None, pitwall_spotter_max_age_s=5)) == 5.0
    assert spotter_max_age(Settings(_env_file=None, pitwall_spotter_max_age_s=0)) is None


def test_the_engine_learns_where_the_pit_entry_is():
    engine = RaceStateEngine(source="replay")
    assert engine.pit_entry_pct is None
    replay_scenario(engine, ScenarioSpec())
    # The scripted race enters the pits at 85 % of lap 5.
    assert engine.pit_entry_pct == pytest.approx(0.85, abs=0.01)


# ---- a replay: box call computed on lap N --------------------------------------
BOX_THIS_LAP = PitCall(
    priority=RadioPriority.CRITICAL,
    spoken_text="Box this lap.",
    detail_text="Box this lap.",
    recommendation="box_now",
    confidence="high",
    rationale="Scripted.",
)


def _replay_to(engine: RaceStateEngine, frames, catalog, lap: int, pct: float) -> None:
    for frame in frames:
        engine.on_frame(frame, catalog)
        p = engine.state.player
        if p.lap is not None and (p.lap, p.lap_dist_pct or 0.0) >= (lap, pct):
            return
    raise AssertionError(f"the scripted race never reached lap {lap} at {pct}")


async def _box_call_on_lap_5():
    source = scenario_source(ScenarioSpec())
    engine = RaceStateEngine(source="replay")
    engine.bind_catalog(source.catalog, session_id=source.session_id)
    engine.set_session_info(source.session_info)
    frames = iter(source.frames)
    _replay_to(engine, frames, source.catalog, 5, 0.40)

    activity = ActivityLog(clock=lambda: (engine.state.session_time, engine.state.tick))
    records: list[dict] = []
    activity.subscribe_callback(records.append)
    # No policy: this test is about air time, not about whether the plan allows it.
    spec = replace(STRATEGIST, policy=None)
    orch = PitwallOrchestrator(
        engine, ScriptedProvider(lambda **_: BOX_THIS_LAP), [spec], feed=RadioFeed(),
        clock=None, activity=activity,
    )
    # The scripted lap is 10 s; a 2 s limit stands in for a 15-60 s model run.
    orch.max_age_s["strategist"] = 2.0
    state = engine.snapshot()
    event = RaceEvent(
        event_type=EventType.FUEL_CRITICAL, tick=state.tick, session_time=state.session_time,
        lap=state.player.lap, severity=Severity.CRITICAL, payload={"lap": state.player.lap},
    )
    [message] = await orch.handle_event(event, state)
    assert message.valid_until_lap == 5 and message.invalid_if_on_pit_road
    assert message.computed_at_session_time == state.session_time
    return engine, frames, source.catalog, orch, message, records


async def test_replay_box_call_airs_late_before_the_pit_entry():
    engine, frames, catalog, orch, message, records = await _box_call_on_lap_5()
    _replay_to(engine, frames, catalog, 5, 0.80)  # 4 s later, still short of 85 %
    aired = orch.feed.pump(engine.state.session_time)
    assert aired == [message]
    assert message.late and message.spoken_text == "Late call: Box this lap."
    assert message.late_by_s == pytest.approx(4.0, abs=0.1)
    assert "radio_late" in _kinds(records) and "radio_expired" not in _kinds(records)


async def test_replay_box_call_is_dropped_after_the_pit_entry():
    engine, frames, catalog, orch, message, records = await _box_call_on_lap_5()
    _replay_to(engine, frames, catalog, 5, 0.90)  # in the pit lane now
    assert engine.state.player.on_pit_road
    assert orch.feed.pump(engine.state.session_time) == []
    assert message.expired and message.expired_reason == "the car is already in the pit lane"
    expired = next(r for r in records if r["kind"] == "radio_expired")
    assert expired["agent"] == "strategist" and expired["data"]["message_id"] == message.message_id
    assert "radio_aired" not in _kinds(records)
