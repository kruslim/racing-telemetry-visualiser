"""Task R1: the regressions the combined Kimi Spa replay found, reproduced offline.

Each grounding fix starts from a recorded answer in ``evals/real/spa_run`` (the
combined replay's journal, main 41ceed6) and shows the verdict it gets now:

1. the engineer's spelled-out "one lap to green" (refused twice live) is speech;
2. the coach's "second gear" matches the gear diagnostic, and "slip 0.23 lap 13" is a
   slip figure followed by a lap number, not 0.23 laps;
3. the engineer's "17.4 litres" is the strategist's figure off the radio: still not
   the engineer's to quote, but the repair hint now says whose it is;

and the runtime fixes are driven by ``ScriptedProvider``:

4. an invalid respond payload gets one forced retry instead of ending the run;
5. a driver-question run is one forced call with a 30 s answer age, and the spotter
   is held to its 14-word limit live;
6. what an agent is shown has its floats cut to four decimals, for the fact set too.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

from evals.pitwall_cases import _end_of_scenario_state
from evals.real.offline import facts_of, payload_of

from rtv.grounding.units import read_figures
from rtv.pitwall import agent_input
from rtv.pitwall.agents import SPOTTER, STRATEGIST
from rtv.pitwall.agents.strategist import PitCall
from rtv.pitwall.framework import (
    ANSWER_MAX_AGE_S,
    AgentRuntime,
    RadioPriority,
)
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import (
    RESPOND_TOOL,
    KimiProvider,
    ProviderResponse,
    ScriptedProvider,
    tool_use_turn,
)
from rtv.pitwall.radio import RadioFeed
from rtv.pitwall.validator import FactSet, validate_output
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, Severity

CASES = Path(__file__).resolve().parents[1] / "evals" / "real" / "spa_run" / "cases.jsonl"


def _case(case_id: str) -> dict:
    for line in CASES.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        if case["case_id"] == case_id:
            return case
    raise KeyError(case_id)


def _report(case: dict, answer: int = 0, **kw):
    output = case["answers"][answer]["output"]
    return validate_output(
        output["spoken_text"], output["detail_text"], facts_of(case),
        payload=payload_of(output), **kw,
    )


# --------------------------------------------------------------------------
# 1. a spelled-out "one"
# --------------------------------------------------------------------------
def test_one_lap_to_green_is_speech_not_a_claim():
    case = _case("vehicle_engineer-1-car_health_warning")
    assert case["answers"][0]["recorded"]["unsupported"] == ["one"]  # refused live
    for i in range(len(case["answers"])):
        assert _report(case, i).ok


def test_other_spelled_out_laps_still_need_a_fact():
    facts = FactSet(typed=True)
    facts.add("race_state", {"fuel": {"laps_remaining": 6.2}})
    assert validate_output("One more lap, then box.", "", facts).ok
    assert not validate_output("Two laps of fuel.", "", facts).ok
    assert not validate_output("1 laps of fuel.", "", facts).ok


# --------------------------------------------------------------------------
# 2. ordinal gears and "slip 0.23 lap 13"
# --------------------------------------------------------------------------
def test_second_gear_and_slip_figure_on_the_recorded_coach_answer():
    case = _case("coach-10-offtrack")
    assert case["answers"][0]["recorded"]["unsupported"] == ["second gear", "0.23"]
    assert _report(case).ok


def test_a_lap_followed_by_a_number_is_that_numbers_prefix():
    figures = read_figures("wheelspin on exit (slip 0.23 lap 13)")
    assert [(f.value, f.family) for f in figures] == [(0.23, None), (13.0, "lap")]
    assert [(f.value, f.family) for f in read_figures("2 laps to go")] == [(2.0, "laps")]


def test_gear_diagnostic_text_grounds_both_gears_and_nothing_else():
    facts = FactSet(typed=True)
    facts.add("get_corner_detail", {"text": "Apex in gear 1 - reference used 2"})
    assert validate_output("Take second gear at the apex.", "", facts).ok
    assert validate_output("You were in first gear.", "", facts).ok
    assert not validate_output("Take fourth gear at the apex.", "", facts).ok
    other = FactSet(typed=True)
    other.add("t", {"text": "Full throttle 2 m later"})
    assert not validate_output("Take second gear.", "", other).ok


# --------------------------------------------------------------------------
# 3. another agent's figure off the radio
# --------------------------------------------------------------------------
def test_the_strategists_fuel_is_named_as_the_strategists_in_the_repair_hint():
    case = _case("vehicle_engineer-5-pit_entry")
    report = _report(case, 1)
    assert report.unsupported == ["17.4"]  # not grounding: still refused
    [hint] = report.unsupported_detail
    assert "strategist's figure from the radio" in hint


def test_the_engineer_prompt_says_not_to_quote_other_agents_figures():
    from rtv.pitwall.agents.vehicle_engineer import VEHICLE_ENGINEER_PROMPT

    assert "not\n  yours to quote" in VEHICLE_ENGINEER_PROMPT


def test_a_heard_figure_never_grounds():
    facts = FactSet(typed=True)
    facts.hear("strategist", "Box this lap, fuel only, 17.4 litres.")
    assert not validate_output("17.4 litres going in.", "", facts).ok


# --------------------------------------------------------------------------
# 4. an invalid respond payload is retried once
# --------------------------------------------------------------------------
def _call(**over) -> PitCall:
    data = {
        "priority": RadioPriority.INFO,
        "spoken_text": "Stay out, the plan stands.",
        "detail_text": "No figures.",
        "recommendation": "stay_out",
        "confidence": "high",
        "rationale": "No figures asserted.",
    }
    data.update(over)
    return PitCall(**data)


def _event(event_type=EventType.FUEL_CRITICAL, **payload) -> RaceEvent:
    return RaceEvent(
        event_type=event_type, tick=1, session_time=10.0, severity=Severity.ADVISORY,
        payload=payload,
    )


def test_an_invalid_respond_payload_gets_one_forced_retry():
    seen: list[dict] = []

    def handler(**kw):
        seen.append(kw)
        if len(seen) == 1:
            return ProviderResponse(invalid_output="the respond call was empty")
        return _call()

    trace: list[tuple[str, dict]] = []
    runtime = AgentRuntime(
        STRATEGIST, ScriptedProvider(handler), tracer=lambda k, **d: trace.append((k, d))
    )
    message = asyncio.run(runtime.invoke(_event(), _end_of_scenario_state()))
    assert message is not None and not message.refused
    assert len(seen) == 2
    assert seen[1]["tools"] == []  # forced respond
    assert "could not be read (the respond call was empty)" in seen[1]["messages"][-1]["content"]
    ends = [d for k, d in trace if k == "llm_call_end"]
    assert ends[0]["invalid_output"] == "the respond call was empty"


def test_a_second_invalid_payload_still_ends_the_run():
    runtime = AgentRuntime(
        STRATEGIST, ScriptedProvider(lambda **_: ProviderResponse(invalid_output="bad"))
    )
    assert asyncio.run(runtime.invoke(_event(), _end_of_scenario_state())) is None
    assert runtime.turns == 2


class _FakeMessages:
    def __init__(self, blocks):
        self.blocks = blocks

    async def create(self, **_):
        return SimpleNamespace(content=self.blocks, stop_reason="tool_use", usage=None)


def test_kimi_reports_why_a_respond_payload_failed():
    block = SimpleNamespace(type="tool_use", id="t1", name=RESPOND_TOOL, input={})
    client = SimpleNamespace(messages=_FakeMessages([block]))
    provider = KimiProvider(client)
    response = asyncio.run(
        provider.complete(
            agent="strategist", model="kimi-k3", system=[], messages=[], tools=[],
            output_model=PitCall, max_tokens=10,
        )
    )
    assert response.output is None
    assert response.invalid_output == "the respond call was empty"
    assert response.stop_reason == "tool_use"

    block.input = {"spoken_text": "x"}
    response = asyncio.run(
        provider.complete(
            agent="strategist", model="kimi-k3", system=[], messages=[], tools=[],
            output_model=PitCall, max_tokens=10,
        )
    )
    assert "Field required" in response.invalid_output


# --------------------------------------------------------------------------
# 5. driver questions: one call, an answer's age, the spotter's word limit
# --------------------------------------------------------------------------
def test_a_question_run_gets_one_tool_round_then_a_forced_answer():
    """R1's replay: with no tools at all, kimi-k3's forced answer ran to max_tokens on two
    of four questions, so a question keeps the "any tool" first call and one round."""
    question = _event(EventType.DRIVER_QUESTION, owner="strategist", text="Stop plan?")
    assert STRATEGIST.tool_iterations(question) == 1
    assert STRATEGIST.tool_iterations(_event()) == STRATEGIST.max_tool_iterations

    seen: list[dict] = []

    def handler(**kw):
        seen.append(kw)
        if len(seen) == 1:
            return tool_use_turn([("get_fuel_projection", {})])
        return _call()

    trace: list[tuple[str, dict]] = []
    runtime = AgentRuntime(
        STRATEGIST, ScriptedProvider(handler), tracer=lambda k, **d: trace.append((k, d))
    )
    assert asyncio.run(runtime.invoke(question, _end_of_scenario_state())) is not None
    assert len(seen) == 2
    assert seen[0]["tools"] and seen[1]["tools"] == []  # the second call must answer
    [start] = [d for k, d in trace if k == "run_start"]
    assert start["input"]["max_tool_iterations"] == 1
    assert agent_input.model_input(start["input"])["messages"] == seen[0]["messages"][:1]


def test_a_record_with_no_tool_rounds_rebuilds_with_no_tools():
    record = agent_input.build_input(
        STRATEGIST, system_prompt="p", state_slice={},
        event={"key": "k", "severity": "info", "session_time": 1.0, "payload": {}},
        prefetched=[("get_fuel_projection", {}, {"laps": 3})], max_tool_iterations=0,
    )
    rebuilt = agent_input.model_input(record)
    assert rebuilt["tools"] == []
    assert "there are no more tools" in rebuilt["messages"][0]["content"]


def test_an_answer_cut_off_at_max_tokens_is_retried_once():
    seen: list[dict] = []

    def handler(**kw):
        seen.append(kw)
        if len(seen) == 1:
            return ProviderResponse(stop_reason="max_tokens")
        return _call()

    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(handler))
    assert asyncio.run(runtime.invoke(_event(), _end_of_scenario_state())) is not None
    assert len(seen) == 2 and seen[1]["max_tokens"] > seen[0]["max_tokens"]
    assert "token limit" in seen[1]["messages"][-1]["content"]


def test_how_late_its_own_call_aired_is_a_fact_its_words_are_not():
    """R1's replay, coach-2: "my lap-2 call aired 41 s late" was refused (no such fact)."""
    previous = [{
        "message_id": "m1", "subject": "coach:T8", "lap": 2, "session_time": 300.0,
        "spoken_text": "Target a 139 lap.", "status": "late", "late_by_s": 40.7,
        "expired_reason": None,
    }]
    facts = FactSet(typed=True)
    facts.add("previous", agent_input.previous_facts(previous))
    assert validate_output("My lap-2 call aired 41 s late.", "", facts).ok
    assert not validate_output("Target a 139 lap time.", "", facts).ok
    shown = agent_input.previous_line(previous[0])
    assert "aired late by 41 s" in shown


def test_an_agents_answer_gets_the_answer_age_limit():
    state = _end_of_scenario_state()
    engine = RaceStateEngine(source="replay")
    engine._state = state.model_copy(deep=True)  # noqa: SLF001 - a fixed race for the test
    orch = PitwallOrchestrator(
        engine, ScriptedProvider(lambda **_: _call()), [STRATEGIST], feed=RadioFeed(), clock=None
    )
    orch.ask("What's the plan for our stop?")
    event = engine.bus.history()[-1]
    [answer] = asyncio.run(orch.handle_event(event))
    assert answer.in_reply_to is not None
    assert answer.max_age_s == ANSWER_MAX_AGE_S == 30.0


def test_the_spotters_recorded_19_word_answer_is_over_its_limit():
    case = _case("spotter-1-driver_question")
    assert SPOTTER.spoken_word_limit == 14
    assert not _report(case).over_word_limit  # the radio-wide limit is 25
    report = _report(case, word_limit=SPOTTER.spoken_word_limit)
    assert report.over_word_limit and report.word_count == 19


def test_the_spotter_limit_reaches_the_prompt_and_the_record():
    record = agent_input.build_input(
        SPOTTER, system_prompt="p", state_slice={}, event={
            "key": "k", "severity": "info", "session_time": 1.0, "payload": {},
        },
    )
    assert record["spoken_word_limit"] == 14
    text = agent_input.model_input(record)["messages"][0]["content"]
    assert "under 14 words" in text
    other = agent_input.build_input(
        STRATEGIST, system_prompt="p", state_slice={}, event={
            "key": "k", "severity": "info", "session_time": 1.0, "payload": {},
        },
    )
    assert "spoken_word_limit" not in other


# --------------------------------------------------------------------------
# 6. what an agent is shown: four decimals
# --------------------------------------------------------------------------
def test_compact_rounds_floats_and_keeps_engine_rounded_figures_exact():
    shown = agent_input.compact(
        {"temps": {"LF": 55.096466064453125}, "fuel_to_add_l": 17.409, "lap": 12, "ok": True}
    )
    assert shown == {"temps": {"LF": 55.0965}, "fuel_to_add_l": 17.409, "lap": 12, "ok": True}


def test_a_recorded_coach_input_is_shorter_compacted():
    case = _case("coach-8-offtrack")
    data = case["input"]
    before = len(agent_input.as_text(data["state_slice"])) + sum(
        len(agent_input.as_text(p["result"])) for p in data["prefetched"]
    )
    after = len(agent_input.as_text(agent_input.compact(data["state_slice"]))) + sum(
        len(agent_input.as_text(agent_input.compact(p["result"]))) for p in data["prefetched"]
    )
    assert after < before


def test_the_fact_set_holds_exactly_the_shown_figures():
    seen: list[dict] = []

    def handler(**kw):
        seen.append(kw)
        return _call()

    trace: list[tuple[str, dict]] = []
    runtime = AgentRuntime(
        STRATEGIST, ScriptedProvider(handler), tracer=lambda k, **d: trace.append((k, d))
    )
    asyncio.run(runtime.invoke(_event(), _end_of_scenario_state()))
    [start] = [d for k, d in trace if k == "run_start"]
    shown = json.dumps(start["input"]["state_slice"])
    digits = [len(x.split(".")[1]) for x in re.findall(r"\d+\.\d+", shown)]
    assert digits and max(digits) <= agent_input.SHOWN_DECIMALS


def test_the_agents_own_recalled_call_is_not_also_in_recent_radio():
    from rtv.pitwall.framework import EventRef, RadioMessage

    own = RadioMessage(
        agent="strategist", priority=RadioPriority.INFO, spoken_text="Plan to box lap 13.",
        detail_text="",
        event_ref=EventRef(
            event_type="fuel_margin_low", key="fuel_margin_low", tick=1, session_time=1.0, lap=4
        ),
        session_time=1.0,
    )
    other = own.model_copy(update={"agent": "coach", "spoken_text": "Brake earlier at T4."})
    record = agent_input.build_input(
        STRATEGIST, system_prompt="p", state_slice={},
        event={"key": "k", "severity": "info", "session_time": 2.0, "payload": {}},
        radio_history=[own, other], previous=[agent_input.previous_call(own)],
    )
    assert [m["agent"] for m in record["radio"]] == ["coach"]
    text = agent_input.model_input(record)["messages"][0]["content"]
    assert text.count("Plan to box lap 13.") == 1


def test_an_incident_delta_is_an_incidents_figure():
    """D5's director run: race control's "4x" was refused although the event had delta 4."""
    facts = FactSet(typed=True)
    facts.add("event", {"stage": "final", "incidents": 14, "incident_limit": 17, "delta": 4})
    assert validate_output("That was a 4x, you're on 14 of 17.", "", facts).ok
    assert not validate_output("That was a 5x.", "", facts).ok
    other = FactSet(typed=True)
    other.add("t", {"delta": 4})  # a delta with no incidents beside it is not one
    assert not validate_output("That was a 4x.", "", other).ok
