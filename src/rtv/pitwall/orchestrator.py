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
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

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
    ) -> None:
        self.engine = engine
        self.feed = feed or RadioFeed()
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
        self.runtimes: dict[str, AgentRuntime] = {
            spec.name: AgentRuntime(
                spec,
                provider,
                tool_config=self.tool_config,
                tool_extras=self.tool_extras,
                clock=clock,
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

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Subscribe to the bus and spin up the pump, the workers and the channel."""
        if self._tasks:
            return
        self._sem = asyncio.Semaphore(self._max_inflight)
        self._sub = self.engine.bus.subscribe(name="pitwall-agents")
        for name in self.runtimes:
            self._inbox[name] = asyncio.Queue(maxsize=1)
            self._tasks.append(asyncio.create_task(self._worker(name), name=f"pitwall-{name}"))
        self._tasks.append(asyncio.create_task(self._pump(), name="pitwall-pump"))
        self._tasks.append(asyncio.create_task(self.feed.run(), name="pitwall-radio"))
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

    async def _pump(self) -> None:
        assert self._sub is not None
        while True:
            events = await self._sub.next_events(timeout=1.0)
            for event in events:
                self.dispatch(event)

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
        if not self._enabled:
            return []
        state = state if state is not None else self.engine.snapshot()
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
            return await runtime.invoke(
                event, state, events=events, radio_history=history
            )
        async with sem:
            return await runtime.invoke(
                event, state, events=events, radio_history=history
            )

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
        log.info("Agent %s %s", name, "enabled" if enabled else "disabled")
        return True

    def agent_enabled(self, name: str) -> bool:
        return bool(self._agent_enabled.get(name, False))

    def reset(self) -> None:
        """Clear cooldowns and the channel, between sessions or replays."""
        for runtime in self.runtimes.values():
            runtime.reset()
        self.feed.clear()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": bool(self._tasks),
            "max_inflight": self._max_inflight,
            "dispatched": self.dispatched,
            "skipped_busy": self.skipped_busy,
            "radio": self.feed.stats(),
            "tts": self.tts.describe() if self.tts is not None else None,
            "agents": [
                {
                    **runtime.spec.describe(),
                    "enabled": self._agent_enabled.get(name, False),
                    **runtime.stats(),
                }
                for name, runtime in self.runtimes.items()
            ],
        }
