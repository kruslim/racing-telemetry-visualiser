"""The single merged radio feed: one channel, priority-ordered, self-superseding.

Several agents will be producing messages at once. A driver has one pair of ears,
so the pitwall has one channel, and that channel needs discipline:

* **Priority then time.** Critical pre-empts whatever is queued behind it.
* **Supersede.** Two advisories about the same subject (two pit-window updates,
  two fuel calls) are not two messages -- the newer one replaces the older one
  while it is still queued. Saying a stale pit lap out loud is worse than silence.
* **One answer per decision.** Some subjects of *different* agents are one
  decision (:mod:`rtv.pitwall.decisions`: the strategist's pit calls and the
  engineer's "box now"). Inside such a group the newer call replaces a queued one
  from any agent, a critical one replaces a queued advisory, and a call that
  contradicts a higher-priority one still standing is dropped. Every loser is
  handed to consumers marked ``superseded`` with the reason, never aired.
* **Airtime.** A message occupies the channel for roughly as long as it takes to
  say. That is what makes superseding meaningful, and it is the hook stage 4's
  TTS layer will hang off.
* **Critical is never superseded.** Losing a "box now, you're on fumes" to a
  tidier queue would be the one unforgivable bug in here.
* **Checked at air time.** An agent decides from a snapshot taken when its run
  started, 15-60 s before the call reaches the front. When it does, the
  deterministic rule stamped on it (:mod:`rtv.pitwall.validity`) is evaluated
  against the race state *now*: a call older than its agent's ``max_age_s`` but
  still true airs tagged late ("Late call: ..."); a call that has stopped being
  true (the stop lap is gone, the car it was about has left) is dropped, critical
  or not, and handed to consumers marked ``expired`` so the review can show it.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any

from rtv.logging import get_logger
from rtv.pitwall import decisions, validity
from rtv.pitwall.activity import Tracer, safe_trace
from rtv.pitwall.framework import LATE_PREFIX, PRIORITY_RANK, RadioMessage, RadioPriority
from rtv.racestate.models import RaceState

log = get_logger("pitwall.radio")

DEFAULT_HISTORY = 200
DEFAULT_QUEUE_SIZE = 64
#: Seconds of channel time per spoken word (~150 wpm, a calm engineer).
SECONDS_PER_WORD = 0.4
MIN_AIRTIME_S = 0.6


class RadioSubscription:
    """A bounded, oldest-dropped queue of emitted messages for one consumer."""

    def __init__(self, feed: RadioFeed, maxsize: int, name: str | None = None) -> None:
        self._feed = feed
        self._queue: deque[RadioMessage] = deque(maxlen=maxsize)
        self._lock = threading.Lock()
        self._dropped = 0
        self._closed = False
        self.name = name or f"radio-{id(self):x}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiter: asyncio.Event | None = None

    def _deliver(self, message: RadioMessage) -> None:
        with self._lock:
            if self._closed:
                return
            if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
                self._dropped += 1
            self._queue.append(message)
        loop, waiter = self._loop, self._waiter
        if loop is not None and waiter is not None:
            try:
                loop.call_soon_threadsafe(waiter.set)
            except RuntimeError:  # pragma: no cover - loop already closed
                pass

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)

    def drain(self) -> list[RadioMessage]:
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
        return items

    async def next_messages(self, timeout: float | None = None) -> list[RadioMessage]:
        if self._waiter is None:
            self._loop = asyncio.get_running_loop()
            self._waiter = asyncio.Event()
        waiter = self._waiter
        items = self.drain()
        if items:
            return items
        waiter.clear()
        items = self.drain()
        if items:
            return items
        try:
            await asyncio.wait_for(waiter.wait(), timeout)
        except TimeoutError:
            return []
        return self.drain()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._queue.clear()
        self._feed.unsubscribe(self)

    def __enter__(self) -> RadioSubscription:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def airtime_s(message: RadioMessage) -> float:
    """How long this call occupies the channel."""
    return max(MIN_AIRTIME_S, len(message.spoken_text.split()) * SECONDS_PER_WORD)


class RadioFeed:
    """Priority queue plus fan-out. Publish is cheap; emission is paced."""

    def __init__(
        self,
        *,
        history: int = DEFAULT_HISTORY,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        airtime: Callable[[RadioMessage], float] | None = None,
        tracer: Tracer | None = None,
        state_source: Callable[[], RaceState | None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        #: The race state at air time (the orchestrator wires the engine's
        #: snapshot). ``None`` = no position rules, time rules only when the
        #: caller knows the session time (:meth:`pump`).
        self.state_source = state_source
        #: Optional activity tracer (see :mod:`rtv.pitwall.activity`).
        self.tracer = tracer
        self._pending: list[RadioMessage] = []
        self._history: deque[RadioMessage] = deque(maxlen=history)
        self._subs: list[RadioSubscription] = []
        self._callbacks: list[Callable[[RadioMessage], None]] = []
        self._queue_size = queue_size
        self._airtime = airtime or airtime_s
        self._seq = 0
        #: Session time at which the channel next frees up (see :meth:`pump`).
        self._busy_until = float("-inf")
        self.published = 0
        self.emitted = 0
        self.superseded = 0
        self.late = 0
        self.expired = 0
        #: Calls dropped for contradicting a higher-priority call on their decision.
        self.conflicts = 0
        #: agent -> its last call another agent's call superseded (see take_superseded).
        self._notes: dict[str, dict[str, Any]] = {}
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- subscription ----------------------------------------------------
    def subscribe(
        self, *, maxsize: int | None = None, name: str | None = None
    ) -> RadioSubscription:
        sub = RadioSubscription(self, maxsize or self._queue_size, name)
        with self._lock:
            self._subs.append(sub)
        return sub

    def subscribe_callback(
        self, fn: Callable[[RadioMessage], None]
    ) -> Callable[[RadioMessage], None]:
        with self._lock:
            self._callbacks.append(fn)
        return fn

    def unsubscribe(self, target: Any) -> None:
        with self._lock:
            if target in self._subs:
                self._subs.remove(target)
            elif target in self._callbacks:
                self._callbacks.remove(target)

    # ---- publishing ------------------------------------------------------
    def publish(self, message: RadioMessage) -> RadioMessage | None:
        """Queue a message. Returns the queued message it superseded, if any.

        Outside a decision group the rule is per agent and subject, as it always
        was. Inside one (:mod:`rtv.pitwall.decisions`) the group is the key, across
        agents: a newer call replaces a queued one of the same or lower priority
        from any agent, and a call that contradicts a higher-priority call still
        standing (queued, or aired inside the conflict window) is dropped itself.
        Either way the call that loses is handed to consumers marked
        ``superseded``, and an agent that lost to another agent is told on its next
        run (:meth:`take_superseded`).
        """
        losers: list[tuple[RadioMessage, RadioMessage, str, str]] = []
        dropped = False
        with self._lock:
            self._seq += 1
            message.seq = self._seq
            self.published += 1
            group = decisions.group_of(message)
            if group is not None:
                blocker = self._conflict_locked(message, group)
                if blocker is not None:
                    standing, where = blocker
                    dropped = True
                    losers.append(
                        (
                            message,
                            standing,
                            "conflict",
                            f"contradicts the {_who(standing)} {standing.priority.value} "
                            f"{group} call ({where})",
                        )
                    )
                else:
                    losers.extend(self._take_group_locked(message, group))
            elif message.priority is not RadioPriority.CRITICAL and message.subject:
                for i, queued in enumerate(self._pending):
                    same_subject = (
                        queued.agent == message.agent
                        and queued.subject == message.subject
                        and queued.priority is not RadioPriority.CRITICAL
                    )
                    if same_subject:
                        losers.append(
                            (
                                self._pending.pop(i),
                                message,
                                "same_agent",
                                f"replaced by a newer {message.agent} call on {message.subject}",
                            )
                        )
                        break
            if not dropped:
                self._pending.append(message)
                self._pending.sort(key=_order_key)
        for loser, winner, rule, reason in losers:
            self._supersede(loser, winner, rule, reason, group=decisions.group_of(loser))
        if not dropped:
            self._trace("radio_queued", message)
            self._signal()
        replaced = [loser for loser, *_ in losers if loser is not message]
        return replaced[0] if replaced else None

    def _conflict_locked(
        self, message: RadioMessage, group: str
    ) -> tuple[RadioMessage, str] | None:
        """A higher-priority call from another agent that ``message`` contradicts.

        Looked for in the queue and in what aired inside
        :data:`~rtv.pitwall.decisions.CONFLICT_WINDOW_S`. Equal priority is not a
        conflict: the newer call is the current answer.
        """
        action = decisions.action_of(message)
        if action is None:
            return None
        rank = PRIORITY_RANK[message.priority]
        window = decisions.CONFLICT_WINDOW_S
        candidates = [(m, "still queued") for m in self._pending]
        candidates += [(m, "aired") for m in reversed(self._history)]
        for other, where in candidates:
            if (
                other.agent == message.agent
                or decisions.group_of(other) != group
                or PRIORITY_RANK[other.priority] >= rank
                or abs(message.session_time - other.session_time) > window
                or not decisions.opposed(action, decisions.action_of(other))
            ):
                continue
            if where == "aired":
                where = f"aired {abs(message.session_time - other.session_time):.0f} s before"
            return other, where
        return None

    def _take_group_locked(
        self, message: RadioMessage, group: str
    ) -> list[tuple[RadioMessage, RadioMessage, str, str]]:
        """Pull the queued calls in ``group`` that ``message`` replaces."""
        out = []
        rank = PRIORITY_RANK[message.priority]
        critical = message.priority is RadioPriority.CRITICAL
        for queued in list(self._pending):
            if queued.priority is RadioPriority.CRITICAL or decisions.group_of(queued) != group:
                continue
            if queued.agent == message.agent:
                if critical:
                    continue  # one agent's own calls: the old rule, critical replaces nothing
                rule = "same_agent"
                reason = f"replaced by a newer {message.agent} call on {group}"
            elif PRIORITY_RANK[queued.priority] < rank:
                continue  # a lower-priority call never displaces a higher one
            elif critical:
                rule = "critical"
                reason = f"replaced by the {_who(message)} critical {group} call"
            else:
                rule = "group"
                reason = f"replaced by the {_who(message)} newer {group} call"
            self._pending.remove(queued)
            out.append((queued, message, rule, reason))
        return out

    def _supersede(
        self,
        message: RadioMessage,
        by: RadioMessage,
        rule: str,
        reason: str,
        *,
        group: str | None,
    ) -> None:
        """Mark ``message`` as never aired because of ``by``, and tell everyone.

        Like :meth:`expire`: it never reaches the history, but subscribers and
        callbacks get it with ``speak=False`` so the journal and the review show
        what was not said and why.
        """
        state = self._air_state()
        message.superseded = True
        message.superseded_by = by.message_id
        message.superseded_reason = reason
        message.speak = False
        message.air_session_time = (
            state.session_time if state is not None else max(message.session_time, by.session_time)
        )
        with self._lock:
            self.superseded += 1
            if rule == "conflict":
                self.conflicts += 1
            if by.agent != message.agent:
                self._notes[message.agent] = {
                    "message_id": message.message_id,
                    "spoken_text": message.spoken_text,
                    "by_message_id": by.message_id,
                    "by_agent": by.agent,
                    "by_priority": by.priority.value,
                    "by_spoken_text": by.spoken_text,
                    "reason": reason,
                }
            subs = list(self._subs)
            callbacks = list(self._callbacks)
        log.debug(
            "Radio: %s/%s superseded by %s/%s (%s)",
            message.agent, message.subject, by.agent, by.subject, rule,
        )
        self._trace(
            "radio_superseded",
            message,
            session_time=message.air_session_time,
            by_message_id=by.message_id,
            by_agent=by.agent,
            by_priority=by.priority.value,
            rule=rule,
            group=group,
            reason=reason,
            spoken_text=message.spoken_text,
        )
        self._deliver(message, subs, callbacks)

    def take_superseded(self, agent: str) -> list[dict[str, Any]]:
        """The agent's last call that another agent's call kept off the air.

        Handed out once: the agent's next run is shown it (one line), so it does
        not repeat a call the pitwall already decided against. Empty when there is
        nothing new.
        """
        with self._lock:
            note = self._notes.pop(agent, None)
        return [note] if note is not None else []

    def _signal(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:  # pragma: no cover - loop already closed
                pass

    def pop(self) -> RadioMessage | None:
        """Take the highest-priority queued message."""
        with self._lock:
            return self._pending.pop(0) if self._pending else None

    def take(self, now: float | None = None) -> RadioMessage | None:
        """The next message to air at session time ``now``, after the air-time check.

        Pops in priority order. A message that has stopped being true is expired
        (see :meth:`expire`) and the next one is tried; one that is merely old is
        tagged late. ``now`` defaults to the state source's session time; with
        neither, nothing is checked.
        """
        state: RaceState | None = None
        looked = False
        while True:
            message = self.pop()
            if message is None:
                return None
            if not looked:  # one snapshot per take, and none for an empty queue
                state, looked = self._air_state(), True
                if now is None and state is not None:
                    now = state.session_time
            if now is None:
                return message
            message.air_session_time = now
            verdict, reason, age = validity.check(message, state, now)
            if verdict == validity.EXPIRED:
                self.expire(message, reason or "no longer valid", age=age)
                continue
            if verdict == validity.LATE:
                self._tag_late(message, age)
            return message

    def _air_state(self) -> RaceState | None:
        source = self.state_source
        if source is None:
            return None
        try:
            return source()
        except Exception:  # pragma: no cover - a broken source must not stop radio
            log.exception("Radio could not read the race state")
            return None

    def _tag_late(self, message: RadioMessage, age: float | None) -> None:
        message.late = True
        message.late_by_s = round(age, 1) if age is not None else None
        if message.speak and not message.spoken_text.startswith(LATE_PREFIX):
            message.spoken_text = LATE_PREFIX + message.spoken_text
        with self._lock:
            self.late += 1
        self._trace(
            "radio_late",
            message,
            session_time=message.air_session_time,
            late_by_s=message.late_by_s,
            max_age_s=message.max_age_s,
            computed_at_session_time=message.computed_at_session_time,
        )

    def expire(self, message: RadioMessage, reason: str, *, age: float | None = None) -> None:
        """Drop a call that is no longer true, and tell every consumer it was dropped.

        It never reaches the history (agents are not shown it as recent radio,
        and it never airs), but subscribers and callbacks get it marked
        ``expired`` with ``speak=False``, so the journal and the radio lists can
        show what was not said and why.
        """
        message.expired = True
        message.expired_reason = reason
        message.speak = False
        with self._lock:
            self.expired += 1
            subs = list(self._subs)
            callbacks = list(self._callbacks)
        log.info("Radio: %s/%s expired before airing: %s", message.agent, message.subject, reason)
        self._trace(
            "radio_expired",
            message,
            session_time=message.air_session_time,
            reason=reason,
            age_s=round(age, 1) if age is not None else None,
            computed_at_session_time=message.computed_at_session_time,
            spoken_text=message.spoken_text,
        )
        self._deliver(message, subs, callbacks)

    def _deliver(
        self,
        message: RadioMessage,
        subs: list[RadioSubscription],
        callbacks: list[Callable[[RadioMessage], None]],
    ) -> None:
        for sub in subs:
            sub._deliver(message)
        for fn in callbacks:
            try:
                fn(message)
            except Exception:  # pragma: no cover - a bad consumer must not stop radio
                log.exception("Radio callback failed for %s", message.agent)

    def emit(self, message: RadioMessage) -> None:
        """Put a message on the air: history, then every consumer."""
        with self._lock:
            self._history.append(message)
            self.emitted += 1
            subs = list(self._subs)
            callbacks = list(self._callbacks)
        if message.late:
            self._trace("radio_aired", message, late=True, late_by_s=message.late_by_s)
        else:
            self._trace("radio_aired", message)
        self._deliver(message, subs, callbacks)

    def _trace(self, kind: str, message: RadioMessage, **extra: Any) -> None:
        safe_trace(
            self.tracer,
            kind,
            agent=message.agent,
            event_key=message.event_ref.key,
            message_id=message.message_id,
            seq=message.seq,
            priority=message.priority.value,
            subject=message.subject,
            refused=message.refused,
            **extra,
        )

    def pump(self, now: float) -> list[RadioMessage]:
        """Advance the channel to session time ``now``, respecting airtime.

        The synchronous, clock-injected twin of :meth:`run`. Driving the radio off
        the *session* clock rather than the wall clock is what makes a replayed
        race produce a byte-identical radio log -- the same property that makes the
        race-state event log reproducible.
        """
        out: list[RadioMessage] = []
        while now >= self._busy_until:
            message = self.take(now)
            if message is None:
                break
            self.emit(message)
            self._busy_until = now + self._airtime(message)
            out.append(message)
        return out

    def flush(self) -> list[RadioMessage]:
        """Emit everything queued, in order, ignoring airtime. Sync; used by tests."""
        out: list[RadioMessage] = []
        while True:
            message = self.take()
            if message is None:
                return out
            self.emit(message)
            out.append(message)

    async def run(self, *, idle_s: float = 0.25) -> None:
        """Pace the channel: one message at a time, each holding it for its airtime."""
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        while True:
            message = self.take()
            if message is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), idle_s)
                except TimeoutError:
                    pass
                continue
            self.emit(message)
            await asyncio.sleep(self._airtime(message))

    # ---- introspection ---------------------------------------------------
    def pending(self) -> list[RadioMessage]:
        with self._lock:
            return list(self._pending)

    def history(self, limit: int | None = None) -> list[RadioMessage]:
        with self._lock:
            items = list(self._history)
        return items[-limit:] if limit else items

    def find(self, message_id: str) -> RadioMessage | None:
        """Look a message up by its content-derived id.

        Searches what has aired *and* what is still queued: audio for a message
        can legitimately be fetched before it reaches the front of the channel.
        """
        if not message_id:
            return None
        with self._lock:
            for message in reversed(self._history):
                if message.message_id == message_id:
                    return message
            for message in self._pending:
                if message.message_id == message_id:
                    return message
        return None

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._history.clear()
            self._notes.clear()
            self._busy_until = float("-inf")

    def __iter__(self) -> Iterator[RadioMessage]:
        return iter(self.history())

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "published": self.published,
                "emitted": self.emitted,
                "superseded": self.superseded,
                "late": self.late,
                "expired": self.expired,
                "conflicts": self.conflicts,
                "pending": len(self._pending),
                "history": len(self._history),
                "subscribers": len(self._subs) + len(self._callbacks),
                "dropped": sum(s.dropped for s in self._subs),
            }


def _who(message: RadioMessage) -> str:
    """``"vehicle_engineer's"``: the possessive used in supersede reasons."""
    return f"{message.agent}'s"


def _order_key(message: RadioMessage) -> tuple[int, int]:
    return (PRIORITY_RANK[message.priority], message.seq)
