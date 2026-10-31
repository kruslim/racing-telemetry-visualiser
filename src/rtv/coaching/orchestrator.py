"""Layer 3a — code-controlled multi-agent deep coaching for one lap.

Deterministic findings (ground truth) go in, and several Kimi calls are composed in
code — not by a single model driving itself:

    fan-out specialists (one per priority corner, in parallel)
        -> synthesise into one prioritised session plan
            -> adversarially verify every claim against the findings
                -> deterministically fact-check the prose (no LLM)

The findings are the same for every specialist, so they sit in a cached system block
(prompt caching). Numbers always come from the findings — the prompts forbid inventing
figures, and the verify step (plus the separate eval harness) checks that they didn't. The
model verifier can be talked round (on the Spa run it accepted a T7 brake point moved the wrong
way), so after it, :mod:`rtv.coaching.agent.prose` checks every distance, speed, gear, time and
brake direction in the plan and the specialist advice against the findings in code. What it
rejects is added to the verdict as an unsupported claim and listed in ``fact_issues``, so a
consumer shows the plan with its wrong figures marked rather than as clean.

Requires the ``ai`` extra (``pip install -e ".[ai]"``) and the LLM key in the env
(KIMI_CODE_API_KEY by default; see ``rtv.llm``).
"""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, Field

from rtv.coaching.agent.prose import check_prose, result_prose
from rtv.pitwall.provider import LLMProvider, structured_call

COACH_SYSTEM = """\
You are an elite race-driving coach analysing iRacing telemetry.

You are given DETERMINISTIC findings computed from the telemetry — these are ground
truth. Never invent or alter numbers; cite only figures present in the findings.

Reading the findings:
- net_dt (seconds): time the MAIN lap lost vs the REFERENCE across a corner. + = lost.
- min_main / min_ref (km/h): minimum corner speed for the main vs the reference lap.
- diags[].agent: which analyst flagged it — speed, brake, throttle, gear, grip, consistency.
- diags[].time_loss (s): time attributed to that specific issue.
- A 'brake' diagnostic 'N m late/early' is the brake-application point vs the reference.
- chief.top3: the corners with the most time available, in priority order.

Channel units: Speed m/s (findings already convert min speed to km/h); Throttle/Brake 0..1;
Gear integer; LatAccel converted to g; distances in metres.

Coach like a real engineer: specific, calm, actionable. Prefer one or two concrete cues a
driver can apply next lap over generic advice. Be concise.
"""


# --------------------------------------------------------------------- schemas
class CornerAdvice(BaseModel):
    corner: str = Field(description="Corner label, e.g. T4")
    diagnosis: str = Field(description="What is happening, grounded in the findings")
    cues: list[str] = Field(description="1-3 concrete things to change next lap")
    est_gain_s: float = Field(description="Time available in this corner, from net_dt")


class Priority(BaseModel):
    corner: str
    why: str
    gain_s: float


class SessionPlan(BaseModel):
    headline: str = Field(description="One-line summary of the biggest opportunity")
    priorities: list[Priority] = Field(description="Ordered corners to work on")
    one_lap_focus: str = Field(description="The single thing to focus on next lap")


class Claim(BaseModel):
    text: str = Field(description="A specific claim made in the plan")
    corner: str
    supported: bool = Field(description="True only if the findings back the exact figures")
    reason: str = Field(description="Why it is/ isn't supported, citing the finding")


class VerdictReport(BaseModel):
    claims: list[Claim]


class CoachResult(BaseModel):
    clean: bool
    plan: SessionPlan | None = None
    advices: list[CornerAdvice] = Field(default_factory=list)
    verdict: VerdictReport | None = None
    #: Figures in the prose that the deterministic check could not ground (empty = all grounded).
    fact_issues: list[str] = Field(default_factory=list)
    model: str = ""
    message: str = ""


# --------------------------------------------------------------------- orchestrator
class CoachOrchestrator:
    """Composes the fan-out -> synthesise -> verify pipeline over one lap's findings."""

    def __init__(self, provider: LLMProvider | None = None, *, model: str = "") -> None:
        if provider is None:
            from rtv.pitwall.provider import KimiProvider  # lazy: the ai extra is optional

            provider = KimiProvider()
        from rtv.llm import model as llm_model

        self._provider = provider
        self._model = llm_model("reasoning", model)

    async def coach(self, findings: dict, *, max_corners: int = 3) -> CoachResult:
        top3 = findings.get("chief", {}).get("top3", [])
        if not top3:
            return CoachResult(
                clean=True, model=self._model,
                message="Clean lap — no significant time loss vs the reference.",
            )

        findings_block = json.dumps(findings, separators=(",", ":"))
        corners = {c["index"]: c for c in findings.get("corners", [])}

        # 1) fan out one specialist per priority corner — in parallel
        advices = await asyncio.gather(
            *(self._specialist(p, corners.get(p["index"], {}), findings_block)
              for p in top3[:max_corners])
        )
        advices = [a for a in advices if a is not None]

        # 2) synthesise into one prioritised plan
        plan = await self._synthesise(advices, findings_block)

        # 3) adversarially verify the plan against the findings
        verdict = await self._verify(plan, findings_block)

        # 4) check the prose figures in code: the verifier is a model and can agree with a
        #    wrong figure, the findings can't
        result = CoachResult(
            clean=False, plan=plan, advices=advices, verdict=verdict, model=self._model
        )
        report = check_prose(result_prose(result.model_dump()), findings)
        result.fact_issues = report.issues
        if report.issues:
            result.verdict = verdict or VerdictReport(claims=[])
            result.verdict.claims += [
                Claim(text=issue.split(": ", 1)[-1], corner=corner, supported=False,
                      reason=f"Deterministic check against the findings ({issue.split(':')[0]}).")
                for issue, corner in zip(report.issues, report.corners, strict=True)
            ]
        return result

    # ---- individual agents ------------------------------------------------
    async def _specialist(self, priority: dict, corner: dict, findings_block: str) -> CornerAdvice:
        instruction = (
            f"Focus only on corner {priority.get('corner')} "
            f"(index {priority.get('index')}). Its findings: "
            f"{json.dumps(corner, separators=(',', ':'))}\n"
            "Diagnose the main loss and give 1-3 concrete cues for the next lap."
        )
        return await self._parse(instruction, CornerAdvice, findings_block, max_tokens=2000)

    async def _synthesise(self, advices: list[CornerAdvice], findings_block: str) -> SessionPlan:
        instruction = (
            "Per-corner specialist analyses:\n"
            f"{json.dumps([a.model_dump() for a in advices], separators=(',', ':'))}\n"
            "Combine them into one prioritised session plan. Keep the ordering by time "
            "available and pick a single focus for the next lap."
        )
        return await self._parse(instruction, SessionPlan, findings_block, max_tokens=3000)

    async def _verify(self, plan: SessionPlan, findings_block: str) -> VerdictReport:
        instruction = (
            "Adversarially fact-check this coaching plan against the findings in your "
            "context. For every concrete claim (corner, metres, km/h, seconds, gear), decide "
            "if the findings support the EXACT figures. Be skeptical — mark unsupported if the "
            "numbers don't match a finding.\n"
            f"PLAN:\n{plan.model_dump_json()}"
        )
        return await self._parse(instruction, VerdictReport, findings_block, max_tokens=3000)

    # ---- shared call ------------------------------------------------------
    async def _parse(self, instruction: str, schema: type, findings_block: str, *, max_tokens: int):
        return await structured_call(
            self._provider,
            agent=f"orchestrator.{schema.__name__}",
            model=self._model,
            system=[
                {"type": "text", "text": COACH_SYSTEM, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"FINDINGS:\n{findings_block}",
                 "cache_control": {"type": "ephemeral"}},
            ],
            prompt=instruction,
            output_model=schema,
            max_tokens=max_tokens,
        )
