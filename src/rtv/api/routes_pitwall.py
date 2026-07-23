"""Pitwall agent surface: status, the radio feed, and the kill switches.

Everything here is operational rather than analytical. The interesting question
for an operator mid-race is "what is running, what has it said, and how do I make
it stop", so that is exactly what this exposes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from rtv.api.deps import get_services
from rtv.pitwall.agents import AGENT_REGISTRY
from rtv.services import AppServices

router = APIRouter(tags=["pitwall-agents"])


def _pitwall(services: AppServices):
    if services.pitwall is None:
        raise HTTPException(
            503,
            "The pitwall agent layer is not running. It needs RTV_PITWALL_AGENTS=true, "
            'the "ai" extra and ANTHROPIC_API_KEY.',
        )
    return services.pitwall


@router.get("/pitwall/status", summary="Agent layer status, per-agent stats and radio counters")
def pitwall_status(services: AppServices = Depends(get_services)) -> dict:
    if services.pitwall is None:
        # Not an error: the deterministic pitwall works without the agent layer,
        # and a UI needs to be able to say *why* the radio is quiet.
        return {
            "available": False,
            "reason": "Agent layer not mounted (RTV_PITWALL_AGENTS, the 'ai' extra "
            "or ANTHROPIC_API_KEY).",
            "known_agents": [spec.name for spec in AGENT_REGISTRY],
        }
    return {"available": True, **services.pitwall.status()}


@router.get("/pitwall/radio", summary="Recent radio messages (bounded ring buffer)")
def pitwall_radio(
    limit: int = Query(50, ge=1, le=500),
    services: AppServices = Depends(get_services),
) -> dict:
    pitwall = _pitwall(services)
    messages = pitwall.feed.history(limit)
    return {
        "count": len(messages),
        "messages": [m.to_api() for m in messages],
        "pending": [m.to_api() for m in pitwall.feed.pending()],
    }


class EnabledRequest(BaseModel):
    enabled: bool = Field(description="False is the kill switch: no agent is invoked.")


@router.post("/pitwall/enabled", summary="Global agent kill switch")
def pitwall_set_enabled(
    body: EnabledRequest, services: AppServices = Depends(get_services)
) -> dict:
    pitwall = _pitwall(services)
    pitwall.set_enabled(body.enabled)
    return {"enabled": pitwall.enabled}


@router.post("/pitwall/agents/{name}/enabled", summary="Enable or disable one agent")
def pitwall_set_agent_enabled(
    name: str, body: EnabledRequest, services: AppServices = Depends(get_services)
) -> dict:
    pitwall = _pitwall(services)
    if not pitwall.set_agent_enabled(name, body.enabled):
        raise HTTPException(404, f"No agent named {name!r}.")
    return {"agent": name, "enabled": pitwall.agent_enabled(name)}


@router.post("/pitwall/reset", summary="Clear agent cooldowns and the radio channel")
def pitwall_reset(services: AppServices = Depends(get_services)) -> dict:
    pitwall = _pitwall(services)
    pitwall.reset()
    return pitwall.status()
