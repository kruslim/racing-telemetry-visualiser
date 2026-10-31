"""Radio audio discipline. Pure logic: no speech engine, no Qt, no timers.

A port of ``frontend/js/audio-manager.js``. One driver, four agents talking; this
module owns every rule about *what gets said and when*, and an injected speaker
owns the noise:

1. Single speaker: one utterance at a time.
2. Critical interrupts whatever is mid-sentence, unless that is critical too.
3. Advisory queues, in priority-then-arrival order.
4. Info is opportunistic and dropped once it has waited ``stale_info_ms``.
5. A newer non-critical message with the same (agent, subject) replaces the
   older one while it is still queued.
6. A muted agent is dropped at the door, never queued.

The speaker is duck-typed: ``speak(message, volume=, cue=)`` and ``cancel()``; the
host calls ``finished(message_id)`` when an utterance ends.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

PRIORITY_RANK = {"critical": 0, "advisory": 1, "info": 2}
DEFAULT_PRIORITY = "info"
STALE_INFO_MS = 15000
SEEN_LIMIT = 256


def priority_rank(priority: Any) -> int:
    return PRIORITY_RANK.get(priority, PRIORITY_RANK[DEFAULT_PRIORITY])


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    if number != number or number in (float("inf"), float("-inf")):
        return 1.0
    return min(1.0, max(0.0, number))


def normalise(raw: dict | None, make_id: Callable[[], str] | None = None) -> dict:
    """Coerce a wire message into exactly the fields the channel cares about."""
    message = raw or {}
    priority = message.get("priority")
    if priority not in PRIORITY_RANK:
        priority = DEFAULT_PRIORITY
    return {
        "message_id": message.get("message_id") or (make_id() if make_id else ""),
        "agent": message.get("agent") or "pitwall",
        "priority": priority,
        "spoken_text": str(message.get("spoken_text") or "").strip(),
        "detail_text": message.get("detail_text") or "",
        "subject": message.get("subject") or "",
        "audio_url": message.get("audio_url"),
        "speak": message.get("speak") is not False,
        "session_time": message.get("session_time"),
        "data": message.get("data") or {},
        "refused": bool(message.get("refused")),
        "grounded": message.get("grounded") is not False,
    }


class RadioAudioManager:
    """The radio channel as the driver hears it."""

    def __init__(
        self,
        speaker: Any = None,
        now: Callable[[], float] | None = None,
        stale_info_ms: float = STALE_INFO_MS,
        volume: float = 1.0,
        on_change: Callable[[dict], None] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ):
        self.speaker = speaker
        self.now = now or (lambda: time.monotonic() * 1000.0)
        self.stale_info_ms = stale_info_ms
        self.volume = _clamp01(volume)
        self.master_muted = False
        self._muted: set[str] = set()
        self._queue: list[dict] = []
        self._current: dict | None = None
        self._seq = 0
        self._seen: list[str] = []
        self._seen_set: set[str] = set()
        self._on_change = on_change
        self._on_event = on_event
        self.counters = {
            "received": 0,
            "spoken": 0,
            "queued": 0,
            "superseded": 0,
            "interrupted": 0,
            "dropped_stale": 0,
            "dropped_muted": 0,
            "dropped_duplicate": 0,
            "dropped_silent": 0,
        }
        self.last_interrupted: dict | None = None

    # ---- controls ------------------------------------------------------
    def set_volume(self, value: Any) -> float:
        self.volume = _clamp01(value)
        self._changed()
        return self.volume

    def set_master_muted(self, muted: bool) -> bool:
        self.master_muted = bool(muted)
        if self.master_muted:
            self.stop()
        self._changed()
        return self.master_muted

    def set_agent_muted(self, agent: str, muted: bool) -> bool:
        if muted:
            self._muted.add(agent)
        else:
            self._muted.discard(agent)
        if muted and self._current and self._current["message"]["agent"] == agent:
            self.stop()
        self._changed()
        return self.is_agent_muted(agent)

    def is_agent_muted(self, agent: str) -> bool:
        return self.master_muted or agent in self._muted

    def muted_agents(self) -> list[str]:
        return sorted(self._muted)

    def mark_heard(self, message_id: str) -> None:
        """Remember a message without speaking it (history loaded on connect)."""
        self._remember(message_id)

    def stop(self) -> None:
        """Stop what is being said and let the queue carry on."""
        if self._current:
            self._cancel_speaker()
            self._current = None
        self._advance()
        self._changed()

    def clear(self) -> None:
        """Silence: kill the current call and throw the queue away."""
        self._cancel_speaker()
        self._current = None
        self._queue = []
        self._changed()

    # ---- the channel ---------------------------------------------------
    def push(self, raw: dict | None) -> dict:
        """Offer one RadioMessage to the channel."""

        def fallback() -> str:
            self._seq += 1
            text = str((raw or {}).get("spoken_text") or "")
            return f"local-{self._seq}-{(raw or {}).get('agent') or '?'}-{len(text)}"

        message = normalise(raw, fallback)
        self.counters["received"] += 1
        if not message["spoken_text"]:
            return self._result("dropped", message, "empty")
        if message["speak"] is False:
            self.counters["dropped_silent"] += 1
            return self._result("dropped", message, "not_spoken")
        if self._is_duplicate(message["message_id"]):
            self.counters["dropped_duplicate"] += 1
            return self._result("dropped", message, "duplicate")
        if self.is_agent_muted(message["agent"]):
            self._remember(message["message_id"])
            self.counters["dropped_muted"] += 1
            return self._result("dropped", message, "muted")

        self._remember(message["message_id"])
        self._expire_stale()
        critical = message["priority"] == "critical"
        self._seq += 1
        entry = {"message": message, "queued_at": self.now(), "seq": self._seq}
        replaced = None if critical else self._supersede(entry)

        if critical and self._current and self._current["message"]["priority"] != "critical":
            interrupted = self._current["message"]
            self._cancel_speaker()
            self.counters["interrupted"] += 1
            self.last_interrupted = interrupted
            self._current = None
            self._start(entry)
            return self._result("interrupted", message, None, interrupted)

        if not self._current and not self._queue:
            self._start(entry)
            return self._result("speaking", message, "superseded" if replaced else None, replaced)

        self._queue.append(entry)
        self._sort_queue()
        if not replaced:
            self.counters["queued"] += 1
        self._changed()
        return self._result("queued", message, "superseded" if replaced else None, replaced)

    def finished(self, message_id: str | None = None) -> bool:
        """The host tells us an utterance ended."""
        if not self._current:
            return False
        if message_id and self._current["message"]["message_id"] != message_id:
            return False
        self.counters["spoken"] += 1
        self._current = None
        self._advance()
        self._changed()
        return True

    def tick(self) -> dict:
        """Advance the clock: drop stale info and start anything waiting."""
        dropped = self._expire_stale()
        started = self._advance()
        if dropped or started:
            self._changed()
        return {"dropped": dropped, "started": started}

    # ---- introspection -------------------------------------------------
    @property
    def speaking(self) -> dict | None:
        return self._current["message"] if self._current else None

    @property
    def queue(self) -> list[dict]:
        return [entry["message"] for entry in self._queue]

    @property
    def idle(self) -> bool:
        return self._current is None

    def snapshot(self) -> dict:
        return {
            "speaking": self.speaking,
            "queue": self.queue,
            "volume": self.volume,
            "master_muted": self.master_muted,
            "muted": self.muted_agents(),
            "counters": dict(self.counters),
        }

    # ---- internals -----------------------------------------------------
    def _supersede(self, entry: dict) -> dict | None:
        agent, subject = entry["message"]["agent"], entry["message"]["subject"]
        if not subject:
            return None
        for index, queued in enumerate(self._queue):
            m = queued["message"]
            if m["agent"] == agent and m["subject"] == subject and m["priority"] != "critical":
                replaced = self._queue.pop(index)
                self.counters["superseded"] += 1
                self._emit("superseded", {"replaced": replaced["message"], "by": entry["message"]})
                return replaced["message"]
        return None

    def _expire_stale(self) -> int:
        if not self._queue:
            return 0
        now = self.now()
        kept, dropped = [], 0
        for entry in self._queue:
            waited = now - entry["queued_at"]
            if entry["message"]["priority"] == "info" and waited > self.stale_info_ms:
                dropped += 1
                self.counters["dropped_stale"] += 1
                self._emit("stale", {"message": entry["message"], "waited_ms": waited})
            else:
                kept.append(entry)
        self._queue = kept
        return dropped

    def _advance(self) -> bool:
        if self._current or not self._queue:
            return False
        self._start(self._queue.pop(0))
        return True

    def _start(self, entry: dict) -> None:
        self._current = entry
        message = entry["message"]
        if self.speaker is not None and hasattr(self.speaker, "speak"):
            # A short radio click ahead of a critical call primes the ear.
            self.speaker.speak(message, volume=self.volume, cue=message["priority"] == "critical")
        self._emit("speaking", {"message": message})
        self._changed()

    def _cancel_speaker(self) -> None:
        if self.speaker is not None and hasattr(self.speaker, "cancel"):
            self.speaker.cancel()

    def _sort_queue(self) -> None:
        self._queue.sort(key=lambda e: (priority_rank(e["message"]["priority"]), e["seq"]))

    def _is_duplicate(self, message_id: str) -> bool:
        if not message_id:
            return False
        if message_id in self._seen_set:
            return True
        if self._current and self._current["message"]["message_id"] == message_id:
            return True
        return any(e["message"]["message_id"] == message_id for e in self._queue)

    def _remember(self, message_id: str) -> None:
        if not message_id or message_id in self._seen_set:
            return
        self._seen_set.add(message_id)
        self._seen.append(message_id)
        while len(self._seen) > SEEN_LIMIT:
            self._seen_set.discard(self._seen.pop(0))

    def _result(
        self, action: str, message: dict, reason: str | None = None, related: dict | None = None
    ) -> dict:
        result: dict = {"action": action, "message": message}
        if reason:
            result["reason"] = reason
        if related:
            result["related"] = related
        self._emit(action, result)
        return result

    def _emit(self, kind: str, detail: dict) -> None:
        if self._on_event:
            self._on_event(kind, detail)

    def _changed(self) -> None:
        if self._on_change:
            self._on_change(self.snapshot())
