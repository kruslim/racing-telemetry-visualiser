"""The pitwall's activity trace: what the agent layer is doing, as it does it.

The radio feed says what the pitwall *decided*; this says how it got there. Every
routing decision, model call, tool call, validator verdict and radio transition is
recorded as one small dict, so the live view can draw the pipeline working and the
post-race review can replay it. Nothing here is inferred or animated: a record
exists because the code path it names actually ran.

The envelope is the contract shared with the session recorder::

    {"seq": 412, "wall_ms": 1730328000123, "session_time": 1834.2, "tick": 110052,
     "kind": "llm_call_end", "agent": "strategist", "run_id": "strategist-37",
     "event_key": "fuel_window_open:L14", "data": {...}}

``ActivityLog`` reuses :class:`~rtv.racestate.bus.EventBus` wholesale -- the same
bounded, drop-oldest, drops-counted subscriptions, the same asyncio wake-up -- so a
consumer subscribes to it exactly as it subscribes to race events.

Tracing is strictly passive. A tracer that raises is logged and ignored; it can
cost a record, never an agent run.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from typing import Any

from rtv.logging import get_logger
from rtv.racestate.bus import EventBus

log = get_logger("pitwall.activity")

DEFAULT_ACTIVITY_HISTORY = 2000
DEFAULT_ACTIVITY_QUEUE = 1024

#: Every kind this module emits. Consumers must ignore kinds they do not know:
#: the session recorder adds its own (race_event, radio, lifecycle, state).
KINDS = (
    "dispatch",
    "inbox_superseded",
    "run_start",
    "llm_call_start",
    "llm_call_end",
    "tool_call",
    "validate",
    "run_end",
    "radio_queued",
    "radio_superseded",
    "radio_aired",
    "plan_proposal_rejected",
)

#: A tracer is anything that takes a record's fields; ``ActivityLog.record`` is one.
Tracer = Callable[..., Any]


class ActivityLog(EventBus):
    """Bounded fan-out of activity records, oldest dropped first."""

    def __init__(
        self,
        *,
        clock: Callable[[], tuple[float, int]] | None = None,
        wall_clock: Callable[[], float] | None = time.time,
        history: int = DEFAULT_ACTIVITY_HISTORY,
        queue_size: int = DEFAULT_ACTIVITY_QUEUE,
    ) -> None:
        super().__init__(queue_size=queue_size, history=history)
        #: ``() -> (session_time, tick)`` for records that do not carry their own,
        #: normally read off the race-state engine.
        self._clock = clock
        self._wall_clock = wall_clock
        self._seq = itertools.count(1)
        self._seq_lock = threading.Lock()

    def record(
        self,
        kind: str,
        *,
        agent: str | None = None,
        run_id: str | None = None,
        event_key: str | None = None,
        session_time: float | None = None,
        tick: int | None = None,
        **data: Any,
    ) -> dict[str, Any] | None:
        """Build one record and publish it. Never raises."""
        try:
            if (session_time is None or tick is None) and self._clock is not None:
                now_t, now_tick = self._clock()
                session_time = now_t if session_time is None else session_time
                tick = now_tick if tick is None else tick
            with self._seq_lock:
                seq = next(self._seq)
            item = {
                "seq": seq,
                "wall_ms": int(self._wall_clock() * 1000) if self._wall_clock else None,
                "session_time": session_time,
                "tick": tick,
                "kind": kind,
                "agent": agent,
                "run_id": run_id,
                "event_key": event_key,
                "data": data,
            }
            self.publish(item)  # type: ignore[arg-type]
            return item
        except Exception:  # pragma: no cover - tracing must never break a run
            log.exception("Could not record %s activity", kind)
            return None

    # The bus is typed for RaceEvents but is generic in practice.
    __call__ = record

    def since(self, seq: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """Records with ``seq`` greater than ``seq``, oldest first."""
        items = [r for r in self.history() if r["seq"] > seq]  # type: ignore[index]
        return items[:limit] if limit else items  # type: ignore[return-value]


#: Data fields kept for the journal but left out of the live feed: a run's whole
#: model input and state snapshot (``run_start``), whole tool results
#: (``tool_call``) and the model's structured answer (``validate``).
JOURNAL_ONLY_FIELDS = ("input", "state_full", "result_full", "output_full")


def public(record: dict[str, Any]) -> dict[str, Any]:
    """``record`` without its journal-only fields, for browsers and the desktop."""
    data = record.get("data")
    if not isinstance(data, dict) or not any(k in data for k in JOURNAL_ONLY_FIELDS):
        return record
    slim = {k: v for k, v in data.items() if k not in JOURNAL_ONLY_FIELDS}
    return {**record, "data": slim}


def summarize(payload: Any, *, limit: int = 12) -> dict[str, Any]:
    """A tool result reduced to its shape: enough to show, never the payload.

    The trace is broadcast to every browser and written to disk; a full tool
    result (a standings table, a corner analysis) would make it heavy and would
    duplicate what the session store already holds.
    """
    if isinstance(payload, dict):
        keys = list(payload)
        out: dict[str, Any] = {"type": "object", "keys": keys[:limit], "size": len(keys)}
        if "error" in payload:
            out["error"] = str(payload["error"])[:200]
        return out
    if isinstance(payload, list | tuple):
        return {"type": "array", "size": len(payload)}
    if payload is None:
        return {"type": "null"}
    text = str(payload)
    return {"type": type(payload).__name__, "size": len(text)}


def safe_trace(tracer: Tracer | None, kind: str, **fields: Any) -> None:
    """Call a tracer if there is one, swallowing anything it raises."""
    if tracer is None:
        return
    try:
        tracer(kind, **fields)
    except Exception:  # pragma: no cover - defensive
        log.exception("Activity tracer failed on %s", kind)
