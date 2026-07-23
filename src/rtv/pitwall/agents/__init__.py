"""The agent registry: the one place a new agent gets added.

Stage 3 added the vehicle engineer, the spotter and the coach -- each a new
:class:`~rtv.pitwall.framework.AgentSpec` module plus one entry in
:data:`AGENT_REGISTRY`. Nothing in the framework, the orchestrator, the radio feed
or the API changed to route them, which is what "pure configuration" means.

The four are scoped so that no two can say the same sentence. Each owns one
question, is shown only the state that question needs, and can only call the
tools that answer it:

===================  ====================================  ===================
agent                owns                                  wakes on
===================  ====================================  ===================
strategist           when we stop and what we take on      fuel + flags + rivals
vehicle_engineer     what state the car is in              recurrence, tyres, health
spotter              what is around us right now           traffic, blue, hazards
coach                how the car is being driven           repeated mistakes, pace
===================  ====================================  ===================

Model selection is per agent and overridable per deployment, so the cheap,
high-frequency agents sit on Haiku while strategy sits on a reasoning model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from rtv.logging import get_logger
from rtv.pitwall.agents.coach import COACH
from rtv.pitwall.agents.spotter import SPOTTER
from rtv.pitwall.agents.strategist import STRATEGIST
from rtv.pitwall.agents.vehicle_engineer import VEHICLE_ENGINEER
from rtv.pitwall.framework import AgentSpec

log = get_logger("pitwall.agents")

#: Every agent this build knows how to run, in radio-priority order of ownership.
AGENT_REGISTRY: tuple[AgentSpec, ...] = (
    STRATEGIST,
    VEHICLE_ENGINEER,
    SPOTTER,
    COACH,
)

#: Sensible tiers. High-frequency, low-stakes agents belong on the fast model;
#: strategy is worth the reasoning model because it runs a handful of times a race.
FAST_MODEL = "claude-haiku-4-5-20251001"
REASONING_MODEL = "claude-sonnet-4-6"


def build_agents(
    *,
    models: Mapping[str, str] | None = None,
    enabled: Mapping[str, bool] | None = None,
    only: Sequence[str] | None = None,
) -> list[AgentSpec]:
    """Materialise the registry with per-deployment model and enable overrides.

    ``models`` maps agent name -> model id (empty string means "keep the default").
    """
    models = models or {}
    enabled = enabled or {}
    out: list[AgentSpec] = []
    for spec in AGENT_REGISTRY:
        if only is not None and spec.name not in only:
            continue
        model = models.get(spec.name) or spec.model
        out.append(
            replace(spec, model=model, enabled=bool(enabled.get(spec.name, spec.enabled)))
        )
    return out


__all__ = [
    "AGENT_REGISTRY",
    "COACH",
    "FAST_MODEL",
    "REASONING_MODEL",
    "SPOTTER",
    "STRATEGIST",
    "VEHICLE_ENGINEER",
    "build_agents",
]
