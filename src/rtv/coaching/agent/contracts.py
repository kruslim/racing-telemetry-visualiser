"""The structured-output contracts for the graph coach (the racing analog of the
reference project's ``agent/contracts.py``).

The schema is a thinking harness, not just a serialization format. Two validators here are
structural, not advisory:

* ``min_length=1`` on ``CoachClaim.citations`` makes an *uncited coaching claim impossible
  to serialize*. A cue like "brake 7 m later into T4" cannot be emitted without pointing at
  the corner + figure in the findings it rests on — grounding enforced by the type system,
  not by hoping the system prompt worked.
* ``cited_corners_were_examined`` cross-references every citation's corner against the
  corners the answer says it examined. The deeper check — cited *figures* vs. the ground
  truth actually retrieved — lives in the validate node (``graph.py``), because only the
  graph state knows what the tools really returned.

Split of authority: the model fills the fields that require judgment; code fills the fields
it already knows. ``session_id``/``main_lap``/``ref_lap`` are facts the code has, so
``CoachingAnswerPayload`` (what the model fills) excludes them and ``CoachingAnswer`` (what
the system emits) adds them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# The findings fields a coaching claim is allowed to cite. Each maps to a real number in a
# ``CornerFinding`` (``models.py``); the validate node checks the value against ground truth.
CitableFigure = Literal["net_dt", "min_main", "min_ref", "brake_m", "time_loss"]


class CoachCitation(BaseModel):
    """A claim points at one figure of one corner in the deterministic findings.

    ``unit`` travels with every value, always — a bare float invites the model to guess the
    unit later (net_dt/time_loss are seconds, min_main/min_ref km/h, brake_m metres).
    """

    corner: str = Field(description="Corner label the figure belongs to, e.g. 'T4'.")
    figure: CitableFigure
    value: float
    unit: str


class CoachClaim(BaseModel):
    statement: str = Field(..., max_length=300)
    corner: str = Field(description="The corner this claim is about, e.g. 'T4'.")
    citations: list[CoachCitation] = Field(
        ...,
        min_length=1,
        description="The specific finding figures this statement rests on. Never empty.",
    )
    confidence: Literal["high", "medium", "low"]


class PriorityCue(BaseModel):
    corner: str
    why: str = Field(max_length=200)
    gain_s: float = Field(description="Time available in this corner — the finding's net_dt.")


class CoachingAnswerPayload(BaseModel):
    """The model-fillable part of the coaching answer — everything except provenance."""

    headline: str = Field(..., max_length=200)
    priorities: list[PriorityCue] = Field(
        default_factory=list, description="Corners to work on, ordered by time available."
    )
    one_lap_focus: str = Field(..., max_length=200)
    claims: list[CoachClaim]
    corners_examined: list[str] = Field(
        default_factory=list, description="Corner labels whose findings were retrieved."
    )
    could_not_determine: list[str] = Field(
        default_factory=list,
        description="Questions or sub-questions the retrieved findings could not answer.",
    )

    @model_validator(mode="after")
    def cited_corners_were_examined(self) -> CoachingAnswerPayload:
        cited = {c.corner for claim in self.claims for c in claim.citations}
        cited |= {p.corner for p in self.priorities}
        missing = cited - set(self.corners_examined)
        if missing:
            raise ValueError(f"Cited corners never examined: {sorted(missing)}")
        return self


class CoachingAnswer(CoachingAnswerPayload):
    """The system's final answer: the model's payload plus code-known provenance."""

    session_id: str
    main_lap: int
    ref_lap: int


class CoachRefusal(BaseModel):
    """A grounded 'the telemetry cannot answer that' — a correct outcome, not a failure.

    ``channels_available`` is filled by code from a tool result (the session's real captured
    channels), never from the model's self-knowledge — models are bad at knowing what they
    do not know, but good at reading a list and noticing an absence.
    """

    question: str
    reason: Literal[
        "channel_not_captured",
        "session_not_found",
        "lap_not_found",
        "insufficient_data",
    ]
    channels_required: list[str]
    channels_available: list[str]
    suggestion: str | None = None


class RefusalPayload(BaseModel):
    """The model-fillable part of a refusal. The question and the available-channel list are
    facts the code already has, so the model is not asked for them."""

    reason: Literal[
        "channel_not_captured",
        "session_not_found",
        "lap_not_found",
        "insufficient_data",
    ]
    channels_required: list[str] = Field(
        default_factory=list,
        description="The channels/data the question would need that are not available.",
    )
    suggestion: str | None = Field(
        default=None,
        description="What capture or session would make the question answerable.",
    )


# The exact headline of a code-built degraded answer. Exposed as a constant so the trace can
# classify an outcome as 'degraded' (validation exhaustion) vs. a genuine 'answer' without
# re-deriving the string in two places (``evals/trace.py``).
DEGRADED_HEADLINE = "The coach could not produce a valid structured answer."


def degraded_answer(
    question: str,
    corners_examined: list[str],
    *,
    session_id: str,
    main_lap: int,
    ref_lap: int,
) -> CoachingAnswer:
    """The code-built honest failure used when validation retries are exhausted.

    Never raise instead: a crash produces nothing reviewable, while this flows into the eval
    harness (and the deferred review queue) like any other answer.
    """
    return CoachingAnswer(
        headline=DEGRADED_HEADLINE,
        priorities=[],
        one_lap_focus="",
        claims=[],
        corners_examined=corners_examined,
        could_not_determine=[question],
        session_id=session_id,
        main_lap=main_lap,
        ref_lap=ref_lap,
    )
