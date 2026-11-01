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
from dataclasses import replace
from pathlib import Path

import pytest
from evals.synthetic_race import ScenarioSpec, replay_scenario
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
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, RaceState, Severity

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


# ==========================================================================
# stage 10: the scheduler (ScriptedDirector), the overlay and the eval runner
# ==========================================================================
from evals.director_rehearsal import (  # noqa: E402
    DIRECTOR_RACE,
    EXAMPLE_SCRIPT,
    journal_root_allowed,
    rehearsal_answer,
    rehearse,
)

from rtv.director import ScriptedDirector  # noqa: E402
from rtv.director.overlay import injected_caution, is_rain, pending_mandatory_stop  # noqa: E402
from rtv.pitwall import agent_input  # noqa: E402
from rtv.pitwall.agents.strategist import STRATEGIST, strategist_policy  # noqa: E402
from rtv.pitwall.agents.vehicle_engineer import VEHICLE_ENGINEER  # noqa: E402
from rtv.pitwall.framework import AgentRuntime  # noqa: E402
from rtv.pitwall.validator import FactSet, validate_output  # noqa: E402
from rtv.racestate import plan as race_plan  # noqa: E402
from rtv.racestate.models import FlagPhase  # noqa: E402


def _script(*entries: dict, name: str = "test") -> ScenarioScript:
    return ScenarioScript(name=name, injections=[_injection(**e) for e in entries])


def _at(session_time: float = 0.0, lap: int | None = 1, pct: float = 0.0, **kw) -> RaceState:
    state = RaceState(session_time=session_time, tick=int(session_time * 10), **kw)
    state.player.lap = lap
    state.player.lap_dist_pct = pct
    state.flags.phase = FlagPhase.GREEN
    return state


_YELLOW = {"from": "green", "to": "yellow", "active": ["yellow", "caution"]}
_GREEN = {"from": "yellow", "to": "green", "active": ["green"]}
_MANDATORY = {
    "regulation": "mandatory_pit_stop", "applies_from_lap": 5, "applies_to_lap": 9,
}
_RAIN = {"measure": "track_wetness", "from": 0.0, "to": 0.35, "forecast_minutes": 8}


@pytest.fixture(scope="module")
def rehearsal():
    return asyncio.run(rehearse(ScenarioScript.load(EXAMPLE_SCRIPT)))


# ---- the walker ------------------------------------------------------------
def test_the_scripted_director_satisfies_the_protocol(script):
    director = ScriptedDirector(script)
    assert isinstance(director, DirectorEngine)
    described = director.describe()
    assert described["implemented"] is True and described["script"] == script.name
    assert described["fired"] == 0 and len(described["pending"]) == 5


def test_at_session_time_fires_once_the_clock_passes_it():
    director = ScriptedDirector(_script({"id": "a", "when": {"at_session_time": 10.0}}))
    assert director.poll(_at(9.9)) == ()
    (event,) = director.poll(_at(10.0))
    assert event.payload[DIRECTOR_ID_KEY] == "a" and event.session_time == 10.0
    assert director.poll(_at(11.0)) == ()  # once means once


def test_at_lap_and_pct_name_a_point_on_track():
    director = ScriptedDirector(
        _script({"id": "a", "when": {"at_lap": 3, "at_lap_dist_pct": 0.3}})
    )
    assert director.poll(_at(1, lap=2, pct=0.9)) == ()
    assert director.poll(_at(2, lap=3, pct=0.29)) == ()
    assert len(director.poll(_at(3, lap=3, pct=0.31))) == 1


def test_at_lap_fires_late_rather_than_never_when_a_poll_misses_the_lap():
    director = ScriptedDirector(_script({"id": "a", "when": {"at_lap": 3}}))
    assert director.poll(_at(1, lap=2)) == ()
    (event,) = director.poll(_at(2, lap=4))
    assert event.lap == 4  # stamped from the state it saw


def test_after_and_delay_are_session_seconds():
    director = ScriptedDirector(
        _script(
            {"id": "a", "when": {"at_session_time": 5.0}},
            {"id": "b", "when": {"after": "a", "delay_s": 30.0}},
        )
    )
    assert [e.payload[DIRECTOR_ID_KEY] for e in director.poll(_at(5.0))] == ["a"]
    assert director.poll(_at(34.9)) == ()
    assert [e.payload[DIRECTOR_ID_KEY] for e in director.poll(_at(35.0))] == ["b"]


def test_an_after_with_no_delay_fires_on_its_parents_poll():
    director = ScriptedDirector(
        _script({"id": "a", "when": {"at_session_time": 1.0}}, {"id": "b", "when": {"after": "a"}})
    )
    assert [e.payload[DIRECTOR_ID_KEY] for e in director.poll(_at(1.0))] == ["a", "b"]


def test_while_flag_reads_the_injected_flag_over_the_telemetry():
    director = ScriptedDirector(
        _script(
            {"id": "fcy", "when": {"at_session_time": 1.0}, "payload": _YELLOW},
            {
                "id": "rain", "event_type": "weather_change",
                "when": {"at_session_time": 2.0, "while_flag": "green"}, "payload": _RAIN,
            },
            {
                "id": "green", "when": {"after": "fcy", "delay_s": 10.0}, "payload": _GREEN,
            },
        )
    )
    director.poll(_at(1.0))
    # The telemetry says green, but the scripted yellow is out: no rain yet.
    assert director.effective_flag(_at(2.0)) is FlagPhase.YELLOW
    assert director.poll(_at(2.0)) == ()
    fired = [e.payload[DIRECTOR_ID_KEY] for e in director.poll(_at(11.0))]
    assert fired == ["green"]  # rain is earlier in the script, so it waits a poll
    assert [e.payload[DIRECTOR_ID_KEY] for e in director.poll(_at(11.1))] == ["rain"]


def test_a_condition_that_stays_true_does_not_fire_again():
    director = ScriptedDirector(
        _script({"id": "a", "when": {"at_lap_dist_pct": 0.5, "once": False}})
    )
    assert len(director.poll(_at(1, pct=0.5))) == 1
    assert director.poll(_at(2, pct=0.6)) == ()  # still true: edge-triggered
    assert director.poll(_at(3, lap=2, pct=0.1)) == ()  # false again
    assert len(director.poll(_at(4, lap=2, pct=0.55))) == 1  # true again: once=false fires


def test_once_means_once_even_when_the_condition_comes_back():
    director = ScriptedDirector(_script({"id": "a", "when": {"at_lap_dist_pct": 0.5}}))
    assert len(director.poll(_at(1, pct=0.5))) == 1
    director.poll(_at(2, lap=2, pct=0.1))
    assert director.poll(_at(3, lap=2, pct=0.6)) == ()


def test_reset_forgets_what_fired_and_the_overlay():
    director = ScriptedDirector(
        _script({"id": "a", "when": {"at_session_time": 1.0}, "payload": _YELLOW})
    )
    director.poll(_at(1.0))
    assert director.overlay(_at(1.0)) and director.describe()["fired"] == 1
    director.reset()
    assert director.overlay(_at(1.0)) == [] and director.describe()["fired"] == 0
    assert len(director.poll(_at(1.0))) == 1


def test_a_poll_never_raises_into_the_pump(monkeypatch):
    director = ScriptedDirector(_script({"id": "a", "when": {"at_session_time": 1.0}}))

    def boom(*a, **k):
        raise RuntimeError("bad state")

    monkeypatch.setattr(director, "_holds", boom)
    assert director.poll(_at(1.0)) == ()
    assert director.errors == 1


def test_a_scripted_event_is_built_by_injected_event():
    director = ScriptedDirector(
        _script({"id": "fcy", "when": {"at_session_time": 1.0}, "payload": _YELLOW,
                 "severity": "critical", "kind": "flag"})
    )
    (event,) = director.poll(_at(1.0))
    assert is_injected(event)
    assert event.key == "flag_change:yellow" and event.severity is Severity.CRITICAL
    assert event.payload[DIRECTOR_KIND_KEY] == "flag"


# ---- the overlay -----------------------------------------------------------
def test_the_overlay_lists_conditions_not_momentary_events():
    director = ScriptedDirector(
        _script(
            {"id": "fcy", "when": {"at_session_time": 1.0}, "payload": _YELLOW},
            {"id": "reg", "event_type": "regulation_change", "kind": "regulation",
             "when": {"at_session_time": 1.0}, "payload": _MANDATORY},
            {"id": "rain", "event_type": "weather_change", "kind": "weather",
             "when": {"at_session_time": 1.0}, "payload": _RAIN},
            {"id": "debris", "event_type": "traffic_close", "kind": "hazard",
             "when": {"at_session_time": 1.0}, "payload": {"car_idx": 1, "side": "ahead"}},
        )
    )
    director.poll(_at(1.0))
    overlay = director.overlay(_at(1.0))
    assert [e["director_id"] for e in overlay] == ["fcy", "rain", "reg"]
    entry = overlay[0]
    assert set(entry) == {"kind", "event_type", "director_id", "since", "lap", "payload"}
    assert entry["payload"] == _YELLOW and entry["since"] == 1.0
    assert overlay[2]["served"] is False
    assert injected_caution(overlay) and pending_mandatory_stop(overlay, _at(1.0, lap=6))


def test_a_scripted_green_clears_the_injected_flag():
    director = ScriptedDirector(
        _script(
            {"id": "fcy", "when": {"at_session_time": 1.0}, "payload": _YELLOW},
            {"id": "green", "when": {"after": "fcy", "delay_s": 5.0}, "payload": _GREEN},
        )
    )
    director.poll(_at(1.0))
    assert injected_caution(director.overlay())
    director.poll(_at(6.0))
    assert director.overlay() == []


def test_a_pit_exit_serves_the_mandatory_stop():
    director = ScriptedDirector(
        _script({"id": "reg", "event_type": "regulation_change",
                 "when": {"at_session_time": 1.0}, "payload": _MANDATORY})
    )
    director.poll(_at(1.0, lap=5))
    later = _at(2.0, lap=6)
    assert director.overlay(later)[0]["served"] is False
    later.player.stint = 2
    assert director.overlay(later)[0]["served"] is True
    assert pending_mandatory_stop(director.overlay(later), later) is None


def test_the_telemetry_is_never_rewritten(rehearsal):
    # Every flag the engine itself reported stayed green; only the overlay knew.
    detected = [
        e for e in rehearsal.events
        if e["event_type"] == "flag_change" and not e["payload"].get(INJECTED_KEY)
    ]
    assert [e["payload"]["to"] for e in detected] == ["green"]


def test_an_agent_sees_the_overlay_in_its_slice_and_its_rule():
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    director = ScriptedDirector(
        _script({"id": "fcy", "when": {"at_session_time": 0.0}, "payload": _YELLOW})
    )
    provider = ScriptedProvider(lambda **_: _answer())
    orch = PitwallOrchestrator(
        engine, provider, [STRATEGIST], feed=RadioFeed(), clock=None, director=director
    )
    (event,) = orch.poll_director()
    asyncio.run(orch.handle_event(event, engine.snapshot()))
    system = provider.calls[0]["system"]
    state_text = system[1]["text"]
    assert '"injected":[{' in state_text and '"director_id":"fcy"' in state_text
    assert system[-1]["text"] == agent_input.INJECTED_RULE


def test_the_app_director_adds_no_overlay_and_no_rule(replayed):
    engine, captured = replayed
    provider = ScriptedProvider(lambda **_: _answer())
    orch = PitwallOrchestrator(engine, provider, [STRATEGIST], feed=RadioFeed(), clock=None)
    assert orch.injected_overlay() == []
    yellow = next(e for e in captured if e.key == "flag_change:yellow")
    asyncio.run(orch.handle_event(yellow, engine.snapshot()))
    system = provider.calls[0]["system"]
    assert len(system) == 2 and '"injected"' not in system[1]["text"]


def test_the_journal_rebuilds_an_overlaid_run_byte_identically(tmp_path):
    from rtv.store import journal

    rehearsal = asyncio.run(
        rehearse(ScenarioScript.load(EXAMPLE_SCRIPT), out_dir=tmp_path / "eval")
    )
    starts = [
        r for r in list(journal.iter_records(rehearsal.journal))
        if r["kind"] == "run_start" and (r["data"].get("input") or {}).get("state_slice", {})
        .get("injected")
    ]
    assert starts, "an agent run during a scripted condition records its overlay"
    rebuilt = agent_input.model_input(starts[0])
    assert rebuilt["system"][-1]["text"] == agent_input.INJECTED_RULE


# ---- the stage-5 equivalence, against the scheduler ---------------------------
def _fcy_at(real_yellow: RaceEvent) -> ScriptedDirector:
    return ScriptedDirector(
        ScenarioScript(
            name="fcy",
            injections=[
                {
                    "id": "fcy_lap_3", "kind": "flag", "event_type": "flag_change",
                    "severity": "critical",
                    "when": {"at_session_time": real_yellow.session_time},
                    "payload": _YELLOW,
                }
            ],
        )
    )


def test_the_scheduler_shapes_a_yellow_exactly_like_a_real_one(real_yellow):
    (scripted,) = _fcy_at(real_yellow).poll(
        RaceState(tick=real_yellow.tick, session_time=real_yellow.session_time)
    )
    assert scripted.key == real_yellow.key and scripted.severity is real_yellow.severity
    extra = set(scripted.payload) - set(real_yellow.payload)
    assert extra == {INJECTED_KEY, DIRECTOR_KIND_KEY, DIRECTOR_ID_KEY}


async def test_a_scheduled_fcy_produces_the_same_downstream_behaviour(replayed, real_yellow):
    engine, _ = replayed
    state = engine.snapshot()
    (scripted,) = _fcy_at(real_yellow).poll(
        state.model_copy(update={"session_time": real_yellow.session_time})
    )
    scripted = scripted.model_copy(update={"tick": real_yellow.tick, "lap": real_yellow.lap})
    detected_orch, injected_orch = _orch(engine), _orch(engine)
    assert injected_orch.dispatch(scripted, state) == detected_orch.dispatch(real_yellow, state)
    detected_orch.reset(), injected_orch.reset()
    from_detector = await detected_orch.handle_event(real_yellow, state)
    from_director = await injected_orch.handle_event(scripted, state)
    assert [(m.agent, m.priority, m.subject, m.spoken_text) for m in from_director] == [
        (m.agent, m.priority, m.subject, m.spoken_text) for m in from_detector
    ]


async def test_the_agents_see_a_scheduled_injection_through_the_running_pump():
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    director = ScriptedDirector(
        _script({"id": "fcy", "when": {"at_session_time": 0.0}, "payload": _YELLOW,
                 "severity": "critical"})
    )
    orch = _orch(engine, director=director)
    await orch.start()
    try:
        for _ in range(200):
            if any(m.event_ref.key == "flag_change:yellow" for m in orch.feed.history()):
                break
            await asyncio.sleep(0.01)
    finally:
        await orch.stop()
    assert orch.injected == 1
    assert any(m.event_ref.key == "flag_change:yellow" for m in orch.feed.history())


# ---- the example script, end to end ---------------------------------------------
def test_the_example_runs_end_to_end_on_the_synthetic_race(rehearsal):
    by_id = {r.id: r for r in rehearsal.injections}
    assert all(r.fired for r in rehearsal.injections)
    assert rehearsal.ok, rehearsal.markdown()
    # Each at its scripted condition.
    assert by_id["fcy_lap_3"].lap == 3
    assert by_id["green_again"].interval_s == pytest.approx(90.0, abs=0.11)
    assert by_id["rain_arrives"].interval_s == pytest.approx(120.0, abs=0.11)
    assert by_id["mandatory_stop"].lap == 5
    assert by_id["debris_hazard"].session_time == pytest.approx(420.0, abs=0.11)
    # FCY -> restart -> regulation -> rain -> hazard, in session time.
    order = sorted(rehearsal.injections, key=lambda r: r.session_time)
    assert [r.id for r in order] == [
        "fcy_lap_3", "green_again", "mandatory_stop", "rain_arrives", "debris_hazard"
    ]


def test_every_injection_carries_its_provenance(rehearsal):
    injected = [e for e in rehearsal.events if e["payload"].get(INJECTED_KEY)]
    assert len(injected) == 5
    for e in injected:
        assert e["payload"][DIRECTOR_ID_KEY] and e["payload"][DIRECTOR_KIND_KEY]


def test_each_injection_wakes_what_the_real_event_would(rehearsal):
    by_id = {r.id: r for r in rehearsal.injections}
    for r in rehearsal.injections:
        assert sorted(set(r.woke) | set(r.templated)) == r.as_real, r.id
    assert by_id["fcy_lap_3"].templated == ["spotter"]
    assert "strategist" in by_id["fcy_lap_3"].woke
    assert by_id["mandatory_stop"].woke == ["strategist"]
    assert set(by_id["rain_arrives"].woke) == {"strategist", "vehicle_engineer"}


def test_the_hazard_reaches_the_driver_through_the_template_not_the_llm(rehearsal):
    hazard = next(r for r in rehearsal.injections if r.id == "debris_hazard")
    assert hazard.templated == ["spotter"] and "spotter" not in hazard.woke
    (aired,) = hazard.aired
    assert aired["source"] == "template" and "ahead" in aired["spoken_text"]


def test_the_mandatory_stop_revises_the_plan_through_the_strategist(rehearsal):
    reg = next(r for r in rehearsal.injections if r.id == "mandatory_stop")
    (revision,) = [r for r in reg.plan_revisions if r.get("agent") == "strategist"]
    assert revision["changes"]["next_stop_lap"]["to"] == 9
    assert reg.checks["plan_revised_ok"]
    said = [a["spoken_text"] for a in reg.aired]
    assert any("mandatory" in s for s in said)


def test_the_radio_reacts_to_the_scripted_yellow(rehearsal):
    fcy = next(r for r in rehearsal.injections if r.id == "fcy_lap_3")
    agents = {a["agent"] for a in fcy.aired if not a["refused"]}
    assert {"spotter", "strategist"} <= agents


def test_the_same_script_over_the_same_race_gives_a_byte_identical_log(rehearsal):
    again = asyncio.run(rehearse(ScenarioScript.load(EXAMPLE_SCRIPT)))
    assert again.event_log() == rehearsal.event_log()
    assert [m["spoken_text"] for m in again.radio] == [m["spoken_text"] for m in rehearsal.radio]


@pytest.mark.parametrize("speed", [1.0, 4.0])
def test_a_chained_injection_fires_the_same_session_interval_at_1x_and_4x(speed):
    clock = {"t": 0.0}
    fired: dict[str, tuple[float, float]] = {}

    def sleep(d: float) -> None:
        clock["t"] += d

    def on_fire(event: RaceEvent, wall: float) -> None:
        fired[event.payload[DIRECTOR_ID_KEY]] = (event.session_time, wall)

    asyncio.run(
        rehearse(
            ScenarioScript.load(EXAMPLE_SCRIPT), speed=speed, sleep=sleep,
            clock=lambda: clock["t"], on_fire=on_fire,
        )
    )
    session_gap = fired["green_again"][0] - fired["fcy_lap_3"][0]
    wall_gap = fired["green_again"][1] - fired["fcy_lap_3"][1]
    assert session_gap == pytest.approx(90.0, abs=0.11)
    assert wall_gap == pytest.approx(90.0 / speed, abs=0.11)


# ---- the journal and no fake data ----------------------------------------------
def test_the_rehearsal_journal_is_its_own_and_says_eval(tmp_path):
    from rtv.store import journal

    out = tmp_path / "eval-out"
    rehearsal = asyncio.run(rehearse(ScenarioScript.load(EXAMPLE_SCRIPT), out_dir=out))
    assert rehearsal.journal is not None and out in rehearsal.journal.parents
    assert rehearsal.journal.name.endswith("-eval.jsonl")
    records = list(journal.iter_records(rehearsal.journal))
    start = records[0]
    assert start["kind"] == "lifecycle" and start["data"]["source"] == "eval"
    assert start["data"]["scenario"] == ScenarioScript.load(EXAMPLE_SCRIPT).name
    injected = [
        r for r in records if r["kind"] == "race_event" and r["data"]["payload"].get(INJECTED_KEY)
    ]
    assert len(injected) == 5


def test_a_rehearsal_refuses_to_write_into_the_app_journal(tmp_path):
    sessions = tmp_path / "data" / "sessions"
    assert not journal_root_allowed(sessions, sessions)
    assert not journal_root_allowed(sessions / "x", sessions)
    assert journal_root_allowed(tmp_path / "eval-out", sessions)


def test_a_replayed_ibt_has_no_field_for_a_traffic_injection():
    spec = replace(
        DIRECTOR_RACE,
        exclude=(
            "CarIdxLapDistPct", "CarIdxLap", "CarIdxPosition", "CarIdxLastLapTime",
            "CarIdxOnPitRoad", "CarIdxTrackSurface",
        ),
    )
    from evals.synthetic_race import scenario_source

    rehearsal = asyncio.run(
        rehearse(ScenarioScript.load(EXAMPLE_SCRIPT), scenario_source(spec))
    )
    assert not rehearsal.standings
    hazard = next(r for r in rehearsal.injections if r.id == "debris_hazard")
    assert any("no standings" in n for n in hazard.notes)
    assert "no CarIdx channels" in rehearsal.markdown()


def test_the_app_with_a_director_script_injects_nothing(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    import time

    from fastapi.testclient import TestClient

    from rtv.config import get_settings
    from rtv.main import create_app

    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_PITWALL", "true")
    monkeypatch.setenv("RTV_PITWALL_DIRECTOR_SCRIPT", str(EXAMPLE))
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            from evals.synthetic_race import seed_scenario_session

            services = client.app.state.services
            seed_scenario_session(services.writer, "scenario")
            r = client.post("/api/v1/replay/start", json={"session_id": "scenario", "speed": 0})
            assert r.status_code == 200
            deadline = time.time() + 30
            while time.time() < deadline and (
                client.get("/api/v1/replay/status").json()["running"]
                or services.recorder.active
            ):
                time.sleep(0.02)
            if services.pitwall is not None:
                assert isinstance(services.pitwall.director, NoopDirector)
                assert services.pitwall.injected == 0
            runs = list((tmp_path / "data" / "sessions").rglob("*.jsonl"))
            assert runs, "the replay was journalled"
            for path in runs:
                for line in path.read_text(encoding="utf-8").splitlines():
                    record = json.loads(line)
                    if record["kind"] == "race_event":
                        assert not record["data"]["payload"].get(INJECTED_KEY)
    finally:
        get_settings.cache_clear()


# ---- weather and regulation mean something ----------------------------------------
def test_rain_and_regulation_wake_their_consumers():
    state = _at(1.0, lap=5)
    rain = RaceEvent(event_type=EventType.WEATHER_CHANGE, tick=1, session_time=1.0, lap=5,
                     payload=_RAIN)
    reg = RaceEvent(event_type=EventType.REGULATION_CHANGE, tick=1, session_time=1.0, lap=5,
                    payload=_MANDATORY)
    assert AgentRuntime(STRATEGIST, None).match(rain, state) is not None
    assert AgentRuntime(VEHICLE_ENGINEER, None).match(rain, state) is not None
    assert AgentRuntime(STRATEGIST, None).match(reg, state) is not None
    assert AgentRuntime(VEHICLE_ENGINEER, None).match(reg, state) is None
    dry = rain.model_copy(update={"payload": {**_RAIN, "to": 0.0}})
    assert not is_rain(dry.payload)
    assert AgentRuntime(VEHICLE_ENGINEER, None).match(dry, state) is None


def test_rain_said_from_a_temperature_drift_is_refused():
    facts = FactSet()
    facts.add("race_state", {"conditions": {"air_temp": 17.5, "air_temp_trend": -0.4,
                                            "track_wetness": 1}})
    report = validate_output("Air temp is dropping, rain is coming.", "", facts)
    assert not report.ok and "rain" in report.unsupported
    assert "temperature trend is not a forecast" in report.unsupported_detail[0]


def test_rain_said_from_the_injected_weather_is_grounded():
    facts = FactSet()
    facts.add("race_state", {"injected": [{"kind": "weather", "event_type": "weather_change",
                                           "director_id": "rain", "payload": _RAIN}]})
    report = validate_output("Rain in 8 minutes, keep heat in the fronts.", "", facts)
    assert report.ok, report.unsupported_detail


def test_a_wet_track_reading_grounds_a_wet_call():
    facts = FactSet()
    facts.add("race_state", {"conditions": {"track_wetness": 4}})
    assert validate_output("Track is wet, careful on the kerbs.", "", facts).ok


async def test_the_engineer_cannot_forecast_rain_from_a_temperature_drift():
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    state = engine.snapshot()

    def handler(**_):
        return {
            "priority": "advisory",
            "spoken_text": "Track temp is falling, rain is on the way.",
            "detail_text": "Temperatures dropping.",
            "finding": "tyre_temps",
            "confidence": "low",
            "rationale": "Conditions trend.",
        }

    runtime = AgentRuntime(VEHICLE_ENGINEER, ScriptedProvider(handler), repair_attempts=0)
    event = RaceEvent(event_type=EventType.TYRE_OUT_OF_BAND, tick=state.tick,
                      session_time=state.session_time, lap=state.player.lap,
                      payload={"measure": "tyre_temp_trend"})
    refused = await runtime.invoke(event, state)
    assert refused.refused
    aired = await AgentRuntime(VEHICLE_ENGINEER, ScriptedProvider(handler)).invoke(
        event, state, injected=[{"kind": "weather", "event_type": "weather_change",
                                 "director_id": "rain", "since": 1.0, "payload": _RAIN}],
    )
    assert not aired.refused


def _race_state(lap: int = 6) -> RaceState:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, replace(DIRECTOR_RACE, laps=lap, end_pct=0.1))
    return engine.snapshot()


def test_the_fuel_alone_needs_no_stop_in_the_director_race():
    state = _race_state()
    assert state.fuel.margin_laps is not None and state.fuel.margin_laps > 0
    assert state.plan is not None and state.plan.stop_count.value == 0


def test_a_mandatory_stop_changes_the_derived_plan_and_the_bounds():
    state = _race_state()
    reg = RaceEvent(event_type=EventType.REGULATION_CHANGE, tick=1, session_time=1.0, lap=5,
                    payload=_MANDATORY)
    derived, missing = race_plan.derive(state, state.plan, pit_lane_loss_s=25.0, events=[reg])
    assert derived["stop_count"][0] == 1 and "next_stop_lap" in missing
    assert race_plan.sanity_check(state, {"next_stop_lap": 9}, base=state.plan) is not None
    assert race_plan.sanity_check(
        state, {"next_stop_lap": 9}, base=state.plan, events=[reg]
    ) is None
    assert "outside the regulation" in race_plan.sanity_check(
        state, {"next_stop_lap": 11}, base=state.plan, events=[reg]
    )
    exit_ = RaceEvent(event_type=EventType.PIT_EXIT, tick=2, session_time=2.0, lap=6)
    assert race_plan.mandatory_stop([reg, exit_]).served


def test_a_plan_proposal_is_checked_against_the_regulation_payload():
    state = _race_state()
    reg = RaceEvent(event_type=EventType.REGULATION_CHANGE, tick=1, session_time=1.0, lap=5,
                    payload=_MANDATORY)
    proposal = {"next_stop_lap": 9, "rationale": "A regulation makes the stop mandatory."}
    assert not race_plan.propose_revision(state.model_copy(deep=True), proposal).accepted
    assert race_plan.propose_revision(
        state.model_copy(deep=True), proposal, events=[reg]
    ).accepted
    # A stop after the regulation's window is refused, whatever backs the lap.
    late = {"next_stop_lap": 11, "rationale": "Later is cheaper."}
    outcome = race_plan.propose_revision(state.model_copy(deep=True), late, events=[reg])
    assert not outcome.accepted


def test_the_strategist_policy_reads_the_scripted_regulation_and_caution():
    state = _race_state()
    overlay = [{"kind": "regulation", "event_type": "regulation_change",
                "director_id": "reg", "since": 1.0, "lap": 5, "payload": _MANDATORY,
                "served": False}]
    plan_stop = _answer().model_copy(update={"recommendation": "plan_stop", "target_lap": 9})
    event = RaceEvent(event_type=EventType.REGULATION_CHANGE, tick=1, session_time=1.0, lap=5,
                      payload=_MANDATORY)
    assert strategist_policy(plan_stop, state, event) is not None  # fuel needs no stop
    assert strategist_policy(plan_stop, state, event, injected=overlay) is None
    late = plan_stop.model_copy(update={"target_lap": 11})
    assert "outside the regulation" in strategist_policy(late, state, event, injected=overlay)
    yellow = _answer().model_copy(update={"recommendation": "pit_under_yellow"})
    caution = [{"kind": "flag", "event_type": "flag_change", "director_id": "fcy",
                "since": 1.0, "lap": 5, "payload": _YELLOW}]
    assert strategist_policy(yellow, state, event, injected=overlay + caution) is None


def test_the_offline_provider_proposes_the_revision_first():
    system = [{"text": "x"}, {"text": "RACE STATE:\n" + json.dumps(
        {"injected": [{"event_type": "regulation_change", "payload": _MANDATORY}]})}]
    first = rehearsal_answer(
        agent="strategist", system=system, turn=0,
        messages=[{"role": "user", "content": "TRIGGER: regulation_change (severity advisory)"}],
    )
    assert first.tool_calls[0].name == "propose_plan_revision"
    assert first.tool_calls[0].input["next_stop_lap"] == 9


def test_director_eval_cases_cover_weather_and_regulation():
    from evals.pitwall_cases import build_agent_cases
    from evals.pitwall_checks import expected_calls

    strategist = [c for c in build_agent_cases("strategist") if c.name.startswith("director-")]
    assert {c.event.event_type.value for c in strategist} == {
        "regulation_change", "weather_change"
    }
    reg = next(c for c in strategist if c.event.event_type is EventType.REGULATION_CHANGE)
    assert any(i["director_id"] == "mandatory_stop" for i in reg.injected)
    allowed = expected_calls(reg.state, reg.injected)
    assert "stay_out" not in allowed and "plan_stop" in allowed
    assert "stay_out" in expected_calls(reg.state)  # without the regulation, fuel says stay
    engineer = [
        c for c in build_agent_cases("vehicle_engineer") if c.name.startswith("director-")
    ]
    assert [c.event.event_type.value for c in engineer] == ["weather_change"]


# ---- race control (stage 09 part A): an injected approach to the incident limit --------
from evals.director_rehearsal import INCIDENT_LIMIT_SCRIPT, SCENARIOS  # noqa: E402

from rtv.pitwall.agents.race_control import RaceControlCall, race_control_policy  # noqa: E402


@pytest.fixture(scope="module")
def incident_rehearsal():
    return asyncio.run(rehearse(ScenarioScript.load(INCIDENT_LIMIT_SCRIPT)))


def test_the_incident_limit_scenario_is_named_for_the_runner():
    assert SCENARIOS["incident_limit"] == INCIDENT_LIMIT_SCRIPT
    assert SCENARIOS["example"] == EXAMPLE_SCRIPT


def test_the_incident_read_out_is_race_controls_template(incident_rehearsal):
    assert incident_rehearsal.ok, incident_rehearsal.markdown()
    contact = next(r for r in incident_rehearsal.injections if r.id == "heavy_contact_lap_3")
    assert contact.templated == ["race_control"] and contact.woke == []
    (aired,) = contact.aired
    assert aired["source"] == "template"
    assert aired["spoken_text"] == "Incident, 14 of 17, 3 to the limit."


def test_the_final_stage_wakes_the_race_control_agent(incident_rehearsal):
    stage = next(r for r in incident_rehearsal.injections if r.id == "limit_final_stage")
    assert stage.woke == ["race_control"] and stage.as_real == ["race_control"]
    assert stage.checks["race_control_ok"] is True
    (aired,) = [a for a in stage.aired if a["agent"] == "race_control"]
    assert not aired["refused"] and aired["source"] != "template"


def _limit_event(*, injected: bool) -> RaceEvent:
    payload = {"stage": "final", "incidents": 14, "incident_limit": 17, "incidents_remaining": 3}
    if injected:
        payload.update({INJECTED_KEY: True, DIRECTOR_KIND_KEY: "custom", DIRECTOR_ID_KEY: "x"})
    return RaceEvent(
        event_type=EventType.INCIDENT_LIMIT, severity=Severity.CRITICAL, session_time=1.0,
        tick=1, payload=payload,
    )


def _limit_call(incidents: int, limit: int) -> RaceControlCall:
    return RaceControlCall(
        priority="critical", spoken_text="One more heavy contact ends our race.",
        detail_text="Close to the limit.", consequence="incident_limit",
        incidents=incidents, incident_limit=limit, confidence="high",
    )


def test_race_control_may_copy_an_injected_events_own_figures():
    state = RaceState()  # the counter says 0 and no limit: telemetry is never rewritten
    assert race_control_policy(_limit_call(14, 17), state, _limit_event(injected=True)) is None
    # Not the event's figures either: still withheld.
    assert race_control_policy(_limit_call(13, 17), state, _limit_event(injected=True))
    assert race_control_policy(_limit_call(14, 18), state, _limit_event(injected=True))


def test_a_real_events_figures_must_still_be_the_counters():
    state = RaceState()
    assert race_control_policy(_limit_call(14, 17), state, _limit_event(injected=False))
