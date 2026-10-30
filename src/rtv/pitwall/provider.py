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


#: Name of the synthetic tool used to obtain a structured answer from a backend
#: that does not implement Anthropic's native ``output_format``. Prefixed so it
#: cannot collide with a real tool in :mod:`rtv.pitwall.tools`.
RESPOND_TOOL = "rtv_final_answer"


def _respond_tool(output_model: type[BaseModel]) -> dict[str, Any]:
    """The output contract, expressed as a tool the model is forced to call."""
    return {
        "name": RESPOND_TOOL,
        "description": (
            "Give your final answer. Every field must be supported by the race "
            "state or by a tool result you were given. Call this exactly once, "
            "and call nothing else alongside it."
        ),
        "input_schema": output_model.model_json_schema(),
    }


class AnthropicProvider:
    """Any backend that speaks the Anthropic Messages format.

    Despite the name this is not Claude-specific: Kimi publishes an
    Anthropic-compatible endpoint, so the same SDK, the same wire format and the
    same tool loop drive both. :mod:`rtv.llm` decides the base URL and credential;
    this class only has to know how a *structured answer* is obtained, which is
    the one thing that genuinely differs.

    ``structured_output="native"``
        ``messages.parse(output_format=...)`` -- Anthropic's own feature, and what
        :class:`~rtv.coaching.orchestrator.CoachOrchestrator` has always used.

    ``structured_output="tool"``
        The contract is offered as a forced tool call and its input is validated
        against ``output_model``. A compatibility layer can faithfully implement
        ``/v1/messages`` and tool calling without implementing ``output_format``,
        so assuming the native path would fail at the first agent call rather than
        at configuration time.

    Both modes return the same :class:`ProviderResponse`, so nothing upstream --
    the tool loop, the fact set, the validator, the radio -- learns which one ran.

    Thinking is deliberately *not* enabled by default. The coach runs post-hoc and
    can afford it; a race-engineer call that lands two corners late is worse than
    no call, and every number is already computed deterministically upstream. Set
    ``thinking=True`` to opt in per deployment.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        thinking: bool = False,
        effort: str | None = None,
        structured_output: str | None = None,
    ) -> None:
        if structured_output is None:
            from rtv.llm import structured_output_mode

            structured_output = structured_output_mode()
        if structured_output not in ("native", "tool"):
            raise ValueError(
                f"structured_output must be 'native' or 'tool', got {structured_output!r}"
            )
        if client is None:
            from rtv.llm import build_client  # lazy: the ai extra stays optional

            client = build_client()
        self._client = client
        self._thinking = thinking
        self._effort = effort
        self._structured_output = structured_output

    @property
    def structured_output(self) -> str:
        return self._structured_output

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
        }
        if self._thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        if self._effort:
            kwargs["output_config"] = {"effort": self._effort}

        if self._structured_output == "native":
            kwargs["output_format"] = output_model
            if tools:
                kwargs["tools"] = [t.to_api() for t in tools]
            response = await self._client.messages.parse(**kwargs)
            parsed = getattr(response, "parsed_output", None)
        else:
            # The contract is always on the table, so the model can answer at any
            # point in the loop. On the last pass the runtime withdraws the real
            # tools, and forcing the respond tool is what makes "you must answer
            # now" mean the same thing it means on the native path.
            api_tools = [t.to_api() for t in tools]
            api_tools.append(_respond_tool(output_model))
            kwargs["tools"] = api_tools
            kwargs["tool_choice"] = (
                {"type": "auto"} if tools else {"type": "tool", "name": RESPOND_TOOL}
            )
            response = await self._client.messages.create(**kwargs)
            parsed = None

        content = getattr(response, "content", []) or []
        calls: list[ToolCall] = []
        for block in content:
            if getattr(block, "type", None) != "tool_use":
                continue
            name = getattr(block, "name", "")
            payload = dict(getattr(block, "input", None) or {})
            if name == RESPOND_TOOL:
                try:
                    parsed = output_model.model_validate(payload)
                except Exception:
                    # Leave parsed as None: the runtime's grounded-refusal path is
                    # the right answer for a contract the model could not satisfy,
                    # and it is already tested. Log loudly -- a backend that never
                    # produces a valid payload is a misconfiguration, not a bad lap.
                    log.exception(
                        "%s: %s returned an invalid %s payload",
                        agent,
                        RESPOND_TOOL,
                        output_model.__name__,
                    )
                continue
            calls.append(ToolCall(id=block.id, name=name, input=payload))

        text = "".join(
            block.text for block in content if getattr(block, "type", None) == "text"
        )
        if parsed is not None:
            # A model that answered *and* called tools has answered. Servicing the
            # calls anyway would append an assistant turn carrying a tool_use with
            # no matching tool_result, which every Messages implementation rejects.
            calls = []
        return ProviderResponse(
            tool_calls=calls,
            output=parsed,
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
