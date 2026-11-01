"""Reading the injected overlay: what a scripted condition means to a consumer.

The overlay is the ``injected`` list :meth:`ScriptedDirector.overlay
<rtv.director.scheduler.ScriptedDirector.overlay>` returns and the orchestrator puts in
an agent's state slice (and hands its policy). These helpers are the only places that
interpret it, so the strategist's policy and its triggers read a scripted caution or
regulation the same way. Every helper returns the "nothing injected" answer for an empty
overlay, which is what every live session has.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rtv.racestate.models import EventType, FlagPhase, RaceState
from rtv.racestate.plan import MandatoryStop, regulation_stop
from rtv.racestate.strategy_math import CAUTION_FLAGS

#: ``measure`` values of a weather_change payload that describe water on the track.
WET_MEASURES = frozenset({"track_wetness", "precipitation", "rain"})

Overlay = Sequence[Mapping[str, Any]]


def injected_flag(injected: Overlay) -> Mapping[str, Any] | None:
    """The injected flag condition in force, if any."""
    for entry in injected:
        if entry.get("event_type") == EventType.FLAG_CHANGE.value:
            return entry
    return None


def injected_caution(injected: Overlay) -> bool:
    """A scripted full-course caution (or red) is out."""
    flag = injected_flag(injected)
    if flag is None:
        return False
    payload = flag.get("payload") or {}
    to = payload.get("to")
    if to == FlagPhase.RED.value:
        return True
    return to == FlagPhase.YELLOW.value and bool(
        CAUTION_FLAGS.intersection(payload.get("active") or ())
    )


def pending_mandatory_stop(injected: Overlay, state: RaceState) -> MandatoryStop | None:
    """A scripted mandatory stop not yet served and whose window has not passed."""
    for entry in injected:
        if entry.get("event_type") != EventType.REGULATION_CHANGE.value:
            continue
        rule = regulation_stop(entry.get("payload") or {}, float(entry.get("since") or 0.0))
        if rule is None:
            continue
        rule = MandatoryStop(rule.from_lap, rule.to_lap, rule.since, bool(entry.get("served")))
        if rule.pending(state):
            return rule
    return None


def is_rain(payload: Mapping[str, Any]) -> bool:
    """A weather_change payload that brings water: a wet measure going up."""
    if payload.get("measure") not in WET_MEASURES:
        return False
    try:
        return float(payload.get("to")) > float(payload.get("from") or 0.0)
    except (TypeError, ValueError):
        return False
