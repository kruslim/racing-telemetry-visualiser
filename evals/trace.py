"""The reviewable trace — a serializable snapshot of one coach run.

``CoachTrace`` is what the human reviewer reads, the (deferred) judge scores, and the eval
store persists. It carries no LangChain message objects — those don't serialize cleanly and the
reviewer doesn't need them; it carries the reconstructed ``(tool, args, result)`` sequence plus
the harvested provenance (corners seen, channels available, the findings actually retrieved) and
the terminal outcome. Trace visibility is what lets a reviewer attribute a failure to retrieval,
tool use, or generation rather than guess at it.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from rtv.coaching.agent.contracts import DEGRADED_HEADLINE, CoachingAnswer, CoachRefusal
from rtv.coaching.agent.graph import REFUSE_TOOL, SUBMIT_COACHING_TOOL
from rtv.coaching.agent.state import CoachState

Outcome = Literal["answer", "refusal", "degraded"]


class ToolInvocation(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    is_error: bool = False


class CoachTrace(BaseModel):
    """A reviewable, serializable snapshot of one coach run."""

    trace_id: str
    question: str
    outcome: Outcome

    tool_invocations: list[ToolInvocation] = Field(default_factory=list)

    tools_called: list[str] = Field(default_factory=list)
    corners_seen: list[str] = Field(default_factory=list)
    channels_available: list[str] | None = None
    findings_by_key: dict[str, Any] = Field(default_factory=dict)

    answer: CoachingAnswer | None = None
    refusal: CoachRefusal | None = None

    iteration: int = 0
    forced_final: bool = False
    validation_retries: int = 0

    @classmethod
    def from_state(cls, state: CoachState, trace_id: str) -> CoachTrace:
        """Build the snapshot from a terminal ``CoachState``.

        Tool invocations pair each assistant tool-call with its result message. The two virtual
        answer-channel tools (``submit_coaching``, ``refuse``) are excluded — they are the
        outcome, already captured in ``answer`` / ``refusal``, not part of the retrieval trace.
        """
        results_by_id: dict[str, Any] = {}
        for msg in state.messages:
            call_id = getattr(msg, "tool_call_id", None)
            if call_id is not None:
                results_by_id[call_id] = msg

        invocations: list[ToolInvocation] = []
        for msg in state.messages:
            for call in getattr(msg, "tool_calls", None) or []:
                if call["name"] in (SUBMIT_COACHING_TOOL, REFUSE_TOOL):
                    continue
                result_msg = results_by_id.get(call["id"])
                payload: dict[str, Any] = {}
                if result_msg is not None:
                    try:
                        payload = json.loads(result_msg.content)
                    except (json.JSONDecodeError, TypeError):
                        payload = {"raw": str(getattr(result_msg, "content", ""))}
                invocations.append(
                    ToolInvocation(
                        name=call["name"],
                        arguments=call.get("args", {}) or {},
                        result=payload,
                        is_error="error" in payload,
                    )
                )

        return cls(
            trace_id=trace_id,
            question=state.question,
            outcome=_classify(state),
            tool_invocations=invocations,
            tools_called=list(state.tools_called),
            corners_seen=list(state.corners_seen),
            channels_available=state.channels_available,
            findings_by_key=dict(state.findings_by_key),
            answer=state.answer,
            refusal=state.refusal,
            iteration=state.iteration,
            forced_final=state.forced_final,
            validation_retries=state.validation_retries,
        )

    def render(self) -> str:
        """A plain-text rendering of the full trace — the reviewer UI."""
        lines = [
            f"trace {self.trace_id}  [{self.outcome.upper()}]",
            f"Q: {self.question}",
            "",
            "── tool calls ─────────────────────────────",
        ]
        if not self.tool_invocations:
            lines.append("  (none — the coach answered or refused without retrieving findings)")
        for i, inv in enumerate(self.tool_invocations, start=1):
            flag = " ⚠ error" if inv.is_error else ""
            lines.append(f"  {i}. {inv.name}({_short(inv.arguments)}){flag}")
            lines.append(f"       → {_short(inv.result)}")

        lines += ["", "── provenance ─────────────────────────────"]
        lines.append(f"  corners seen:      {', '.join(self.corners_seen) or '(none)'}")
        lines.append(
            f"  channels available: {', '.join(self.channels_available or []) or '(unknown)'}"
        )
        if self.forced_final:
            lines.append("  ⚠ forced final turn (iteration cap tripped)")
        if self.validation_retries:
            lines.append(f"  validation retries: {self.validation_retries}")

        lines += ["", "── outcome ────────────────────────────────"]
        if self.refusal is not None:
            lines.append(f"  REFUSAL ({self.refusal.reason}): {self.refusal.suggestion or ''}")
            lines.append(f"    required: {', '.join(self.refusal.channels_required) or '(none)'}")
        elif self.answer is not None:
            lines.append(f"  {self.answer.headline}")
            for p in self.answer.priorities:
                lines.append(f"    → {p.corner}: {p.why} (+{p.gain_s:.3f}s)")
            for claim in self.answer.claims:
                cited = ", ".join(
                    f"{c.corner}.{c.figure}={c.value}{c.unit}" for c in claim.citations
                )
                lines.append(f"    • [{claim.confidence}] {claim.statement}  ⟵ {cited}")
            if self.answer.could_not_determine:
                lines.append(
                    f"  could not determine: {'; '.join(self.answer.could_not_determine)}"
                )
        return "\n".join(lines)

    def to_judge_payload(self) -> dict[str, Any]:
        """The dict a judge sees — the findings + tool trace, not just the final coaching, so it
        can check grounding against ground truth (the deferred calibrated judge uses this)."""
        return {
            "question": self.question,
            "outcome": self.outcome,
            "tool_invocations": [inv.model_dump(mode="json") for inv in self.tool_invocations],
            "corners_seen": self.corners_seen,
            "channels_available": self.channels_available,
            "findings": list(self.findings_by_key.values()),
            "answer": self.answer.model_dump(mode="json") if self.answer else None,
            "refusal": self.refusal.model_dump(mode="json") if self.refusal else None,
        }


def _classify(state: CoachState) -> Outcome:
    if state.refusal is not None:
        return "refusal"
    if state.answer is not None and state.answer.headline == DEGRADED_HEADLINE:
        return "degraded"
    if state.answer is not None:
        return "answer"
    return "degraded"


def _short(obj: Any, limit: int = 160) -> str:
    text = json.dumps(obj, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"
