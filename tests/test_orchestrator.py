"""Orchestrator flow tests with a scripted provider (no API key, no network).

Verifies the multi-agent composition: one specialist per priority corner, then a
synthesis, then a verification — and that each call ships the cached findings block.
"""

from __future__ import annotations

import asyncio

from rtv.coaching.orchestrator import (
    Claim,
    CoachOrchestrator,
    CornerAdvice,
    Priority,
    SessionPlan,
    VerdictReport,
)
from rtv.pitwall.provider import ProviderResponse


class FakeProvider:
    """Answers each structured call by its output schema, recording what was sent."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, *, output_model, messages, system, tools, **kw):
        self.calls.append(
            {"schema": output_model.__name__, "messages": messages, "system": system,
             "tools": list(tools)}
        )
        if output_model is CornerAdvice:
            out = CornerAdvice(
                corner="T4", diagnosis="late on the brakes",
                cues=["brake 5 m later"], est_gain_s=0.3,
            )
        elif output_model is SessionPlan:
            out = SessionPlan(
                headline="Brake later in T4",
                priorities=[Priority(corner="T4", why="late braking", gain_s=0.3)],
                one_lap_focus="T4 brake point",
            )
        elif output_model is VerdictReport:
            out = VerdictReport(claims=[
                Claim(text="brake 5 m later in T4", corner="T4",
                      supported=True, reason="matches brake finding"),
            ])
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected schema {output_model}")
        return ProviderResponse(output=out)


def _findings(top_n: int) -> dict:
    corners = [
        {"index": i, "label": f"T{i + 1}", "net_dt": 0.3 - 0.05 * i, "diags": []}
        for i in range(max(top_n, 1))
    ]
    top3 = [{"index": c["index"], "label": c["label"], "gain": c["net_dt"], "why": "x"}
            for c in corners[:top_n]]
    return {"corners": corners, "chief": {"top3": top3, "net_lap": 0.6, "lost": 0.6,
                                          "losing_n": top_n, "total": len(corners)}}


def test_fan_out_then_synthesise_then_verify():
    provider = FakeProvider()
    orch = CoachOrchestrator(provider, model="kimi-k3")
    result = asyncio.run(orch.coach(_findings(top_n=2)))

    schemas = [c["schema"] for c in provider.calls]
    # 2 specialists + 1 synthesis + 1 verification
    assert schemas == ["CornerAdvice", "CornerAdvice", "SessionPlan", "VerdictReport"]
    assert result.clean is False
    assert len(result.advices) == 2
    assert result.plan.headline
    assert result.verdict.claims


def test_max_corners_caps_fan_out():
    provider = FakeProvider()
    orch = CoachOrchestrator(provider)
    asyncio.run(orch.coach(_findings(top_n=5), max_corners=3))
    specialists = [c for c in provider.calls if c["schema"] == "CornerAdvice"]
    assert len(specialists) == 3


def test_clean_lap_makes_no_api_calls():
    provider = FakeProvider()
    orch = CoachOrchestrator(provider)
    result = asyncio.run(orch.coach(_findings(top_n=0)))
    assert result.clean is True
    assert provider.calls == []


def test_every_call_ships_cached_findings_block():
    provider = FakeProvider()
    orch = CoachOrchestrator(provider)
    asyncio.run(orch.coach(_findings(top_n=1)))
    for call in provider.calls:
        system = call["system"]
        assert len(system) == 2  # philosophy + findings
        assert all(b["cache_control"] == {"type": "ephemeral"} for b in system)
        assert "FINDINGS:" in system[1]["text"]
        assert call["tools"] == []  # one structured answer, no tool loop


# The real Spa T7 corner (lap 12 vs 6) and the brake advice the Kimi run actually gave for it:
# from 6540 m to ~6570 m is later, while the driver was already 32 m late. Its verifier agreed.
SPA_T7 = {
    "index": 6, "label": "T7", "distance": 6753.161, "min_main": 55.594, "min_ref": 66.041,
    "net_dt": 2.851, "entry_m": 6605.397, "exit_m": 6924.137,
    "diags": [
        {"agent": "speed", "text": "Min speed -10 km/h vs reference", "distance": 6753.161,
         "time_loss": 1.259},
        {"agent": "brake", "text": "Brake point 32 m late", "distance": 6540.355,
         "time_loss": 1.284},
    ],
}
SPA_FINDINGS = {
    "corners": [SPA_T7],
    "chief": {"top3": [{"index": 6, "label": "T7", "gain": 2.851, "why": "Brake point 32 m late"}],
              "net_lap": 5.641, "lost": 4.230, "losing_n": 1, "total": 1},
}


class SpaProvider(FakeProvider):
    def __init__(self, cue: str) -> None:
        super().__init__()
        self.cue = cue

    async def complete(self, *, output_model, messages, system, tools, **kw):
        self.calls.append({"schema": output_model.__name__})
        if output_model is CornerAdvice:
            out = CornerAdvice(corner="T7", diagnosis="Brake point 32 m late, 10 km/h down.",
                               cues=[self.cue], est_gain_s=2.851)
        elif output_model is SessionPlan:
            out = SessionPlan(
                headline="T7 is worth 2.85 s.",
                priorities=[Priority(corner="T7", why="Brake point 32 m late", gain_s=2.851)],
                one_lap_focus=f"T7: {self.cue}",
            )
        else:
            out = VerdictReport(claims=[Claim(text=self.cue, corner="T7", supported=True,
                                              reason="derivation follows")])
        return ProviderResponse(output=out)


def test_deterministic_check_overrules_the_verifier_on_a_wrong_brake_direction():
    provider = SpaProvider("Move your brake point from 6540 m to ~6570 m.")
    result = asyncio.run(CoachOrchestrator(provider).coach(SPA_FINDINGS))

    assert [c["schema"] for c in provider.calls][-1] == "VerdictReport"  # no extra model call
    assert any("brake later at T7" in i and "6508 m" in i for i in result.fact_issues)
    assert any("~6570 m" in i for i in result.fact_issues)
    rejected = [c for c in result.verdict.claims if not c.supported]
    assert rejected and all(c.corner == "T7" for c in rejected)
    assert all(c.reason.startswith("Deterministic check") for c in rejected)
    assert any(c.supported for c in result.verdict.claims)  # the model's own claim is kept


def test_grounded_advice_adds_no_fact_issues():
    provider = SpaProvider("Move your brake point from 6540 m to 6508 m.")
    result = asyncio.run(CoachOrchestrator(provider).coach(SPA_FINDINGS))
    assert result.fact_issues == []
    assert all(c.supported for c in result.verdict.claims)
