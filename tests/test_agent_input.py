"""Each agent run's full model input is journalled and rebuilds the exact first call.

Offline: the agents run on ``ScriptedProvider``, which keeps every call it was
sent, and the journal is written by the real :class:`SessionRecorder`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from evals.synthetic_race import ScenarioSpec, replay_scenario

from rtv.desktop.model import activity as activity_model
from rtv.desktop.model import review as review_model
from rtv.pitwall import agent_input
from rtv.pitwall.activity import ActivityLog, public
from rtv.pitwall.agents import AGENT_REGISTRY
from rtv.pitwall.agents.strategist import STRATEGIST, PitCall
from rtv.pitwall.framework import AgentRuntime, EventRef, RadioMessage, RadioPriority
from rtv.pitwall.provider import ProviderResponse, ScriptedProvider, tool_use_turn
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.store import journal
from rtv.store.journal import JOURNAL_SCHEMA_VERSION, SessionRecorder
from rtv.store.run_stats import compare_markdown, load_run_stats

RUN3 = Path(__file__).parent / "fixtures" / "journals" / "spa-run3-excerpt.jsonl"


@pytest.fixture(scope="module")
def engine() -> RaceStateEngine:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    return engine


def _event(t: float = 842.5) -> RaceEvent:
    return RaceEvent(
        event_type=EventType("pit_window_open"),
        tick=int(t * 60),
        session_time=t,
        lap=7,
        severity=Severity.ADVISORY,
        payload={"lap": 7, "laps_remaining_fuel": 4.25, "note": "ünïcode ok"},
    )


def _radio(n: int) -> list[RadioMessage]:
    ref = EventRef(event_type="traffic_close", key="traffic_close:L6", tick=1, session_time=1.0)
    return [
        RadioMessage(
            agent="spotter",
            priority=RadioPriority.ADVISORY,
            spoken_text=f"Car close behind, call {i}.",
            detail_text="",
            event_ref=ref,
        )
        for i in range(n)
    ]


def _answer() -> PitCall:
    return PitCall(
        priority=RadioPriority.ADVISORY,
        spoken_text="Stay out, no stop needed.",
        detail_text="Stay out.",
        recommendation="stay_out",
        confidence="high",
        rationale="No figures asserted.",
    )


async def _run_journalled(tmp_path, engine, spec, handler, radio=()):
    """One agent run with the real recorder behind the activity log."""
    log = ActivityLog(wall_clock=None)
    recorder = SessionRecorder(tmp_path, wall_clock=lambda: 1_790_000_000.0)
    log.subscribe_callback(recorder.on_activity)
    run_id = recorder.begin("s1", "replay")
    provider = ScriptedProvider(handler)
    runtime = AgentRuntime(spec, provider, tracer=log, retry_attempts=0)
    message = await runtime.invoke(_event(), engine.snapshot(), radio_history=list(radio))
    recorder.end()
    return journal.run_path(tmp_path, "s1", run_id), provider, runtime, message


def _dump(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.name)
async def test_record_reload_rebuilds_byte_identical_first_call(tmp_path, engine, spec):
    # The handler gives no answer: the run errors, but the first call was sent.
    path, provider, runtime, _ = await _run_journalled(
        tmp_path, engine, spec, lambda **_: ProviderResponse(), radio=_radio(7)
    )
    sent = provider.calls[0]
    [start] = journal.agent_inputs(path)
    rebuilt = agent_input.model_input(start)

    assert _dump(rebuilt["system"]) == _dump(sent["system"])
    assert _dump(rebuilt["messages"][:1]) == _dump(sent["messages"][:1])
    assert rebuilt["model"] == sent["model"] == runtime.spec.model
    assert rebuilt["max_tokens"] == sent["max_tokens"]
    assert rebuilt["tools"] == sent["tools"]
    assert rebuilt["output_contract"] == sent["output_model"]

    data = start["data"]["input"]
    assert data["v"] == agent_input.INPUT_VERSION
    assert data["provider"] == {"name": "scripted"}
    assert [p["tool"] for p in data["prefetched"]] == [name for name, _ in spec.prefetch]
    # Only the five calls the model was shown, but every id is still listed.
    assert [m["spoken_text"] for m in data["radio"]][0] == "Car close behind, call 2."
    assert len(data["radio"]) == 5 and len(start["data"]["context_radio"]) == 7
    assert agent_input.spec_drift(start, runtime.spec, runtime.system_prompt) == []


async def test_run_start_comes_first_and_tool_results_are_whole_in_the_journal(tmp_path, engine):
    def handler(*, turn, **_):
        if turn == 0:
            return tool_use_turn([("get_fuel_projection", {})])
        return _answer()

    path, provider, _, message = await _run_journalled(tmp_path, engine, STRATEGIST, handler)
    assert message is not None and not message.refused
    run = [r for r in journal.iter_records(path) if r.get("run_id") == "strategist-1"]
    kinds = [r["kind"] for r in run]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    # Prefetched tool calls are held until run_start is written, then follow it.
    assert kinds[1:3] == ["tool_call", "tool_call"]
    tools = [r["data"] for r in run if r["kind"] == "tool_call"]
    assert [t["prefetched"] for t in tools[:2]] == [True, True]
    # The model's own call is the third; its whole result rebuilds turn two.
    third = tools[2]
    assert third["tool"] == "get_fuel_projection" and "prefetched" not in third
    second_turn = provider.calls[1]["messages"][2]["content"][0]["content"]
    assert agent_input.as_text(third["result_full"]) == second_turn


def test_live_feed_leaves_out_journal_only_fields():
    record = {"kind": "run_start", "data": {"model": "m", "input": {"v": 1}}}
    assert public(record) == {"kind": "run_start", "data": {"model": "m"}}
    assert record["data"]["input"] == {"v": 1}  # the journal's copy is untouched
    tool = {"kind": "tool_call", "data": {"tool": "x", "result": {}, "result_full": {"a": 1}}}
    assert "result_full" not in public(tool)["data"]
    plain = {"kind": "dispatch", "data": {"accepted": []}}
    assert public(plain) is plain


async def test_spec_drift_names_what_changed(tmp_path, engine):
    path, _, runtime, _ = await _run_journalled(
        tmp_path, engine, STRATEGIST, lambda **_: ProviderResponse()
    )
    [start] = journal.agent_inputs(path)
    changed = replace(runtime.spec, role_prompt="New prompt.", model="other-model")
    later = AgentRuntime(changed, ScriptedProvider(lambda **_: ProviderResponse()))
    assert agent_input.spec_drift(start, changed, later.system_prompt) == ["prompt", "model"]


async def test_new_journal_says_schema_3_and_counts_inputs(tmp_path, engine):
    path, *_ = await _run_journalled(tmp_path, engine, STRATEGIST, lambda **_: ProviderResponse())
    assert JOURNAL_SCHEMA_VERSION == 3
    summary = journal.summarize_run(path)
    assert summary["schema_version"] == 3 and "schema_version" not in summary["meta"]
    stats = load_run_stats(path)
    assert (stats["schema_version"], stats["runs_with_input"]) == (3, 1)


def test_old_journal_reads_as_schema_1_without_inputs():
    summary = journal.summarize_run(RUN3)
    assert summary["schema_version"] == 1 and summary["has_agents"]
    assert journal.agent_inputs(RUN3) == []
    start = next(r for r in journal.iter_records(RUN3) if r["kind"] == "run_start")
    assert agent_input.input_of(start) is None
    with pytest.raises(ValueError, match="schema version 1"):
        agent_input.model_input(start)

    stats = load_run_stats(RUN3)
    assert (stats["schema_version"], stats["runs_with_input"], stats["agent_runs"]) == (1, 0, 4)
    table = compare_markdown([stats, stats], ["run 3", "again"])
    assert "| runs with recorded input | 0 | 0 |" in table

    # The desktop review still plays it.
    records = list(journal.iter_records(RUN3))
    assert review_model.audit_rows(records)
    assert review_model.lane_items(records, [])
    model = activity_model.create_model()
    for rec in records:
        activity_model.apply(model, rec)
    assert model.agents["vehicle_engineer"].runs >= 1
