"""Graph-coach loop tests — hermetic, driven by a scripted model. No network, no key, no cost.

The refusal test comes first, deliberately: it is the behaviour whose regression matters most —
a coach that invents a tyre temperature instead of refusing. The happy path and the in-loop
citation validator follow.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from evals.fixtures import build_fixture  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from rtv.coaching.agent import REFUSE_TOOL, SUBMIT_COACHING_TOOL, run_coach  # noqa: E402
from rtv.coaching.agent.prompts import FORCED_ANSWER_PROMPT  # noqa: E402

FIND_ARGS = {"session_id": "synthetic", "main_lap": 5, "ref_lap": 3}

# A faithful answer for the t4_brake_loss fixture: T4 loses 0.30s, cited by net_dt.
GOOD_T4 = {
    "headline": "Biggest gain is in T4 — you are braking late.",
    "priorities": [{"corner": "T4", "why": "late braking", "gain_s": 0.30}],
    "one_lap_focus": "Brake a car-length later into T4.",
    "claims": [
        {
            "statement": "T4 minimum speed is well down on the reference.",
            "corner": "T4",
            "citations": [{"corner": "T4", "figure": "net_dt", "value": 0.30, "unit": "s"}],
            "confidence": "high",
        }
    ],
    "corners_examined": ["T1", "T4", "T7"],
    "could_not_determine": [],
}


# ── The refusal path — written before the happy path, on purpose ────────────────────────────


def test_refuses_tyre_temp_instead_of_confabulating(make_model, ai):
    """The headline behaviour: a channel the session never captured produces a grounded refusal
    naming what is missing and what IS available — never an invented temperature."""
    model = make_model(
        [
            ai("list_available_channels", {"session_id": "synthetic"}),
            ai(
                REFUSE_TOOL,
                {
                    "reason": "channel_not_captured",
                    "channels_required": ["TyreTemp"],
                    "suggestion": "Enable tyre-temperature logging in the telemetry capture.",
                },
            ),
        ]
    )
    provider = build_fixture("no_tyre_temp")
    state = run_coach("What were my tyre temps through T4?", provider, model)

    assert state.answer is None
    assert state.refusal is not None
    assert state.refusal.reason == "channel_not_captured"
    assert state.refusal.channels_required == ["TyreTemp"]
    # Grounded in the tool result: the available list is the session's real channels, and the
    # missing channel is not smuggled into it.
    assert set(state.refusal.channels_available) == set(provider.channels)
    assert "TyreTemp" not in state.refusal.channels_available
    assert state.validation_retries == 0


def test_refusal_channels_available_is_code_filled_from_the_provider(make_model, ai):
    """If the model refuses after retrieving findings (so the session is known) but without
    listing channels, the grounding still comes from code, never from model self-knowledge."""
    model = make_model(
        [
            ai("get_lap_findings", FIND_ARGS),
            ai(REFUSE_TOOL, {"reason": "channel_not_captured", "channels_required": ["TyreTemp"]}),
        ]
    )
    provider = build_fixture("no_tyre_temp")
    state = run_coach("Tyre temps in T4?", provider, model)

    assert state.refusal is not None
    assert set(state.refusal.channels_available) == set(provider.channels)


def test_missing_session_refuses(make_model, ai):
    """A session that does not exist: get_lap_findings errors, the coach refuses."""
    model = make_model(
        [
            ai("get_lap_findings", FIND_ARGS),
            ai(REFUSE_TOOL, {"reason": "session_not_found", "channels_required": []}),
        ]
    )
    state = run_coach("Review my lap.", build_fixture("missing_session"), model)
    assert state.refusal is not None
    assert state.refusal.reason == "session_not_found"


# ── The happy path ──────────────────────────────────────────────────────────────────────────


def test_answers_with_validated_structure_and_visible_trace(make_model, ai):
    model = make_model([ai("get_lap_findings", FIND_ARGS), ai(SUBMIT_COACHING_TOOL, GOOD_T4)])
    state = run_coach("Where am I losing time?", build_fixture("t4_brake_loss"), model)

    assert state.refusal is None
    assert state.answer is not None
    assert state.answer.priorities[0].corner == "T4"
    assert state.answer.claims[0].citations[0].corner == "T4"
    # Provenance is filled by code from the retrieved findings, never by the model.
    assert (state.answer.session_id, state.answer.main_lap, state.answer.ref_lap) == (
        "synthetic",
        5,
        3,
    )
    assert state.tools_called == ["get_lap_findings"]
    assert "T4" in state.corners_seen
    assert state.validation_retries == 0


# ── Termination: the iteration cap ──────────────────────────────────────────────────────────


def test_iteration_cap_forces_a_final_turn_with_data_tools_unbound(make_model, ai):
    degraded = {**GOOD_T4, "could_not_determine": ["full lap analysis"]}
    model = make_model(
        [
            ai("get_lap_findings", FIND_ARGS),
            ai("get_lap_findings", FIND_ARGS),
            ai(SUBMIT_COACHING_TOOL, degraded),
        ]
    )
    state = run_coach(
        "Exhaustively analyse everything.", build_fixture("t4_brake_loss"), model, max_iterations=2
    )

    # The final invocation had only the answer channels bound — no data tools.
    assert model.invocations[-1]["tools"] == [SUBMIT_COACHING_TOOL, REFUSE_TOOL]
    # The degraded-honest instruction actually reached the model.
    assert any(
        isinstance(m, HumanMessage) and m.content == FORCED_ANSWER_PROMPT
        for m in model.invocations[-1]["messages"]
    )
    assert state.answer is not None
    assert state.forced_final is True


# ── Validation as a turn, not an error ──────────────────────────────────────────────────────


def test_validation_failure_feeds_back_field_name_and_escape_instruction(make_model, ai):
    uncited = {
        "headline": "T4 is your worst corner.",
        "priorities": [],
        "one_lap_focus": "Work on T4.",
        "claims": [
            {
                "statement": "T4 is slow.",
                "corner": "T4",
                "citations": [],  # structurally impossible — must bounce
                "confidence": "high",
            }
        ],
        "corners_examined": ["T4"],
    }
    model = make_model(
        [
            ai("get_lap_findings", FIND_ARGS),
            ai(SUBMIT_COACHING_TOOL, uncited),
            ai(SUBMIT_COACHING_TOOL, GOOD_T4),
        ]
    )
    state = run_coach("Where am I losing time?", build_fixture("t4_brake_loss"), model)

    assert state.answer is not None
    assert state.validation_retries == 1
    feedback = [
        m
        for m in state.messages
        if isinstance(m, ToolMessage) and "failed validation" in str(m.content)
    ]
    assert feedback, "the validation error must re-enter the conversation"
    text = str(feedback[0].content)
    assert "citations" in text  # names the failing field
    assert "could_not_determine" in text  # the escape instruction


def test_confabulated_figure_is_caught_against_the_findings(make_model, ai):
    """The model can cite a corner that doesn't exist; it cannot lie to the retrieved findings."""
    confabulated = {
        "headline": "T9 is costing you the most.",
        "priorities": [{"corner": "T9", "why": "invented", "gain_s": 0.9}],
        "one_lap_focus": "Fix T9.",
        "claims": [
            {
                "statement": "T9 loses nearly a second.",
                "corner": "T9",
                "citations": [{"corner": "T9", "figure": "net_dt", "value": 0.9, "unit": "s"}],
                "confidence": "high",
            }
        ],
        "corners_examined": ["T9"],
    }
    model = make_model(
        [
            ai("get_lap_findings", FIND_ARGS),
            ai(SUBMIT_COACHING_TOOL, confabulated),
            ai(REFUSE_TOOL, {"reason": "insufficient_data", "channels_required": []}),
        ]
    )
    state = run_coach("Is T9 my problem?", build_fixture("t4_brake_loss"), model)

    feedback = [
        m for m in state.messages if isinstance(m, ToolMessage) and "hallucinated" in str(m.content)
    ]
    assert feedback, "the figure cross-check must catch the confabulation"
    # Given the escape, the model legally withdrew into a refusal.
    assert state.answer is None
    assert state.refusal is not None


def test_retry_exhaustion_degrades_to_a_code_built_answer_not_an_exception(make_model, ai):
    bad = {
        "headline": "T9 wins.",
        "priorities": [{"corner": "T9", "why": "x", "gain_s": 0.9}],
        "one_lap_focus": "T9.",
        "claims": [],
        "corners_examined": ["T9"],
    }
    model = make_model(
        [
            ai("get_lap_findings", FIND_ARGS),
            ai(SUBMIT_COACHING_TOOL, bad),
            ai(SUBMIT_COACHING_TOOL, bad),
            ai(SUBMIT_COACHING_TOOL, bad),
        ]
    )
    question = "What should I fix?"
    state = run_coach(question, build_fixture("t4_brake_loss"), model)

    assert state.validation_retries == 3  # two retries burned, third failure exhausts
    assert state.answer is not None
    assert state.answer.claims == []
    assert question in state.answer.could_not_determine


def test_prose_final_reply_is_nudged_into_the_structured_channel(make_model, ai):
    model = make_model(
        [
            ai("get_lap_findings", FIND_ARGS),
            AIMessage(content="Just brake later, you'll be fine!"),
            ai(SUBMIT_COACHING_TOOL, GOOD_T4),
        ]
    )
    state = run_coach("Coach me.", build_fixture("t4_brake_loss"), model)

    assert state.answer is not None
    assert state.validation_retries == 1
