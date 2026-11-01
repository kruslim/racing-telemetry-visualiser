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

import asyncio
import hashlib
import inspect
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field

from rtv.logging import get_logger
from rtv.pitwall import agent_input
from rtv.pitwall.activity import Tracer, safe_trace, summarize
from rtv.pitwall.agent_input import SPOKEN_WORD_LIMIT
from rtv.pitwall.agent_input import as_text as _as_text
from rtv.pitwall.validator import FactSet, GroundingReport, validate_output
from rtv.racestate.derived import agent_payload
from rtv.racestate.models import EventType, RaceEvent, RaceState

log = get_logger("pitwall.framework")

#: Pause before the single retry of a failed model call.
RETRY_BACKOFF_S = 0.5


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


#: What a call aired after its ``max_age_s`` starts with, on the radio and in TTS.
LATE_PREFIX = agent_input.LATE_PREFIX


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
    #: Who wrote the words: ``"model"`` (an agent's LLM run), ``"template"`` (a
    #: deterministic caller, :mod:`rtv.pitwall.callers`: no model call), ``"driver"``
    #: (a push-to-talk transcript) or ``"pitwall"`` (a notice about the pitwall).
    source: str = "model"
    #: False for anything that is *shown* but must not be *said* -- the driver's
    #: own push-to-talk transcript, for instance. The audio manager drops these.
    speak: bool = True
    #: Set when a backend TTS provider is configured; ``None`` means "use the
    #: browser's Web Speech API". See :mod:`rtv.pitwall.tts`.
    audio_url: str | None = None
    #: ---- air-time validity, stamped by deterministic code when the run starts
    #: (:func:`rtv.pitwall.validity.stamp`) and checked by the radio when the
    #: message reaches the front of the channel. ``None`` = no such rule.
    #: The :class:`RaceState` version the agent's snapshot came from.
    state_version: int | None = None
    #: Session time of that snapshot: the age of the call is measured from here.
    computed_at_session_time: float | None = None
    #: Older than this when it airs and still valid: aired, tagged late.
    max_age_s: float | None = None
    #: Dropped once the player's lap counter is past this lap.
    valid_until_lap: int | None = None
    #: Dropped once the session clock is past this.
    expires_at_session_time: float | None = None
    #: Dropped once the player is past this lap fraction on :attr:`valid_until_lap`
    #: (the pit entry, for a box call) without being in the pit lane.
    invalid_if_lap_dist_past: float | None = None
    #: Dropped once the player is in the pit lane (a box call that has happened).
    invalid_if_on_pit_road: bool = False
    #: The car the call is about; dropped once it has left the running order.
    subject_car_idx: int | None = None
    #: ---- what the radio decided at air time
    #: Session time the channel reached the message (aired or dropped).
    air_session_time: float | None = None
    #: Aired after :attr:`max_age_s`; the spoken text starts with ``LATE_PREFIX``.
    late: bool = False
    #: Seconds between :attr:`computed_at_session_time` and the air time.
    late_by_s: float | None = None
    #: Never aired because it had stopped being true; :attr:`expired_reason` says why.
    expired: bool = False
    expired_reason: str | None = None
    #: Never aired because a newer or higher-priority call on the same decision
    #: replaced it while queued, or because it contradicted one that stands
    #: (:mod:`rtv.pitwall.decisions`). :attr:`superseded_by` is that call's id.
    superseded: bool = False
    superseded_by: str | None = None
    superseded_reason: str | None = None
    #: The ``message_id`` of the driver transcript this answers (a driver
    #: question, :mod:`rtv.pitwall.questions`); ``None`` for every other call.
    in_reply_to: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def message_id(self) -> str:
        """A stable, content-derived id -- the key for audio and de-duplication.

        Derived rather than random for the same reason the event log has no
        wall-clock field: a replayed race must produce the same ids, so a cached
        audio clip and a UI de-dup both stay meaningful across a re-run.

        Deliberately *not* a function of :attr:`seq`. The sequence number is
        assigned when the feed accepts the message, which is after the point
        where its audio URL is stamped -- an id that changed between those two
        moments would produce a URL that resolves to nothing. Agent, triggering
        event and words are enough: two messages that agree on all three are the
        same call, and should share one clip.
        """
        spoken = self.spoken_text
        if self.late and spoken.startswith(LATE_PREFIX):
            # Tagging a call late at air time must not change its id: the audio
            # URL and the journal's radio_queued record were keyed before it.
            spoken = spoken[len(LATE_PREFIX) :]
        raw = "|".join(
            (
                self.agent,
                self.event_ref.key,
                str(self.event_ref.tick),
                spoken,
            )
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

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
    #: The agent the tool runs for. Tools whose answer is scoped per role (the race
    #: plan: the strategist sees all of it, the others its tyre posture and pace
    #: target) read it; nothing else does.
    agent: str = ""


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


def agent_model(spec: AgentSpec, settings: Any = None, override: str = "") -> str:
    """The model id an agent runs on.

    First non-empty wins: ``override``, the spec's own ``model``, the per-agent
    setting ``RTV_PITWALL_<NAME>_MODEL``, then the ``rtv.llm`` default for the
    spec's tier (``RTV_LLM_KIMI_MODEL_FAST`` or ``..._REASONING``).
    """
    from rtv import llm
    from rtv.config import get_settings

    if override.strip():
        return override.strip()
    if spec.model.strip():
        return spec.model.strip()
    s = settings or get_settings()
    per_agent = str(getattr(s, f"pitwall_{spec.name}_model", "") or "")
    return llm.model(spec.tier, per_agent, s)


def agent_max_age(spec: AgentSpec, settings: Any = None) -> float | None:
    """Seconds an agent's call may wait before it airs late; ``None`` = never late.

    ``RTV_PITWALL_<NAME>_MAX_AGE_S`` wins over the spec's default; zero or a
    negative value there turns the late tag off for that agent.
    """
    from rtv.config import get_settings

    s = settings or get_settings()
    value = getattr(s, f"pitwall_{spec.name}_max_age_s", None)
    if value is None:
        return spec.max_age_s
    value = float(value)
    return value if value > 0 else None


@dataclass(frozen=True)
class AgentSpec:
    """One agent, entirely declarative."""

    name: str
    role_prompt: str
    output_model: type[AgentOutput]
    tools: tuple[ToolSpec, ...] = ()
    triggers: tuple[Trigger, ...] = ()
    #: ``"fast"`` or ``"reasoning"``: which ``rtv.llm`` default the agent falls
    #: back to when neither ``model`` nor ``RTV_PITWALL_<NAME>_MODEL`` is set.
    tier: str = "fast"
    #: The model id. Empty means "resolve from settings" (:func:`agent_model`),
    #: which :func:`~rtv.pitwall.agents.build_agents` and :class:`AgentRuntime` do.
    model: str = ""
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
    #: Deterministic decision check: ``(output, state, event) -> reason | None``.
    #: The validator proves the numbers are real; a policy proves the call is
    #: one the deterministic data allows (the strategist's stop plan, say). A
    #: violation gets one repair turn, then the message is withheld, not aired.
    policy: Callable[[AgentOutput, RaceState, RaceEvent], str | None] | None = None
    #: Tools the runtime calls before the first model turn, as ``(name, args)``;
    #: ``args`` may be a callable of ``(state, event)``. Their results go into the
    #: first message and the fact set. On the Spa replay every run made the same
    #: one or two calls first, at about 11 s per model turn.
    prefetch: tuple[tuple[str, Any], ...] = ()
    #: Ceiling on tool-use round trips before the runtime demands an answer.
    max_tool_iterations: int = 4
    enabled: bool = True
    #: Seconds a call may wait for the channel before it airs tagged late.
    #: ``None`` = never late. ``RTV_PITWALL_<NAME>_MAX_AGE_S`` overrides it
    #: (:func:`agent_max_age`).
    max_age_s: float | None = None
    #: Deterministic air-time rules for a message: ``(message, state, event,
    #: pit_entry_pct) -> {field: value}`` over :class:`RadioMessage`'s validity
    #: fields (``valid_until_lap``, ``invalid_if_lap_dist_past``, ...). Read
    #: from the message's structured ``data``, never from the spoken words.
    validity: Callable[..., dict[str, Any]] | None = None
    #: What is shown (never spoken) in place of a call the ``policy`` withheld.
    #: The default is the strategist's; race control's policy guards incident
    #: counts and penalty claims, so it says so (stage 09).
    withheld_text: str = ""
    #: Hold the answer to the Layer-1 prose check (brake direction against the corner
    #: analysis) whenever a tool handed the run a Layer-1 corner. The coach's, task A2.
    prose_check: bool = False

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
            "max_age_s": self.max_age_s,
            "prefetch": [name for name, _ in self.prefetch],
            "output_contract": self.output_model.__name__,
        }


# --------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------
REFUSAL_SPOKEN = "Standby - I can't back that call with data yet."
#: Shown (never spoken) for a call the agent's policy ruled out.
POLICY_WITHHELD_SPOKEN = "Call withheld: it contradicts the fuel plan."


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
        retry_attempts: int = 1,
        retry_backoff_s: float = RETRY_BACKOFF_S,
        tracer: Tracer | None = None,
    ) -> None:
        if not spec.model:
            spec = replace(spec, model=agent_model(spec))
        self.spec = spec
        #: Optional activity tracer (see :mod:`rtv.pitwall.activity`). Passive:
        #: ``None`` costs one check per step and nothing else.
        self.tracer = tracer
        #: The run being traced. One agent never overlaps itself (one worker,
        #: one-slot inbox), so per-instance state is enough.
        self._trace_run: dict[str, Any] | None = None
        #: run_start fields not yet written, and records held back behind it.
        self._pending_start: dict[str, Any] | None = None
        self._held: list[tuple[str, dict[str, Any]]] = []
        #: Why the last :meth:`match` returned ``None``, for the dispatch trace.
        self.last_skip: str | None = None
        self.provider = provider
        self.tool_config = dict(tool_config or {})
        self.tool_extras = dict(tool_extras or {})
        self._clock = clock
        self._repair_attempts = repair_attempts
        #: One retry on a *transport* failure, then give up. A pit call that
        #: lands three corners late is worse than no call, so this is not a
        #: general retry policy -- it is the single attempt that covers a dropped
        #: connection without turning a vendor outage into a queue of stale advice.
        self._retry_attempts = max(0, retry_attempts)
        self._retry_backoff_s = max(0.0, retry_backoff_s)
        self._last_fired: dict[str, float] = {}
        self.invocations = 0
        self.refusals = 0
        self.errors = 0
        self.retries = 0
        #: Provider completions, i.e. actual billable model calls -- one
        #: invocation is several of these when the model uses its tools. This is
        #: the number the session cost guard counts.
        self.turns = 0
        self.last_latency_ms: float | None = None
        #: Tokens over every call this runtime made, when the backend reports them.
        self.tokens: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.avg_latency_ms: float | None = None
        self._latency_total_ms = 0.0

    # ---- triggering ------------------------------------------------------
    def match(self, event: RaceEvent, state: RaceState) -> Trigger | None:
        """The first trigger that fires and is off cooldown, else ``None``."""
        self.last_skip = "spec_disabled"
        if not self.spec.enabled:
            return None
        self.last_skip = "no_trigger"
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
                self.last_skip = "cooldown"
                return None
            self.last_skip = None
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
        superseded: Sequence[Mapping[str, Any]] = (),
        previous_calls: Sequence[Mapping[str, Any]] = (),
        injected: Sequence[Mapping[str, Any]] = (),
    ) -> RadioMessage | None:
        """Run the agent once. Returns ``None`` only on an unrecoverable error.

        ``injected`` is the race director's overlay of scripted conditions in force
        (:meth:`rtv.director.ScriptedDirector.overlay`; eval rehearsals only). When
        non-empty it goes into the state slice as ``injected``, so the agent is shown
        it and the validator may cite it, and to a policy that takes ``injected``.

        ``superseded`` is this agent's last call that another agent's call kept off
        the air (:meth:`RadioFeed.take_superseded`), shown as one line so it does
        not repeat it. ``previous_calls`` are this agent's own earlier calls
        (:func:`rtv.pitwall.agent_input.previous_call` entries, oldest first), shown
        so it revises them rather than restating them.
        """
        self.invocations += 1
        started = time.perf_counter()
        turns_before = self.turns
        run = self._begin_trace(event, events, radio_history)
        try:
            message = await self._invoke(
                event, state, events, radio_history, superseded, previous_calls, injected
            )
        finally:
            # A run that failed before its input was built still gets a run_start.
            self._start_run(None)
        if run is not None:
            if message is None:
                outcome = "error"
            elif message.refused:
                outcome = "refused"
            else:
                outcome = "radio"
            self._trace(
                "run_end",
                outcome=outcome,
                message_id=message.message_id if message is not None else None,
                priority=message.priority.value if message is not None else None,
                tools_used=list(message.tools_used) if message is not None else [],
                turns=self.turns - turns_before,
                latency_ms=round((time.perf_counter() - started) * 1000.0, 2),
            )
            self._trace_run = None
        return message

    async def _invoke(
        self,
        event: RaceEvent,
        state: RaceState,
        events: Sequence[RaceEvent],
        radio_history: Sequence[RadioMessage],
        superseded: Sequence[Mapping[str, Any]] = (),
        previous_calls: Sequence[Mapping[str, Any]] = (),
        injected: Sequence[Mapping[str, Any]] = (),
    ) -> RadioMessage | None:
        spec = self.spec
        slice_ = spec.state_slice(state)
        if injected:
            # The director's scripted conditions sit beside the telemetry, never in
            # it: race_state.injected.* is their provenance for the validator.
            slice_ = {**slice_, "injected": agent_input.jsonable(list(injected))}

        # The agent sees the event with its speeds in km/h and its streak counts
        # derived (rtv.racestate.derived); the FactSet holds exactly that view.
        event = event.model_copy(update={"payload": agent_payload(event.payload)})
        facts = FactSet()
        facts.add("race_state", slice_)
        facts.add("event", event.payload)

        ctx = ToolContext(
            state=state,
            event=event,
            events=list(events),
            config=self.tool_config,
            extras=self.tool_extras,
            agent=spec.name,
        )
        tools_used: list[str] = []
        prefetched = self._prefetch(ctx, facts, tools_used, state, event)
        # The run's whole model input as data. It goes on run_start, and the first
        # call is rebuilt from it, so the journal holds exactly what was sent.
        record = agent_input.build_input(
            spec,
            system_prompt=self.system_prompt,
            state_slice=slice_,
            event=event.to_api(),
            radio_history=radio_history,
            prefetched=prefetched,
            superseded=superseded,
            provider=self._provider_info(),
            previous=previous_calls,
        )
        self._start_run(record, state)
        first = agent_input.model_input(record)
        system = first["system"]
        messages: list[dict[str, Any]] = first["messages"]

        try:
            output = await self._converse_with_retry(
                system, messages, ctx, facts, tools_used, event
            )
        except Exception:
            self.errors += 1
            log.exception("Agent %s failed on %s", spec.name, event.key)
            return None
        if output is None:
            self.errors += 1
            log.warning("Agent %s produced no structured output for %s", spec.name, event.key)
            return None

        report = self._validate(output, facts)
        violation = self._check_policy(output, state, event, injected)
        self._trace_validate(report, repaired=False, policy=violation, output=output)
        if (not report.ok or violation) and self._repair_attempts > 0:
            repaired = await self._repair(
                system, messages, ctx, facts, tools_used, report, violation
            )
            if repaired is not None:
                output = repaired
                report = self._validate(output, facts)
                violation = self._check_policy(output, state, event, injected)
                self._trace_validate(report, repaired=True, policy=violation, output=output)

        return self._to_message(event, output, report, tools_used, violation)

    def _check_policy(
        self,
        output: AgentOutput,
        state: RaceState,
        event: RaceEvent,
        injected: Sequence[Mapping[str, Any]] = (),
    ) -> str | None:
        if self.spec.policy is None:
            return None
        try:
            if injected and _takes_injected(self.spec.policy):
                return self.spec.policy(output, state, event, injected=injected)
            return self.spec.policy(output, state, event)
        except Exception:
            log.exception("Policy check failed for %s", self.spec.name)
            return None

    def _validate(self, output: AgentOutput, facts: FactSet) -> GroundingReport:
        return validate_output(
            output.spoken_text,
            output.detail_text,
            facts,
            payload=_payload_of(output),
            prose_check=self.spec.prose_check,
        )

    # ---- the tool-use loop ----------------------------------------------
    async def _converse_with_retry(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        ctx: ToolContext,
        facts: FactSet,
        tools_used: list[str],
        event: RaceEvent,
    ) -> AgentOutput | None:
        """One retry on a transport failure, then let the caller degrade.

        Deliberately shallow. A 502 from the vendor is worth one more attempt; a
        four-attempt exponential backoff would mean a pit call arriving after the
        stop it was about. The message history is rebuilt from the caller's list
        each attempt, because a half-finished tool exchange is not a prefix a
        second attempt can safely continue from.
        """
        attempts = self._retry_attempts + 1
        original = list(messages)
        for attempt in range(attempts):
            try:
                return await self._converse(system, messages, ctx, facts, tools_used)
            except Exception:
                if attempt + 1 >= attempts:
                    raise
                self.retries += 1
                log.warning(
                    "Agent %s: model call failed on %s, retrying once in %.2fs",
                    self.spec.name, event.key, self._retry_backoff_s,
                )
                if self._retry_backoff_s:
                    await asyncio.sleep(self._retry_backoff_s)
                messages[:] = original
        return None  # pragma: no cover - unreachable; the loop returns or raises

    async def _complete(self, **kwargs: Any) -> Any:
        """The one place a model is actually called. Counted and timed here."""
        started = time.perf_counter()
        call = None
        if self._trace_run is not None:
            self._trace_run["calls"] += 1
            call = self._trace_run["calls"]
            self._trace(
                "llm_call_start",
                call=call,
                model=kwargs.get("model"),
                tools_offered=len(kwargs.get("tools") or []),
                messages=len(kwargs.get("messages") or []),
            )
        response: Any = None
        error: str | None = None
        try:
            response = await self.provider.complete(**kwargs)
            return response
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            self.turns += 1
            self._latency_total_ms += elapsed
            self.last_latency_ms = round(elapsed, 2)
            self.avg_latency_ms = round(self._latency_total_ms / self.turns, 2)
            usage = getattr(response, "usage", None) or {}
            for key in ("input_tokens", "output_tokens"):
                self.tokens[key] += usage.get(key, 0)
            if call is not None:
                tool_calls = getattr(response, "tool_calls", None) or []
                self._trace(
                    "llm_call_end",
                    call=call,
                    model=kwargs.get("model"),
                    ok=error is None,
                    error=error,
                    latency_ms=round(elapsed, 2),
                    tool_calls=[c.name for c in tool_calls],
                    answered=getattr(response, "output", None) is not None,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                )

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
            response = await self._complete(
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
                if not offer_tools or not getattr(response, "text", ""):
                    return response.output
                # Answered in prose instead of the contract: ask once more with
                # the tools withdrawn, which forces the respond tool.
                messages.append(
                    {"role": "assistant", "content": response.assistant_content or response.text}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": "Give that answer in the required structure now.",
                    }
                )
                response = await self._complete(
                    agent=spec.name,
                    model=spec.model,
                    system=system,
                    messages=messages,
                    tools=[],
                    output_model=spec.output_model,
                    max_tokens=spec.max_tokens,
                )
                return response.output
            messages.append({"role": "assistant", "content": response.assistant_content})
            results = []
            for call in response.tool_calls:
                payload, is_error = self._run_tool(
                    call.name, call.input or {}, ctx, facts, tools_used
                )
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

    def _run_tool(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
        facts: FactSet,
        tools_used: list[str],
        *,
        prefetched: bool = False,
    ) -> tuple[Any, bool]:
        tool = self.spec.tool(name)
        if tool is None:
            return {"error": f"Unknown tool {name!r}."}, True
        tools_used.append(name)
        tool_started = time.perf_counter()
        try:
            payload = tool(ctx, args)
            is_error = False
        except Exception as exc:  # tool bugs are the agent's problem, not fatal
            log.exception("Tool %s failed for %s", name, self.spec.name)
            payload, is_error = {"error": str(exc)}, True
        else:
            # Everything a tool returned is now citable ground truth.
            facts.add(name, payload)
        extra = {"prefetched": True} if prefetched else {}
        self._trace(
            "tool_call",
            tool=name,
            args=_trace_args(args),
            ok=not is_error,
            result=summarize(payload),
            # The whole result, for the journal only (stripped from the live feed):
            # with the run_start input it rebuilds every later turn of the run.
            result_full=agent_input.jsonable(payload),
            latency_ms=round((time.perf_counter() - tool_started) * 1000.0, 2),
            **extra,
        )
        return payload, is_error

    def _prefetch(
        self,
        ctx: ToolContext,
        facts: FactSet,
        tools_used: list[str],
        state: RaceState,
        event: RaceEvent,
    ) -> list[tuple[str, dict[str, Any], Any]]:
        """Run the spec's prefetch tools: deterministic, local and sub-millisecond."""
        out: list[tuple[str, dict[str, Any], Any]] = []
        for name, args in self.spec.prefetch:
            try:
                resolved = dict(args(state, event) if callable(args) else args or {})
            except Exception:
                log.exception("Prefetch args failed for %s/%s", self.spec.name, name)
                continue
            payload, is_error = self._run_tool(
                name, resolved, ctx, facts, tools_used, prefetched=True
            )
            if not is_error:
                out.append((name, resolved, payload))
        return out

    async def _repair(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        ctx: ToolContext,
        facts: FactSet,
        tools_used: list[str],
        report: GroundingReport,
        violation: str | None = None,
    ) -> AgentOutput | None:
        """One corrective turn. Cheaper than a wrong number on the radio."""
        problems = []
        if violation:
            problems.append(f"The call contradicts the deterministic data: {violation}.")
        if report.unsupported:
            problems.append(
                "These figures are not in the race state or in any tool result you were "
                f"given: {'; '.join(report.unsupported_detail or report.unsupported)}."
            )
        if report.contradictions:
            problems.append(
                "These statements contradict the corner analysis or corner map you were given: "
                f"{'; '.join(report.contradictions)}."
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

    # ---- activity trace --------------------------------------------------
    def _begin_trace(
        self,
        event: RaceEvent,
        events: Sequence[RaceEvent],
        radio_history: Sequence[RadioMessage],
    ) -> dict[str, Any] | None:
        if self.tracer is None:
            return None
        run = {
            "run_id": f"{self.spec.name}-{self.invocations}",
            "event_key": event.key,
            "session_time": event.session_time,
            "tick": event.tick,
            "calls": 0,
        }
        self._trace_run = run
        # run_start waits for the model input, which needs the prefetch results;
        # the prefetch tool_call records are held until it is written.
        self._pending_start = {
            "trigger": event.event_type.value,
            "severity": event.severity.value,
            "model": self.spec.model,
            # The radio this run is shown: the only way one agent hears another.
            "context_radio": [
                {"message_id": m.message_id, "agent": m.agent} for m in radio_history
            ],
            "context_events": len(events),
        }
        self._held = []
        return run

    def _start_run(self, record: dict[str, Any] | None, state: RaceState | None = None) -> None:
        """Write the pending run_start (with ``input`` when there is one), then the
        records held back for it. Does nothing once the run_start is written.

        ``state`` is the whole snapshot the run was given (``state_full``): the input
        only holds the agent's slice, and the policy, the validity rule and the eval
        checks read the rest. Journal-only, like ``input``.
        """
        start, held = self._pending_start, self._held
        if start is None:
            return
        self._pending_start, self._held = None, []
        if record is not None:
            start["input"] = record
        if state is not None:
            start["state_full"] = agent_input.jsonable(state)
        self._emit("run_start", start)
        for kind, data in held:
            self._emit(kind, data)

    def _trace(self, kind: str, **data: Any) -> None:
        if self._trace_run is None:
            return
        if self._pending_start is not None:
            self._held.append((kind, data))
            return
        self._emit(kind, data)

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        run = self._trace_run
        if run is None:
            return
        safe_trace(
            self.tracer,
            kind,
            agent=self.spec.name,
            run_id=run["run_id"],
            event_key=run["event_key"],
            session_time=run["session_time"],
            tick=run["tick"],
            **data,
        )

    def _trace_validate(
        self,
        report: GroundingReport,
        *,
        repaired: bool,
        policy: str | None = None,
        output: AgentOutput | None = None,
    ) -> None:
        extra = {}
        if output is not None:
            # The model's own structured answer, before refusal or truncation
            # rewrites it: what an eval replays through today's checks.
            extra["output_full"] = output.model_dump(mode="json")
        self._trace(
            "validate",
            ok=report.ok and policy is None,
            policy=policy,
            unsupported=list(report.unsupported),
            # Why each figure failed, for the audit (rendered there with rounded values).
            # A prose contradiction is a reason too, so both audits show it unchanged.
            unsupported_detail=list(report.unsupported_detail) + list(report.contradictions),
            contradictions=list(report.contradictions),
            word_count=report.word_count,
            over_word_limit=report.over_word_limit,
            repaired=repaired,
            **extra,
        )

    # ---- context assembly ------------------------------------------------
    @property
    def system_prompt(self) -> str:
        """The stable half of the system prompt: role prompt plus grounding rules."""
        return self.spec.role_prompt + "\n\n" + GROUNDING_RULES

    def _provider_info(self) -> dict[str, Any]:
        describe = getattr(self.provider, "describe", None)
        if callable(describe):
            try:
                return dict(describe())
            except Exception:  # pragma: no cover - defensive
                log.exception("Provider describe() failed")
        return {"name": type(self.provider).__name__}

    # ---- output ----------------------------------------------------------
    def _to_message(
        self,
        event: RaceEvent,
        output: AgentOutput,
        report: GroundingReport,
        tools_used: list[str],
        violation: str | None = None,
    ) -> RadioMessage:
        spoken, detail, priority = output.spoken_text, output.detail_text, output.priority
        refused = False
        speak = True
        if violation:
            # A wrong call is worse than a late one, and "standby" on the radio is
            # noise: the message is kept for the audit trail but never said.
            self.refusals += 1
            refused = True
            speak = False
            priority = RadioPriority.INFO
            spoken = self.spec.withheld_text or POLICY_WITHHELD_SPOKEN
            detail = f"Withheld: {violation}. Original text: " + output.spoken_text
        elif not report.ok and report.unsupported:
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
        elif not report.ok and report.contradictions:
            # A cue the corner analysis contradicts ("brake later" where the driver
            # already brakes late) is as wrong as an invented figure.
            self.refusals += 1
            refused = True
            priority = RadioPriority.INFO
            spoken = REFUSAL_SPOKEN
            detail = (
                "Withheld: the call contradicts the corner analysis or corner map "
                f"({'; '.join(report.contradictions)}). Original text: " + output.spoken_text
            )
        elif report.over_word_limit:
            spoken = _truncate_words(spoken, SPOKEN_WORD_LIMIT)
        if event.event_type is EventType.VISUAL_OBSERVATION:
            # A camera note is seconds old and number-free: it never pre-empts a
            # fuel, flag or traffic call, whatever the agent asked for.
            priority = RadioPriority.INFO

        data = _payload_of(output)
        subject = self.spec.subject(event, output)
        if violation:
            data["withheld"] = violation
            # Its own subject, so it can never supersede a standing call it did
            # not replace.
            subject += ":withheld"
        return RadioMessage(
            agent=self.spec.name,
            priority=priority,
            spoken_text=spoken,
            detail_text=detail,
            data=data,
            event_ref=EventRef.of(event),
            session_time=event.session_time,
            timestamp=self._clock() if self._clock else None,
            subject=subject,
            grounded=report.ok,
            refused=refused,
            ungrounded=list(report.unsupported),
            tools_used=list(dict.fromkeys(tools_used)),
            model=self.spec.model,
            speak=speak,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "invocations": self.invocations,
            # Billable model calls. Higher than `invocations`: one wake-up is a
            # tool round trip or two plus the answer.
            "llm_calls": self.turns,
            "refusals": self.refusals,
            "errors": self.errors,
            "retries": self.retries,
            "last_latency_ms": self.last_latency_ms,
            "avg_latency_ms": self.avg_latency_ms,
            "input_tokens": self.tokens["input_tokens"],
            "output_tokens": self.tokens["output_tokens"],
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


def _takes_injected(policy: Callable[..., Any]) -> bool:
    """A policy that reads the director's overlay declares an ``injected`` keyword."""
    try:
        return "injected" in inspect.signature(policy).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins, C callables
        return False


def _payload_of(output: AgentOutput) -> dict[str, Any]:
    """The agent-specific half of an output: what the UI renders, and what the
    validator must check exactly. The three base fields are prose, not payload."""
    return output.model_dump(
        mode="json", exclude={"spoken_text", "detail_text", "priority"}
    )



def _trace_args(args: Any, *, limit: int = 120) -> dict[str, Any]:
    """A tool call's arguments, with any long string cut short for the trace."""
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in list(args.items())[:12]:
        if isinstance(value, str) and len(value) > limit:
            value = value[:limit] + "..."
        elif not isinstance(value, str | int | float | bool | type(None)):
            value = _as_text(value)[:limit]
        out[key] = value
    return out


def _truncate_words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]) + "..."
