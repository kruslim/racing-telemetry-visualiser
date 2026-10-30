"""The seed eval set — one case per defense, handwritten before any real review.

Each case is a defense of the coaching pipeline turned into a falsifiable assertion against a
deterministic fixture (``evals.fixtures.build_fixture``). These are the cases you can imagine up
front; the valuable ones come later, from real reviewed failures (``origin: from_review``, minted
by the deferred HITL review gate). Seeding by hand first guards every named defense before a
single review has happened.
"""

from __future__ import annotations

from evals.schemas import EvalCase

SEED_CASES: tuple[EvalCase, ...] = (
    # Defense: the refusal path. Tyre temperatures were never captured, so a tyre-temp question is
    # structurally unanswerable — the coach must refuse and name what is missing, never invent one.
    EvalCase(
        case_id="seed_tyre_temp_refusal",
        question="What were my tyre temperatures through T4?",
        source_fixture="no_tyre_temp",
        expected_outcome="refusal",
        expected_refusal_reason="channel_not_captured",
        origin="handwritten",
    ),
    # Defense: refusal on a missing session. Nothing exists to coach; the agent must refuse rather
    # than fabricate a review.
    EvalCase(
        case_id="seed_missing_session_refusal",
        question="Review my lap 5 against lap 3.",
        source_fixture="missing_session",
        expected_outcome="refusal",
        expected_refusal_reason="session_not_found",
        origin="handwritten",
    ),
    # Defense: the citation validator + priority pick. A known dominant loss in T4 (with a brake
    # diagnostic) must be surfaced, cited, and chosen as the top priority.
    EvalCase(
        case_id="seed_t4_brake_priority",
        question="Where am I losing the most time and what should I fix?",
        source_fixture="t4_brake_loss",
        expected_outcome="answer",
        must_cite_corners=["T4"],
        expected_top_priority="T4",
        origin="handwritten",
    ),
    # Defense: priority ordering across several losing corners — the biggest loss (T3) must lead.
    EvalCase(
        case_id="seed_multi_corner_priority",
        question="Give me a prioritised plan for this lap.",
        source_fixture="multi_corner",
        expected_outcome="answer",
        must_cite_corners=["T3"],
        expected_top_priority="T3",
        origin="handwritten",
    ),
    # Defense: the confabulation guard inside an answer. A question naming a real (T4) and a
    # nonexistent (T9) corner must be coached about the real one without ever citing the invented.
    EvalCase(
        case_id="seed_nonexistent_corner_in_question",
        question="Compare my T4 with my T9 — which costs me more?",
        source_fixture="t4_brake_loss",
        expected_outcome="answer",
        must_cite_corners=["T4"],
        must_not_cite_corners=["T9"],
        origin="handwritten",
    ),
)

SEED_CASES_BY_ID: dict[str, EvalCase] = {case.case_id: case for case in SEED_CASES}
