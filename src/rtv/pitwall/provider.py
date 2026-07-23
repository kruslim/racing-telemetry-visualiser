"""Model providers behind one narrow interface.

Two implementations, for the two things this repo has always needed:

* :class:`AnthropicProvider` -- the real Claude API, through the same
  ``messages.parse`` + structured-output pattern the Layer-3b coach uses.
* :class:`ScriptedProvider` -- a deterministic stand-in so the whole agent layer,
  including the tool loop and the validator, is exercised by ``pytest`` with no
  API key and no network. Same pattern as the stubbed client in
  ``tests/test_orchestrator.py``, generalised to tool use.

The interface is one method, so the runtime never learns which one it has.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from rtv.logging import get_logger

log = get_logger("pitwall.provider")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ProviderResponse:
    """One model turn: either tool calls to service, or the final structured answer."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    output: BaseModel | None = None
    text: str = ""
    #: The assistant content to append verbatim to the message history.
    assistant_content: Any = field(default_factory=list)


class LLMProvider(Protocol):  # pragma: no cover - structural type
    async def complete(
        self,
        *,
        agent: str,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: Sequence[Any],
        output_model: type[BaseModel],
        max_tokens: int,
    ) -> ProviderResponse: ...


class AnthropicProvider:
    """The Claude API, via the existing ``ai`` extra.

    Structured output uses ``messages.parse`` with a Pydantic ``output_format``,
    exactly as :class:`~rtv.coaching.orchestrator.CoachOrchestrator` does, so the
    two LLM layers share one house style.

    Thinking is deliberately *not* enabled here. The coach runs post-hoc and can
    afford it; a race-engineer call that lands two corners late is worse than no
    call, and every number is already computed deterministically upstream. Set
    ``thinking=True`` to opt in per deployment.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        thinking: bool = False,
        effort: str | None = None,
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic  # lazy: the ai extra stays optional

            client = AsyncAnthropic()
        self._client = client
        self._thinking = thinking
        self._effort = effort

    async def complete(
        self,
        *,
        agent: str,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: Sequence[Any],
        output_model: type[BaseModel],
        max_tokens: int,
    ) -> ProviderResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_format": output_model,
        }
        if tools:
            kwargs["tools"] = [t.to_api() for t in tools]
        if self._thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        if self._effort:
            kwargs["output_config"] = {"effort": self._effort}

        response = await self._client.messages.parse(**kwargs)
        content = getattr(response, "content", []) or []
        calls = [
            ToolCall(id=block.id, name=block.name, input=dict(block.input or {}))
            for block in content
            if getattr(block, "type", None) == "tool_use"
        ]
        text = "".join(
            block.text for block in content if getattr(block, "type", None) == "text"
        )
        return ProviderResponse(
            tool_calls=calls,
            output=getattr(response, "parsed_output", None),
            text=text,
            assistant_content=content,
        )


class ScriptedProvider:
    """A deterministic provider driven by a plain Python callable.

    The handler receives the same arguments the runtime would send a real model
    and returns a :class:`ProviderResponse`. That makes "the agent calls
    ``simulate_pit_outcome``, then answers with these exact numbers" a two-line
    test, and lets the offline smoke script drive a full radio feed.
    """

    def __init__(self, handler: Callable[..., ProviderResponse | dict | BaseModel]) -> None:
        self._handler = handler
        #: Every call, for assertions on prompt shape and tool exposure.
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        agent: str,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: Sequence[Any],
        output_model: type[BaseModel],
        max_tokens: int,
    ) -> ProviderResponse:
        record = {
            "agent": agent,
            "model": model,
            "system": system,
            "messages": list(messages),
            "tools": [t.name for t in tools],
            "output_model": output_model.__name__,
            "max_tokens": max_tokens,
        }
        self.calls.append(record)
        result = self._handler(
            agent=agent,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            output_model=output_model,
            max_tokens=max_tokens,
            # Turn is per *conversation*, derived from the history: an invocation
            # starts with one user message and each tool round adds two more.
            # Counting calls across the provider's lifetime instead would make a
            # handler behave differently on the second event of a race.
            turn=max(0, (len(messages) - 1) // 2),
            call_index=len(self.calls) - 1,
        )
        if isinstance(result, ProviderResponse):
            return result
        if isinstance(result, BaseModel):
            return ProviderResponse(output=result)
        if isinstance(result, dict):
            return ProviderResponse(output=output_model.model_validate(result))
        raise TypeError(f"Scripted handler returned {type(result)!r}")  # pragma: no cover


def tool_use_turn(calls: Sequence[tuple[str, dict[str, Any]]]) -> ProviderResponse:
    """Build a scripted tool-use turn: ``[("get_fuel_projection", {})]``."""
    tool_calls = [
        ToolCall(id=f"toolu_{i}", name=name, input=dict(args))
        for i, (name, args) in enumerate(calls)
    ]
    return ProviderResponse(
        tool_calls=tool_calls,
        assistant_content=[
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
            for c in tool_calls
        ],
    )
