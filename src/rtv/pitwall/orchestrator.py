"""The pitwall orchestrator: race events in, one prioritised radio feed out.

It subscribes to the race-state event bus, routes each event to whichever agents
declare a matching trigger, runs them concurrently under two constraints, and
merges everything they say into a single :class:`~rtv.pitwall.radio.RadioFeed`.

Concurrency, deliberately:

* **Per-agent serialisation.** Each agent has one worker and a one-slot inbox, so
  it can never overlap itself. A newer event replaces a waiting one (latest-wins)
  -- except that a queued *critical* event is never displaced by a lesser one.
* **Global cap.** A semaphore bounds in-flight model calls, so a burst of events
  at a safety car cannot fan out into a dozen simultaneous API requests.

Cost control lives here too: the kill switch and the per-agent enable flags mean
"stop spending money" is one call, and a disabled agent is never invoked at all.

Two things make it survive a long race rather than merely start one:

* **Supervision.** The pump and every worker run under a restart loop. A task
  that dies on an unexpected exception is restarted with a backoff and counted,
  because the failure mode of an unsupervised ``asyncio`` task is the worst one
  available: the radio simply goes quiet and nothing says so.
* **A per-agent circuit breaker.** Consecutive failed invocations disable an
  agent and record why. A revoked API key would otherwise burn one doomed call
  per race event for the rest of the session.

The race director (:mod:`rtv.director`, planned) plugs in here as well: whatever
it returns from ``poll()`` is published onto the engine's own bus, so an injected
event reaches the agents through exactly the same path a detected one does.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from rtv.director.engine import DirectorEngine, NoopDirector
from rtv.logging import get_logger
from rtv.pitwall import agent_input, validity
from rtv.pitwall.activity import ActivityLog, safe_trace
from rtv.pitwall.callers import DeterministicCaller, build_callers
from rtv.pitwall.framework import (
    AgentRuntime,
    AgentSpec,
    EventRef,
    RadioMessage,
    RadioPriority,
    agent_max_age,
)
from rtv.pitwall.questions import QuestionCaller, is_question, route_question
from rtv.pitwall.radio import RadioFeed
from rtv.pitwall.tts import RadioTTS
from rtv.racestate.engine import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, RaceState, Severity

log = get_logger("pitwall.orchestrator")

DEFAULT_MAX_INFLIGHT = 2
_KEEP = object()
#: The lifecycle events autonomy follows (no agent trigger matches them).
_LIFECYCLE_EVENTS = frozenset(
    {
        EventType.SESSION_START,
        EventType.SESSION_GREEN,
        EventType.SESSION_CHECKERED,
        EventType.SESSION_END,
    }
)
#: How much recent radio each agent is shown, for continuity between calls.
RADIO_CONTEXT = 5
#: How many recent events tools may reconstruct history from.
EVENT_CONTEXT = 200
#: Consecutive failed invocations before an agent is taken off the air.
DEFAULT_FAILURE_LIMIT = 3
#: Hard ceiling on billable model calls in one session. Generous on purpose --
#: it is a runaway guard, not a budget. A normal race hour is a low-tens number.
DEFAULT_MAX_CALLS = 400
#: Seconds a supervised task waits before restarting, so a tight failure loop
#: cannot spin a core while it logs.
SUPERVISOR_BACKOFF_S = 1.0


class PitwallOrchestrator:
    """Routes race events to agents and merges their output onto one channel."""

    def __init__(
        self,
        engine: RaceStateEngine,
        provider: Any,
        agents: Sequence[AgentSpec],
        *,
        feed: RadioFeed | None = None,
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        enabled: bool = True,
        tool_config: Mapping[str, Any] | None = None,
        tool_extras: Mapping[str, Any] | None = None,
        clock: Callable[[], float] | None = time.time,
        tts: RadioTTS | None = None,
        director: DirectorEngine | None = None,
        failure_limit: int = DEFAULT_FAILURE_LIMIT,
        max_calls_per_session: int = DEFAULT_MAX_CALLS,
        retry_backoff_s: float | None = None,
        activity: ActivityLog | None = None,
        vision_radio: bool = False,
        autonomous: bool = False,
        autonomous_sessions: Sequence[str] = ("race",),
        rosters: Mapping[str, Sequence[str]] | None = None,
        callers: Sequence[DeterministicCaller] | None = None,
    ) -> None:
        self.engine = engine
        self.feed = feed or RadioFeed()
        if self.feed.state_source is None:
            # The air-time check (rtv.pitwall.validity) judges a call against the
            # race as it is when the channel reaches it, not when the run began.
            self.feed.state_source = engine.snapshot
        #: The activity trace (see :mod:`rtv.pitwall.activity`). Optional and
        #: passive: with ``None`` nothing is recorded and nothing else changes.
        self.activity: ActivityLog | None = None
        #: The race director (planned -- see :mod:`rtv.director`). Never ``None``:
        #: the default :class:`~rtv.director.engine.NoopDirector` returns nothing,
        #: which keeps the pump's hot path free of a null check.
        self.director: DirectorEngine = director or NoopDirector()
        #: Optional backend voice. When it can speak, every published message
        #: carries an ``audio_url``; when it cannot, the field stays ``None`` and
        #: the browser's Web Speech API does the talking.
        self.tts = tts
        self.tool_config = dict(tool_config or {})
        # The engine is always available to tools that need more than a snapshot
        # (the session-info YAML, for instance). Anything else -- the Layer-1
        # coaching service -- is injected by services.build_pitwall.
        self.tool_extras = {"engine": engine, **dict(tool_extras or {})}
        self._enabled = enabled
        self._max_inflight = max(1, max_inflight)
        runtime_kwargs: dict[str, Any] = {}
        if retry_backoff_s is not None:
            runtime_kwargs["retry_backoff_s"] = retry_backoff_s
        self.runtimes: dict[str, AgentRuntime] = {
            spec.name: AgentRuntime(
                spec,
                provider,
                tool_config=self.tool_config,
                tool_extras=self.tool_extras,
                clock=clock,
                **runtime_kwargs,
            )
            for spec in agents
        }
        self._agent_enabled = {spec.name: spec.enabled for spec in agents}
        #: Seconds each agent's call may wait before it airs tagged late.
        self.max_age_s: dict[str, float | None] = {
            spec.name: agent_max_age(spec) for spec in agents
        }
        if activity is not None:
            self.attach_activity(activity)
        self._sem: asyncio.Semaphore | None = None
        self._inbox: dict[str, asyncio.Queue] = {}
        self._tasks: list[asyncio.Task] = []
        self._sub = None
        self.dispatched = 0
        self.skipped_busy = 0
        self.injected = 0
        #: Supervision bookkeeping: consecutive agent failures, why an agent was
        #: taken off the air, and how many times a supervised task had to restart.
        self._failure_limit = max(0, failure_limit)
        self._failures: dict[str, int] = dict.fromkeys(self.runtimes, 0)
        self._breaker: dict[str, str] = {}
        self.restarts: dict[str, int] = {}
        self._degraded: set[str] = set()
        #: Telemetry stopped arriving. Distinct from the operator kill switch:
        #: resuming must not silently re-enable a layer somebody turned off.
        self._suspended = False
        self._suspend_reason: str | None = None
        #: Session cost guard. 0 = unlimited. Counted in *model calls*, not
        #: wake-ups: one wake-up is a tool round trip or two plus the answer.
        self._max_calls = max(0, max_calls_per_session)
        self._call_baseline = 0
        #: Model calls made outside the agents (the live VLM) that the same
        #: session budget covers, by layer. See :meth:`record_call`.
        self._layer_calls: dict[str, int] = {}
        self._layer_lock = threading.Lock()
        #: Each agent's newest model call per subject, newest subject last (D1).
        self._own_calls: dict[str, dict[str, RadioMessage]] = {}
        self._own_lock = threading.Lock()
        self._budget_announced = False
        #: Whether a live ``visual_observation`` may wake an agent. Off by
        #: default: a camera note arrives 25-40 s after the moment and is
        #: unproven on real footage, so it is review material (the timeline and
        #: the journal) until RTV_VISION_LIVE_RADIO says otherwise.
        self.vision_radio = vision_radio
        #: Session lifecycle autonomy (RTV_PITWALL_AUTONOMOUS). A third flag next
        #: to the kill switch and ``suspended``: standby holds the agents between
        #: sessions and before the green flag. It never turns on anything the
        #: operator switched off -- the kill switch and per-agent flags are checked
        #: first and are never written here.
        self.autonomous = bool(autonomous)
        self.autonomous_sessions = {s.strip() for s in autonomous_sessions if s.strip()}
        self.rosters = {
            kind: {n.strip() for n in names if n.strip()}
            for kind, names in dict(rosters or {}).items()
        }
        self._standby_reason: str | None = (
            "waiting for a session" if self.autonomous else None
        )
        #: Agents armed for this segment; None = every mounted agent.
        self._armed_roster: set[str] | None = None
        self.session_type: str | None = None
        #: The last arm / disarm decision, for status and the journal.
        self.last_autonomy: dict[str, Any] | None = None
        #: Template calls (:mod:`rtv.pitwall.callers`): no model call, published
        #: from ``dispatch`` itself. ``None`` = the defaults (the spotter's).
        self.callers: list[DeterministicCaller] = (
            build_callers() if callers is None else list(callers)
        )
        # Driver questions that are plain lookups are answered the same way.
        if not any(isinstance(c, QuestionCaller) for c in self.callers):
            self.callers.append(QuestionCaller())
        self.template_calls = 0
        self.questions = 0

    def attach_activity(self, activity: ActivityLog | None) -> None:
        """Trace routing, every agent run and the radio into ``activity``."""
        self.activity = activity
        for runtime in self.runtimes.values():
            runtime.tracer = activity
        self.feed.tracer = activity

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Subscribe to the bus and spin up the pump, the workers and the channel."""
        if self._tasks:
            return
        self._sem = asyncio.Semaphore(self._max_inflight)
        self._sub = self.engine.bus.subscribe(name="pitwall-agents")
        for name in self.runtimes:
            self._inbox[name] = asyncio.Queue(maxsize=1)
            self._tasks.append(
                asyncio.create_task(
                    self._supervise(f"agent:{name}", lambda n=name: self._worker(n)),
                    name=f"pitwall-{name}",
                )
            )
        self._tasks.append(
            asyncio.create_task(self._supervise("pump", self._pump), name="pitwall-pump")
        )
        self._tasks.append(
            asyncio.create_task(self._supervise("radio", self.feed.run), name="pitwall-radio")
        )
        log.info(
            "Pitwall agents started: %s (max %d in-flight LLM calls)",
            ", ".join(self.runtimes) or "none", self._max_inflight,
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown is best effort
                pass
        self._tasks.clear()
        self._inbox.clear()
        if self._sub is not None:
            self._sub.close()
            self._sub = None

    async def _supervise(
        self, label: str, factory: Callable[[], Any], *, backoff: float = SUPERVISOR_BACKOFF_S
    ) -> None:
        """Run a long-lived coroutine forever, restarting it if it dies.

        An unsupervised ``asyncio`` task that raises does not crash anything -- it
        just stops, and the pitwall goes quiet with no error anywhere a driver
        would see. So every restart is logged and counted, and ``status()``
        reports the count: a layer that has restarted its pump forty times is
        running, but it is not healthy, and those are different answers.
        """
        while True:
            try:
                await factory()
                return  # a clean return means the loop chose to finish
            except asyncio.CancelledError:
                raise
            except Exception:
                self.restarts[label] = self.restarts.get(label, 0) + 1
                log.exception(
                    "Pitwall task %s failed; restarting (restart #%d)",
                    label, self.restarts[label],
                )
                await asyncio.sleep(backoff)

    async def _pump(self) -> None:
        assert self._sub is not None
        while True:
            events = await self._sub.next_events(timeout=1.0)
            for event in events:
                self.dispatch(event)
            # The director gets a look on every pass, including the idle ones --
            # a scripted yellow must be able to land in a quiet minute, not only
            # in the wake of a detector event.
            self.poll_director()

    async def _worker(self, name: str) -> None:
        queue = self._inbox[name]
        while True:
            event = await queue.get()
            try:
                message = await self._run_agent(name, event)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - runtime already logs its own
                log.exception("Agent worker %s failed", name)
                continue
            if message is not None:
                self.publish(message)

    # ---- the race director (planned; see rtv.director) --------------------
    def poll_director(self, state: RaceState | None = None) -> list[RaceEvent]:
        """Ask the director for injected events and put them on the **race** bus.

        Publishing to ``engine.bus`` rather than straight into ``dispatch()`` is
        the whole point of the seam. An injected event takes the identical route
        a detected one takes -- ring buffer, ``/ws/pitwall``, every subscriber,
        and back around into this orchestrator's own subscription -- so no
        consumer anywhere needs to know a director exists.

        Defensive by construction: a director is third-party-ish code on the hot
        path, and a broken one must cost injections, not the agent layer.
        """
        director = self.director
        if director is None:  # pragma: no cover - the default is never None
            return []
        try:
            state = state if state is not None else self.engine.snapshot()
            events = list(director.poll(state))
        except Exception:
            log.exception("Race director %r failed while polling", getattr(director, "name", "?"))
            return []
        for event in events:
            self.injected += 1
            log.info("Director injected %s at t=%.1fs", event.key, event.session_time)
            self.engine.bus.publish(event)
        return events

    # ---- the radio ------------------------------------------------------
    def publish(self, message: RadioMessage) -> RadioMessage:
        """Stamp a message with its audio URL and put it on the channel.

        The stamp is a URL, not audio: synthesis happens when a browser fetches
        it, so a message superseded before it airs costs nothing. Everything an
        agent says goes through here, which is why the URL and the route that
        serves it cannot disagree about ids.
        """
        if self.tts is not None and message.speak and message.audio_url is None:
            message.audio_url = self.tts.url_for(message)
        self.feed.publish(message)
        return message

    def ask(self, text: str, *, source: str = "text") -> RadioMessage:
        """The driver asks the pitwall something; returns their transcript.

        The entry point for every way a question arrives: the desktop text box
        (``source="text"``) and push-to-talk speech (``"voice"``, task C2). Three
        things happen, none of them a model call:

        1. The transcript goes on the channel at once (:meth:`driver_message`).
        2. :func:`~rtv.pitwall.questions.route_question` picks the owner and, for a
           plain lookup, the intent.
        3. A ``driver_question`` event goes on the **race** bus, so the journal
           records it and ``/ws/pitwall`` shows it. The pump dispatches it like any
           event: a lookup is answered by the question template at once, anything
           else wakes the owner's ``Trigger("driver_question")``. Either answer
           carries ``in_reply_to`` = the transcript's ``message_id``.
        """
        text = text.strip()
        route = route_question(text)
        transcript = self._transcript(text, source, route.to_payload())
        state = self.engine.snapshot()
        event = RaceEvent(
            event_type=EventType.DRIVER_QUESTION,
            tick=state.tick,
            session_time=state.session_time,
            lap=state.player.lap,
            severity=Severity.ADVISORY,
            payload={
                "text": text,
                "source": source,
                "question_id": transcript.message_id,
                **route.to_payload(),
            },
            state_version=state.version,
        )
        self.questions += 1
        log.info(
            "Driver question -> %s (%s): %s",
            route.owner, route.intent or "agent", text,
        )
        self.engine.bus.publish(event)
        if not self._tasks:
            # No pump (inline use, tests): route it here so lookups still answer.
            self.dispatch(event, state)
        return transcript

    def driver_message(self, text: str, *, source: str = "voice") -> RadioMessage:
        """Something the driver said: a question to the pitwall (see :meth:`ask`)."""
        return self.ask(text, source=source)

    def _transcript(self, text: str, source: str, route: dict[str, Any]) -> RadioMessage:
        """Put the driver's own words on the channel.

        Emitted rather than queued: the driver has *already* used the airtime, so
        making the transcript wait behind an advisory would misrepresent when it
        happened. ``speak=False`` -- the pitwall does not read the driver's own
        words back to them; it is here so the UI can show it and so the agents
        see it in their recent-radio context.
        """
        state = self.engine.snapshot()
        message = RadioMessage(
            agent="driver",
            priority=RadioPriority.INFO,
            spoken_text=text,
            detail_text=f"Driver push-to-talk ({source}).",
            data={"source": source, "question": route},
            event_ref=EventRef(
                event_type="driver_message",
                key="driver_message",
                tick=state.tick,
                session_time=state.session_time,
                lap=state.player.lap,
                state_version=state.version,
            ),
            session_time=state.session_time,
            subject="driver",
            speak=False,
            source="driver",
        )
        self.feed.emit(message)
        log.info("Driver: %s", message.spoken_text)
        return message

    def _reply_unanswered(self, event: RaceEvent, reason: str) -> RadioMessage | None:
        """Tell the driver, honestly, that nobody can answer their question now."""
        if not is_question(event):
            return None
        owner = str(event.payload.get("owner") or "pitwall")
        who = owner.replace("_", " ")
        spoken = {
            "disabled": "Pitwall is switched off, no answers right now.",
            "suspended": "No live telemetry, I can't answer that right now.",
            "stale": "No live telemetry, I can't answer that right now.",
            "budget_exhausted": "Pitwall AI budget reached, I can't answer that.",
            "no_agent": f"No {who} on the pitwall to answer that.",
            "agent_disabled": f"The {who} is off the air, no answer.",
            "cooldown": f"The {who} is busy, ask again in a moment.",
            "no_answer": f"No answer from the {who}, sorry.",
            "standby": "Pitwall is on standby, ask again once the session is live.",
            "roster": f"The {who} is not on the pitwall for this session.",
        }.get(reason, "No answer to that right now.")
        state = self.engine.snapshot()
        message = RadioMessage(
            agent="pitwall",
            priority=RadioPriority.INFO,
            spoken_text=spoken,
            detail_text=(
                f"The driver's question ({event.payload.get('text')!r}) was routed to "
                f"{owner} and could not be answered: {reason}."
            ),
            data={"pitwall_notice": True, "unanswered": reason, "owner": owner},
            event_ref=EventRef.of(event),
            session_time=state.session_time,
            subject=f"answer:{event.payload.get('question_id')}",
            source="pitwall",
            in_reply_to=event.payload.get("question_id"),
            # Off with the kill switch: the operator asked for a silent radio.
            speak=reason != "disabled",
        )
        if reason == "disabled":
            self.feed.emit(message)
            return message
        return self.publish(message)

    # ---- routing ---------------------------------------------------------
    def dispatch(self, event: RaceEvent, state: RaceState | None = None) -> list[str]:
        """Route one event. Returns the agents that accepted it.

        Synchronous and side-effect-light on purpose: it decides *who* runs, and
        the async workers decide *when*. That makes trigger routing unit-testable
        without an event loop.
        """
        self.observe_lifecycle(event)
        if not self._enabled or self._suspended:
            blocked = "disabled" if not self._enabled else "suspended"
            self._trace_dispatch(event, [], {}, blocked)
            self._reply_unanswered(event, blocked)
            return []
        if event.event_type is EventType.VISUAL_OBSERVATION and not self.vision_radio:
            # Still on the bus, so the recorder journals it and the timeline and
            # the review show it; it just never reaches the radio.
            self._trace_dispatch(event, [], {}, "vision_review_only")
            return []
        state = state if state is not None else self.engine.snapshot()
        if state.stale:
            # The numbers are the last known ones, not current ones. An agent
            # reasoning over them would be grounded in a race that has stopped.
            self._trace_dispatch(event, [], {}, "stale")
            self._reply_unanswered(event, "stale")
            return []
        # Template calls first: no model, no budget, on the channel before any
        # agent has even been considered.
        templated = self._call_templates(event, state)
        question = is_question(event) and not templated
        if self._standby_reason is not None:
            # Autonomy holds the model-backed agents only; templates cost nothing.
            self._trace_dispatch(event, [], {}, "standby", templated)
            if question:
                self._reply_unanswered(event, "standby")
            return []
        if self.budget_exhausted:
            self._announce_budget_exhausted()
            self._trace_dispatch(event, [], {}, "budget_exhausted", templated)
            if question:
                self._reply_unanswered(event, "budget_exhausted")
            return []
        accepted: list[str] = []
        skipped: dict[str, str] = {}
        for name, runtime in self.runtimes.items():
            if not self._agent_enabled.get(name, True):
                skipped[name] = "disabled"
                continue
            if self._armed_roster is not None and name not in self._armed_roster:
                skipped[name] = "roster"
                continue
            trigger = runtime.match(event, state)
            if trigger is None:
                if runtime.last_skip == "cooldown":
                    skipped[name] = "cooldown"
                continue
            runtime.arm(trigger, event)
            accepted.append(name)
            self.dispatched += 1
            queue = self._inbox.get(name)
            if queue is not None:
                self._offer(name, queue, event)
        self._trace_dispatch(event, accepted, skipped, None, templated)
        if question and not accepted:
            owner = event.payload.get("owner")
            if owner not in self.runtimes:
                reason = "no_agent"
            else:
                reason = "agent_disabled" if skipped.get(owner) == "disabled" else (
                    skipped.get(owner) or "no_agent"
                )
            self._reply_unanswered(event, reason)
        return accepted

    def _call_templates(self, event: RaceEvent, state: RaceState) -> list[str]:
        """Run the deterministic callers for ``event`` and publish what they say.

        The agent's enable flag covers its template calls too: an operator who
        turned the spotter off wants it quiet, not just cheaper.
        """
        out: list[str] = []
        for caller in self.callers:
            if not caller.handles(event) or not self._agent_enabled.get(caller.agent, True):
                continue
            message = caller.call(event, state)
            if message is None:
                continue
            self.template_calls += 1
            safe_trace(
                self.activity,
                "template_call",
                agent=caller.agent,
                event_key=event.key,
                session_time=event.session_time,
                tick=event.tick,
                message_id=message.message_id,
                priority=message.priority.value,
                subject=message.subject,
                spoken_text=message.spoken_text,
            )
            self.publish(message)
            out.append(caller.agent)
        return out

    def _trace_dispatch(
        self,
        event: RaceEvent,
        accepted: list[str],
        skipped: dict[str, str],
        blocked: str | None,
        templated: list[str] | None = None,
    ) -> None:
        extra = {"templated": templated} if templated else {}
        safe_trace(
            self.activity,
            "dispatch",
            event_key=event.key,
            session_time=event.session_time,
            tick=event.tick,
            event_type=event.event_type.value,
            severity=event.severity.value,
            accepted=accepted,
            skipped=skipped,
            blocked=blocked,
            **extra,
        )

    def _offer(self, name: str, queue: asyncio.Queue, event: RaceEvent) -> None:
        """Latest-wins hand-off, except that a queued critical event holds its slot."""
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        try:
            waiting = queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - raced with the worker
            waiting = None
        if waiting is not None and _holds_slot(waiting, event):
            queue.put_nowait(waiting)  # a critical event or a question is never displaced
            self.skipped_busy += 1
            log.debug("%s busy on a critical event; dropped %s", name, event.key)
            safe_trace(
                self.activity, "inbox_superseded", agent=name, event_key=event.key,
                dropped=event.key, kept=waiting.key,
            )
            return
        if waiting is not None:
            self.skipped_busy += 1
            log.debug("%s superseded queued %s with %s", name, waiting.key, event.key)
            safe_trace(
                self.activity, "inbox_superseded", agent=name, event_key=event.key,
                dropped=waiting.key, kept=event.key,
            )
        queue.put_nowait(event)

    async def _run_agent(self, name: str, event: RaceEvent) -> RadioMessage | None:
        runtime = self.runtimes[name]
        state = self.engine.snapshot()
        events = self.engine.bus.history(EVENT_CONTEXT)
        history = self.feed.history(RADIO_CONTEXT)
        # Its last call another agent's call kept off the air, shown once.
        superseded = self.feed.take_superseded(name)
        # Its own last call per subject, minus the one that note already covers.
        skip = {n.get("message_id") for n in superseded}
        previous = [p for p in self.previous_calls(name) if p["message_id"] not in skip]
        sem = self._sem
        if sem is None:  # inline use (tests, smoke) without start()
            message = await runtime.invoke(
                event, state, events=events, radio_history=history, superseded=superseded,
                previous_calls=previous,
            )
        else:
            async with sem:
                message = await runtime.invoke(
                    event, state, events=events, radio_history=history,
                    superseded=superseded, previous_calls=previous,
                )
        self._record_outcome(name, message is not None)
        if message is not None:
            validity.stamp(
                message,
                runtime.spec,
                state,
                event,
                max_age_s=self.max_age_s.get(name),
                pit_entry_pct=getattr(self.engine, "pit_entry_pct", None),
            )
            if is_question(event):
                # Tied to the question, and its own subject: an answer must not
                # supersede the standing pit call (or be superseded by it).
                qid = event.payload.get("question_id")
                message.in_reply_to = qid
                message.subject = f"answer:{qid}"
        elif is_question(event):
            self._reply_unanswered(event, "no_answer")
        if message is not None:
            self._remember_call(message)
        return message

    # ---- each agent's own earlier calls (task D1) ---------------------------
    def _remember_call(self, message: RadioMessage) -> None:
        """Keep an agent's newest call per subject, so its next run can revise it.

        The message object itself is kept: the radio sets ``late`` / ``expired`` on
        it at air time, so the next run sees what actually happened to the call.
        Refusals and withheld calls are not calls the agent made to the driver.
        """
        if message.refused or not message.subject or message.source != "model":
            return
        if message.in_reply_to is not None:
            return  # an answer to the driver is not a standing call
        with self._own_lock:
            calls = self._own_calls.setdefault(message.agent, {})
            calls.pop(message.subject, None)  # re-insert: newest subject last
            calls[message.subject] = message

    def previous_calls(self, name: str) -> list[dict[str, Any]]:
        """Agent ``name``'s last call on each of its last few subjects, oldest first,
        with what the radio did with each (the ``previous`` of its run input)."""
        with self._own_lock:
            mine = list(self._own_calls.get(name, {}).values())
        mine = mine[-agent_input.PREVIOUS_SUBJECTS :] if agent_input.PREVIOUS_SUBJECTS else []
        if not mine:
            return []
        aired = {m.message_id for m in self.feed.history()}
        return [
            agent_input.previous_call(m, queued=_not_aired_yet(m, aired)) for m in mine
        ]

    # ---- degradation notices --------------------------------------------
    def _notice(
        self, agent: str, spoken: str, detail: str, subject: str, data: dict[str, Any]
    ) -> RadioMessage:
        """An info-priority message about the pitwall itself, not about the race.

        It goes through :meth:`publish` like everything else, so it obeys the
        same priority, supersede and airtime rules and gets an audio URL if one
        is available. ``grounded=True`` is honest here: every figure in it is the
        orchestrator's own bookkeeping, which is as traceable as data gets.
        """
        state = self.engine.snapshot()
        message = RadioMessage(
            agent=agent,
            priority=RadioPriority.INFO,
            spoken_text=spoken,
            detail_text=detail,
            data={"pitwall_notice": True, **data},
            event_ref=EventRef(
                event_type="pitwall_notice",
                key=f"pitwall_notice:{subject}",
                tick=state.tick,
                session_time=state.session_time,
                lap=state.player.lap,
                state_version=state.version,
            ),
            session_time=state.session_time,
            subject=subject,
            source="pitwall",
        )
        return self.publish(message)

    def _announce_degraded(self, name: str, failures: int, disabled: bool) -> None:
        """Say once, out loud, that an agent has stopped working.

        The spec's wording is "Pitwall AI degraded", and saying it matters more
        than it looks: the failure mode of a silent agent layer is a driver who
        thinks nobody has anything to tell them. Once per episode, re-armed by a
        success -- a chatty failure notice would be its own outage.
        """
        if name in self._degraded:
            return
        self._degraded.add(name)
        self._notice(
            agent=name,
            spoken=f"Pitwall AI degraded - no {name.replace('_', ' ')} calls for now.",
            detail=(
                f"The {name} agent's model call failed {failures} time(s) including a "
                "retry. The deterministic race state, the event log and every other "
                "agent are unaffected."
                + (" It has been taken off the air until re-enabled." if disabled else "")
            ),
            subject="pitwall_degraded",
            data={"degraded": True, "agent": name, "consecutive_failures": failures,
                  "disabled": disabled},
        )

    def _announce_budget_exhausted(self) -> None:
        if self._budget_announced:
            return
        self._budget_announced = True
        log.error(
            "Pitwall session call budget exhausted (%d/%d model calls); agents "
            "are now silent. Raise RTV_PITWALL_MAX_CALLS_PER_SESSION or reset.",
            self.calls_used, self._max_calls,
        )
        self._notice(
            agent="pitwall",
            spoken="Pitwall AI budget reached, agents are standing down.",
            detail=(
                f"The session cost guard stopped the agent layer at {self.calls_used} "
                f"model calls (limit {self._max_calls}). The deterministic race state, "
                "the event log and the radio channel keep running."
            ),
            subject="pitwall_budget",
            data={"budget_exhausted": True, "calls_used": self.calls_used,
                  "max_calls": self._max_calls},
        )

    # ---- the cost guard ---------------------------------------------------
    @property
    def calls_used(self) -> int:
        """Billable model calls since the last :meth:`reset`, every layer included."""
        with self._layer_lock:
            return self._agent_calls() + sum(self._layer_calls.values())

    def _agent_calls(self) -> int:
        return sum(r.turns for r in self.runtimes.values()) - self._call_baseline

    @property
    def calls_by_layer(self) -> dict[str, int]:
        """``calls_used`` split by layer: ``agents`` plus each :meth:`record_call` layer."""
        with self._layer_lock:
            return {"agents": self._agent_calls(), **self._layer_calls}

    def record_call(self, layer: str, n: int = 1) -> None:
        """Count ``n`` model calls another layer made (the live VLM) against the budget.

        Thread-safe: the live vision worker calls it from its own thread.
        """
        with self._layer_lock:
            self._layer_calls[layer] = self._layer_calls.get(layer, 0) + n

    @property
    def calls_remaining(self) -> int | None:
        """``None`` when uncapped."""
        return None if not self._max_calls else max(0, self._max_calls - self.calls_used)

    @property
    def budget_exhausted(self) -> bool:
        return bool(self._max_calls) and self.calls_used >= self._max_calls

    def _record_outcome(self, name: str, ok: bool) -> None:
        """Trip the per-agent breaker after enough consecutive failures.

        ``AgentRuntime.invoke`` already swallows its own errors and returns
        ``None``, which is right for one bad call and wrong for a hundred: a
        revoked key, a model id that no longer exists or a provider outage would
        otherwise cost one doomed request per race event for the rest of the
        session. The breaker turns that into *n* requests and a reason an
        operator can read on ``/api/v1/pitwall/status``.

        Any success re-arms it. This is a cost guard, not a quarantine.
        """
        if ok:
            self._failures[name] = 0
            self._degraded.discard(name)
            return
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        trip = bool(self._failure_limit) and count >= self._failure_limit
        if trip and self._agent_enabled.get(name, False):
            self._agent_enabled[name] = False
            self._breaker[name] = (
                f"Disabled automatically after {count} consecutive failed invocations. "
                "Re-enable with POST /api/v1/pitwall/agents/"
                f"{name}/enabled once the cause is fixed."
            )
            log.error(
                "Agent %s disabled by the circuit breaker after %d consecutive failures.",
                name, count,
            )
        self._announce_degraded(name, count, disabled=name in self._breaker)

    async def handle_event(
        self, event: RaceEvent, state: RaceState | None = None
    ) -> list[RadioMessage]:
        """Route and run an event to completion, inline. Used by tests and smoke.

        Same routing, same runtime, same radio discipline as the live path -- it
        just skips the worker queues so a caller can await the result.
        """
        state = state if state is not None else self.engine.snapshot()
        messages: list[RadioMessage] = []
        for name in self.dispatch(event, state):
            message = await self._run_agent(name, event)
            if message is not None:
                self.publish(message)
                messages.append(message)
        return messages

    # ---- session lifecycle autonomy -----------------------------------------
    @property
    def standby(self) -> bool:
        return self._standby_reason is not None

    @property
    def standby_reason(self) -> str | None:
        return self._standby_reason

    def observe_lifecycle(self, event: RaceEvent) -> None:
        """Follow the session lifecycle events; arm and disarm under autonomy.

        Called from :meth:`dispatch` for every event, before the kill switch is
        consulted, so standby tracks the session even while the operator has the
        layer off. It only ever writes the standby flag and the armed roster.
        """
        kind = event.event_type
        if kind not in _LIFECYCLE_EVENTS:
            return
        payload = event.payload or {}
        if kind is EventType.SESSION_START:
            self.session_type = payload.get("session_type")
            if self.autonomous:
                self._set_standby("waiting for the green flag", event, armed_roster=None)
        elif not self.autonomous:
            return
        elif kind is EventType.SESSION_GREEN:
            self.session_type = payload.get("session_type") or self.session_type
            self._arm_for(self.session_type, event)
        elif kind is EventType.SESSION_CHECKERED:
            # Only once the player has the flag: the leader's flag is not ours.
            if payload.get("player_finished") is not False and not self.standby:
                self._set_standby("the player took the checkered flag", event)
        elif kind is EventType.SESSION_END and not self.standby:
            self._set_standby("the session ended", event)
        elif kind is EventType.SESSION_END:
            self._standby_reason = "waiting for a session"

    def _arm_for(self, session_type: str | None, event: RaceEvent) -> None:
        if session_type is None:
            self._set_standby("the session type is unknown, so autonomy does not arm", event)
        elif session_type in self.autonomous_sessions:
            self._arm(None, event)
        elif self.rosters.get(session_type):
            self._arm(set(self.rosters[session_type]) & set(self.runtimes), event)
        else:
            self._set_standby(f"no agents are on the {session_type} roster", event)

    def _arm(self, roster: set[str] | None, event: RaceEvent) -> None:
        self._standby_reason = None
        self._armed_roster = roster
        names = sorted(roster) if roster is not None else list(self.runtimes)
        log.info(
            "Pitwall armed for %s at t=%.1fs: %s",
            self.session_type or "an unknown session", event.session_time,
            ", ".join(names) or "none",
        )
        self._trace_autonomy(event, armed=True, reason="green flag")

    def _set_standby(
        self, reason: str, event: RaceEvent, *, armed_roster: Any = _KEEP
    ) -> None:
        was_armed = self.autonomous and self._standby_reason is None
        self._standby_reason = reason
        if armed_roster is not _KEEP:
            self._armed_roster = armed_roster
        if was_armed:
            limit = f"/{self._max_calls}" if self._max_calls else ""
            log.info(
                "Pitwall standing down (%s): %d%s model calls used this session.",
                reason, self.calls_used, limit,
            )
        self._trace_autonomy(event, armed=False, reason=reason)

    def _trace_autonomy(self, event: RaceEvent, *, armed: bool, reason: str) -> None:
        self.last_autonomy = {
            "armed": armed,
            "reason": reason,
            "event": event.event_type.value,
            "session_time": event.session_time,
            "session_type": self.session_type,
            "roster": sorted(self._armed_roster) if self._armed_roster is not None else None,
            "calls_used": self.calls_used,
            "max_calls_per_session": self._max_calls or None,
        }
        data = {k: v for k, v in self.last_autonomy.items() if k != "session_time"}
        safe_trace(
            self.activity, "autonomy", event_key=event.key,
            session_time=event.session_time, tick=event.tick, **data,
        )

    @property
    def mode(self) -> str:
        """One word for the UI: off, suspended, standby, armed (autonomy) or on."""
        if not self._enabled:
            return "off"
        if self._suspended:
            return "suspended"
        if self._standby_reason is not None:
            return "standby"
        return "armed" if self.autonomous else "on"

    # ---- control ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """The kill switch. Off means no agent is invoked and nothing is spent."""
        self._enabled = bool(enabled)
        log.info("Pitwall agents %s", "enabled" if self._enabled else "DISABLED (kill switch)")

    def set_agent_enabled(self, name: str, enabled: bool) -> bool:
        if name not in self.runtimes:
            return False
        self._agent_enabled[name] = bool(enabled)
        if enabled:
            # An explicit re-enable clears the breaker; leaving it latched would
            # disable the agent again on its very next failure.
            self._failures[name] = 0
            self._breaker.pop(name, None)
        log.info("Agent %s %s", name, "enabled" if enabled else "disabled")
        return True

    def agent_enabled(self, name: str) -> bool:
        return bool(self._agent_enabled.get(name, False))

    # ---- the connection lifecycle ----------------------------------------
    @property
    def suspended(self) -> bool:
        return self._suspended

    def suspend(self, reason: str) -> None:
        """Stand the agents down without touching the operator's kill switch.

        Deliberately a *second* flag rather than reusing ``set_enabled(False)``.
        Telemetry dropping out and an operator saying "stop" are different facts,
        and folding them together would mean a reconnect quietly re-enabling a
        layer somebody had turned off on purpose.

        The channel keeps running: anything already queued still airs, because
        the driver asking for it has not disconnected.
        """
        if self._suspended:
            return
        self._suspended = True
        self._suspend_reason = reason
        log.info("Pitwall agents suspended: %s", reason)
        self._notice(
            agent="pitwall",
            spoken="Telemetry lost, pitwall standing by.",
            detail=f"Agents suspended: {reason}. They resume when frames return.",
            subject="pitwall_connection",
            data={"suspended": True, "reason": reason},
        )

    def resume(self) -> None:
        """Telemetry is back. Drop the cooldowns that elapsed during the gap."""
        if not self._suspended:
            return
        self._suspended = False
        reason, self._suspend_reason = self._suspend_reason, None
        # Cooldowns are measured in session time, which did not advance while we
        # were disconnected -- but the race did. Clearing them is what makes a
        # resume clean rather than a silent agent for the next cooldown window.
        for runtime in self.runtimes.values():
            runtime.reset()
        log.info("Pitwall agents resumed (was: %s)", reason)
        self._notice(
            agent="pitwall",
            spoken="Telemetry back, pitwall live.",
            detail="Agents resumed and their trigger cooldowns cleared.",
            subject="pitwall_connection",
            data={"suspended": False},
        )

    def reset(self) -> None:
        """Clear cooldowns, breakers and the channel, between sessions or replays."""
        for runtime in self.runtimes.values():
            runtime.reset()
        for name in self._failures:
            self._failures[name] = 0
        self._breaker.clear()
        self._degraded.clear()
        self._suspended = False
        self._suspend_reason = None
        # A new session gets a fresh cost budget, without discarding the
        # per-agent lifetime counters the status endpoint reports.
        self._call_baseline = sum(r.turns for r in self.runtimes.values())
        with self._layer_lock:
            self._layer_calls.clear()
        self._budget_announced = False
        self.director.reset()
        for caller in self.callers:
            caller.reset()
        with self._own_lock:
            self._own_calls.clear()
        self.feed.clear()

    def healthy(self) -> bool:
        """True when nothing has had to be restarted and no breaker has tripped."""
        return not self.restarts and not self._breaker and not self.budget_exhausted

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": bool(self._tasks),
            "healthy": self.healthy(),
            "suspended": self._suspended,
            "suspend_reason": self._suspend_reason,
            "mode": self.mode,
            "autonomous": self.autonomous,
            "standby": self.standby,
            "standby_reason": self._standby_reason,
            "armed_roster": (
                sorted(self._armed_roster) if self._armed_roster is not None else None
            ),
            "session_type": self.session_type,
            "last_autonomy": self.last_autonomy,
            "max_inflight": self._max_inflight,
            "dispatched": self.dispatched,
            "skipped_busy": self.skipped_busy,
            "injected": self.injected,
            "template_calls": self.template_calls,
            "questions": self.questions,
            # False for the templates-only mount (no LLM key): spotter calls and
            # lookup answers still run, nothing calls a model.
            "agents_mounted": bool(self.runtimes),
            "callers": [caller.describe() for caller in self.callers],
            "failure_limit": self._failure_limit,
            "restarts": dict(self.restarts),
            # The session cost guard, in billable model calls.
            "calls_used": self.calls_used,
            "calls_by_layer": self.calls_by_layer,
            "calls_remaining": self.calls_remaining,
            "max_calls_per_session": self._max_calls or None,
            "budget_exhausted": self.budget_exhausted,
            "radio": self.feed.stats(),
            "tts": self.tts.describe() if self.tts is not None else None,
            "director": self.director.describe(),
            "agents": [
                {
                    **runtime.spec.describe(),
                    "enabled": self._agent_enabled.get(name, False),
                    "consecutive_failures": self._failures.get(name, 0),
                    "disabled_reason": self._breaker.get(name),
                    "degraded": name in self._degraded,
                    **runtime.stats(),
                }
                for name, runtime in self.runtimes.items()
            ],
        }


def _not_aired_yet(message: RadioMessage, aired: set[str]) -> bool:
    """Still waiting for the channel: neither aired nor dropped."""
    if message.expired or message.air_session_time is not None:
        return False
    return message.message_id not in aired


def _holds_slot(waiting: RaceEvent, incoming: RaceEvent) -> bool:
    """Whether a queued event keeps its inbox slot against ``incoming``.

    A critical event is never displaced by a lesser one, and a driver's question
    is never displaced by a detector event short of critical: the driver asked.
    """
    if waiting.severity is Severity.CRITICAL:
        return incoming.severity is not Severity.CRITICAL
    if is_question(waiting):
        return incoming.severity is not Severity.CRITICAL and not is_question(incoming)
    return False
