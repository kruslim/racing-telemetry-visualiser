"""The agent framework: one runtime, many agents, all declared as configuration.

An agent here is exactly four things:

===================  =====================================================
role prompt          who it is and what it is allowed to say
scoped tools         the deterministic functions it may call (a subset)
triggers             which :class:`RaceEvent`s wake it, and when
output contract      a Pydantic model it must produce
===================  =====================================================

:class:`AgentRuntime` is the *only* place that talks to a model. Adding the
vehicle engineer or the spotter in a later stage is a new :class:`AgentSpec`
literal plus a prompt -- no framework change, which is the point.

Cost model: the LLM is called when a trigger fires on a race event, never on a
timer and never per tick. All the continuous mathematics already happened in
:mod:`rtv.racestate`; by the time an agent wakes up, the numbers exist.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from rtv.logging import get_logger
from rtv.pitwall.validator import FactSet, GroundingReport, validate_output
from rtv.racestate.models import RaceEvent, RaceState

log = get_logger("pitwall.framework")

#: Radio is a scarce channel; a message longer than this stops being radio.
SPOKEN_WORD_LIMIT = 25


class RadioPriority(StrEnum):
    CRITICAL = "critical"
    ADVISORY = "advisory"
    INFO = "info"


#: Lower sorts first. Used by the radio queue and by the /ws/pitwall consumer.
PRIORITY_RANK: dict[RadioPriority, int] = {
    RadioPriority.CRITICAL: 0,
    RadioPriority.ADVISORY: 1,
    RadioPriority.INFO: 2,
}


class AgentOutput(BaseModel):
    """The base output contract every agent's model must satisfy.

    Agents subclass this and add their own structured payload; the runtime turns
    whatever they add into :attr:`RadioMessage.data` for the UI, so a new agent
    gets a UI-ready widget payload for free.
    """

    priority: RadioPriority = Field(
        description="critical = act now, advisory = decide soon, info = context."
    )
    spoken_text: str = Field(
        description=f"Radio call, under {SPOKEN_WORD_LIMIT} words, as an engineer says it."
    )
    detail_text: str = Field(
        description="The longer reasoning for the pitwall screen. Numbers must be cited."
    )


class EventRef(BaseModel):
    """Which deterministic observation caused this message."""

    event_type: str
    key: str
    tick: int
    session_time: float
    lap: int | None = None
    state_version: int = 0

    @classmethod
    def of(cls, event: RaceEvent) -> EventRef:
        return cls(
            event_type=event.event_type.value,
            key=event.key,
            tick=event.tick,
            session_time=event.session_time,
            lap=event.lap,
            state_version=event.state_version,
        )


class RadioMessage(BaseModel):
    """One thing the pitwall says, plus everything needed to justify it."""

    agent: str
    priority: RadioPriority
    spoken_text: str
    detail_text: str
    data: dict[str, Any] = Field(default_factory=dict)
    event_ref: EventRef
    #: Deterministic session clock (replay-stable). Prefer this for ordering.
    session_time: float = 0.0
    #: Wall clock, for the UI only. ``None`` in replay so logs stay reproducible.
    timestamp: float | None = None
    #: What this message is *about*. Two messages with the same subject supersede.
    subject: str = ""
    seq: int = 0
    #: False when the citation validator could not back every figure.
    grounded: bool = True
    #: True when the agent refused rather than invent (a grounded refusal).
    refused: bool = False
    ungrounded: list[str] = Field(default_factory=list)
    #: Tools the agent actually called, for the pitwall's audit panel.
    tools_used: list[str] = Field(default_factory=list)
    model: str = ""

    def to_api(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
@dataclass
class ToolContext:
    """Everything a tool is allowed to read. Deterministic by construction."""

    state: RaceState
    event: RaceEvent
    #: Recent events from the bus ring buffer, oldest first.
    events: list[RaceEvent] = field(default_factory=list)
    #: Operator-tunable constants (pit-lane loss, standings window, ...).
    config: Mapping[str, Any] = field(default_factory=dict)
    #: Handles a tool may need beyond the snapshot -- the live engine (for the
    #: session-info YAML) and the Layer-1 coaching service (for post-hoc corner
    #: detail). Deliberately untyped and optional: a tool that finds its handle
    #: missing must return a stated "unavailable", never a guess. See
    #: :mod:`rtv.pitwall.tools`.
    extras: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """A deterministic function the model may call, plus its JSON schema."""

    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[[ToolContext, dict[str, Any]], Any]

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def __call__(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        return self.fn(ctx, args)


# --------------------------------------------------------------------------
# triggers + spec
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Trigger:
    """An event type, an optional predicate, and a per-trigger cooldown."""

    event_type: str
    predicate: Callable[[RaceEvent, RaceState], bool] | None = None
    #: Minimum session-seconds between invocations *for this trigger type*.
    cooldown_s: float | None = None
    #: Human label used in logs and the /api/v1/pitwall/status payload.
    label: str = ""

    def matches(self, event: RaceEvent, state: RaceState) -> bool:
        if event.event_type.value != self.event_type:
            return False
        if self.predicate is None:
            return True
        try:
            return bool(self.predicate(event, state))
        except Exception:  # pragma: no cover - a bad predicate must not kill the loop
            log.exception("Trigger predicate failed for %s", self.event_type)
            return False


@dataclass(frozen=True)
class AgentSpec:
    """One agent, entirely declarative."""

    name: str
    role_prompt: str
    output_model: type[AgentOutput]
    tools: tuple[ToolSpec, ...] = ()
    triggers: tuple[Trigger, ...] = ()
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1500
    #: Default cooldown for triggers that do not set their own.
    cooldown_s: float = 30.0
    #: Compact, agent-relevant slice of RaceState. Keeps the prompt small *and*
    #: defines exactly which numbers this agent may cite.
    state_slice: Callable[[RaceState], dict[str, Any]] = field(
        default=lambda state: state.to_api()
    )
    #: Radio supersede key. Two advisories with the same subject collapse.
    subject: Callable[[RaceEvent, AgentOutput], str] = field(
        default=lambda event, out: event.event_type.value
    )
    #: Ceiling on tool-use round trips before the runtime demands an answer.
    max_tool_iterations: int = 4
    enabled: bool = True

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "enabled": self.enabled,
            "tools": [t.name for t in self.tools],
            "triggers": [
                {
                    "event_type": t.event_type,
                    "label": t.label or t.event_type,
                    "conditional": t.predicate is not None,
                    "cooldown_s": t.cooldown_s if t.cooldown_s is not None else self.cooldown_s,
                }
                for t in self.triggers
            ],
            "max_tokens": self.max_tokens,
            "output_contract": self.output_model.__name__,
        }


# --------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------
REFUSAL_SPOKEN = "Standby - I can't back that call with data yet."


class AgentRuntime:
    """Turns a fired trigger into a validated :class:`RadioMessage`.

    The flow is fixed for every agent: assemble a compact context, let the model
    call its scoped tools, force a structured answer, validate every number in it
    against the facts that were actually shown, and only then put it on the radio.
    """

    def __init__(
        self,
        spec: AgentSpec,
        provider: Any,
        *,
        tool_config: Mapping[str, Any] | None = None,
        tool_extras: Mapping[str, Any] | None = None,
        clock: Callable[[], float] | None = None,
        repair_attempts: int = 1,
    ) -> None:
        self.spec = spec
        self.provider = provider
        self.tool_config = dict(tool_config or {})
        self.tool_extras = dict(tool_extras or {})
        self._clock = clock
        self._repair_attempts = repair_attempts
        self._last_fired: dict[str, float] = {}
        self.invocations = 0
        self.refusals = 0
        self.errors = 0

    # ---- triggering ------------------------------------------------------
    def match(self, event: RaceEvent, state: RaceState) -> Trigger | None:
        """The first trigger that fires and is off cooldown, else ``None``."""
        if not self.spec.enabled:
            return None
        for trigger in self.spec.triggers:
            if not trigger.matches(event, state):
                continue
            cooldown = (
                trigger.cooldown_s if trigger.cooldown_s is not None else self.spec.cooldown_s
            )
            last = self._last_fired.get(trigger.event_type)
            # Session time, not wall clock: cooldowns must survive a 4x replay.
            if last is not None and event.session_time - last < cooldown:
                log.debug(
                    "%s: %s suppressed by cooldown (%.1fs)",
                    self.spec.name, trigger.event_type, cooldown,
                )
                return None
            return trigger
        return None

    def arm(self, trigger: Trigger, event: RaceEvent) -> None:
        """Record the firing so the cooldown starts. Called on dispatch."""
        self._last_fired[trigger.event_type] = event.session_time

    def reset(self) -> None:
        self._last_fired.clear()

    # ---- invocation ------------------------------------------------------
    async def invoke(
        self,
        event: RaceEvent,
        state: RaceState,
        *,
        events: Sequence[RaceEvent] = (),
        radio_history: Sequence[RadioMessage] = (),
    ) -> RadioMessage | None:
        """Run the agent once. Returns ``None`` only on an unrecoverable error."""
        spec = self.spec
        self.invocations += 1
        slice_ = spec.state_slice(state)

        facts = FactSet()
        facts.add("race_state", slice_)
        facts.add("event", event.payload)

        ctx = ToolContext(
            state=state,
            event=event,
            events=list(events),
            config=self.tool_config,
            extras=self.tool_extras,
        )
        system = self._system_blocks(slice_)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._instruction(event, radio_history)}
        ]
        tools_used: list[str] = []

        try:
            output = await self._converse(system, messages, ctx, facts, tools_used)
        except Exception:
            self.errors += 1
            log.exception("Agent %s failed on %s", spec.name, event.key)
            return None
        if output is None:
            self.errors += 1
            log.warning("Agent %s produced no structured output for %s", spec.name, event.key)
            return None

        report = self._validate(output, facts)
        if not report.ok and self._repair_attempts > 0:
            repaired = await self._repair(system, messages, ctx, facts, tools_used, report)
            if repaired is not None:
                output = repaired
                report = self._validate(output, facts)

        return self._to_message(event, output, report, tools_used)

    def _validate(self, output: AgentOutput, facts: FactSet) -> GroundingReport:
        return validate_output(
            output.spoken_text, output.detail_text, facts, payload=_payload_of(output)
        )

    # ---- the tool-use loop ----------------------------------------------
    async def _converse(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        ctx: ToolContext,
        facts: FactSet,
        tools_used: list[str],
    ) -> AgentOutput | None:
        spec = self.spec
        for iteration in range(spec.max_tool_iterations + 1):
            # On the last pass the tools are withdrawn, so the model must answer.
            offer_tools = iteration < spec.max_tool_iterations
            response = await self.provider.complete(
                agent=spec.name,
                model=spec.model,
                system=system,
                messages=messages,
                tools=list(spec.tools) if offer_tools else [],
                output_model=spec.output_model,
                max_tokens=spec.max_tokens,
            )
            if response.output is not None and not response.tool_calls:
                return response.output
            if not response.tool_calls:
                return response.output
            messages.append({"role": "assistant", "content": response.assistant_content})
            results = []
            for call in response.tool_calls:
                tool = spec.tool(call.name)
                if tool is None:
                    payload: Any = {"error": f"Unknown tool {call.name!r}."}
                    is_error = True
                else:
                    tools_used.append(call.name)
                    try:
                        payload = tool(ctx, call.input or {})
                        is_error = False
                    except Exception as exc:  # tool bugs are the agent's problem, not fatal
                        log.exception("Tool %s failed for %s", call.name, spec.name)
                        payload, is_error = {"error": str(exc)}, True
                    else:
                        # Everything a tool returned is now citable ground truth.
                        facts.add(call.name, payload)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": _as_text(payload),
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
        return None

    async def _repair(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        ctx: ToolContext,
        facts: FactSet,
        tools_used: list[str],
        report: GroundingReport,
    ) -> AgentOutput | None:
        """One corrective turn. Cheaper than a wrong number on the radio."""
        problems = []
        if report.unsupported:
            problems.append(
                "These figures are not in the race state or in any tool result you were "
                f"given: {', '.join(report.unsupported)}."
            )
        if report.over_word_limit:
            problems.append(
                f"spoken_text is {report.word_count} words; the limit is {SPOKEN_WORD_LIMIT}."
            )
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your answer was rejected by the grounding validator. "
                    + " ".join(problems)
                    + " Restate it using only figures you can point at, or say plainly that "
                    "you do not have the data. Do not guess."
                ),
            }
        )
        try:
            return await self._converse(system, messages, ctx, facts, tools_used)
        except Exception:  # pragma: no cover - defensive; the first answer already failed
            log.exception("Repair turn failed for %s", self.spec.name)
            return None

    # ---- context assembly ------------------------------------------------
    def _system_blocks(self, slice_: dict[str, Any]) -> list[dict[str, Any]]:
        """Stable role prompt first (cached), volatile state after it.

        Ordering matters: prompt caching is a prefix match, so the part that never
        changes has to come first or nothing caches (see the Layer-3b coach).
        """
        return [
            {
                "type": "text",
                "text": self.spec.role_prompt + "\n\n" + GROUNDING_RULES,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": "RACE STATE:\n" + _as_text(slice_)},
        ]

    def _instruction(
        self, event: RaceEvent, radio_history: Sequence[RadioMessage]
    ) -> str:
        parts = [
            f"TRIGGER: {event.key} (severity {event.severity.value}) at "
            f"session time {event.session_time:.1f}s"
            + (f", lap {event.lap}" if event.lap is not None else "")
            + f".\nEvent payload: {_as_text(event.payload)}"
        ]
        if radio_history:
            recent = "\n".join(
                f"- [{m.agent}/{m.priority.value}] {m.spoken_text}" for m in radio_history[-5:]
            )
            parts.append(
                "RECENT RADIO (do not repeat a call that still stands):\n" + recent
            )
        parts.append(
            "Call the tools you need, then answer in the required structure. "
            "Keep spoken_text under "
            f"{SPOKEN_WORD_LIMIT} words."
        )
        return "\n\n".join(parts)

    # ---- output ----------------------------------------------------------
    def _to_message(
        self,
        event: RaceEvent,
        output: AgentOutput,
        report: GroundingReport,
        tools_used: list[str],
    ) -> RadioMessage:
        spoken, detail, priority = output.spoken_text, output.detail_text, output.priority
        refused = False
        if not report.ok and report.unsupported:
            # Grounded refusal: we would rather say nothing useful than something
            # wrong. The unsupported figures are kept so an operator can see why.
            self.refusals += 1
            refused = True
            priority = RadioPriority.INFO
            spoken = REFUSAL_SPOKEN
            detail = (
                "Withheld: the agent cited figures that are not traceable to the race "
                f"state or to a tool result ({', '.join(report.unsupported)}). "
                "Original text: " + output.spoken_text
            )
        elif report.over_word_limit:
            spoken = _truncate_words(spoken, SPOKEN_WORD_LIMIT)

        data = _payload_of(output)
        return RadioMessage(
            agent=self.spec.name,
            priority=priority,
            spoken_text=spoken,
            detail_text=detail,
            data=data,
            event_ref=EventRef.of(event),
            session_time=event.session_time,
            timestamp=self._clock() if self._clock else None,
            subject=self.spec.subject(event, output),
            grounded=report.ok,
            refused=refused,
            ungrounded=list(report.unsupported),
            tools_used=list(dict.fromkeys(tools_used)),
            model=self.spec.model,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "invocations": self.invocations,
            "refusals": self.refusals,
            "errors": self.errors,
            "cooldowns": dict(self._last_fired),
        }


GROUNDING_RULES = """\
GROUNDING (absolute):
- Every number you say must appear in the RACE STATE block or in a tool result you
  received in this conversation. Never estimate, never round a number you were not
  given, never carry a figure over from a previous race.
- If the data you need is missing or a tool reports it unavailable, say so plainly.
  A grounded "I don't have fuel numbers yet" beats an invented lap count.
- Anything a tool marks as an assumption is an assumption. Say so when it matters.
- spoken_text is radio: short, calm, imperative. detail_text is for the pitwall
  screen and may be longer, but is held to the same grounding rule."""


def _payload_of(output: AgentOutput) -> dict[str, Any]:
    """The agent-specific half of an output: what the UI renders, and what the
    validator must check exactly. The three base fields are prose, not payload."""
    return output.model_dump(
        mode="json", exclude={"spoken_text", "detail_text", "priority"}
    )


def _as_text(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        return json.dumps(value, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


def _truncate_words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]) + "..."
