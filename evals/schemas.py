"""The eval contracts — the racing analog of canopy's ``evals/schemas.py``.

Every schema here is deliberately structured. The load-bearing choice is that ``ErrorType`` is
**not generic**: each member maps to a named weak point of this coaching pipeline, so a label
points at a specific fix — a sentence in a tool description, the citation validator, a refusal
path that didn't fire. A generic thumbs-down tells you nothing about which defense failed.

``ReviewFeedback`` and ``CalibrationReport`` (the HITL review gate and judge calibration) land
with the deferred follow-up phase; this module ships the taxonomy + ``EvalCase`` the hermetic
regression suite needs now.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ErrorType(StrEnum):
    """Failure taxonomy for the coach, one member per known weak point.

    Each label names the defense it corresponds to, so a label is a pointer to a fix.
    """

    HALLUCINATED_FIGURE = "hallucinated_figure"  # cited a number no finding supports (validator)
    WRONG_PRIORITY_CORNER = "wrong_priority_corner"  # top priority isn't the biggest loss
    UNGROUNDED_CUE = "ungrounded_cue"  # a cue not tied to any retrieved finding
    FABRICATED_BRAKE_POINT = "fabricated_brake_point"  # a brake-metre figure with no diagnostic
    MISSED_LOCKUP = "missed_lockup"  # a lock-up diagnostic the advice ignored
    UNIT_ERROR = "unit_error"  # a figure reported in the wrong unit, or none
    GENERIC_ADVICE = "generic_advice"  # vague coaching that engages no specific finding
    OVERCONFIDENT = "overconfident"  # high confidence on thin evidence
    MISSED_REFUSAL = "missed_refusal"  # coached a question the telemetry couldn't answer
    FALSE_REFUSAL = "false_refusal"  # refused a question the telemetry could answer


class Severity(StrEnum):
    """How much a failure matters — phrasing vs. a wrong call the driver acts on."""

    COSMETIC = "cosmetic"  # phrasing; the coaching is right
    MISLEADING = "misleading"  # a careful driver would be misled
    UNSAFE = "unsafe"  # would send the driver the wrong way (brake later where they should earlier)


# Where a failure originated — the payoff of the trace: a reviewer who sees the tool results can
# say a failure was retrieval, tool use, or generation, not just mark it "bad."
FailureStage = Literal["retrieval", "tool_use", "generation", "none"]


class EvalCase(BaseModel):
    """One regression-suite row: a question, a named hermetic fixture, and what "good" means.

    ``source_fixture`` is a *name* resolved by ``evals.fixtures.build_fixture`` — the case stays
    ignorant of how the findings are produced. The ``must_cite`` / ``must_not_cite`` pair is the
    mechanical, deterministic half of scoring; the fuzzy half is the (deferred) judge's.
    """

    case_id: str
    question: str
    source_fixture: str  # a key into evals.fixtures.build_fixture

    expected_outcome: Literal["answer", "refusal"]
    must_cite_corners: list[str] = Field(default_factory=list)
    must_not_cite_corners: list[str] = Field(default_factory=list)  # confabulation guard
    expected_top_priority: str | None = None
    expected_refusal_reason: str | None = None

    origin: Literal["handwritten", "from_review"] = "handwritten"
    source_trace_id: str | None = None
    error_types_observed: list[ErrorType] = Field(default_factory=list)

    @model_validator(mode="after")
    def refusal_fields_match_outcome(self) -> EvalCase:
        if self.expected_outcome == "answer" and self.expected_refusal_reason is not None:
            raise ValueError("An 'answer' case cannot specify expected_refusal_reason.")
        if self.expected_outcome == "refusal" and self.must_cite_corners:
            raise ValueError("A 'refusal' case cannot require cited corners — it cites nothing.")
        if self.origin == "from_review" and self.source_trace_id is None:
            raise ValueError("A 'from_review' case must record the source_trace_id it grew from.")
        return self
