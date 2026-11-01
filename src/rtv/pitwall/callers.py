"""Deterministic callers: radio calls written by templates, with no model call.

Some calls are fully determined by the detector event that prompts them. "Car
left", "clear", "blue flag, P3 lapping you, 1.2 behind" have one right wording,
and every figure in them is already in the event payload. Asking a model for that
sentence costs 12-17 s, and a spotter call is useful for about one second. So
these calls skip the agent layer: a :class:`DeterministicCaller` maps an event to
a :class:`TemplateCall` and :func:`template_message` turns it into an ordinary
:class:`~rtv.pitwall.framework.RadioMessage` with ``source="template"``.

From there it is a radio call like any other. It goes through the same
:class:`~rtv.pitwall.radio.RadioFeed`, so priority order, same-subject supersede
("clear" replaces a queued "car left") and B1's air-time check all apply: the
message carries ``computed_at_session_time``, ``max_age_s`` and whatever rule
the template set (``subject_car_idx``, an expiry), so a call about a car that has
gone is dropped rather than aired.

Grounded by construction: a template only formats values it read from the event,
which the tests prove by running every template through the citation validator
with the event as the only fact source.

The public surface, for any later caller (driver questions answered from engine
state, C1, will use it):

* :class:`TemplateCall` -- what to say, at which priority, about which subject.
* :func:`template_message` -- build the stamped ``RadioMessage`` for an event.
* :class:`DeterministicCaller` -- an agent name plus ``{event_type: template}``;
  :meth:`DeterministicCaller.call` returns the message or ``None``.
* :func:`build_callers` -- the callers the orchestrator runs by default.

The orchestrator calls them synchronously in ``dispatch``, before any agent is
considered, and publishes straight onto the feed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rtv.logging import get_logger
from rtv.pitwall.framework import EventRef, RadioMessage, RadioPriority
from rtv.racestate.models import EventType, FlagPhase, RaceEvent, RaceState

log = get_logger("pitwall.callers")

TEMPLATE_SOURCE = "template"
MODEL_NAME = "template"

#: Seconds a spotter template call may wait for the channel before it airs
#: tagged late (``RTV_PITWALL_SPOTTER_MAX_AGE_S`` overrides it).
SPOTTER_MAX_AGE_S = 3.0
#: "Car left" is worthless a second and a half later: dropped, never aired late.
ALONGSIDE_EXPIRES_S = 1.5
#: A closing car or a blue flag is still worth hearing a few seconds on, not more.
TRAFFIC_EXPIRES_S = 8.0
#: A flag or an incident is called at most once per this many session seconds.
HAZARD_COOLDOWN_S = 10.0


@dataclass
class TemplateCall:
    """One deterministic call: the words, how urgent, what it is about, and its rules."""

    spoken_text: str
    detail_text: str
    priority: RadioPriority = RadioPriority.ADVISORY
    #: Same-subject calls from the same agent supersede each other while queued.
    subject: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    #: Air-time rules (see :mod:`rtv.pitwall.validity`). ``expires_after_s`` is
    #: relative to the event; it becomes ``expires_at_session_time``.
    expires_after_s: float | None = None
    subject_car_idx: int | None = None
    valid_until_lap: int | None = None
    #: ``None`` uses the caller's ``max_age_s``; ``0`` turns the late tag off.
    max_age_s: float | None = None
    #: Session seconds before the same subject may be called again (a flapping
    #: flag must not become a stream of critical calls). ``None`` = no limit.
    cooldown_s: float | None = None


#: A template reads the event (and, if it must, the state) and returns a call,
#: or ``None`` when this event is not worth saying.
Template = Callable[[RaceEvent, RaceState], "TemplateCall | None"]


def template_message(
    agent: str,
    event: RaceEvent,
    call: TemplateCall,
    *,
    max_age_s: float | None = None,
    state: RaceState | None = None,
) -> RadioMessage:
    """The radio message for ``call``, stamped for the air-time check.

    The call's age is measured from the event: that is the moment the fact was
    true, and the moment a driver would want to hear it.
    """
    age = call.max_age_s if call.max_age_s is not None else max_age_s
    if age is not None and age <= 0:
        age = None
    expires = (
        round(event.session_time + call.expires_after_s, 3)
        if call.expires_after_s is not None
        else None
    )
    return RadioMessage(
        agent=agent,
        priority=call.priority,
        spoken_text=call.spoken_text,
        detail_text=call.detail_text,
        data={**call.data, "source": TEMPLATE_SOURCE, "event": event.key},
        event_ref=EventRef.of(event),
        session_time=event.session_time,
        subject=call.subject,
        model=MODEL_NAME,
        source=TEMPLATE_SOURCE,
        state_version=state.version if state is not None else event.state_version,
        computed_at_session_time=event.session_time,
        max_age_s=age,
        expires_at_session_time=expires,
        subject_car_idx=call.subject_car_idx,
        valid_until_lap=call.valid_until_lap,
    )


class DeterministicCaller:
    """An agent's template calls: ``{event_type: template}`` under one agent name."""

    def __init__(
        self,
        agent: str,
        templates: Mapping[str, Template],
        *,
        max_age_s: float | None = None,
    ) -> None:
        self.agent = agent
        self.templates = dict(templates)
        self.max_age_s = max_age_s
        self.calls = 0
        self.cooled = 0
        self._last: dict[str, float] = {}

    def handles(self, event: RaceEvent) -> bool:
        return event.event_type.value in self.templates

    def call(self, event: RaceEvent, state: RaceState) -> RadioMessage | None:
        """The message for ``event``, or ``None`` if no template wants to speak."""
        template = self.templates.get(event.event_type.value)
        if template is None:
            return None
        try:
            call = template(event, state)
        except Exception:  # a broken template costs the call, never the radio
            log.exception("Template for %s/%s failed", self.agent, event.key)
            return None
        if call is None:
            return None
        if call.cooldown_s is not None:
            last = self._last.get(call.subject)
            if last is not None and 0 <= event.session_time - last < call.cooldown_s:
                self.cooled += 1
                return None
            self._last[call.subject] = event.session_time
        self.calls += 1
        return template_message(self.agent, event, call, max_age_s=self.max_age_s, state=state)

    def reset(self) -> None:
        """Forget the cooldowns, between sessions or replays."""
        self._last.clear()

    def describe(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "events": sorted(self.templates),
            "max_age_s": self.max_age_s,
            "calls": self.calls,
            "cooled": self.cooled,
        }


# --------------------------------------------------------------------------
# the spotter's templates
# --------------------------------------------------------------------------
_ALONGSIDE_WORDS = {
    "car_left": "Car left.",
    "car_right": "Car right.",
    "car_left_right": "Three wide, cars both sides.",
    "two_cars_left": "Two cars left.",
    "two_cars_right": "Two cars right.",
    "clear": "Clear.",
}


def _seconds(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return f"{float(value):.1f}"


def _car(position: Any) -> str:
    return f"P{position}" if isinstance(position, int) and position > 0 else "Car"


def _alongside(event: RaceEvent, state: RaceState) -> TemplateCall | None:
    side = event.payload.get("side")
    words = _ALONGSIDE_WORDS.get(str(side))
    if words is None:
        return None
    return TemplateCall(
        spoken_text=words,
        detail_text=f"iRacing proximity sensor: {side} (was {event.payload.get('from')}).",
        priority=RadioPriority.INFO if side == "clear" else RadioPriority.ADVISORY,
        # One subject for every side state: "clear" replaces a queued "car left".
        subject="traffic:alongside",
        data={"threat": "clear" if side == "clear" else "alongside", "side": side},
        expires_after_s=ALONGSIDE_EXPIRES_S,
        # Dropped when stale, never aired as "Late call: car left".
        max_age_s=0,
    )


def _traffic_close(event: RaceEvent, state: RaceState) -> TemplateCall | None:
    p = event.payload
    gap = _seconds(p.get("gap"))
    side = p.get("side")
    if side not in ("ahead", "behind"):
        return None
    who = _car(p.get("position"))
    if side == "behind":
        spoken = f"{who} behind, {gap}, closing." if gap else f"{who} behind, closing."
    else:
        spoken = f"Closing on {who} ahead, {gap}." if gap else f"Closing on {who} ahead."
    idx = p.get("car_idx")
    return TemplateCall(
        spoken_text=spoken,
        detail_text=(
            f"Car {idx} is {p.get('gap')} s {side} and closing at "
            f"{p.get('closing_rate_s_per_s')} s/s (threshold {p.get('threshold_gap_s')} s)."
        ),
        subject="traffic:car_closing",
        data={"threat": "car_closing", "side": side, "car_idx": idx, "gap": p.get("gap")},
        subject_car_idx=idx if isinstance(idx, int) else None,
        expires_after_s=TRAFFIC_EXPIRES_S,
    )


def _blue_flag(event: RaceEvent, state: RaceState) -> TemplateCall | None:
    p = event.payload
    gap = _seconds(p.get("gap"))
    who = _car(p.get("position"))
    spoken = f"Blue flag, {who} lapping you, {gap} behind." if gap else f"Blue flag, {who}."
    idx = p.get("car_idx")
    return TemplateCall(
        spoken_text=spoken,
        detail_text=(
            f"Car {idx} is {p.get('laps_ahead')} laps up and {p.get('gap')} s behind on track."
        ),
        subject="traffic:lapped_traffic",
        data={"threat": "lapped_traffic", "side": "behind", "car_idx": idx, "gap": p.get("gap")},
        subject_car_idx=idx if isinstance(idx, int) else None,
        expires_after_s=TRAFFIC_EXPIRES_S,
    )


def _hazard_flag(event: RaceEvent, state: RaceState) -> TemplateCall | None:
    to = event.payload.get("to")
    active = set(event.payload.get("active") or [])
    if to == FlagPhase.RED.value:
        spoken = "Red flag, red flag."
    elif to == FlagPhase.YELLOW.value:
        full_course = bool(active & {"caution", "caution_waving"})
        spoken = "Full course yellow." if full_course else "Yellow flag, careful."
    else:
        return None
    return TemplateCall(
        spoken_text=spoken,
        detail_text=(
            f"Flags went {event.payload.get('from')} to {to} ({', '.join(sorted(active))})."
        ),
        priority=RadioPriority.CRITICAL,
        subject="hazard:flag",
        data={"threat": "hazard", "flag": to, "active": sorted(active)},
        cooldown_s=HAZARD_COOLDOWN_S,
    )


def _incident(event: RaceEvent, state: RaceState) -> TemplateCall | None:
    total = event.payload.get("incidents")
    if not isinstance(total, int):
        return None
    if event.payload.get("incident_limit") is not None:
        # With a known limit the count is race control's read-out ("Incident, 14 of
        # 17."), not a second, different sentence about the same incident.
        return None
    return TemplateCall(
        spoken_text=f"Incident, that's {total}x.",
        detail_text=(
            f"Incident count up {event.payload.get('delta')} to {total} "
            f"at {event.payload.get('lap_dist_pct')} of the lap."
        ),
        subject="hazard:incident",
        data={"threat": "hazard", "incidents": total, "delta": event.payload.get("delta")},
        cooldown_s=HAZARD_COOLDOWN_S,
    )


#: Every per-event spotter call. The LLM spotter keeps only slow summaries.
SPOTTER_TEMPLATES: dict[str, Template] = {
    EventType.CAR_ALONGSIDE.value: _alongside,
    EventType.TRAFFIC_CLOSE.value: _traffic_close,
    EventType.BLUE_FLAG.value: _blue_flag,
    EventType.FLAG_CHANGE.value: _hazard_flag,
    EventType.INCIDENT.value: _incident,
}


# --------------------------------------------------------------------------
# race control's templates (stage 09): read-outs with exact numbers, no model
# --------------------------------------------------------------------------
RACE_CONTROL = "race_control"
#: Seconds a race-control read-out may wait for the channel before it airs late.
RACE_CONTROL_MAX_AGE_S = 5.0

#: How each driver flag is said when it is shown.
_PENALTY_WORDS = {
    "black": "Black flag. Serve it in the pit lane.",
    "repair": "Meatball flag, pit for repairs.",
    "furled": "Furled black flag, that's a warning.",
    "disqualify": "Black flag with the cross, we're disqualified.",
}
_PENALTY_NAMES = {
    "black": "Black flag",
    "repair": "Meatball flag",
    "furled": "Warning flag",
    "disqualify": "Disqualification flag",
}


def _rc_incident(event: RaceEvent, state: RaceState) -> TemplateCall | None:
    """"Incident, 14 of 17." -- only when the session has a numeric limit."""
    p = event.payload
    total, limit, left = p.get("incidents"), p.get("incident_limit"), p.get("incidents_remaining")
    if not isinstance(total, int) or not isinstance(limit, int):
        return None
    stage = p.get("incident_stage")
    if stage == "reached":
        spoken = f"Incident, {total} of {limit}, that's the limit."
        priority = RadioPriority.CRITICAL
    elif stage in ("approaching", "final") and isinstance(left, int):
        spoken = f"Incident, {total} of {limit}, {left} to the limit."
        priority = RadioPriority.ADVISORY
    else:
        spoken = f"Incident, {total} of {limit}."
        priority = RadioPriority.ADVISORY
    return TemplateCall(
        spoken_text=spoken,
        detail_text=(
            f"Incident count up {p.get('delta')} to {total} of a {limit} limit "
            f"({left} left), at {p.get('lap_dist_pct')} of the lap."
        ),
        priority=priority,
        subject="race_control:incidents",
        data={"consequence": "incident_limit", "incidents": total, "incident_limit": limit},
        cooldown_s=HAZARD_COOLDOWN_S,
    )


def _rc_penalty(event: RaceEvent, state: RaceState) -> TemplateCall | None:
    """The flag itself, shown or cleared. What to do about it is the agent's call."""
    p = event.payload
    flag, change = p.get("flag"), p.get("state")
    if flag not in _PENALTY_WORDS:
        return None
    if change == "shown":
        spoken = _PENALTY_WORDS[flag]
        priority = RadioPriority.ADVISORY if flag == "furled" else RadioPriority.CRITICAL
    elif change == "cleared":
        spoken = f"{_PENALTY_NAMES[flag]} cleared."
        priority = RadioPriority.INFO
    else:
        return None
    return TemplateCall(
        spoken_text=spoken,
        detail_text=(
            f"SessionFlags {flag} {change}; driver flags now: "
            f"{', '.join(p.get('active') or []) or 'none'}."
        ),
        priority=priority,
        subject=f"race_control:penalty:{flag}",
        data={"consequence": "penalty", "penalty": flag, "state": change},
        cooldown_s=HAZARD_COOLDOWN_S,
    )


def _rc_track_limits(event: RaceEvent, state: RaceState) -> TemplateCall | None:
    p = event.payload
    offs, window = p.get("offs"), p.get("window_laps")
    if not isinstance(offs, int) or not isinstance(window, int):
        return None
    return TemplateCall(
        spoken_text=f"Track limits, {offs} offs in {window} laps.",
        detail_text=(
            f"{offs} off-tracks on laps {p.get('laps')}; the pattern threshold is "
            f"{p.get('threshold')} in {window} laps. iRacing reports no warning count."
        ),
        subject="race_control:track_limits",
        data={"consequence": "track_limits", "offs": offs},
    )


def _rc_blue_ignored(event: RaceEvent, state: RaceState) -> TemplateCall | None:
    p = event.payload
    corners = p.get("corners")
    if not isinstance(corners, int):
        return None
    idx = p.get("car_idx")
    return TemplateCall(
        spoken_text=f"Blue flag, {_car(p.get('position'))} held up {corners} corners, let them by.",
        detail_text=(
            f"Car {idx}, {p.get('laps_ahead')} laps up, has been within {p.get('gap')} s "
            f"behind for {corners} corners (threshold {p.get('threshold_corners')})."
        ),
        subject="race_control:blue_flag",
        data={"consequence": "blue_flag", "car_idx": idx},
        subject_car_idx=idx if isinstance(idx, int) else None,
        expires_after_s=TRAFFIC_EXPIRES_S,
    )


#: Race control's read-outs. ``incident_limit`` has none on purpose: the incident
#: read-out already says the count against the limit, and the stage event exists to
#: wake the agent when there is a consequence (``agents/race_control.py``).
RACE_CONTROL_TEMPLATES: dict[str, Template] = {
    EventType.INCIDENT.value: _rc_incident,
    EventType.PENALTY.value: _rc_penalty,
    EventType.TRACK_LIMITS.value: _rc_track_limits,
    EventType.BLUE_FLAG_IGNORED.value: _rc_blue_ignored,
}


def race_control_caller(settings: Any = None) -> DeterministicCaller:
    return DeterministicCaller(
        RACE_CONTROL, RACE_CONTROL_TEMPLATES, max_age_s=RACE_CONTROL_MAX_AGE_S
    )


def spotter_max_age(settings: Any = None) -> float | None:
    """``RTV_PITWALL_SPOTTER_MAX_AGE_S`` if set (0 = never late), else 3 s."""
    from rtv.config import get_settings

    s = settings or get_settings()
    value = getattr(s, "pitwall_spotter_max_age_s", None)
    if value is None:
        return SPOTTER_MAX_AGE_S
    value = float(value)
    return value if value > 0 else None


def spotter_caller(settings: Any = None) -> DeterministicCaller:
    return DeterministicCaller(
        "spotter", SPOTTER_TEMPLATES, max_age_s=spotter_max_age(settings)
    )


def build_callers(
    settings: Any = None, *, only: Sequence[str] | None = None
) -> list[DeterministicCaller]:
    """The deterministic callers to run; ``only`` filters by agent name."""
    callers = [spotter_caller(settings), race_control_caller(settings)]
    if only:
        callers = [c for c in callers if c.agent in only]
    return callers


__all__ = [
    "RACE_CONTROL_TEMPLATES",
    "SPOTTER_TEMPLATES",
    "TEMPLATE_SOURCE",
    "DeterministicCaller",
    "Template",
    "TemplateCall",
    "build_callers",
    "race_control_caller",
    "spotter_caller",
    "template_message",
]
