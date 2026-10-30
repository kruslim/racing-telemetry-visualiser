"""The v2 pitwall agent layer: event-driven agents over the deterministic engine.

The hybrid cost model is the whole design. Everything continuous -- gaps, fuel
consumption, tyre trends, pit-window bounds -- is computed sixty times a second by
:mod:`rtv.racestate`, for free. A model is called only when a race *event* fires
an agent's trigger, so a race costs a handful of calls rather than a per-tick bill.

Every agent is (role prompt) + (scoped deterministic tools) + (trigger predicates)
+ (Pydantic output contract) over one shared :class:`AgentRuntime`. Every number an
agent says is checked against the facts it was actually shown, and an agent that
cannot back a figure refuses instead of inventing one.
"""

from rtv.pitwall.agents import AGENT_REGISTRY, build_agents
from rtv.pitwall.framework import (
    AgentOutput,
    AgentRuntime,
    AgentSpec,
    EventRef,
    RadioMessage,
    RadioPriority,
    ToolContext,
    ToolSpec,
    Trigger,
)
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import (
    AnthropicProvider,
    LLMProvider,
    ProviderResponse,
    ScriptedProvider,
    ToolCall,
    tool_use_turn,
)
from rtv.pitwall.radio import RadioFeed, RadioSubscription
from rtv.pitwall.tools import ALL_TOOLS, TOOLS_BY_NAME
from rtv.pitwall.validator import FactSet, GroundingReport, validate_output

__all__ = [
    "AGENT_REGISTRY",
    "ALL_TOOLS",
    "TOOLS_BY_NAME",
    "AgentOutput",
    "AgentRuntime",
    "AgentSpec",
    "AnthropicProvider",
    "EventRef",
    "FactSet",
    "GroundingReport",
    "LLMProvider",
    "PitwallOrchestrator",
    "ProviderResponse",
    "RadioFeed",
    "RadioMessage",
    "RadioPriority",
    "RadioSubscription",
    "ScriptedProvider",
    "ToolCall",
    "ToolContext",
    "ToolSpec",
    "Trigger",
    "build_agents",
    "tool_use_turn",
    "validate_output",
]
