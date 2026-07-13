"""Hard assertions — the deterministic, cheap tier of scoring.

Two-tier scoring: **hard assertions** are objectively checkable structural properties (did it
refuse? did it cite the right corner? is the top priority the real biggest loss?) —
deterministic, free, run on every commit. **Judge scoring** is fuzzy, expensive, and deferred.
This module is the first tier: a ``CoachTrace`` and an ``EvalCase`` in, a pass/fail out. No LLM.

The confabulation guard is the sharpest check: a ``must_not_cite`` corner may not appear
anywhere the coach could launder it into truth — not cited in the answer, not in the corners it
saw. A refusal is still allowed to *name* a missing channel in ``channels_required``; that is the
grounded refusal doing its job.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from evals.schemas import EvalCase
from evals.trace import CoachTrace


class AssertionResult(BaseModel):
    name: str
    passed: bool
    detail: str


class CaseResult(BaseModel):
    case_id: str
    trace_id: str
    outcome: str
    passed: bool
    assertions: list[AssertionResult] = Field(default_factory=list)

    @property
    def failures(self) -> list[AssertionResult]:
        return [a for a in self.assertions if not a.passed]


def _cited_corners(trace: CoachTrace) -> set[str]:
    if trace.answer is None:
        return set()
    cited = {c.corner for claim in trace.answer.claims for c in claim.citations}
    cited |= {p.corner for p in trace.answer.priorities}
    return cited


def check_case(case: EvalCase, trace: CoachTrace) -> CaseResult:
    """Score one trace against one case with deterministic structural assertions."""
    checks: list[AssertionResult] = []

    # 1. Outcome. A "degraded" run satisfies neither answer nor refusal — the coach failing to
    #    express itself — and must not pass as either.
    outcome_ok = (case.expected_outcome == "refusal" and trace.outcome == "refusal") or (
        case.expected_outcome == "answer" and trace.outcome == "answer"
    )
    checks.append(
        AssertionResult(
            name="outcome",
            passed=outcome_ok,
            detail=f"expected {case.expected_outcome}, got {trace.outcome}",
        )
    )

    # 2. Refusal reason, when specified.
    if case.expected_refusal_reason is not None:
        got = trace.refusal.reason if trace.refusal else None
        checks.append(
            AssertionResult(
                name="refusal_reason",
                passed=got == case.expected_refusal_reason,
                detail=f"expected reason {case.expected_refusal_reason!r}, got {got!r}",
            )
        )

    cited = _cited_corners(trace)

    # 3. Required citations — the coaching must rest on these corners.
    for corner in case.must_cite_corners:
        checks.append(
            AssertionResult(
                name=f"must_cite:{corner}",
                passed=corner in cited,
                detail=f"{corner} {'cited' if corner in cited else 'NOT cited'}",
            )
        )

    # 4. Confabulation guard — the forbidden corner appears nowhere it could pass as real.
    laundered = cited | set(trace.corners_seen)
    for corner in case.must_not_cite_corners:
        checks.append(
            AssertionResult(
                name=f"must_not_cite:{corner}",
                passed=corner not in laundered,
                detail=(
                    f"{corner} leaked into the trace as real"
                    if corner in laundered
                    else f"{corner} correctly absent from cited/seen"
                ),
            )
        )

    # 5. Top-priority correctness, when specified.
    if case.expected_top_priority is not None:
        top = (
            trace.answer.priorities[0].corner
            if trace.answer and trace.answer.priorities
            else None
        )
        checks.append(
            AssertionResult(
                name="top_priority",
                passed=top == case.expected_top_priority,
                detail=f"expected top {case.expected_top_priority!r}, got {top!r}",
            )
        )

    return CaseResult(
        case_id=case.case_id,
        trace_id=trace.trace_id,
        outcome=trace.outcome,
        passed=all(c.passed for c in checks),
        assertions=checks,
    )
