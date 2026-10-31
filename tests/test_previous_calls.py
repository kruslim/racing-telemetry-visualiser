"""Each agent is shown its own earlier calls, so it revises them instead of restating (D1).

Offline: the agents run on ``ScriptedProvider``, which keeps every call it was sent. The
context builder is pure (:mod:`rtv.pitwall.agent_input`); the orchestrator keeps each
agent's newest call per subject and reads what the radio did with it at air time.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from evals.synthetic_race import ScenarioSpec, replay_scenario

from rtv.pitwall import agent_input
from rtv.pitwall.activity import ActivityLog
from rtv.pitwall.agents.strategist import STRATEGIST, PitCall
from rtv.pitwall.framework import (
    LATE_PREFIX,
    AgentRuntime,
    EventRef,
    RadioMessage,
    RadioPriority,
)
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ProviderResponse, ScriptedProvider
from rtv.pitwall.radio import RadioFeed
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.store import journal
from rtv.store.journal import SessionRecorder


@pytest.fixture(scope="module")
def engine() -> RaceStateEngine:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    return engine


def _message(text="Plan to box lap 13.", *, lap=4, subject="pit_stop", t=300.0, **kw):
    return RadioMessage(
        agent=kw.pop("agent", "strategist"),
        priority=RadioPriority.ADVISORY,
        spoken_text=text,
        detail_text=text,
        event_ref=EventRef(
            event_type="pit_window_open", key=f"pit_window_open:L{lap}", tick=int(t * 60),
            session_time=t, lap=lap,
        ),
        session_time=t,
        subject=subject,
        **kw,
    )


def _event(t: float, lap: int, kind: str = "pit_window_open") -> RaceEvent:
    return RaceEvent(
        event_type=EventType(kind),
        tick=int(t * 60),
        session_time=t,
        lap=lap,
        severity=Severity.ADVISORY,
        payload={"lap": lap},
    )


def _call(text: str) -> PitCall:
    return PitCall(
        priority=RadioPriority.ADVISORY,
        spoken_text=text,
        detail_text=text,
        recommendation="stay_out",
        confidence="high",
        rationale="No figures asserted.",
    )


def _first_message(call: dict) -> str:
    return call["messages"][0]["content"]


# ---- the pure context builder ---------------------------------------------------
def test_each_status_reads_as_one_line():
    base = {"lap": 4, "session_time": 300.0, "spoken_text": "Plan to box lap 13."}
    line = agent_input.previous_line
    assert line({**base, "status": "aired"}) == 'You said on lap 4: "Plan to box lap 13." (aired)'
    assert line({**base, "status": "late", "late_by_s": 9.2}) == (
        'You said on lap 4: "Plan to box lap 13." (aired late by 9 s)'
    )
    assert line({**base, "status": "expired", "expired_reason": "past the pit entry"}) == (
        'You said on lap 4: "Plan to box lap 13." (never aired, dropped: past the pit entry)'
    )
    assert line({**base, "status": "queued"}).endswith("(not aired yet, still queued)")
    assert line({**base, "lap": None, "status": "aired"}).startswith("You said at 300s:")


def test_previous_call_reads_the_radio_fields_and_drops_the_late_tag():
    late = _message(late=True, late_by_s=9.0)
    late.spoken_text = LATE_PREFIX + late.spoken_text
    entry = agent_input.previous_call(late)
    assert entry["status"] == "late" and entry["late_by_s"] == 9.0
    assert entry["spoken_text"] == "Plan to box lap 13."
    assert entry["message_id"] == late.message_id and entry["lap"] == 4

    dropped = _message(expired=True, expired_reason="lap 5 is over", late_by_s=3.0)
    entry = agent_input.previous_call(dropped)
    assert (entry["status"], entry["expired_reason"], entry["late_by_s"]) == (
        "expired", "lap 5 is over", None,
    )
    assert agent_input.previous_call(_message(), queued=True)["status"] == "queued"
    assert agent_input.previous_call(_message())["status"] == "aired"


def test_instruction_puts_the_earlier_calls_before_the_radio_and_keeps_two():
    calls = [
        agent_input.previous_call(_message(f"Call {i}.", subject=f"s{i}", lap=i))
        for i in range(4)
    ]
    record = agent_input.build_input(
        STRATEGIST,
        system_prompt="p",
        state_slice={},
        event=_event(400.0, 6).to_api(),
        radio_history=[_message("Someone else.", agent="spotter")],
        previous=calls,
    )
    assert [p["spoken_text"] for p in record["previous"]] == ["Call 2.", "Call 3."]
    text = _first_message(agent_input.model_input(record))
    assert "YOUR EARLIER CALLS" in text and "revise or confirm" in text
    assert text.index("YOUR EARLIER CALLS") < text.index("RECENT RADIO")
    assert 'You said on lap 2: "Call 2." (aired)' in text
    assert "Call 1." not in text


def test_no_earlier_calls_means_no_section():
    record = agent_input.build_input(
        STRATEGIST, system_prompt="p", state_slice={}, event=_event(400.0, 6).to_api()
    )
    assert record["previous"] == []
    assert "YOUR EARLIER CALLS" not in _first_message(agent_input.model_input(record))


def test_a_version_1_record_rebuilds_exactly_as_before():
    record = agent_input.build_input(
        STRATEGIST, system_prompt="p", state_slice={"a": 1}, event=_event(400.0, 6).to_api(),
        radio_history=[_message("Box.", agent="spotter")],
    )
    old = {k: v for k, v in record.items() if k != "previous"} | {"v": 1}
    rebuilt = agent_input.model_input(old)
    assert _first_message(rebuilt) == agent_input.instruction(
        old["event"], old["radio"], old["prefetched"]
    )
    assert rebuilt == agent_input.model_input(record)
    with pytest.raises(ValueError):
        agent_input.model_input({**record, "v": 99})


# ---- journalled, byte-identical -------------------------------------------------
async def test_the_journal_rebuilds_a_first_call_with_earlier_calls_byte_identical(
    tmp_path, engine
):
    log = ActivityLog(wall_clock=None)
    recorder = SessionRecorder(tmp_path, wall_clock=lambda: 1_790_000_000.0)
    log.subscribe_callback(recorder.on_activity)
    run_id = recorder.begin("s1", "replay")
    provider = ScriptedProvider(lambda **_: ProviderResponse())
    runtime = AgentRuntime(STRATEGIST, provider, tracer=log, retry_attempts=0)
    previous = [agent_input.previous_call(_message("Plan to box lap 13.", late=True,
                                                   late_by_s=9.0))]
    await runtime.invoke(_event(842.5, 7), engine.snapshot(), previous_calls=previous)
    recorder.end()

    [start] = journal.agent_inputs(journal.run_path(tmp_path, "s1", run_id))
    assert start["data"]["input"]["v"] == 2
    assert start["data"]["input"]["previous"] == previous
    rebuilt = agent_input.model_input(start)

    def dump(value) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    sent = provider.calls[0]
    assert dump(rebuilt["messages"][:1]) == dump(sent["messages"][:1])
    assert dump(rebuilt["system"]) == dump(sent["system"])
    assert '"Plan to box lap 13." (aired late by 9 s)' in _first_message(sent)


# ---- the orchestrator keeps and reports the agent's own calls -------------------
def _orchestrator(engine, texts):
    answers = iter(texts)
    provider = ScriptedProvider(lambda **_: _call(next(answers)))
    spec = replace(STRATEGIST, policy=None)
    orch = PitwallOrchestrator(engine, provider, [spec], feed=RadioFeed(), clock=None)
    orch.max_age_s["strategist"] = 2.0  # a call 9 s old airs tagged late
    return orch, provider


async def test_the_next_run_sees_the_last_call_and_what_the_radio_did_with_it(engine):
    orch, provider = _orchestrator(engine, ["Plan to box lap 13.", "Box lap 13 confirmed."])
    first = await orch._run_agent("strategist", _event(300.0, 4))
    assert orch.previous_calls("strategist")[0]["status"] == "queued"
    orch.publish(first)
    assert orch.previous_calls("strategist")[0]["status"] == "queued"
    orch.feed.pump(first.computed_at_session_time + 9.0)  # airs, late by 9 s
    [entry] = orch.previous_calls("strategist")
    assert entry["status"] == "late" and entry["spoken_text"] == "Plan to box lap 13."

    await orch._run_agent("strategist", _event(420.0, 6))
    shown = _first_message(provider.calls[-1])
    assert 'You said on lap 4: "Plan to box lap 13." (aired late by 9 s)' in shown
    # One subject: the newer call replaces the older one, it is not a log.
    [entry] = orch.previous_calls("strategist")
    assert entry["spoken_text"] == "Box lap 13 confirmed."
    assert orch.previous_calls("coach") == []

    orch.reset()
    assert orch.previous_calls("strategist") == []


async def test_dropped_calls_are_reported_and_refusals_are_not_remembered(engine):
    orch, _ = _orchestrator(engine, ["Box this lap."])
    first = await orch._run_agent("strategist", _event(300.0, 4))
    orch.feed.expire(first, "past the pit entry")
    [entry] = orch.previous_calls("strategist")
    assert entry["status"] == "expired" and entry["expired_reason"] == "past the pit entry"

    refused = _message("Standby.", subject="pit_stop:withheld", refused=True)
    orch._remember_call(refused)
    template = _message("Car left.", agent="strategist", subject="other", source="template")
    orch._remember_call(template)
    assert [e["subject"] for e in orch.previous_calls("strategist")] == ["pit_stop"]


async def test_at_most_two_subjects_newest_last(engine):
    orch, _ = _orchestrator(engine, [])
    for i, subject in enumerate(["a", "b", "c", "a"]):
        orch._remember_call(_message(f"Call {i}.", subject=subject, lap=i))
    assert [e["spoken_text"] for e in orch.previous_calls("strategist")] == ["Call 2.", "Call 3."]
