"""The agent registry: the one place a new agent gets added.

Stage 3 adds the vehicle engineer, the spotter and the coach here -- each a new
:class:`~rtv.pitwall.framework.AgentSpec` module plus one entry in
:data:`AGENT_REGISTRY`. Nothing in the framework, the orchestrator, the radio feed
or the API has to change for that, which is what "pure configuration" means.

Model selection is per agent and overridable per deployment, so the cheap,
high-frequency agents can sit on Haiku while strategy sits on a reasoning model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from rtv.logging import get_logger
from rtv.pitwall.agents.strategist import STRATEGIST
from rtv.pitwall.framework import AgentSpec

log = get_logger("pitwall.agents")

#: Every agent this build knows how to run, in radio-priority order of ownership.
AGENT_REGISTRY: tuple[AgentSpec, ...] = (STRATEGIST,)

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
    "FAST_MODEL",
    "REASONING_MODEL",
    "STRATEGIST",
    "build_agents",
]
