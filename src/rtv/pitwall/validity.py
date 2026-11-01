"""Air-time validity: is a radio call still worth saying when the channel reaches it?

A Kimi call takes 12-17 s and a run up to a minute, so what an agent decided from
its snapshot can be stale by the time the call airs. Two different things can be
wrong with it, and they get two different answers (user decision, PLAN.md Q3):

* **Late but still true.** "Tyres are 4 degrees over on the left" twenty seconds
  on. It is aired, tagged: the spoken text starts with "Late call:" and the
  journal records ``radio_late``.
* **No longer true.** "Box this lap" after the car has passed the pit entry, a
  traffic call about a car that has gone, a lap-12 target after lap 12. A late tag
  does not make it true, so it is dropped and the journal records
  ``radio_expired`` with the reason.

Everything here is deterministic. :func:`stamp` writes the rule onto the message
from the agent's own snapshot when the run finishes (the snapshot was taken when
it started); :func:`check` evaluates it against the race state at air time. No
model is asked whether its own call is still valid.
"""

from __future__ import annotations

from typing import Any

from rtv.logging import get_logger
from rtv.pitwall.framework import AgentSpec, RadioMessage
from rtv.racestate.models import RaceEvent, RaceState

log = get_logger("pitwall.validity")

FRESH = "fresh"
LATE = "late"
EXPIRED = "expired"

#: The rule fields an agent's ``validity`` hook may set.
RULE_FIELDS = (
    "valid_until_lap",
    "expires_at_session_time",
    "invalid_if_lap_dist_past",
    "invalid_if_on_pit_road",
    "subject_car_idx",
)


def stamp(
    message: RadioMessage,
    spec: AgentSpec,
    state: RaceState,
    event: RaceEvent,
    *,
    max_age_s: float | None,
    pit_entry_pct: float | None = None,
) -> RadioMessage:
    """Write the air-time rule onto ``message`` from the run's own snapshot."""
    message.state_version = state.version
    message.computed_at_session_time = state.session_time
    message.max_age_s = max_age_s
    if spec.validity is None:
        return message
    try:
        rules = spec.validity(message, state, event, pit_entry_pct) or {}
    except Exception:  # a bad rule costs the rule, never the call
        log.exception("Validity rule failed for %s", spec.name)
        return message
    for name, value in rules.items():
        if name in RULE_FIELDS and value is not None:
            setattr(message, name, value)
    return message


def check(
    message: RadioMessage, state: RaceState | None, now: float
) -> tuple[str, str | None, float | None]:
    """``(verdict, reason, age_s)`` for airing ``message`` at session time ``now``.

    ``verdict`` is :data:`FRESH`, :data:`LATE` or :data:`EXPIRED`. A message with
    no ``computed_at_session_time`` (a driver transcript, a pitwall notice) has no
    age and no rule: always fresh. ``state`` may be ``None`` (no engine wired, or
    a test driving the clock): then only the time rules apply.
    """
    computed = message.computed_at_session_time
    if computed is None:
        return FRESH, None, None
    age = max(0.0, now - computed)
    reason = _invalid_reason(message, state, now)
    if reason is not None:
        return EXPIRED, reason, age
    if message.max_age_s is not None and age > message.max_age_s:
        return LATE, f"{age:.0f} s old, limit {message.max_age_s:g} s", age
    return FRESH, None, age


def _invalid_reason(message: RadioMessage, state: RaceState | None, now: float) -> str | None:
    expires = message.expires_at_session_time
    if expires is not None and now > expires:
        return f"expired at session time {expires:.1f}s"
    if state is None or state.stale:
        # The last known numbers are not the car's position now; judge on time only.
        return None
    player = state.player
    lap = player.lap
    until = message.valid_until_lap
    if until is not None and lap is not None and lap > until:
        return f"lap {until} has passed (now lap {lap})"
    if message.invalid_if_on_pit_road and player.on_pit_road:
        return "the car is already in the pit lane"
    past = message.invalid_if_lap_dist_past
    if (
        past is not None
        and until is not None
        and lap == until
        and player.lap_dist_pct is not None
        and player.lap_dist_pct > past
        and not player.on_pit_road
    ):
        return f"past the pit entry on lap {until} without stopping"
    idx = message.subject_car_idx
    if idx is not None and state.capabilities.standings and state.standings.cars:
        car = next((c for c in state.standings.cars if c.idx == idx), None)
        if car is None:
            return f"car {idx} is no longer in the running order"
        if car.on_pit_road:
            return f"car {idx} is in the pit lane"
    return None


def box_call_rules(stop_lap: int | None, pit_entry_pct: float | None) -> dict[str, Any]:
    """The rule for "stop at the end of ``stop_lap``".

    Invalid once that lap is over, once the car is in the pit lane (the stop is
    happening), and -- when this session has shown where the pit entry is -- once
    the car is past it on the stop lap. A pit entry in the first half of the lap
    sits after the line, so the stop lap's entry is on the lap after it.
    """
    if stop_lap is None:
        return {}
    rules: dict[str, Any] = {"invalid_if_on_pit_road": True}
    if pit_entry_pct is not None and pit_entry_pct < 0.5:
        rules["valid_until_lap"] = stop_lap + 1
    else:
        rules["valid_until_lap"] = stop_lap
    if pit_entry_pct is not None:
        rules["invalid_if_lap_dist_past"] = round(float(pit_entry_pct), 4)
    return rules
