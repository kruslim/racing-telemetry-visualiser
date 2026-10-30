"""CoachTrace tests — render, judge payload, and outcome classification from a terminal state."""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from evals.fixtures import build_fixture  # noqa: E402
from evals.trace import CoachTrace  # noqa: E402

from rtv.coaching.agent import run_coach  # noqa: E402

FIND_ARGS = {"session_id": "synthetic", "main_lap": 5, "ref_lap": 3}

GOOD_T4 = {
    "headline": "Biggest gain is in T4.",
    "priorities": [{"corner": "T4", "why": "late braking", "gain_s": 0.30}],
    "one_lap_focus": "Brake later into T4.",
    "claims": [
        {
            "statement": "T4 min speed is down on the reference.",
            "corner": "T4",
            "citations": [{"corner": "T4", "figure": "net_dt", "value": 0.30, "unit": "s"}],
            "confidence": "high",
        }
    ],
    "corners_examined": ["T1", "T4", "T7"],
    "could_not_determine": [],
}


def test_trace_from_answer_state_renders_and_exposes_the_tool_calls(make_model, ai):
    model = make_model([ai("get_lap_findings", FIND_ARGS), ai("submit_coaching", GOOD_T4)])
    state = run_coach("Where am I losing time?", build_fixture("t4_brake_loss"), model)

    trace = CoachTrace.from_state(state, trace_id="t1")
    assert trace.outcome == "answer"
    # The primary tool call is in the retrieval trace; the virtual answer tool is not.
    assert [inv.name for inv in trace.tool_invocations] == ["get_lap_findings"]
    assert "T4" in trace.corners_seen

    rendered = trace.render()
    assert "get_lap_findings" in rendered
    assert "T4" in rendered

    payload = trace.to_judge_payload()
    assert payload["outcome"] == "answer"
    assert payload["findings"], "the judge payload must carry the retrieved findings"
    assert payload["answer"] is not None


def test_trace_from_refusal_state_classifies_and_renders(make_model, ai):
    model = make_model(
        [
            ai("list_available_channels", {"session_id": "synthetic"}),
            ai("refuse", {"reason": "channel_not_captured", "channels_required": ["TyreTemp"]}),
        ]
    )
    state = run_coach("Tyre temps?", build_fixture("no_tyre_temp"), model)

    trace = CoachTrace.from_state(state, trace_id="t2")
    assert trace.outcome == "refusal"
    assert trace.refusal is not None
    assert "REFUSAL" in trace.render()
    assert trace.to_judge_payload()["refusal"] is not None
