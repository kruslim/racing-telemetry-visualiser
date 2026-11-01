"""Decision groups: the subjects of different agents that are one decision.

Supersede in :class:`~rtv.pitwall.radio.RadioFeed` used to be per agent and
subject, so the vehicle engineer could say "box for damage" while the strategist
said "stay out", and the driver heard both. Some calls from different agents are
answers to the same question; this module says which, in a fixed table, so the
radio can keep one standing answer per decision.

* :data:`DECISION_GROUPS` maps a group to the ``(agent, subject)`` pairs in it.
  Today that is one decision, ``pit_stop``: when the car stops. The strategist's
  every call is about it (subject ``pit_stop``), and the engineer joins it only
  when it asks for a stop now (``damage_stop`` or ``tyre_stop``, set from its
  structured ``pit_request``, never from its words).
* :func:`action_of` reads what a call *does* about the decision from its
  structured answer: ``box_now``, ``stop_later`` or ``stay_out`` (``None`` for
  ``hold``, refusals and anything that does not take a side).
* :func:`opposed` says whether two actions contradict each other.

Considered and left out: the coach's ``coaching`` and the engineer's ``grip``
both answer a recurring lockup, but from different sides (the driver's technique,
the car's balance). They complement each other rather than decide the same thing,
and dropping one for the other would lose information, so they stay separate.

Deterministic and model-free, like the rest of the radio's rules. The radio
applies them (see :meth:`RadioFeed.publish`).
"""

from __future__ import annotations

from typing import Any

#: group -> the (agent, subject) pairs that answer it.
DECISION_GROUPS: dict[str, frozenset[tuple[str, str]]] = {
    "pit_stop": frozenset(
        {
            ("strategist", "pit_stop"),
            ("vehicle_engineer", "damage_stop"),
            ("vehicle_engineer", "tyre_stop"),
        }
    ),
}

#: Two opposite calls in one group at most this many session seconds apart are a
#: contradiction: the lower-priority one is dropped. Further apart, the newer one
#: is a revision and airs.
CONFLICT_WINDOW_S = 60.0

#: The strategist's recommendation -> what it does about the stop.
_STRATEGIST_ACTIONS: dict[str, str] = {
    "box_now": "box_now",
    "box_next_lap": "box_now",
    "pit_under_yellow": "box_now",
    "plan_stop": "stop_later",
    "extend": "stay_out",
    "stay_out": "stay_out",
}

#: Pairs of actions that cannot both stand.
_OPPOSED: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"box_now", "stay_out"}),
        frozenset({"box_now", "stop_later"}),
    }
)

_BY_PAIR: dict[tuple[str, str], str] = {
    pair: group for group, pairs in DECISION_GROUPS.items() for pair in pairs
}


def group_of(message: Any) -> str | None:
    """The decision group of a radio message, or ``None`` for a stand-alone call."""
    return _BY_PAIR.get((getattr(message, "agent", ""), getattr(message, "subject", "")))


def action_of(message: Any) -> str | None:
    """What a call in a group does about its decision, from its structured answer.

    ``None`` for a call outside any group, a refusal, and a call that takes no side
    (the strategist's ``hold``).
    """
    if group_of(message) is None or getattr(message, "refused", False):
        return None
    if message.agent == "strategist":
        return _STRATEGIST_ACTIONS.get(str((message.data or {}).get("recommendation")))
    if message.agent == "vehicle_engineer":
        # The engineer is in the group only when it asked for a stop now.
        return "box_now"
    return None


def opposed(a: str | None, b: str | None) -> bool:
    """True when two actions on one decision contradict each other."""
    return a is not None and b is not None and frozenset({a, b}) in _OPPOSED
