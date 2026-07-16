"""Coaching endpoints.

``/coaching/lap-findings`` is purely deterministic feature extraction (no LLM): it
returns the compact ~20-finding model that every coaching surface — the MCP server,
the orchestrator, the evals — consumes. Keeping it a plain REST endpoint means the
findings have one source of truth.

``/coaching/chat`` is the LLM race-engineer: a natural-language question is answered
by the LangGraph coach (``run_coach``) grounded on those same deterministic findings,
and the corners it coaches are resolved back to lap distances so the frontend can pin
annotations onto the graph.
"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from rtv.api.deps import get_coaching, get_repo
from rtv.coaching.agent.contracts import CoachingAnswer
from rtv.coaching.agent.graph import run_coach
from rtv.coaching.agent.provider import RepositoryFindingsProvider
from rtv.coaching.features import CoachingService
from rtv.config import get_settings
from rtv.logging import get_logger
from rtv.store.repository import Repository

log = get_logger("api.coaching")

router = APIRouter(tags=["coaching"])


@router.get(
    "/coaching/lap-findings",
    summary="Deterministic corner-level findings for a lap vs a reference lap",
)
def lap_findings(
    session_id: str = Query(..., description="Session to analyse."),
    main_lap: int = Query(..., ge=0, description="The lap being coached."),
    ref_lap: int = Query(..., ge=0, description="The reference (target) lap to compare against."),
    coaching: CoachingService = Depends(get_coaching),
) -> dict:
    try:
        return coaching.lap_findings(session_id, main_lap, ref_lap).to_dict()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------------------
# Chat (LLM) — request / response contract
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    session_id: str = Field(..., description="Session to analyse.")
    main_lap: int = Field(..., ge=0, description="The lap being coached.")
    ref_lap: int = Field(..., ge=0, description="The reference (target) lap.")
    question: str = Field(..., min_length=1, max_length=1000)


class ChatCitation(BaseModel):
    corner: str
    figure: str
    value: float
    unit: str


class ChatClaim(BaseModel):
    statement: str
    corner: str
    confidence: str
    citations: list[ChatCitation]


class ChatPriority(BaseModel):
    corner: str
    why: str
    gain_s: float


class ChatAnnotation(BaseModel):
    """A graph marker: a corner the coach flagged, pinned to a lap distance."""

    corner: str
    label: str
    distance_m: float
    lap_dist_pct: float
    note: str
    gain_s: float
    severity: Literal["high", "medium", "low"]
    kind: Literal["priority", "claim"]


class ChatRefusalOut(BaseModel):
    reason: str
    channels_required: list[str]
    channels_available: list[str]
    suggestion: str | None = None


class ChatResponse(BaseModel):
    kind: Literal["answer", "refusal"]
    session_id: str
    main_lap: int
    ref_lap: int
    model: str
    markdown: str
    headline: str | None = None
    one_lap_focus: str | None = None
    priorities: list[ChatPriority] = Field(default_factory=list)
    claims: list[ChatClaim] = Field(default_factory=list)
    could_not_determine: list[str] = Field(default_factory=list)
    refusal: ChatRefusalOut | None = None
    annotations: list[ChatAnnotation] = Field(default_factory=list)


def _severity(gain_s: float) -> Literal["high", "medium", "low"]:
    g = abs(gain_s)
    if g >= 0.30:
        return "high"
    if g >= 0.12:
        return "medium"
    return "low"


def _resolve_annotations(findings: dict, answer: CoachingAnswer) -> list[ChatAnnotation]:
    """Map the coached corners back to lap distances so the graph can flag them.

    Priorities carry their own time-available figure; claim-only corners borrow the
    corner's deterministic ``net_dt``. One annotation per corner, priorities winning.
    """
    corners = {c.get("label"): c for c in findings.get("corners", [])}
    lap_len = float(findings.get("lap_length_m") or 0.0) or 1.0
    out: dict[str, ChatAnnotation] = {}

    for p in answer.priorities:
        corner = corners.get(p.corner)
        if corner is None:
            continue
        dist = float(corner.get("distance", 0.0))
        out[p.corner] = ChatAnnotation(
            corner=p.corner,
            label=p.corner,
            distance_m=dist,
            lap_dist_pct=max(0.0, min(1.0, dist / lap_len)),
            note=p.why,
            gain_s=p.gain_s,
            severity=_severity(p.gain_s),
            kind="priority",
        )

    for claim in answer.claims:
        if claim.corner in out:
            continue
        corner = corners.get(claim.corner)
        if corner is None:
            continue
        dist = float(corner.get("distance", 0.0))
        gain = float(corner.get("net_dt", 0.0))
        out[claim.corner] = ChatAnnotation(
            corner=claim.corner,
            label=claim.corner,
            distance_m=dist,
            lap_dist_pct=max(0.0, min(1.0, dist / lap_len)),
            note=claim.statement,
            gain_s=gain,
            severity=_severity(gain),
            kind="claim",
        )

    return sorted(out.values(), key=lambda a: -abs(a.gain_s))


def _answer_markdown(answer: CoachingAnswer) -> str:
    lines = [f"**{answer.headline}**"]
    if answer.priorities:
        lines.append("")
        lines.append("**Where the time is:**")
        for p in answer.priorities:
            lines.append(f"- **{p.corner}** — {p.why} _(+{p.gain_s:.2f}s)_")
    if answer.one_lap_focus:
        lines.append("")
        lines.append(f"**Next-lap focus:** {answer.one_lap_focus}")
    if answer.could_not_determine:
        lines.append("")
        lines.append("**Couldn't determine from this data:**")
        for q in answer.could_not_determine:
            lines.append(f"- {q}")
    return "\n".join(lines)


def _refusal_markdown(refusal) -> str:
    lines = ["**I can't answer that from this telemetry.**", ""]
    lines.append(f"_Reason: {refusal.reason.replace('_', ' ')}._")
    if refusal.channels_required:
        lines.append("")
        lines.append(f"**Would need:** {', '.join(refusal.channels_required)}")
    if refusal.suggestion:
        lines.append("")
        lines.append(refusal.suggestion)
    return "\n".join(lines)


def _build_model(model_name: str):
    """Construct the LangChain Anthropic chat model the graph coach drives."""
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=model_name, temperature=0, max_tokens=2048, timeout=90)


@router.post(
    "/coaching/chat",
    response_model=ChatResponse,
    summary="Ask the LLM race engineer a question about a lap; get an answer + graph annotations",
)
def coaching_chat(
    body: ChatRequest,
    repo: Repository = Depends(get_repo),
    coaching: CoachingService = Depends(get_coaching),
) -> ChatResponse:
    settings = get_settings()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(
            503,
            "The coaching chatbot needs an Anthropic API key. Set ANTHROPIC_API_KEY "
            "in the server environment to enable it.",
        )

    # Pin the agent to the frontend's currently-selected laps by naming them in the
    # question — the coach's get_lap_findings tool then retrieves exactly these.
    framed = (
        f"For session '{body.session_id}', analyse lap {body.main_lap} against "
        f"reference lap {body.ref_lap}. {body.question.strip()}"
    )
    provider = RepositoryFindingsProvider(repo)

    try:
        model = _build_model(settings.coaching_model)
        state = run_coach(framed, provider, model)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - upstream/model failure
        log.exception("Coaching chat failed")
        raise HTTPException(502, f"Coaching model error: {exc}") from exc

    if state.refusal is not None:
        return ChatResponse(
            kind="refusal",
            session_id=body.session_id,
            main_lap=body.main_lap,
            ref_lap=body.ref_lap,
            model=settings.coaching_model,
            markdown=_refusal_markdown(state.refusal),
            refusal=ChatRefusalOut(
                reason=state.refusal.reason,
                channels_required=state.refusal.channels_required,
                channels_available=state.refusal.channels_available,
                suggestion=state.refusal.suggestion,
            ),
        )

    answer = state.answer
    if answer is None:  # pragma: no cover - graph guarantees one of the two
        raise HTTPException(502, "Coaching model returned no answer.")

    # Annotations come from the deterministic findings for these exact laps.
    annotations: list[ChatAnnotation] = []
    try:
        findings = coaching.lap_findings(body.session_id, body.main_lap, body.ref_lap).to_dict()
        annotations = _resolve_annotations(findings, answer)
    except (KeyError, FileNotFoundError):
        log.warning("Could not build annotations for %s", body.session_id)

    return ChatResponse(
        kind="answer",
        session_id=body.session_id,
        main_lap=body.main_lap,
        ref_lap=body.ref_lap,
        model=settings.coaching_model,
        markdown=_answer_markdown(answer),
        headline=answer.headline,
        one_lap_focus=answer.one_lap_focus,
        priorities=[
            ChatPriority(corner=p.corner, why=p.why, gain_s=p.gain_s) for p in answer.priorities
        ],
        claims=[
            ChatClaim(
                statement=c.statement,
                corner=c.corner,
                confidence=c.confidence,
                citations=[
                    ChatCitation(corner=ci.corner, figure=ci.figure, value=ci.value, unit=ci.unit)
                    for ci in c.citations
                ],
            )
            for c in answer.claims
        ],
        could_not_determine=answer.could_not_determine,
        annotations=annotations,
    )
