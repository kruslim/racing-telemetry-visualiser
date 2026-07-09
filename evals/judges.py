"""Optional LLM judge for the *soft* qualities the deterministic checks can't score.

Factuality is handled deterministically in :mod:`evals.checks`. This judge only rates
clarity, actionability and grounding — and it is told the findings so it can sanity-check
grounding too. Needs the ``ai`` extra and ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

JUDGE_SYSTEM = """\
You are a strict evaluator of race-coaching quality. You are given the deterministic
telemetry findings (ground truth) and a coach's plan. Rate the plan. Be harsh: vague or
generic advice scores low. You are NOT scoring factuality of numbers (handled elsewhere)
— score whether the advice is clear, specific and usable by a driver next lap.
"""


class JudgeScore(BaseModel):
    clarity: int = Field(ge=1, le=5, description="Is it clear and unambiguous?")
    actionability: int = Field(ge=1, le=5, description="Can a driver act on it next lap?")
    grounding: int = Field(ge=1, le=5, description="Does it engage with the specific findings?")
    comment: str = Field(description="One-sentence justification")


async def judge(result: dict[str, Any], findings: dict[str, Any], client: Any, *,
                model: str = "claude-opus-4-8") -> JudgeScore:
    plan = result.get("plan")
    msg = (
        "FINDINGS:\n" + json.dumps(findings, separators=(",", ":")) + "\n\n"
        "COACH PLAN:\n" + json.dumps({"plan": plan, "advices": result.get("advices", [])},
                                     separators=(",", ":"))
    )
    resp = await client.messages.parse(
        model=model,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": msg}],
        output_format=JudgeScore,
    )
    return resp.parsed_output
