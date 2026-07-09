"""Layer 3a — code-controlled multi-agent coaching over the Claude API.

This is the orchestration *showcase*: deterministic findings (ground truth) go in, and
several Claude calls are composed in code — not by a single model driving itself:

    fan-out specialists (one per priority corner, in parallel)
        -> synthesise into one prioritised session plan
            -> adversarially verify every claim against the findings

The findings are the same for every specialist, so they sit in a cached system block
(prompt caching). Numbers always come from the findings — the prompts forbid inventing
figures, and the verify step (plus the separate eval harness) checks that they didn't.

Requires the ``ai`` extra (``pip install -e ".[ai]"``) and ANTHROPIC_API_KEY in the env.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field

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
    model: str = ""
    message: str = ""


# --------------------------------------------------------------------- orchestrator
class CoachOrchestrator:
    """Composes the fan-out -> synthesise -> verify pipeline over one lap's findings."""

    def __init__(self, client: Any | None = None, *, model: str = "claude-opus-4-8") -> None:
        if client is None:
            from anthropic import AsyncAnthropic  # imported lazily so the ai extra is optional

            client = AsyncAnthropic()
        self._client = client
        self._model = model

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

        return CoachResult(
            clean=False, plan=plan, advices=advices, verdict=verdict, model=self._model
        )

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
        resp = await self._client.messages.parse(
            model=self._model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=[
                {"type": "text", "text": COACH_SYSTEM, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"FINDINGS:\n{findings_block}",
                 "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": instruction}],
            output_format=schema,
        )
        return resp.parsed_output
