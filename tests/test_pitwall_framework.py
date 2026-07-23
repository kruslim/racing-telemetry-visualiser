"""Agent framework tests: triggers, the tool loop, grounding and radio discipline.

All offline. The model is a scripted provider, so what is under test is the
runtime -- context assembly, tool dispatch, the citation validator, the repair
turn and the grounded refusal -- rather than anything Claude does.
"""

from __future__ import annotations

import pytest

from rtv.pitwall.agents.strategist import STRATEGIST, PitCall
from rtv.pitwall.framework import (
    AgentRuntime,
    RadioPriority,
    ToolContext,
)
from rtv.pitwall.provider import ScriptedProvider, tool_use_turn
from rtv.pitwall.radio import RadioFeed, airtime_s
from rtv.pitwall.tools import TOOLS_BY_NAME
from rtv.pitwall.validator import FactSet, validate_output
from rtv.racestate import RaceStateEngine, replay_scenario
from rtv.racestate.models import EventType, RaceEvent, RaceState, Severity
from rtv.racestate.scenario import ScenarioSpec


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine() -> RaceStateEngine:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    return engine


@pytest.fixture()
def state(engine) -> RaceState:
    return engine.snapshot()


def _event(event_type: str = "pit_window_open", **payload) -> RaceEvent:
    return RaceEvent(
        event_type=EventType(event_type),
        tick=1000,
        session_time=42.0,
        lap=payload.get("lap", 5),
        severity=Severity.ADVISORY,
        payload=payload or {"lap": 5},
    )


def _answer(**kw) -> PitCall:
    base = dict(
        priority=RadioPriority.ADVISORY,
        spoken_text="Stay out, no stop needed.",
        detail_text="Nothing to cite.",
        recommendation="stay_out",
        confidence="high",
        rationale="No figures asserted.",
    )
    return PitCall(**{**base, **kw})


# --------------------------------------------------------------------------
# the citation validator
# --------------------------------------------------------------------------
def test_a_figure_from_the_facts_is_supported():
    facts = FactSet()
    facts.add("fuel", {"per_lap": 2.487, "laps_remaining": 8.0})
    report = validate_output("Eight laps of fuel at 2.5 a lap.", "", facts)
    assert report.ok is True
    assert report.unsupported == []


def test_an_invented_figure_is_caught():
    facts = FactSet()
    facts.add("fuel", {"per_lap": 2.487, "laps_remaining": 8.0})
    report = validate_output("Box now, you have 41.7 litres.", "", facts)
    assert report.ok is False
    assert "41.7" in report.unsupported


def test_a_figure_phrased_off_a_negative_value_is_still_grounded():
    """"6.3 laps short" from a margin of -6.298 carries the sign in the words."""
    facts = FactSet()
    facts.add("fuel", {"margin_laps": -6.298})
    assert validate_output("We're 6.3 laps short.", "", facts).ok is True


def test_a_lap_time_is_read_as_one_figure_not_two():
    facts = FactSet()
    facts.add("laps", {"best_lap_time": 92.4})
    assert validate_output("Best is a 1:32.4.", "", facts).ok is True


def test_the_structured_payload_is_checked_exactly_not_loosely():
    """A claimed rejoin position must be copied from a tool, not merely plausible."""
    facts = FactSet()
    facts.add("outcome", {"rejoin_position": 8, "tyre_pressure": 18.9})
    loose = validate_output("", "", facts, payload={"rejoin_position": 19})
    assert loose.ok is False
    assert "rejoin_position=19" in loose.unsupported
    assert validate_output("", "", facts, payload={"rejoin_position": 8}).ok is True


def test_a_long_spoken_call_is_not_radio():
    report = validate_output(" ".join(["word"] * 40), "", FactSet())
    assert report.over_word_limit is True
    assert report.word_count == 40


def test_strings_inside_tool_results_are_citable():
    """An assumption line a tool wrote is a fact the agent legitimately saw."""
    facts = FactSet()
    facts.add("sim", {"assumptions": ["Pit-lane loss 25.0s."]})
    assert validate_output("Stop costs us 25 seconds.", "", facts).ok is True


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
def test_every_tool_runs_against_a_real_state_and_returns_json_safe_data(state):
    import json

    ctx = ToolContext(state=state, event=_event(), events=[], config={})
    for name, tool in TOOLS_BY_NAME.items():
        result = tool(ctx, {})
        assert isinstance(result, dict), name
        json.dumps(result, default=str)  # must survive the wire


def test_the_pit_simulation_tool_honours_the_configured_pit_lane_loss(state):
    ctx = ToolContext(state=state, event=_event(), events=[], config={"pit_lane_loss_s": 40.0})
    out = TOOLS_BY_NAME["simulate_pit_outcome"](ctx, {})
    assert out["pit_lane_loss_s"] == pytest.approx(40.0)


def test_an_unknown_state_section_is_reported_not_silently_dropped(state):
    ctx = ToolContext(state=state, event=_event(), events=[], config={})
    out = TOOLS_BY_NAME["get_race_state_slice"](ctx, {"sections": ["fuel", "nonsense"]})
    assert "fuel" in out
    assert out["unknown_sections"] == ["nonsense"]


# --------------------------------------------------------------------------
# triggering + cooldown
# --------------------------------------------------------------------------
def test_a_matching_trigger_fires_and_a_foreign_event_does_not(state):
    runtime = AgentRuntime(STRATEGIST, provider=None)
    assert runtime.match(_event("pit_window_open"), state) is not None
    assert runtime.match(_event("lockup"), state) is None


def test_a_predicate_can_veto_a_matching_event_type(state):
    """A checkered flag is a flag change, but it is not a strategy decision."""
    runtime = AgentRuntime(STRATEGIST, provider=None)
    yellow = _event("flag_change", **{"from": "green", "to": "yellow"})
    checkered = _event("flag_change", **{"from": "green", "to": "checkered"})
    assert runtime.match(yellow, state) is not None
    assert runtime.match(checkered, state) is None


def test_the_cooldown_is_measured_in_session_time_not_wall_clock(state):
    runtime = AgentRuntime(STRATEGIST, provider=None)
    first = _event("rival_pitted", gap_to_player=2.0)
    trigger = runtime.match(first, state)
    assert trigger is not None
    runtime.arm(trigger, first)

    soon = _event("rival_pitted", gap_to_player=2.0)
    soon.session_time = first.session_time + 5.0
    assert runtime.match(soon, state) is None  # inside the 45 s cooldown

    later = _event("rival_pitted", gap_to_player=2.0)
    later.session_time = first.session_time + 60.0
    assert runtime.match(later, state) is not None


def test_a_disabled_agent_never_matches(state):
    from dataclasses import replace

    runtime = AgentRuntime(replace(STRATEGIST, enabled=False), provider=None)
    assert runtime.match(_event("pit_window_open"), state) is None


def test_cooldowns_are_per_trigger_type_not_per_agent(state):
    runtime = AgentRuntime(STRATEGIST, provider=None)
    rival = _event("rival_pitted", gap_to_player=2.0)
    runtime.arm(runtime.match(rival, state), rival)
    # A different trigger type is unaffected by the rival cooldown.
    window = _event("pit_window_open")
    window.session_time = rival.session_time + 1.0
    assert runtime.match(window, state) is not None


# --------------------------------------------------------------------------
# the tool loop
# --------------------------------------------------------------------------
async def test_the_runtime_services_tool_calls_and_feeds_results_back(state):
    seen: list[list[str]] = []

    def handler(*, turn, messages, **_):
        seen.append([m["role"] for m in messages])
        if turn == 0:
            return tool_use_turn([("get_fuel_projection", {})])
        return _answer()

    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(handler))
    message = await runtime.invoke(_event(), state)

    assert message is not None
    assert message.tools_used == ["get_fuel_projection"]
    # Second turn sees: user instruction, assistant tool_use, user tool_result.
    assert seen[1] == ["user", "assistant", "user"]


async def test_a_tool_result_becomes_citable_ground_truth(state):
    """The pit-loss figure is only in the tool's output, and may still be said."""

    def handler(*, turn, **_):
        if turn == 0:
            return tool_use_turn([("simulate_pit_outcome", {})])
        return _answer(
            spoken_text="A stop costs us 25 seconds.",
            detail_text="simulate_pit_outcome reports a 25.0 s pit-lane loss.",
        )

    runtime = AgentRuntime(
        STRATEGIST, ScriptedProvider(handler), tool_config={"pit_lane_loss_s": 25.0}
    )
    message = await runtime.invoke(_event(), state)
    assert message.grounded is True and message.refused is False


async def test_a_failing_tool_is_reported_to_the_model_not_raised(state):
    from dataclasses import replace

    from rtv.pitwall.framework import ToolSpec

    def boom(ctx, args):
        raise RuntimeError("sensor offline")

    bad = ToolSpec("boom", "Always fails.", {"type": "object", "properties": {}}, boom)
    results: list[dict] = []

    def handler(*, turn, messages, **_):
        if turn == 0:
            return tool_use_turn([("boom", {})])
        results.extend(
            b for m in messages if isinstance(m["content"], list) for b in m["content"]
        )
        return _answer()

    runtime = AgentRuntime(
        replace(STRATEGIST, tools=(bad,)), ScriptedProvider(handler)
    )
    message = await runtime.invoke(_event(), state)
    assert message is not None  # the agent still answered
    assert any(r.get("is_error") and "sensor offline" in r["content"] for r in results)


async def test_tools_are_withdrawn_on_the_last_pass_so_the_model_must_answer(state):
    """A model that only ever calls tools is not allowed to loop forever."""
    offered: list[list[str]] = []

    def handler(*, tools, **_):
        offered.append([t.name for t in tools])
        return tool_use_turn([("get_fuel_projection", {})])

    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(handler))
    message = await runtime.invoke(_event(), state)

    assert message is None  # no structured answer was ever produced
    assert len(offered) == STRATEGIST.max_tool_iterations + 1
    assert offered[-1] == []  # the final pass offered no tools at all


# --------------------------------------------------------------------------
# grounding in the loop
# --------------------------------------------------------------------------
async def test_an_invented_figure_is_repaired_before_it_reaches_the_radio(state):
    def handler(*, call_index, messages, **_):
        if call_index == 0:
            return _answer(spoken_text="Box now, 41.7 litres, rejoin P19.")
        # The runtime must have told the model exactly which figure was rejected.
        last = messages[-1]["content"]
        assert "41.7" in last and "grounding validator" in last
        return _answer(spoken_text="Box now. I do not have a rejoin projection.")

    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(handler))
    message = await runtime.invoke(_event(), state)

    assert message.refused is False
    assert message.grounded is True
    assert "41.7" not in message.spoken_text


async def test_an_unrepairable_invention_becomes_a_grounded_refusal(state):
    def handler(**_):
        return _answer(
            spoken_text="Box now, 41.7 litres, rejoin P19.",
            detail_text="Rejoin P19.",
            rejoin_position=19,
        )

    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(handler))
    message = await runtime.invoke(_event(), state)

    assert message.refused is True
    assert message.grounded is False
    assert message.priority is RadioPriority.INFO
    assert "41.7" in message.ungrounded
    assert "rejoin_position=19" in message.ungrounded
    # The withheld text is preserved so an operator can see what was blocked.
    assert "41.7" in message.detail_text
    assert runtime.refusals == 1


async def test_an_over_long_call_is_trimmed_to_radio_length(state):
    long = " ".join(["standby"] * 40)

    def handler(**_):
        return _answer(spoken_text=long)

    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(handler), repair_attempts=0)
    message = await runtime.invoke(_event(), state)
    assert len(message.spoken_text.split()) <= 26  # 25 words plus the ellipsis
    assert message.refused is False  # length is a style fault, not an invention


async def test_a_provider_failure_produces_no_message_rather_than_a_bad_one(state):
    def handler(**_):
        raise RuntimeError("API down")

    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(handler))
    assert await runtime.invoke(_event(), state) is None
    assert runtime.errors == 1


# --------------------------------------------------------------------------
# context assembly
# --------------------------------------------------------------------------
async def test_the_stable_role_prompt_is_cached_and_the_state_is_not(state):
    provider = ScriptedProvider(lambda **_: _answer())
    runtime = AgentRuntime(STRATEGIST, provider)
    await runtime.invoke(_event(), state)

    system = provider.calls[0]["system"]
    assert len(system) == 2
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "GROUNDING" in system[0]["text"]
    assert "cache_control" not in system[1]  # the state changes every call
    assert system[1]["text"].startswith("RACE STATE:")


async def test_recent_radio_is_shown_for_continuity(state):
    provider = ScriptedProvider(lambda **_: _answer())
    runtime = AgentRuntime(STRATEGIST, provider)
    earlier = (await AgentRuntime(STRATEGIST, ScriptedProvider(
        lambda **_: _answer(spoken_text="Window opens lap five.")
    )).invoke(_event(), state),)
    await runtime.invoke(_event(), state, radio_history=earlier)

    prompt = provider.calls[0]["messages"][0]["content"]
    assert "RECENT RADIO" in prompt
    assert "Window opens lap five." in prompt


async def test_the_state_slice_bounds_what_the_agent_can_cite(state):
    """The strategist is not shown car health, so it cannot quote an oil temp."""
    provider = ScriptedProvider(lambda **_: _answer())
    await AgentRuntime(STRATEGIST, provider).invoke(_event(), state)
    block = provider.calls[0]["system"][1]["text"]
    assert '"fuel"' in block and '"standings"' in block
    # No car-health or weather *readings* -- so no oil temp to misquote as a gap.
    assert "oil_temp" not in block and "water_temp" not in block
    assert "air_temp" not in block and "track_wetness" not in block


# --------------------------------------------------------------------------
# radio feed
# --------------------------------------------------------------------------
def _message(agent="strategist", priority=RadioPriority.ADVISORY, subject="pit_stop",
             text="Window opens lap five."):
    from rtv.pitwall.framework import EventRef, RadioMessage

    return RadioMessage(
        agent=agent,
        priority=priority,
        spoken_text=text,
        detail_text=text,
        event_ref=EventRef.of(_event()),
        subject=subject,
    )


def test_critical_pre_empts_queued_advisories():
    feed = RadioFeed()
    feed.publish(_message(text="Advisory one", subject="a"))
    feed.publish(_message(priority=RadioPriority.CRITICAL, text="Box box box", subject="b"))
    feed.publish(_message(text="Advisory two", subject="c"))
    assert [m.spoken_text for m in feed.flush()] == [
        "Box box box", "Advisory one", "Advisory two",
    ]


def test_a_newer_advisory_supersedes_a_stale_one_about_the_same_subject():
    feed = RadioFeed()
    feed.publish(_message(text="Window opens lap five."))
    replaced = feed.publish(_message(text="Window open now, plan is lap five."))
    assert replaced is not None and replaced.spoken_text == "Window opens lap five."
    assert [m.spoken_text for m in feed.flush()] == ["Window open now, plan is lap five."]
    assert feed.superseded == 1


def test_a_critical_message_is_never_superseded():
    feed = RadioFeed()
    feed.publish(_message(priority=RadioPriority.CRITICAL, text="Box now, out of fuel"))
    feed.publish(_message(text="Window still open."))
    aired = [m.spoken_text for m in feed.flush()]
    assert "Box now, out of fuel" in aired
    assert feed.superseded == 0


def test_different_subjects_do_not_supersede_each_other():
    feed = RadioFeed()
    feed.publish(_message(subject="pit_stop"))
    feed.publish(_message(subject="fuel", text="Fuel is tight."))
    assert len(feed.flush()) == 2


def test_the_channel_paces_itself_on_the_session_clock():
    """A second call waits for the first to finish being said."""
    feed = RadioFeed()
    first = _message(subject="a", text="Box this lap, fuel only, we come out fourth.")
    feed.publish(first)
    assert feed.pump(100.0) == [first]

    second = _message(subject="b", text="Traffic ahead.")
    feed.publish(second)
    assert feed.pump(100.0 + airtime_s(first) - 0.1) == []  # channel still busy
    assert feed.pump(100.0 + airtime_s(first)) == [second]


def test_a_slow_consumer_loses_the_oldest_message_and_counts_it():
    feed = RadioFeed()
    sub = feed.subscribe(maxsize=2)
    for i in range(4):
        feed.emit(_message(subject=str(i), text=f"call {i}"))
    assert [m.spoken_text for m in sub.drain()] == ["call 2", "call 3"]
    assert sub.dropped == 2
