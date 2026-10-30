"""The race-director seam (stage 5).

The director itself is planned, not implemented -- so these tests do two jobs.
They pin the :class:`ScenarioScript` schema, which is a published contract the
moment the example JSON exists; and they prove the *seam*: an injected
full-course yellow must reach the agents, the ring buffer and the socket through
exactly the same path a detected one does, or the whole design is a sketch.

The one-shot director defined here is deliberately not shipped in
``rtv.director``. It exists to show that the protocol is implementable in a dozen
lines -- which is the claim stage 5 is making.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from rtv.director import (
    DIRECTOR_ID_KEY,
    DIRECTOR_KIND_KEY,
    INJECTED_KEY,
    DirectorEngine,
    InjectionKind,
    InjectionTrigger,
    NoopDirector,
    ScenarioScript,
    ScriptedInjection,
    injected_event,
    is_injected,
)
from rtv.pitwall.agents import AGENT_REGISTRY
from rtv.pitwall.agents.strategist import PitCall
from rtv.pitwall.framework import RadioPriority
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ScriptedProvider
from rtv.pitwall.radio import RadioFeed
from rtv.racestate import RaceStateEngine, replay_scenario
from rtv.racestate.models import EventType, RaceEvent, RaceState, Severity
from rtv.racestate.scenario import ScenarioSpec

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "director_scenario.example.json"


# --------------------------------------------------------------------------
# fixtures: one replayed race, and the shipped example script
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def replayed() -> tuple[RaceStateEngine, list[RaceEvent]]:
    engine = RaceStateEngine(source="replay")
    captured: list[RaceEvent] = []
    engine.bus.subscribe_callback(captured.append)
    replay_scenario(engine, ScenarioSpec())
    return engine, captured


@pytest.fixture(scope="module")
def script() -> ScenarioScript:
    return ScenarioScript.load(EXAMPLE)


@pytest.fixture(scope="module")
def real_yellow(replayed) -> RaceEvent:
    _, captured = replayed
    event = next((e for e in captured if e.key == "flag_change:yellow"), None)
    assert event is not None, "the scripted race must contain a real full-course yellow"
    return event


def _answer(text="Yellow out, hold position, we stay out.") -> PitCall:
    return PitCall(
        priority=RadioPriority.ADVISORY,
        spoken_text=text,
        detail_text=text,
        recommendation="hold",
        confidence="medium",
        rationale="No figures asserted.",
    )


def _orch(engine, **kw) -> PitwallOrchestrator:
    return PitwallOrchestrator(
        engine,
        ScriptedProvider(lambda **_: _answer()),
        kw.pop("agents", list(AGENT_REGISTRY)),
        feed=kw.pop("feed", RadioFeed()),
        clock=None,
        **kw,
    )


class OneShotDirector:
    """A director in a dozen lines: fire one injection once, past a session time.

    Everything a real implementation adds -- ``after``/``delay_s`` chains,
    ``while_flag`` guards, repeat handling -- is scheduling. The seam this proves
    is the same one it would use.
    """

    name = "one-shot"

    def __init__(self, injection: ScriptedInjection, *, at: float) -> None:
        self.injection = injection
        self.at = at
        self.fired = False

    def bind(self, script: ScenarioScript | None) -> None:
        if script is not None:
            self.injection = script.injections[0]

    def poll(self, state: RaceState) -> Sequence[RaceEvent]:
        if self.fired or state.session_time < self.at:
            return ()
        self.fired = True
        return (injected_event(self.injection, state),)

    def reset(self) -> None:
        self.fired = False

    def describe(self) -> dict:
        return {"director": self.name, "fired": self.fired}


# --------------------------------------------------------------------------
# the schema
# --------------------------------------------------------------------------
def test_the_shipped_example_script_loads_and_validates(script):
    assert script.name
    assert script.version == 1
    assert len(script.injections) == 5


def test_the_example_covers_the_three_interventions_the_spec_names(script):
    kinds = {i.kind for i in script.injections}
    assert {InjectionKind.FLAG, InjectionKind.WEATHER, InjectionKind.REGULATION} <= kinds


def test_every_example_injection_names_a_real_event_type(script):
    known = {e.value for e in EventType}
    assert {i.event_type.value for i in script.injections} <= known


def test_the_example_is_json_round_trippable(script):
    again = ScenarioScript.from_json(script.model_dump_json())
    assert again == script


def test_the_raw_json_on_disk_is_the_model_verbatim(script):
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert {i["id"] for i in raw["injections"]} == {i.id for i in script.injections}


def test_lookup_by_id(script):
    assert script.injection("fcy_lap_3").event_type is EventType.FLAG_CHANGE
    with pytest.raises(KeyError):
        script.injection("no_such_entry")


def test_of_kind_filters(script):
    assert [i.id for i in script.of_kind(InjectionKind.REGULATION)] == ["mandatory_stop"]


def test_a_trigger_with_no_condition_is_rejected():
    # "when?" is the entire point of a timed injection; defaulting to "now" would
    # be a guess, and this codebase refuses those.
    with pytest.raises(ValidationError):
        InjectionTrigger()


def test_a_trigger_that_constrains_without_conditioning_is_rejected():
    with pytest.raises(ValidationError):
        InjectionTrigger(while_flag="green", once=True)


def test_delay_needs_something_to_be_delayed_from():
    with pytest.raises(ValidationError):
        InjectionTrigger(at_lap=3, delay_s=30.0)
    assert InjectionTrigger(after="x", delay_s=30.0).delay_s == 30.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"at_session_time": -1.0},
        {"at_lap": -2},
        {"at_lap_dist_pct": 1.4},
    ],
)
def test_out_of_range_conditions_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        InjectionTrigger(**kwargs)


def test_trigger_describes_itself_for_logs():
    trigger = InjectionTrigger(at_lap=7, at_lap_dist_pct=0.3, while_flag="green")
    described = trigger.describe()
    assert "lap 7" in described and "30%" in described and "green" in described


def _injection(**kw) -> dict:
    return {
        "id": kw.pop("id", "a"),
        "event_type": kw.pop("event_type", "flag_change"),
        "when": kw.pop("when", {"at_lap": 3}),
        **kw,
    }


def test_duplicate_injection_ids_are_rejected():
    with pytest.raises(ValidationError):
        ScenarioScript(
            name="dupe", injections=[_injection(id="a"), _injection(id="a")]
        )


def test_an_unresolvable_after_reference_is_rejected():
    with pytest.raises(ValidationError):
        ScenarioScript(
            name="dangling",
            injections=[_injection(id="a", when={"after": "ghost"})],
        )


def test_an_injection_cannot_wait_on_itself():
    with pytest.raises(ValidationError):
        ScenarioScript(
            name="loop", injections=[_injection(id="a", when={"after": "a"})]
        )


def test_an_unknown_kind_is_rejected():
    with pytest.raises(ValidationError):
        ScenarioScript(name="x", injections=[_injection(kind="sabotage")])


def test_an_unknown_event_type_is_rejected():
    with pytest.raises(ValidationError):
        ScenarioScript(name="x", injections=[_injection(event_type="alien_abduction")])


def test_a_forward_reference_resolves_because_ids_are_collected_first():
    script = ScenarioScript(
        name="forward",
        injections=[_injection(id="a", when={"after": "b"}), _injection(id="b")],
    )
    assert script.injection("a").when.after == "b"


def test_weather_and_regulation_are_director_only_event_types():
    # No detector emits these: nothing in the channel catalog announces rain that
    # has not arrived. They exist so a script can say it as an ordinary event.
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    emitted = {e.event_type for e in engine.bus.history()}
    assert EventType.WEATHER_CHANGE not in emitted
    assert EventType.REGULATION_CHANGE not in emitted


# --------------------------------------------------------------------------
# injected_event
# --------------------------------------------------------------------------
def test_an_injected_event_is_stamped_from_the_race_state(replayed, script):
    engine, _ = replayed
    state = engine.snapshot()
    event = injected_event(script.injection("fcy_lap_3"), state)
    assert event.tick == state.tick
    assert event.session_time == state.session_time
    assert event.lap == state.player.lap
    assert event.state_version == state.version


def test_an_injected_event_carries_no_wall_clock(replayed, script):
    engine, _ = replayed
    a = injected_event(script.injection("fcy_lap_3"), engine.snapshot())
    b = injected_event(script.injection("fcy_lap_3"), engine.snapshot())
    assert a.model_dump() == b.model_dump()  # replay-stable, like every RaceEvent


def test_injection_provenance_is_stamped_and_unspoofable(replayed, script):
    engine, _ = replayed
    entry = script.injection("fcy_lap_3").model_copy(
        update={"payload": {"to": "yellow", INJECTED_KEY: False, DIRECTOR_ID_KEY: "lie"}}
    )
    event = injected_event(entry, engine.snapshot())
    assert event.payload[INJECTED_KEY] is True
    assert event.payload[DIRECTOR_ID_KEY] == "fcy_lap_3"
    assert event.payload[DIRECTOR_KIND_KEY] == InjectionKind.FLAG.value
    assert is_injected(event)


def test_a_detector_event_is_not_marked_as_injected(real_yellow):
    assert not is_injected(real_yellow)


def test_overrides_let_an_implementation_date_an_injection_itself(replayed, script):
    engine, _ = replayed
    event = injected_event(
        script.injection("fcy_lap_3"), engine.snapshot(), tick=99, session_time=1.5, lap=2
    )
    assert (event.tick, event.session_time, event.lap) == (99, 1.5, 2)


# --------------------------------------------------------------------------
# the default director
# --------------------------------------------------------------------------
def test_the_default_director_never_fires(replayed, script):
    engine, _ = replayed
    director = NoopDirector(script)
    for _ in range(10):
        assert director.poll(engine.snapshot()) == ()
    assert director.polls == 10


def test_the_default_director_says_it_is_planned(script):
    described = NoopDirector(script).describe()
    assert described["implemented"] is False
    assert described["status"] == "planned"
    assert described["script"] == script.name
    assert described["injections"] == 5
    assert "interfaces only" in described["reason"]


def test_the_default_director_still_validates_a_script(script):
    director = NoopDirector()
    assert director.describe()["script"] is None
    director.bind(script)
    assert director.describe()["injections"] == 5
    director.bind(None)
    assert director.describe()["script"] is None


def test_both_directors_satisfy_the_protocol(script):
    assert isinstance(NoopDirector(script), DirectorEngine)
    assert isinstance(
        OneShotDirector(script.injection("fcy_lap_3"), at=0.0), DirectorEngine
    )


# --------------------------------------------------------------------------
# the seam
# --------------------------------------------------------------------------
def test_the_orchestrator_defaults_to_a_noop_director(replayed):
    engine, _ = replayed
    orch = _orch(engine)
    assert isinstance(orch.director, NoopDirector)
    assert orch.poll_director() == []
    assert orch.injected == 0
    assert orch.status()["director"]["status"] == "planned"


def test_an_injected_event_reaches_the_same_bus_the_detectors_use(replayed, script):
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    seen: list[RaceEvent] = []
    engine.bus.subscribe_callback(seen.append)
    orch = _orch(
        engine, director=OneShotDirector(script.injection("fcy_lap_3"), at=0.0)
    )

    injected = orch.poll_director()

    assert len(injected) == 1
    assert seen == injected  # it went out through the ordinary publish path
    assert engine.bus.history(1)[0] is injected[0]  # and into the ring buffer
    assert orch.injected == 1
    assert orch.poll_director() == []  # once means once


def test_a_broken_director_costs_injections_not_the_agent_layer(replayed):
    engine, _ = replayed

    class Exploding:
        name = "boom"

        def bind(self, script):  # pragma: no cover - never called
            pass

        def poll(self, state):
            raise RuntimeError("the director is on fire")

        def reset(self):
            pass

        def describe(self):
            return {"director": self.name}

    orch = _orch(engine, director=Exploding())
    assert orch.poll_director() == []
    assert orch.injected == 0


def test_a_scripted_yellow_is_shaped_exactly_like_a_real_one(real_yellow, script):
    scripted = injected_event(
        script.injection("fcy_lap_3"),
        RaceState(tick=real_yellow.tick, session_time=real_yellow.session_time),
        lap=real_yellow.lap,
    )
    assert scripted.event_type is real_yellow.event_type
    assert scripted.key == real_yellow.key == "flag_change:yellow"
    assert scripted.severity is real_yellow.severity is Severity.CRITICAL
    # Same payload keys, plus provenance and nothing else.
    extra = set(scripted.payload) - set(real_yellow.payload)
    assert extra == {INJECTED_KEY, DIRECTOR_KIND_KEY, DIRECTOR_ID_KEY}
    assert set(real_yellow.payload) <= set(scripted.payload)
    assert scripted.payload["to"] == real_yellow.payload["to"] == "yellow"


async def test_a_scripted_fcy_produces_the_same_downstream_behaviour(
    replayed, real_yellow, script
):
    """The stage-5 claim, asserted: same routing, same agents, same radio.

    Two fresh orchestrators over the same race state, one fed the yellow the
    scripted race actually threw and one fed the director's. If any part of the
    system had a "was this real?" branch in it, these would diverge.
    """
    engine, _ = replayed
    state = engine.snapshot()
    scripted = injected_event(
        script.injection("fcy_lap_3"),
        state,
        tick=real_yellow.tick,
        session_time=real_yellow.session_time,
        lap=real_yellow.lap,
    )

    detected_orch, injected_orch = _orch(engine), _orch(engine)
    assert injected_orch.dispatch(scripted, state) == detected_orch.dispatch(
        real_yellow, state
    )

    detected_orch.reset(), injected_orch.reset()
    from_detector = await detected_orch.handle_event(real_yellow, state)
    from_director = await injected_orch.handle_event(scripted, state)

    assert [m.agent for m in from_director] == [m.agent for m in from_detector]
    assert from_director, "a full-course yellow must wake somebody"
    assert [m.priority for m in from_director] == [m.priority for m in from_detector]
    assert [m.event_ref.key for m in from_director] == [
        m.event_ref.key for m in from_detector
    ]
    assert [m.subject for m in from_director] == [m.subject for m in from_detector]
    # The radio carries no trace of the difference either.
    assert [m.spoken_text for m in from_director] == [
        m.spoken_text for m in from_detector
    ]


async def test_the_agents_see_the_injection_through_the_running_pump(replayed, script):
    """End to end: director -> engine bus -> orchestrator subscription -> radio."""
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    orch = _orch(
        engine, director=OneShotDirector(script.injection("fcy_lap_3"), at=0.0)
    )
    await orch.start()
    try:
        for _ in range(200):
            if orch.feed.history():
                break
            await asyncio.sleep(0.01)
    finally:
        await orch.stop()

    assert orch.injected == 1
    assert orch.dispatched >= 1
    said = orch.feed.history()
    assert said, "the injected yellow reached an agent and produced radio"
    assert said[0].event_ref.key == "flag_change:yellow"
