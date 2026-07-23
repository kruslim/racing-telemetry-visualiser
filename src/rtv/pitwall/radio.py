"""The single merged radio feed: one channel, priority-ordered, self-superseding.

Several agents will be producing messages at once. A driver has one pair of ears,
so the pitwall has one channel, and that channel needs discipline:

* **Priority then time.** Critical pre-empts whatever is queued behind it.
* **Supersede.** Two advisories about the same subject (two pit-window updates,
  two fuel calls) are not two messages -- the newer one replaces the older one
  while it is still queued. Saying a stale pit lap out loud is worse than silence.
* **Airtime.** A message occupies the channel for roughly as long as it takes to
  say. That is what makes superseding meaningful, and it is the hook stage 4's
  TTS layer will hang off.
* **Critical is never superseded or dropped.** Losing a "box now, you're on fumes"
  to a tidier queue would be the one unforgivable bug in here.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any

from rtv.logging import get_logger
from rtv.pitwall.framework import PRIORITY_RANK, RadioMessage, RadioPriority

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
    ) -> None:
        self._lock = threading.Lock()
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
        """Queue a message. Returns the message it superseded, if any."""
        replaced: RadioMessage | None = None
        with self._lock:
            self._seq += 1
            message.seq = self._seq
            self.published += 1
            if message.priority is not RadioPriority.CRITICAL and message.subject:
                for i, queued in enumerate(self._pending):
                    same_subject = (
                        queued.agent == message.agent
                        and queued.subject == message.subject
                        and queued.priority is not RadioPriority.CRITICAL
                    )
                    if same_subject:
                        replaced = self._pending.pop(i)
                        self.superseded += 1
                        break
            self._pending.append(message)
            self._pending.sort(key=_order_key)
        if replaced is not None:
            log.debug(
                "Radio: %s/%s superseded (seq %d -> %d)",
                message.agent, message.subject, replaced.seq, message.seq,
            )
        self._signal()
        return replaced

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

    def emit(self, message: RadioMessage) -> None:
        """Put a message on the air: history, then every consumer."""
        with self._lock:
            self._history.append(message)
            self.emitted += 1
            subs = list(self._subs)
            callbacks = list(self._callbacks)
        for sub in subs:
            sub._deliver(message)
        for fn in callbacks:
            try:
                fn(message)
            except Exception:  # pragma: no cover - a bad consumer must not stop radio
                log.exception("Radio callback failed for %s", message.agent)

    def pump(self, now: float) -> list[RadioMessage]:
        """Advance the channel to session time ``now``, respecting airtime.

        The synchronous, clock-injected twin of :meth:`run`. Driving the radio off
        the *session* clock rather than the wall clock is what makes a replayed
        race produce a byte-identical radio log -- the same property that makes the
        race-state event log reproducible.
        """
        out: list[RadioMessage] = []
        while now >= self._busy_until:
            message = self.pop()
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
            message = self.pop()
            if message is None:
                return out
            self.emit(message)
            out.append(message)

    async def run(self, *, idle_s: float = 0.25) -> None:
        """Pace the channel: one message at a time, each holding it for its airtime."""
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        while True:
            message = self.pop()
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

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._history.clear()
            self._busy_until = float("-inf")

    def __iter__(self) -> Iterator[RadioMessage]:
        return iter(self.history())

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "published": self.published,
                "emitted": self.emitted,
                "superseded": self.superseded,
                "pending": len(self._pending),
                "history": len(self._history),
                "subscribers": len(self._subs) + len(self._callbacks),
                "dropped": sum(s.dropped for s in self._subs),
            }


def _order_key(message: RadioMessage) -> tuple[int, int]:
    return (PRIORITY_RANK[message.priority], message.seq)
