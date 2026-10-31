"""The live seam: a kept visual note becomes one ``visual_observation`` event.

The research plan's order is post-race first, live later and event-triggered
only, so live *capture* (grabbing frames while iRacing runs) is not built yet.
What is built is the seam it will use, exercised by tests: given a grounded note
for the session the engine is consuming, publish it on the engine's
:class:`~rtv.racestate.bus.EventBus` like any detector event. From there the
recorder journals it, ``/ws/pitwall`` streams it, and the spotter and coach may
take it at the lowest radio priority.

The event never touches :class:`~rtv.racestate.models.RaceState`: it is published
straight to the bus, not through the engine's update loop, and its payload holds
words only (the note already passed :mod:`rtv.vision.grounding`).
"""

from __future__ import annotations

from typing import Any

from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.vision.models import VisualNote

#: Observation kinds each live consumer cares about. The strategist and vehicle
#: engineer need numbers and are never woken by vision.
SPOTTER_KINDS = frozenset({"traffic", "track_state", "damage"})
COACH_KINDS = frozenset({"kerb_use", "line", "car_behaviour"})


def visual_events(note: VisualNote, *, tick: int, state_version: int = 0) -> list[RaceEvent]:
    """One event per kept observation of ``note`` (none for a note that failed)."""
    if note.status != "ok":
        return []
    primary = note.moment.primary
    out: list[RaceEvent] = []
    for obs in note.kept:
        payload: dict[str, Any] = {
            "source": "vision",
            "kind": obs.kind,
            "text": obs.text,
            "confidence": obs.confidence,
            "status": obs.status,
            "note_id": note.note_id,
            "corner": note.moment.corner,
            "trigger": {
                "kind": primary.kind.value,
                "session_time": primary.session_time,
                "journal_seq": primary.journal_seq,
            },
        }
        out.append(
            RaceEvent(
                event_type=EventType.VISUAL_OBSERVATION,
                tick=tick,
                session_time=note.moment.session_time,
                lap=note.moment.lap,
                severity=Severity.INFO,
                payload=payload,
                state_version=state_version,
            )
        )
    return out


def publish_note(engine: Any, session_id: str, note: VisualNote) -> int:
    """Publish ``note`` if ``engine`` is consuming ``session_id``; returns events sent."""
    if engine is None:
        return 0
    state = engine.snapshot()
    if state.session.session_id != session_id:
        return 0
    events = visual_events(note, tick=state.tick, state_version=state.version)
    if events:
        engine.bus.publish_many(events)
    return len(events)
