"""Model providers behind one narrow interface.

Two implementations, for the two things this repo has always needed:

* :class:`KimiProvider` -- Kimi Code over its Anthropic-compatible Messages
  endpoint, with the structured answer obtained through a forced respond tool.
* :class:`ScriptedProvider` -- a deterministic stand-in so the whole agent layer,
  including the tool loop and the validator, is exercised by ``pytest`` with no
  API key and no network.

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
    #: Token counts reported by the backend (``rtv.llm.usage_of``), when it sent them.
    usage: dict[str, int] | None = None
    #: The model called the respond tool but its payload failed the contract: why, in
    #: one line, so the runtime can ask once more (``None`` when nothing was invalid).
    invalid_output: str | None = None
    #: The backend's stop reason ("end_turn", "tool_use", "max_tokens"), for the trace.
    stop_reason: str | None = None


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


#: Name of the synthetic tool the structured answer is obtained through (Kimi's
#: compatibility layer implements tool calling, not the SDK's native
#: ``output_format``). Prefixed so it cannot collide with a real tool in
#: :mod:`rtv.pitwall.tools`.
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


class KimiProvider:
    """Kimi Code, through the Anthropic-compatible Messages endpoint.

    :mod:`rtv.llm` decides the base URL and credential. The output contract is
    offered as a forced tool call and its input is validated against
    ``output_model``, so a structured answer never depends on a feature the
    compatibility layer does not implement.

    Thinking is deliberately *not* enabled by default. A race-engineer call that
    lands two corners late is worse than no call, and every number is already
    computed deterministically upstream. Set ``thinking=True`` to opt in.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        thinking: bool = False,
        effort: str | None = None,
    ) -> None:
        if client is None:
            from rtv.llm import build_client  # lazy: the ai extra stays optional

            client = build_client()
        self._client = client
        self._thinking = thinking
        self._effort = effort

    def describe(self) -> dict[str, Any]:
        """The call settings that are not in the messages, for the run journal."""
        return {"name": "kimi", "thinking": self._thinking, "effort": self._effort}

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

        # The contract is always on the table, so the model can answer at any
        # point in the loop. On the last pass the runtime withdraws the real
        # tools, and forcing the respond tool is what makes it "answer now".
        #
        # kimi-k3 thinks unless told not to, and rejects a named tool_choice
        # while it does. So thinking is switched off explicitly when it is off
        # here, and with thinking on the last pass uses "any", which is the same
        # thing when respond is the only tool on offer.
        api_tools = [t.to_api() for t in tools]
        api_tools.append(_respond_tool(output_model))
        kwargs["tools"] = api_tools
        if not self._thinking:
            kwargs["thinking"] = {"type": "disabled"}
        if tools and not self._thinking:
            # "any": a turn must be a tool call, real or the respond tool. With
            # "auto", kimi-k3 answered in plain text on about a third of the calls
            # on the second Spa replay once the tool results were prefetched, and
            # a text answer is no answer (verified 2026-10-31: "any" is accepted
            # with thinking disabled).
            kwargs["tool_choice"] = {"type": "any"}
        elif tools:
            kwargs["tool_choice"] = {"type": "auto"}
        elif self._thinking:
            kwargs["tool_choice"] = {"type": "any"}
        else:
            kwargs["tool_choice"] = {"type": "tool", "name": RESPOND_TOOL}
        response = await self._client.messages.create(**kwargs)
        from rtv.llm import record_usage

        usage = record_usage(agent, model, response)
        parsed = None
        invalid: str | None = None

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
                except Exception as exc:
                    invalid = _contract_error(exc, payload)
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
            usage=usage,
            invalid_output=invalid if parsed is None else None,
            stop_reason=getattr(response, "stop_reason", None),
        )


def _contract_error(exc: Exception, payload: dict[str, Any]) -> str:
    """One line on why a respond payload failed the contract, for the retry turn."""
    if not payload:
        return "the respond call was empty"
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            parts = [
                f"{'.'.join(str(p) for p in e.get('loc', ())) or 'payload'}: {e.get('msg')}"
                for e in errors()[:4]
            ]
            if parts:
                return "; ".join(parts)
        except Exception:  # pragma: no cover - defensive
            pass
    return type(exc).__name__


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

    def describe(self) -> dict[str, Any]:
        return {"name": "scripted"}

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


async def structured_call(
    provider: LLMProvider,
    *,
    agent: str,
    model: str,
    system: list[dict[str, Any]],
    prompt: str,
    output_model: type[BaseModel],
    max_tokens: int,
) -> BaseModel | None:
    """One tool-free question, one structured answer (``None`` if it was invalid).

    For the post-session layers (orchestrator, eval judge) that need a typed
    answer but no tool loop. An invalid payload is asked for once more: on the
    Spa replay kimi-k3 sent an empty or token-garbled respond call for about one
    orchestrator specialist in three, and each one was dropped from the plan.
    These layers run after the session, so the retry costs no race time.
    """
    for _attempt in range(2):
        response = await provider.complete(
            agent=agent,
            model=model,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=(),
            output_model=output_model,
            max_tokens=max_tokens,
        )
        if response.output is not None:
            return response.output
    return None
