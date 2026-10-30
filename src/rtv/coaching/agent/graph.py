"""The coach agent as a LangGraph state graph (the racing analog of the reference
project's ``agent/graph.py``).

Nodes and edges:

    START -> agent -> (conditional) tools | validate | refuse
    tools -> agent                       [the cycle]
    validate -> (conditional) END | agent    [retry on schema / figure failure]
    refuse -> END

Why a graph rather than the hand-rolled ``asyncio.gather`` pipeline in ``orchestrator.py``:
explicit, inspectable control flow — conditional edges, a tool-calling loop, an in-loop
citation validator, and (in the deferred HITL phase) interrupts for human review and a trace
that can be replayed. The orchestrator stays as the multi-agent *showcase*; this is the
model-driven, refusal-capable path. They share only the ``LapFindings`` ground truth.

Termination is engineered, not hoped for:

1. the model coaches (via ``submit_coaching`` — structured output through the tool channel);
2. the iteration cap trips -> one final turn with the data tools unbound and an instruction to
   coach honestly from what was retrieved (the answer/refuse channels stay bound);
3. the grounded refusal path.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from rtv.coaching.agent.contracts import (
    CoachingAnswer,
    CoachingAnswerPayload,
    CoachRefusal,
    RefusalPayload,
    degraded_answer,
)
from rtv.coaching.agent.executor import CoachToolExecutor
from rtv.coaching.agent.prompts import (
    FORCED_ANSWER_PROMPT,
    NOT_STRUCTURED_FEEDBACK,
    SYSTEM_PROMPT,
    VALIDATION_FEEDBACK_TEMPLATE,
)
from rtv.coaching.agent.provider import FindingsProvider
from rtv.coaching.agent.registry import GET_LAP_FINDINGS_TOOL, LIST_CHANNELS_TOOL
from rtv.coaching.agent.state import CoachState
from rtv.coaching.agent.tool_schema import inline_schema_defs
from rtv.coaching.agent.validation import check_figures

SUBMIT_COACHING_TOOL = "submit_coaching"
REFUSE_TOOL = "refuse"

# The two virtual tools. The model fills only the judgment fields; code fills the facts it
# already knows (provenance, the available-channel list a refusal cites).
_SUBMIT_COACHING_DEF = {
    "name": SUBMIT_COACHING_TOOL,
    "description": (
        "Deliver the final coaching. Every claim and priority must cite the corner + figure "
        "from the findings it rests on. List anything the findings could not answer in "
        "could_not_determine rather than guessing."
    ),
    "input_schema": inline_schema_defs(CoachingAnswerPayload.model_json_schema()),
}
_REFUSE_DEF = {
    "name": REFUSE_TOOL,
    "description": (
        "Decline to coach because the telemetry cannot answer the question (a channel was "
        "not captured, the session/lap does not exist, or the data is insufficient). A "
        "grounded refusal is a correct outcome. Only use after checking what is available."
    ),
    "input_schema": inline_schema_defs(RefusalPayload.model_json_schema()),
}


def _tool_calls(message: Any) -> list[dict]:
    return getattr(message, "tool_calls", None) or []


def _merged_findings(state: CoachState) -> dict:
    """All retrieved corner findings, merged into one dict for figure validation."""
    corners: list[dict] = []
    for findings in state.findings_by_key.values():
        corners.extend(findings.get("corners", []))
    return {"corners": corners}


def build_graph(model: Any, executor: CoachToolExecutor):
    """Compile the coach graph over a bound chat ``model`` and a tool ``executor``.

    ``model`` needs only ``bind_tools(defs).invoke(messages) -> AIMessage`` — satisfied by any
    LangChain chat model and by a scripted fake in tests (no network, no key).
    """

    def agent_node(state: CoachState) -> dict:
        history = list(state.messages)
        updates: list[Any] = []
        forced = state.iteration >= state.max_iterations

        if forced and not state.forced_final:
            nudge = HumanMessage(content=FORCED_ANSWER_PROMPT)
            history = [*history, nudge]
            updates.append(nudge)

        if forced:
            tools = [_SUBMIT_COACHING_DEF, _REFUSE_DEF]  # data tools unbound
        else:
            tools = [*executor.definitions(), _SUBMIT_COACHING_DEF, _REFUSE_DEF]

        response: AIMessage = model.bind_tools(tools).invoke(
            [SystemMessage(content=SYSTEM_PROMPT), *history]
        )
        updates.append(response)
        return {
            "messages": updates,
            "iteration": state.iteration + 1,
            "forced_final": forced or state.forced_final,
        }

    def route_from_agent(state: CoachState) -> Literal["tools", "validate", "refuse"]:
        names = {call["name"] for call in _tool_calls(state.messages[-1])}
        if SUBMIT_COACHING_TOOL in names:
            return "validate"
        if REFUSE_TOOL in names:
            return "refuse"
        if names:
            return "tools"
        # Prose where structured coaching belongs: the validate node turns it into feedback
        # and a bounded retry rather than accepting or crashing.
        return "validate"

    def tools_node(state: CoachState) -> dict:
        """Execute the requested tools and harvest the trace as results stream past."""
        new_messages: list[ToolMessage] = []
        tools_called = list(state.tools_called)
        corners_seen = list(state.corners_seen)
        findings_by_key = dict(state.findings_by_key)
        channels = state.channels_available
        session_id, main_lap, ref_lap = state.session_id, state.main_lap, state.ref_lap

        for call in _tool_calls(state.messages[-1]):
            payload = executor.execute(call["name"], call["args"])
            tools_called.append(call["name"])

            if call["name"] == GET_LAP_FINDINGS_TOOL and "error" not in payload:
                # The primary tool's result IS the LapFindings dict — the validator's truth.
                sid = payload.get("session_id")
                m = payload.get("main_lap")
                r = payload.get("ref_lap")
                findings_by_key[f"{sid}:{m}:{r}"] = payload
                for corner in payload.get("corners", []):
                    label = corner.get("label")
                    if label and label not in corners_seen:
                        corners_seen.append(label)
                session_id, main_lap, ref_lap = sid, m, r
            if call["name"] == LIST_CHANNELS_TOOL and "error" not in payload:
                channels = payload.get("channels", [])
                if session_id is None:
                    session_id = payload.get("session_id")

            new_messages.append(
                ToolMessage(
                    content=json.dumps(payload),
                    tool_call_id=call["id"],
                    name=call["name"],
                )
            )

        return {
            "messages": new_messages,
            "tools_called": tools_called,
            "corners_seen": corners_seen,
            "findings_by_key": findings_by_key,
            "channels_available": channels,
            "session_id": session_id,
            "main_lap": main_lap,
            "ref_lap": ref_lap,
        }

    def _validation_failure(state: CoachState, feedback: str, tool_call_id: str | None) -> dict:
        retries = state.validation_retries + 1
        if retries > state.max_validation_retries:
            answer = degraded_answer(
                state.question,
                list(state.corners_seen),
                session_id=state.session_id or "",
                main_lap=state.main_lap or 0,
                ref_lap=state.ref_lap or 0,
            )
            messages: list[Any] = []
            if tool_call_id:
                messages.append(
                    ToolMessage(
                        content="Validation failed and retries are exhausted.",
                        tool_call_id=tool_call_id,
                        name=SUBMIT_COACHING_TOOL,
                    )
                )
            return {"messages": messages, "validation_retries": retries, "answer": answer}

        message: Any
        if tool_call_id:
            message = ToolMessage(
                content=feedback, tool_call_id=tool_call_id, name=SUBMIT_COACHING_TOOL
            )
        else:
            message = HumanMessage(content=feedback)
        return {"messages": [message], "validation_retries": retries}

    def validate_node(state: CoachState) -> dict:
        last = state.messages[-1]
        submit = next(
            (c for c in _tool_calls(last) if c["name"] == SUBMIT_COACHING_TOOL), None
        )
        if submit is None:
            return _validation_failure(state, NOT_STRUCTURED_FEEDBACK, tool_call_id=None)

        errors: list[str] = []
        payload: CoachingAnswerPayload | None = None
        try:
            payload = CoachingAnswerPayload.model_validate(submit["args"])
        except ValidationError as exc:
            errors = [
                f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            ]

        if payload is not None:
            # The highest-value validator: every cited figure must match a finding that was
            # actually retrieved. The in-model validator checks corners the answer *says* it
            # examined; this checks the numbers against ground truth that really came back.
            figure_issues = check_figures(
                [p.model_dump() for p in payload.priorities],
                [c.model_dump() for c in payload.claims],
                _merged_findings(state),
            )
            errors.extend(f"  figures: {issue}" for issue in figure_issues)

        if errors:
            feedback = VALIDATION_FEEDBACK_TEMPLATE.format(errors="\n".join(errors))
            return _validation_failure(state, feedback, tool_call_id=submit["id"])

        answer = CoachingAnswer(
            **payload.model_dump(),
            session_id=state.session_id or "",
            main_lap=state.main_lap or 0,
            ref_lap=state.ref_lap or 0,
        )
        ack = ToolMessage(
            content="Coaching accepted.", tool_call_id=submit["id"], name=SUBMIT_COACHING_TOOL
        )
        return {"messages": [ack], "answer": answer}

    def route_from_validate(state: CoachState) -> Literal["agent", "__end__"]:
        return END if state.answer is not None else "agent"

    def refuse_node(state: CoachState) -> dict:
        """Construct the grounded refusal. The available-channel list comes from a tool result
        (state) or straight from the provider — never from the model."""
        call = next((c for c in _tool_calls(state.messages[-1]) if c["name"] == REFUSE_TOOL), None)
        try:
            payload = RefusalPayload.model_validate(call["args"] if call else {})
        except ValidationError:
            payload = RefusalPayload(reason="insufficient_data", channels_required=[])

        if state.channels_available is not None:
            available = state.channels_available
        elif state.session_id is not None:
            available = executor.available_channels(state.session_id)
        else:
            available = []

        refusal = CoachRefusal(
            question=state.question,
            reason=payload.reason,
            channels_required=payload.channels_required,
            channels_available=available,
            suggestion=payload.suggestion,
        )
        messages: list[Any] = []
        if call:
            messages.append(
                ToolMessage(content="Refusal recorded.", tool_call_id=call["id"], name=REFUSE_TOOL)
            )
        return {"messages": messages, "refusal": refusal}

    graph: StateGraph = StateGraph(CoachState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("validate", validate_node)
    graph.add_node("refuse", refuse_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_from_agent)
    graph.add_edge("tools", "agent")
    graph.add_conditional_edges("validate", route_from_validate)
    graph.add_edge("refuse", END)

    return graph.compile()


def run_coach(
    question: str,
    provider: FindingsProvider,
    model: Any,
    *,
    max_iterations: int = 6,
) -> CoachState:
    """Ask one natural-language coaching question; return the terminal state.

    Exactly one of ``state.answer`` / ``state.refusal`` is set on return, and the trace
    (``tools_called``, ``corners_seen``, ``findings_by_key``) shows how it got there.
    """
    executor = CoachToolExecutor(provider)
    graph = build_graph(model, executor)
    initial = CoachState(
        messages=[HumanMessage(content=question)],
        question=question,
        max_iterations=max_iterations,
    )
    result = graph.invoke(initial, config={"recursion_limit": 4 * max_iterations + 16})
    return CoachState.model_validate(result)
