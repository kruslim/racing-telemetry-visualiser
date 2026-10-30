"""In-process publish/subscribe for :class:`RaceEvent`s.

The producer is always a plain thread (the live poller or the replay driver);
consumers are a mix of asyncio tasks (``/ws/pitwall``) and synchronous callbacks
(later stages: strategist / spotter agents). So the queue is a thread-safe
``deque`` and the async wake-up goes through ``loop.call_soon_threadsafe``.

Back-pressure follows the same philosophy as ``/ws/live``: every subscriber gets
a **bounded** queue, and a consumer that falls behind loses the *oldest* events
rather than growing without limit. Drops are counted, never silent -- a pitwall
that quietly discards a black flag would be worse than one that admits it.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from rtv.logging import get_logger
from rtv.racestate.models import RaceEvent

log = get_logger("racestate.bus")

DEFAULT_QUEUE_SIZE = 256
DEFAULT_HISTORY = 500


class Subscription:
    """A bounded, latest-wins event queue for one consumer."""

    def __init__(self, bus: EventBus, maxsize: int, name: str | None = None) -> None:
        self._bus = bus
        self._queue: deque[RaceEvent] = deque(maxlen=maxsize)
        self._lock = threading.Lock()
        self._dropped = 0
        self._closed = False
        self.name = name or f"sub-{id(self):x}"
        # Bound lazily on first async use so a Subscription can be created off-loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiter: asyncio.Event | None = None

    # ---- producer side (engine thread) ---------------------------------
    def _deliver(self, event: RaceEvent) -> None:
        with self._lock:
            if self._closed:
                return
            if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
                self._dropped += 1
            self._queue.append(event)  # deque(maxlen=) evicts the oldest for us
        loop, waiter = self._loop, self._waiter
        if loop is not None and waiter is not None:
            try:
                loop.call_soon_threadsafe(waiter.set)
            except RuntimeError:  # pragma: no cover - loop already closed
                pass

    # ---- consumer side: synchronous ------------------------------------
    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def dropped(self) -> int:
        """Events evicted because this consumer could not keep up."""
        with self._lock:
            return self._dropped

    def get_nowait(self) -> RaceEvent | None:
        with self._lock:
            return self._queue.popleft() if self._queue else None

    def drain(self) -> list[RaceEvent]:
        """Take everything queued right now (oldest first)."""
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
        return items

    # ---- consumer side: asyncio ----------------------------------------
    def _bind_loop(self) -> asyncio.Event:
        if self._waiter is None:
            self._loop = asyncio.get_running_loop()
            self._waiter = asyncio.Event()
        return self._waiter

    async def next_events(self, timeout: float | None = None) -> list[RaceEvent]:
        """Await the next batch of events; ``[]`` if ``timeout`` elapses first."""
        waiter = self._bind_loop()
        items = self.drain()
        if items:
            return items
        waiter.clear()
        # Re-check after clearing: the producer may have delivered in between.
        items = self.drain()
        if items:
            return items
        try:
            await asyncio.wait_for(waiter.wait(), timeout)
        except TimeoutError:
            return []
        return self.drain()

    # ---- lifecycle ------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._queue.clear()
        self._bus.unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class EventBus:
    """Fan-out hub. Publishing is O(subscribers) and never blocks the producer."""

    def __init__(
        self, *, queue_size: int = DEFAULT_QUEUE_SIZE, history: int = DEFAULT_HISTORY
    ) -> None:
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        self._callbacks: list[Callable[[RaceEvent], None]] = []
        self._history: deque[RaceEvent] = deque(maxlen=history)
        self._published = 0

    # ---- subscription ---------------------------------------------------
    def subscribe(
        self, *, maxsize: int | None = None, name: str | None = None
    ) -> Subscription:
        """Queue-style subscription, usable from sync code or asyncio."""
        sub = Subscription(self, maxsize or self._queue_size, name)
        with self._lock:
            self._subs.append(sub)
        return sub

    def subscribe_callback(
        self, fn: Callable[[RaceEvent], None]
    ) -> Callable[[RaceEvent], None]:
        """Register a synchronous callback, invoked on the *producer* thread.

        Keep it short -- anything slow belongs behind :meth:`subscribe` instead.
        """
        with self._lock:
            self._callbacks.append(fn)
        return fn

    def unsubscribe(self, target: Subscription | Callable[[RaceEvent], None]) -> None:
        with self._lock:
            if isinstance(target, Subscription):
                if target in self._subs:
                    self._subs.remove(target)
            elif target in self._callbacks:
                self._callbacks.remove(target)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs) + len(self._callbacks)

    # ---- publishing -----------------------------------------------------
    def publish(self, event: RaceEvent) -> None:
        with self._lock:
            self._history.append(event)
            self._published += 1
            subs = list(self._subs)
            callbacks = list(self._callbacks)
        for sub in subs:
            sub._deliver(event)
        for fn in callbacks:
            try:
                fn(event)
            except Exception:  # pragma: no cover - a bad consumer must not stop the loop
                log.exception("Event callback failed for %s", event.event_type)

    def publish_many(self, events: Iterable[RaceEvent]) -> None:
        for event in events:
            self.publish(event)

    # ---- introspection --------------------------------------------------
    def history(self, limit: int | None = None) -> list[RaceEvent]:
        """Recent events, oldest first (bounded ring buffer)."""
        with self._lock:
            items = list(self._history)
        return items[-limit:] if limit else items

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    @property
    def published(self) -> int:
        with self._lock:
            return self._published

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "published": self._published,
                "subscribers": len(self._subs),
                "callbacks": len(self._callbacks),
                "history": len(self._history),
                "dropped": sum(s._dropped for s in self._subs),
            }
