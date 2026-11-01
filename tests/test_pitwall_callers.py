"""B2: the deterministic spotter. Detector events become radio calls with no model.

Covers the template caller (:mod:`rtv.pitwall.callers`), the ``car_alongside``
detector on iRacing's ``CarLeftRight`` sensor, and the orchestrator path that
publishes template calls from ``dispatch``. The moving field is the G0 scripted
race (``tests/helpers.py``); test-only synthetic data, nothing here is served.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest
from evals.synthetic_race import ScenarioSpec, scenario_source

from rtv.pitwall.agents import AGENT_REGISTRY, SPOTTER
from rtv.pitwall.callers import (
    SPOTTER_TEMPLATES,
    DeterministicCaller,
    TemplateCall,
    build_callers,
    spotter_caller,
    template_message,
)
from rtv.pitwall.framework import RadioPriority
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ScriptedProvider
from rtv.pitwall.radio import RadioFeed
from rtv.pitwall.validator import FactSet, validate_output
from rtv.racestate import RaceStateEngine
from rtv.racestate import detectors as det
from rtv.racestate.models import EventType, RaceEvent, RaceState, Severity
from tests.helpers import field_spec

#: The G0 moving field plus a car alongside: left, then three wide, then clear,
#: then a car on the right and clear again.
ALONGSIDE = ((2, 0.20, 0.30, 2), (2, 0.30, 0.40, 4), (3, 0.50, 0.60, 3))


def _spec() -> ScenarioSpec:
    return replace(field_spec(), alongside=ALONGSIDE)


def _no_model(**_):
    raise AssertionError("a template call must never reach the model")


def _orch(engine, *, agents=(), feed=None, **kw) -> PitwallOrchestrator:
    provider = ScriptedProvider(kw.pop("handler", _no_model))
    orch = PitwallOrchestrator(
        engine, provider, list(agents), feed=feed or RadioFeed(), clock=None, **kw
    )
    orch.provider = provider
    return orch


def _event(event_type: str, payload: dict, *, t: float = 40.0, lap: int = 4) -> RaceEvent:
    return RaceEvent(
        event_type=EventType(event_type),
        tick=int(t * 60),
        session_time=t,
        lap=lap,
        severity=Severity.ADVISORY,
        payload=payload,
    )


def _drive(source, orch: PitwallOrchestrator) -> list:
    """Replay ``source`` frame by frame with the orchestrator and radio in the loop.

    The engine's own bus feeds ``dispatch`` after every frame and the radio is
    pumped on the session clock -- the live path, minus the asyncio scheduling.
    """
    engine = orch.engine
    engine.bind_catalog(source.catalog, session_id=source.session_id)
    if source.session_info:
        engine.set_session_info(source.session_info)
    aired = []
    orch.feed.subscribe_callback(aired.append)
    orch.woken = []  # (event key, agent) for every agent the dispatch woke
    sub = engine.bus.subscribe(name="test-dispatch")
    for frame in source.frames:
        engine.on_frame(frame, source.catalog)
        for event in sub.drain():
            orch.woken += [(event.key, name) for name in orch.dispatch(event)]
        orch.feed.pump(frame.session_time)
    sub.close()
    return aired


# --------------------------------------------------------------------------
# the CarLeftRight detector
# --------------------------------------------------------------------------
def test_alongside_fires_on_every_change_and_clear_only_after_a_car():
    assert det.detect_car_alongside(None, 1) == ("clear", None)  # first clear is silent
    state, payload = det.detect_car_alongside("clear", 2)
    assert state == "car_left" and payload == {"side": "car_left", "from": "clear", "raw": 2}
    assert det.detect_car_alongside("car_left", 2) == ("car_left", None)  # no change
    assert det.detect_car_alongside("car_left", 4)[1]["side"] == "car_left_right"
    assert det.detect_car_alongside("car_left_right", 1)[1] == {
        "side": "clear", "from": "car_left_right", "raw": 1,
    }
    # Spotter off (0) or no value: state resets, nothing fires.
    assert det.detect_car_alongside("car_left", 0) == (None, None)
    assert det.detect_car_alongside("car_left", None) == (None, None)


def test_the_engine_reports_car_alongside_only_when_the_sensor_exists():
    with_sensor = RaceStateEngine(source="replay")
    _drive(scenario_source(_spec()), _orch(with_sensor))
    keys = [e.key for e in with_sensor.bus.history() if e.event_type is EventType.CAR_ALONGSIDE]
    assert keys == [
        "car_alongside:car_left",
        "car_alongside:car_left_right",
        "car_alongside:clear",
        "car_alongside:car_right",
        "car_alongside:clear",
    ]
    assert with_sensor.state.capabilities.car_alongside

    # A real .ibt has no CarLeftRight: no capability, no events, and it says why.
    without = RaceStateEngine(source="replay")
    _drive(scenario_source(field_spec()), _orch(without))
    assert not without.state.capabilities.car_alongside
    assert "CarLeftRight" in without.state.capabilities.missing
    assert not any(e.event_type is EventType.CAR_ALONGSIDE for e in without.bus.history())


# --------------------------------------------------------------------------
# detector -> radio on the moving field, with no model anywhere
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def field_run():
    engine = RaceStateEngine(source="replay")
    orch = _orch(engine, agents=list(AGENT_REGISTRY))
    aired = _drive(scenario_source(_spec()), orch)
    return engine, orch, aired


def test_the_moving_field_produces_template_calls_without_a_model(field_run):
    engine, orch, aired = field_run
    spotter = [m for m in aired if m.agent == "spotter"]
    assert spotter, "the spotter said nothing on a moving field"
    assert all(m.source == "template" and m.model == "template" for m in spotter)
    keys = {m.event_ref.key for m in spotter}
    assert {"blue_flag", "car_alongside:car_left", "car_alongside:car_right"} <= keys
    blue = next(m for m in spotter if m.event_ref.key == "blue_flag")
    assert blue.spoken_text.startswith("Blue flag, P")
    assert blue.subject_car_idx == 7
    # The LLM spotter was only ever woken for a lap-line summary.
    assert {key for key, name in orch.woken if name == "spotter"} <= {"lap_completed"}
    assert orch.template_calls >= len(spotter)


def test_every_template_call_is_grounded_by_its_event_alone(field_run):
    engine, _, aired = field_run
    events = {(e.key, e.tick): e for e in engine.bus.history()}
    checked = 0
    for message in (m for m in aired if m.source == "template"):
        event = events[(message.event_ref.key, message.event_ref.tick)]
        facts = FactSet()
        facts.add("event", event.payload)
        report = validate_output(message.spoken_text, message.detail_text, facts)
        assert report.ok, (message.spoken_text, report.unsupported)
        checked += 1
    assert checked


@pytest.mark.parametrize(
    ("event_type", "payload", "spoken"),
    [
        ("car_alongside", {"side": "car_left", "from": "clear"}, "Car left."),
        ("car_alongside", {"side": "two_cars_right", "from": "car_right"}, "Two cars right."),
        ("car_alongside", {"side": "clear", "from": "car_left"}, "Clear."),
        (
            "traffic_close",
            {"car_idx": 3, "gap": 0.62, "side": "behind", "position": 5,
             "closing_rate_s_per_s": 0.31, "threshold_gap_s": 1.0},
            "P5 behind, 0.6, closing.",
        ),
        (
            "traffic_close",
            {"car_idx": 2, "gap": 0.84, "side": "ahead", "position": 3,
             "closing_rate_s_per_s": 0.2, "threshold_gap_s": 1.0},
            "Closing on P3 ahead, 0.8.",
        ),
        (
            "blue_flag",
            {"car_idx": 7, "gap": 1.234, "position": 1, "laps_ahead": 1.04},
            "Blue flag, P1 lapping you, 1.2 behind.",
        ),
        ("flag_change", {"from": "green", "to": "red", "active": ["red"]}, "Red flag, red flag."),
        ("incident", {"incidents": 4, "delta": 2, "lap_dist_pct": 0.31}, "Incident, that's 4x."),
    ],
)
def test_each_template_reads_only_its_payload(event_type, payload, spoken):
    event = _event(event_type, payload)
    message = spotter_caller().call(event, RaceState())
    assert message is not None and message.spoken_text == spoken
    facts = FactSet()
    facts.add("event", payload)
    assert validate_output(message.spoken_text, message.detail_text, facts).ok


def test_a_green_flag_or_an_unknown_side_says_nothing():
    caller = spotter_caller()
    green = _event("flag_change", {"from": "yellow", "to": "green"})
    assert caller.call(green, RaceState()) is None
    assert caller.call(_event("car_alongside", {"side": "sideways"}), RaceState()) is None
    assert caller.call(_event("lap_completed", {"lap": 3}), RaceState()) is None


# --------------------------------------------------------------------------
# latency: event to radio
# --------------------------------------------------------------------------
def test_a_template_call_airs_on_the_frame_its_event_fired(field_run):
    """In replay the radio reaches it on the same session tick: zero session latency."""
    _, _, aired = field_run
    side = [m for m in aired if m.event_ref.key == "car_alongside:car_left"]
    assert side and side[0].air_session_time == side[0].event_ref.session_time


def test_dispatch_to_published_is_well_under_100_ms(field_run):
    engine, _, _ = field_run
    orch = _orch(engine)
    event = _event("car_alongside", {"side": "car_left", "from": "clear"},
                   t=engine.state.session_time)
    started = time.perf_counter()
    orch.dispatch(event, engine.snapshot())
    elapsed = time.perf_counter() - started
    assert [m.spoken_text for m in orch.feed.pending()] == ["Car left."]
    assert elapsed < 0.1


async def test_bus_event_to_radio_air_under_100_ms_with_the_live_loop():
    """The real asyncio path: bus -> pump -> dispatch -> feed.run -> consumer."""
    engine = RaceStateEngine(source="replay")
    _drive(scenario_source(field_spec()), _orch(engine))  # a live-looking state
    orch = _orch(engine)
    aired: list = []
    got = asyncio.Event()

    def on_air(message):
        aired.append((time.perf_counter(), message))
        got.set()

    orch.feed.subscribe_callback(on_air)
    await orch.start()
    try:
        await asyncio.sleep(0.05)  # let the pump subscribe and park
        started = time.perf_counter()
        engine.bus.publish(
            _event("car_alongside", {"side": "car_right", "from": "clear"},
                   t=engine.state.session_time)
        )
        await asyncio.wait_for(got.wait(), 1.0)
    finally:
        await orch.stop()
    at, message = aired[0]
    assert message.spoken_text == "Car right."
    assert at - started < 0.1


# --------------------------------------------------------------------------
# radio discipline: supersede and expiry still apply
# --------------------------------------------------------------------------
def test_clear_supersedes_a_queued_car_left():
    engine = RaceStateEngine(source="replay")
    orch = _orch(engine)
    state = RaceState(session_time=40.0)
    orch.dispatch(_event("car_alongside", {"side": "car_left", "from": "clear"}, t=40.0), state)
    orch.dispatch(_event("car_alongside", {"side": "clear", "from": "car_left"}, t=40.3), state)
    assert [m.spoken_text for m in orch.feed.pending()] == ["Clear."]
    assert orch.feed.superseded == 1


def test_a_side_call_that_waited_too_long_is_dropped_not_aired_late():
    feed = RadioFeed()
    message = spotter_caller().call(
        _event("car_alongside", {"side": "car_left", "from": "clear"}, t=40.0), RaceState()
    )
    assert message.expires_at_session_time == 41.5 and message.max_age_s is None
    feed.publish(message)
    assert feed.pump(42.0) == []
    assert feed.expired == 1 and message.expired


def test_a_call_about_a_car_that_left_expires(field_run):
    engine, _, _ = field_run
    state = engine.snapshot()
    car = next(c for c in state.standings.cars if not c.is_player)
    message = spotter_caller().call(
        _event(
            "traffic_close",
            {"car_idx": car.idx, "gap": 0.5, "side": "behind", "position": car.position},
            t=state.session_time,
        ),
        state,
    )
    assert message.subject_car_idx == car.idx and message.max_age_s == 3.0
    gone = state.model_copy(deep=True)
    next(c for c in gone.standings.cars if c.idx == car.idx).on_pit_road = True
    feed = RadioFeed(state_source=lambda: gone)
    feed.publish(message)
    assert feed.pump(state.session_time + 1.0) == []
    assert message.expired and message.expired_reason == f"car {car.idx} is in the pit lane"


def test_a_held_traffic_call_airs_tagged_late():
    feed = RadioFeed()
    message = spotter_caller().call(
        _event("traffic_close", {"car_idx": 3, "gap": 0.5, "side": "behind"}, t=40.0),
        RaceState(),
    )
    feed.publish(message)
    aired = feed.pump(44.0)
    assert aired and aired[0].late and aired[0].spoken_text.startswith("Late call: ")


def test_a_flapping_flag_is_called_once_per_cooldown():
    caller = spotter_caller()
    yellow = {"from": "green", "to": "yellow", "active": ["yellow"]}
    assert caller.call(_event("flag_change", yellow, t=40.0), RaceState()) is not None
    assert caller.call(_event("flag_change", yellow, t=45.0), RaceState()) is None
    assert caller.call(_event("flag_change", yellow, t=51.0), RaceState()) is not None
    caller.reset()
    assert caller.call(_event("flag_change", yellow, t=52.0), RaceState()) is not None


# --------------------------------------------------------------------------
# the orchestrator's switches
# --------------------------------------------------------------------------
def test_kill_switch_and_disabled_spotter_silence_templates_too():
    engine = RaceStateEngine(source="replay")
    state = RaceState()
    left = _event("car_alongside", {"side": "car_left", "from": "clear"})

    off = _orch(engine, enabled=False)
    off.dispatch(left, state)
    assert off.feed.pending() == []

    muted = _orch(engine, agents=[SPOTTER])
    muted.set_agent_enabled("spotter", False)
    muted.dispatch(left, state)
    assert muted.feed.pending() == []

    stale = _orch(engine)
    stale.dispatch(left, RaceState(stale=True))
    assert stale.feed.pending() == []

    none = _orch(engine, callers=[])
    none.dispatch(left, state)
    assert none.feed.pending() == []


def test_an_exhausted_model_budget_does_not_silence_templates():
    engine = RaceStateEngine(source="replay")
    orch = _orch(engine, max_calls_per_session=1)
    orch.record_call("vision", 1)
    assert orch.budget_exhausted
    orch.dispatch(_event("car_alongside", {"side": "car_left", "from": "clear"}), RaceState())
    spoken = [m.spoken_text for m in orch.feed.pending()]
    assert "Car left." in spoken


def test_the_journal_record_says_template(field_run):
    _, _, aired = field_run
    record = next(m for m in aired if m.source == "template").model_dump(mode="json")
    assert record["source"] == "template"
    assert record["data"]["source"] == "template"


def test_status_lists_the_callers():
    orch = _orch(RaceStateEngine(source="replay"))
    status = orch.status()
    assert status["callers"][0]["agent"] == "spotter"
    assert "car_alongside" in status["callers"][0]["events"]
    assert "template_calls" in status


def test_build_callers_follows_agents_only():
    assert [c.agent for c in build_callers()] == ["spotter"]
    assert build_callers(only=["strategist"]) == []


# --------------------------------------------------------------------------
# the public API C1 builds on
# --------------------------------------------------------------------------
def test_a_custom_caller_is_a_dict_of_templates():
    def answer(event, state):
        return TemplateCall(
            spoken_text=f"P{event.payload['position']}.",
            detail_text="Position from the engine.",
            priority=RadioPriority.INFO,
            subject="answer:position",
            expires_after_s=10.0,
        )

    caller = DeterministicCaller("strategist", {"lap_completed": answer}, max_age_s=5.0)
    message = caller.call(_event("lap_completed", {"position": 4}, t=10.0), RaceState())
    assert message.agent == "strategist" and message.source == "template"
    assert message.max_age_s == 5.0 and message.expires_at_session_time == 20.0
    assert message.computed_at_session_time == 10.0

    direct = template_message("coach", _event("lap_completed", {}), TemplateCall("Box.", "x"))
    assert direct.source == "template" and direct.max_age_s is None


def test_a_broken_template_costs_the_call_not_the_radio():
    def broken(event, state):
        raise RuntimeError("boom")

    caller = DeterministicCaller("spotter", {"blue_flag": broken})
    assert caller.call(_event("blue_flag", {}), RaceState()) is None


# --------------------------------------------------------------------------
# what is left of the LLM spotter
# --------------------------------------------------------------------------
def test_the_llm_spotter_has_no_per_event_triggers_left():
    types = {t.event_type for t in SPOTTER.triggers}
    assert types == {"lap_completed", "visual_observation", "driver_question"}
    assert not types & set(SPOTTER_TEMPLATES)
    summary = next(t for t in SPOTTER.triggers if t.event_type == "lap_completed")
    assert summary.cooldown_s >= 300


def test_the_lap_line_summary_wakes_only_with_a_field_around_us(field_run):
    engine, _, _ = field_run
    state = engine.snapshot()
    lap = _event("lap_completed", {"lap": 4, "new_lap": 5}, t=state.session_time)
    orch = _orch(engine, agents=[SPOTTER])
    assert orch.dispatch(lap, state) == ["spotter"]

    empty = state.model_copy(deep=True)
    empty.capabilities.standings = False
    assert _orch(engine, agents=[SPOTTER]).dispatch(lap, empty) == []
