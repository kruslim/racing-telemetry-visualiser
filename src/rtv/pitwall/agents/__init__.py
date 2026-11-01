"""The agent registry: the one place a new agent gets added.

Stage 3 added the vehicle engineer, the spotter and the coach -- each a new
:class:`~rtv.pitwall.framework.AgentSpec` module plus one entry in
:data:`AGENT_REGISTRY`; stage 09 added race control the same way. Nothing in the
framework, the orchestrator, the radio feed or the API changed to route them, which
is what "pure configuration" means (stage 09's one small exception, a per-spec
withheld text, is recorded in ``docs/PITWALL.md``).

The five are scoped so that no two can say the same sentence. Each owns one
question, is shown only the state that question needs, and can only call the
tools that answer it:

===================  ====================================  ===================
agent                owns                                  wakes on
===================  ====================================  ===================
strategist           when we stop and what we take on      fuel + flags + rivals
vehicle_engineer     what state the car is in              recurrence, tyres, health
spotter              what is around us right now           traffic, blue, hazards
coach                how the car is being driven           repeated mistakes, pace
race_control         compliance and its consequences       incident limit, penalties
===================  ====================================  ===================

Model selection is per agent and overridable per deployment: each spec names a
tier (the strategist ``"reasoning"``, the others ``"fast"``), and
:func:`build_agents` resolves it through ``RTV_PITWALL_<NAME>_MODEL`` and then the
``rtv.llm`` default for that tier (see :func:`rtv.pitwall.framework.agent_model`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from rtv.config import Settings
from rtv.logging import get_logger
from rtv.pitwall.agents.coach import COACH
from rtv.pitwall.agents.race_control import RACE_CONTROL
from rtv.pitwall.agents.spotter import SPOTTER
from rtv.pitwall.agents.strategist import STRATEGIST
from rtv.pitwall.agents.vehicle_engineer import VEHICLE_ENGINEER
from rtv.pitwall.framework import AgentSpec, agent_model

log = get_logger("pitwall.agents")

#: Every agent this build knows how to run, in radio-priority order of ownership.
AGENT_REGISTRY: tuple[AgentSpec, ...] = (
    STRATEGIST,
    VEHICLE_ENGINEER,
    SPOTTER,
    COACH,
    RACE_CONTROL,
)

def build_agents(
    *,
    models: Mapping[str, str] | None = None,
    enabled: Mapping[str, bool] | None = None,
    only: Sequence[str] | None = None,
    settings: Settings | None = None,
) -> list[AgentSpec]:
    """Materialise the registry with per-deployment model and enable overrides.

    ``models`` maps agent name -> model id and wins over settings (an empty string
    means "use settings"). Otherwise each agent gets ``RTV_PITWALL_<NAME>_MODEL``,
    falling back to the ``rtv.llm`` default for its tier.
    """
    models = models or {}
    enabled = enabled or {}
    out: list[AgentSpec] = []
    for spec in AGENT_REGISTRY:
        if only is not None and spec.name not in only:
            continue
        model = agent_model(spec, settings, models.get(spec.name, ""))
        out.append(
            replace(spec, model=model, enabled=bool(enabled.get(spec.name, spec.enabled)))
        )
    return out


__all__ = [
    "AGENT_REGISTRY",
    "COACH",
    "RACE_CONTROL",
    "SPOTTER",
    "STRATEGIST",
    "VEHICLE_ENGINEER",
    "build_agents",
]
