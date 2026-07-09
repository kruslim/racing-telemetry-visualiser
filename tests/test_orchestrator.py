"""Orchestrator flow tests with a stubbed Claude client (no API key, no network).

Verifies the multi-agent composition: one specialist per priority corner, then a
synthesis, then a verification — and that each call ships the cached findings block.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("anthropic")  # orchestrator imports AsyncAnthropic lazily, but be explicit

from rtv.coaching.orchestrator import (  # noqa: E402
    Claim,
    CoachOrchestrator,
    CornerAdvice,
    Priority,
    SessionPlan,
    VerdictReport,
)


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def parse(self, *, output_format, messages, system, **kw):
        self.calls.append(
            {"schema": output_format.__name__, "messages": messages, "system": system}
        )
        if output_format is CornerAdvice:
            out = CornerAdvice(
                corner="T4", diagnosis="late on the brakes",
                cues=["brake 5 m later"], est_gain_s=0.3,
            )
        elif output_format is SessionPlan:
            out = SessionPlan(
                headline="Brake later in T4",
                priorities=[Priority(corner="T4", why="late braking", gain_s=0.3)],
                one_lap_focus="T4 brake point",
            )
        elif output_format is VerdictReport:
            out = VerdictReport(claims=[
                Claim(text="brake 5 m later in T4", corner="T4",
                      supported=True, reason="matches brake finding"),
            ])
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected schema {output_format}")
        return SimpleNamespace(parsed_output=out)


class FakeClient:
    def __init__(self) -> None:
        self.messages = FakeMessages()


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
    client = FakeClient()
    orch = CoachOrchestrator(client=client, model="claude-opus-4-8")
    result = asyncio.run(orch.coach(_findings(top_n=2)))

    schemas = [c["schema"] for c in client.messages.calls]
    # 2 specialists + 1 synthesis + 1 verification
    assert schemas == ["CornerAdvice", "CornerAdvice", "SessionPlan", "VerdictReport"]
    assert result.clean is False
    assert len(result.advices) == 2
    assert result.plan.headline
    assert result.verdict.claims


def test_max_corners_caps_fan_out():
    client = FakeClient()
    orch = CoachOrchestrator(client=client)
    asyncio.run(orch.coach(_findings(top_n=5), max_corners=3))
    specialists = [c for c in client.messages.calls if c["schema"] == "CornerAdvice"]
    assert len(specialists) == 3


def test_clean_lap_makes_no_api_calls():
    client = FakeClient()
    orch = CoachOrchestrator(client=client)
    result = asyncio.run(orch.coach(_findings(top_n=0)))
    assert result.clean is True
    assert client.messages.calls == []


def test_every_call_ships_cached_findings_block():
    client = FakeClient()
    orch = CoachOrchestrator(client=client)
    asyncio.run(orch.coach(_findings(top_n=1)))
    for call in client.messages.calls:
        system = call["system"]
        assert len(system) == 2  # philosophy + findings
        assert all(b["cache_control"] == {"type": "ephemeral"} for b in system)
        assert "FINDINGS:" in system[1]["text"]
