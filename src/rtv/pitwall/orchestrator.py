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
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from rtv.director.engine import DirectorEngine, NoopDirector
from rtv.logging import get_logger
from rtv.pitwall.framework import (
    AgentRuntime,
    AgentSpec,
    EventRef,
    RadioMessage,
    RadioPriority,
)
from rtv.pitwall.radio import RadioFeed
from rtv.pitwall.tts import RadioTTS
from rtv.racestate.engine import RaceStateEngine
from rtv.racestate.models import RaceEvent, RaceState, Severity

log = get_logger("pitwall.orchestrator")

DEFAULT_MAX_INFLIGHT = 2
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
    ) -> None:
        self.engine = engine
        self.feed = feed or RadioFeed()
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
        self._budget_announced = False

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

    def driver_message(self, text: str, *, source: str = "voice") -> RadioMessage:
        """Record something the driver said onto the channel.

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
            spoken_text=text.strip(),
            detail_text=f"Driver push-to-talk ({source}).",
            data={"source": source},
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
        )
        self.feed.emit(message)
        log.info("Driver: %s", message.spoken_text)
        return message

    # ---- routing ---------------------------------------------------------
    def dispatch(self, event: RaceEvent, state: RaceState | None = None) -> list[str]:
        """Route one event. Returns the agents that accepted it.

        Synchronous and side-effect-light on purpose: it decides *who* runs, and
        the async workers decide *when*. That makes trigger routing unit-testable
        without an event loop.
        """
        if not self._enabled or self._suspended:
            return []
        if self.budget_exhausted:
            self._announce_budget_exhausted()
            return []
        state = state if state is not None else self.engine.snapshot()
        if state.stale:
            # The numbers are the last known ones, not current ones. An agent
            # reasoning over them would be grounded in a race that has stopped.
            return []
        accepted: list[str] = []
        for name, runtime in self.runtimes.items():
            if not self._agent_enabled.get(name, True):
                continue
            trigger = runtime.match(event, state)
            if trigger is None:
                continue
            runtime.arm(trigger, event)
            accepted.append(name)
            self.dispatched += 1
            queue = self._inbox.get(name)
            if queue is not None:
                self._offer(name, queue, event)
        return accepted

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
        if (
            waiting is not None
            and waiting.severity is Severity.CRITICAL
            and event.severity is not Severity.CRITICAL
        ):
            queue.put_nowait(waiting)  # a critical event is never displaced
            self.skipped_busy += 1
            log.debug("%s busy on a critical event; dropped %s", name, event.key)
            return
        if waiting is not None:
            self.skipped_busy += 1
            log.debug("%s superseded queued %s with %s", name, waiting.key, event.key)
        queue.put_nowait(event)

    async def _run_agent(self, name: str, event: RaceEvent) -> RadioMessage | None:
        runtime = self.runtimes[name]
        state = self.engine.snapshot()
        events = self.engine.bus.history(EVENT_CONTEXT)
        history = self.feed.history(RADIO_CONTEXT)
        sem = self._sem
        if sem is None:  # inline use (tests, smoke) without start()
            message = await runtime.invoke(
                event, state, events=events, radio_history=history
            )
        else:
            async with sem:
                message = await runtime.invoke(
                    event, state, events=events, radio_history=history
                )
        self._record_outcome(name, message is not None)
        return message

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
        """Billable model calls since the last :meth:`reset`."""
        return sum(r.turns for r in self.runtimes.values()) - self._call_baseline

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
        self._budget_announced = False
        self.director.reset()
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
            "max_inflight": self._max_inflight,
            "dispatched": self.dispatched,
            "skipped_busy": self.skipped_busy,
            "injected": self.injected,
            "failure_limit": self._failure_limit,
            "restarts": dict(self.restarts),
            # The session cost guard, in billable model calls.
            "calls_used": self.calls_used,
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
