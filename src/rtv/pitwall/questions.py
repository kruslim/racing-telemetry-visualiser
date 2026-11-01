"""Driver questions: route what the driver asked, and answer the lookups from the engine.

A question from the cockpit ("how many laps of fuel?", "gap behind?", "where am I
losing time?") becomes a ``driver_question`` :class:`~rtv.racestate.models.RaceEvent`
on the race bus, so it is journalled and replayed like any detector event. Then:

1. :func:`route_question` maps the words to an owner with a keyword table, no
   model: fuel, pit and strategy go to the strategist, tyres, temperatures and
   damage to the vehicle engineer, traffic and gaps to the spotter, driving and
   corners to the coach, incidents, penalties and track limits to race control
   (stage 09). Nothing matched goes to the strategist, the lead voice.
2. A question that is a plain lookup (laps of fuel, gap ahead or behind, last or
   best lap, position, laps to go, tyre temperatures, incidents) also gets an ``intent``.
   :data:`QUESTION_CALLER` answers it from the engine state with a template, with
   no model call and no key: B2's :class:`~rtv.pitwall.callers.DeterministicCaller`
   path. A value the engine does not have (no field data in an ``.ibt`` replay, no
   fuel figure before a lap is done) gets an honest "no data" answer, never a guess.
3. Anything else wakes the owning agent through its ``Trigger("driver_question")``
   (:func:`question_trigger`); its answer goes through the validator as usual.

Every answer carries ``in_reply_to``: the ``message_id`` of the driver's own
transcript on the radio, so a UI can show the answer under the question.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rtv.pitwall.callers import DeterministicCaller, TemplateCall
from rtv.pitwall.framework import RadioMessage, RadioPriority, Trigger
from rtv.racestate.models import EventType, RaceEvent, RaceState

#: Who answers a question nothing in the table matched.
DEFAULT_OWNER = "strategist"
#: Owners in tie-break order (the radio's order of ownership).
OWNERS = ("strategist", "vehicle_engineer", "spotter", "coach", "race_control")
#: An answer is about a moment; after this it airs tagged late.
ANSWER_MAX_AGE_S = 10.0
#: A spoken answer is dropped if the channel has not reached it within this.
ANSWER_EXPIRES_S = 30.0


@dataclass(frozen=True)
class Route:
    """Where a question goes: its owner and, for a plain lookup, which one."""

    owner: str
    #: A :data:`LOOKUPS` key when a template can answer it, else ``None``.
    intent: str | None = None
    #: False when no keyword matched and the owner is :data:`DEFAULT_OWNER`.
    matched: bool = True

    @property
    def answered_by(self) -> str:
        return "template" if self.intent else "agent"

    def to_payload(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "intent": self.intent,
            "matched": self.matched,
            "answered_by": self.answered_by,
        }


# --------------------------------------------------------------------------
# the router
# --------------------------------------------------------------------------
def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


_FUEL = r"(fuel|gas|petrol|tank|juice)"
_AHEAD = r"(ahead|in front|front of me|car in front|leader)"
_BEHIND = r"(behind|back(?! straight)|following|chasing|on my tail|mirrors?)"
_GAP = r"(gap|how far|distance|interval|how close|how much time|seconds?|margin to)"
_TYRE = r"(tyres?|tires?|rubber|fronts|rears)"
_TEMP = r"(temps?|temperatures?|hot|cold|warm)"

#: Lookups a template answers, checked in order; the first match wins. Each is
#: ``(intent, owner, must match, must not match)``.
_LOOKUP_TABLE: tuple[tuple[str, str, re.Pattern[str], re.Pattern[str] | None], ...] = (
    (
        "fuel_laps",
        "strategist",
        _rx(
            rf"(how (many|much)|laps?|enough|left|remaining|last|range).*{_FUEL}"
            rf"|{_FUEL}.*(laps?|left|remaining|enough|last|range|status|level|situation)"
            rf"|^{_FUEL}\??$"
        ),
        _rx(r"\b(save|saving|lift|coast|should|strategy|mode|map|pit|box|stop)\b"),
    ),
    (
        "tyre_temps",
        "vehicle_engineer",
        _rx(rf"{_TYRE}.*{_TEMP}|{_TEMP}.*{_TYRE}|^{_TEMP}\??$|tyre temps|tire temps"),
        _rx(r"\b(why|should|what (do|can) i do|how do i|manage|cool|overheat\w*)\b"),
    ),
    (
        "gap_ahead",
        "spotter",
        _rx(rf"{_GAP}.*{_AHEAD}|{_AHEAD}.*{_GAP}|who('?s| is) {_AHEAD}|^gap ahead|^ahead\??$"),
        _rx(r"\b(catch|pass|overtake|closing|attack|how do i|should)\b"),
    ),
    (
        "gap_behind",
        "spotter",
        _rx(rf"{_GAP}.*{_BEHIND}|{_BEHIND}.*{_GAP}|who('?s| is) {_BEHIND}|^gap behind|^behind\??$"),
        _rx(r"\b(defend|block|catching|closing|pull away|should|how do i)\b"),
    ),
    (
        "best_lap",
        "coach",
        _rx(r"(best|fastest|quickest|personal best|\bpb\b).*\blap\b|\blap\b.*(best|fastest|pb)"),
        _rx(r"\b(how|why|where|what do i|compare|improve)\b"),
    ),
    (
        "last_lap",
        "coach",
        _rx(r"(last|previous|that)\s+lap(\s+time)?|lap\s*time|what did i do|what was that lap"),
        _rx(r"\b(how|why|where|better|improve|losing|lose|lost|gain)\b"),
    ),
    (
        "laps_to_go",
        "strategist",
        _rx(
            r"laps? (to go|left|remaining)|how many laps|how long (to go|left)"
            r"|time (left|remaining)|how much (longer|time is left)|when does it end"
        ),
        _rx(r"\b(fuel|gas|tank|tyres?|tires?|pit|box)\b"),
    ),
    (
        "incidents",
        "race_control",
        _rx(r"\bincidents?\b|incident count|how many x\b|\bmy x\b"),
        _rx(r"\b(why|should|what (do|can) i do|how do i|avoid|penalty|black flag)\b"),
    ),
    (
        "position",
        "spotter",
        _rx(r"\bposition\b|what place|which place|where am i (running|placed)|\bwhat p\b|\bp\?$"),
        _rx(r"\b(gain|lose|defend|pit|box|undercut|should|corner|braking|track)\b"),
    ),
)

#: Topic words per owner for everything that needs judgement: ``(pattern, weight)``.
_TOPIC_TABLE: Mapping[str, tuple[tuple[re.Pattern[str], int], ...]] = {
    "strategist": (
        (_rx(r"\b(pit|box|boxing|stop|stopping|strategy|undercut|overcut|window)\b"), 3),
        (_rx(rf"\b{_FUEL}\b|\b(save|saving|lift and coast|coast)\b"), 2),
        (_rx(r"\b(safety car|caution|yellow|full course|finish|flag|stint|plan)\b"), 1),
    ),
    "vehicle_engineer": (
        (_rx(r"\b(damage|damaged|broken|engine|oil|water|brakes? (temp|fade|wear))\b"), 3),
        (_rx(rf"\b{_TYRE}\b|\b{_TEMP}\b|\b(pressure|pressures|psi|wear|graining|blister\w*)\b"), 2),
        (_rx(r"\b(car|grip|balance|understeer|oversteer|setup|wing|handling|vibration)\b"), 1),
    ),
    "spotter": (
        (_rx(r"\b(traffic|blue flags?|lapped|lapping|alongside|three wide|clear)\b"), 3),
        (_rx(rf"\b{_GAP}\b|\b{_AHEAD}\b|\b{_BEHIND}\b|\b(closing|catching|pulling away)\b"), 2),
        (_rx(r"\b(who|cars?|rivals?|leader|field|left|right|defend|attack|pass)\b"), 1),
    ),
    "race_control": (
        (
            _rx(
                r"\b(penalty|penalties|black flag|drive ?through|stop and go|meatball"
                r"|disqualif\w*|dq|stewards?|track limits?|incident limit|warning flag)\b"
            ),
            4,
        ),
        (_rx(r"\b(incidents?|serve|served|serving|warning|limits?)\b"), 2),
    ),
    "coach": (
        (
            _rx(
                r"\b(corner|turn|t\d{1,2}|apex|braking|brake point|line|kerbs?|curbs?"
                r"|eau rouge|raidillon|kemmel|les combes|malmedy|rivage|pouhon|fagnes"
                r"|campus|stavelot|blanchimont|bus stop|chicane|la source)\b"
            ),
            3,
        ),
        (_rx(r"\b(faster|slower|losing time|lose time|lost time|sector|pace|consistent)\b"), 2),
        (_rx(r"\b(throttle|exit|entry|driving|drive|gear|lap|mistake|lock(ing|ed)? up)\b"), 1),
    ),
}


def normalise(text: str) -> str:
    """Lower case, curly quotes straightened, punctuation but ``?`` and ``'`` dropped."""
    text = text.lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"[^a-z0-9'?\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def route_question(text: str) -> Route:
    """The owner (and lookup, if any) for a question. Pure: no state, no model."""
    words = normalise(text)
    for intent, owner, must, must_not in _LOOKUP_TABLE:
        if must.search(words) and not (must_not and must_not.search(words)):
            return Route(owner=owner, intent=intent)
    scores = {
        owner: sum(weight for pattern, weight in _TOPIC_TABLE[owner] if pattern.search(words))
        for owner in OWNERS
    }
    best = max(scores.values())
    if best <= 0:
        return Route(owner=DEFAULT_OWNER, matched=False)
    return Route(owner=next(o for o in OWNERS if scores[o] == best))


# --------------------------------------------------------------------------
# the lookups, answered from the engine state
# --------------------------------------------------------------------------
def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def lap_time(seconds: float) -> str:
    """``138.42`` -> ``"2:18.4"``; under a minute -> ``"58.3"``."""
    minutes, rest = divmod(round(seconds, 1), 60.0)
    if minutes:
        return f"{int(minutes)}:{rest:04.1f}"
    return f"{rest:.1f}"


def _player_car(state: RaceState) -> Any:
    idx = state.standings.player_idx
    for car in state.standings.cars:
        if car.is_player or (idx is not None and car.idx == idx):
            return car
    return None


def _no_data(spoken: str, why: str, **data: Any) -> TemplateCall:
    return TemplateCall(spoken, why, data={"no_data": True, **data})


def _answer_fuel_laps(state: RaceState) -> TemplateCall:
    fuel = state.fuel
    laps = _num(fuel.laps_remaining)
    if laps is None:
        why = (
            "The engine has no fuel level in this session."
            if fuel.level is None
            else "No per-lap consumption yet: it needs a completed green lap."
        )
        spoken = (
            "No fuel data in this session."
            if fuel.level is None
            else "No fuel figure yet, I need a full lap of consumption."
        )
        return _no_data(spoken, why)
    margin = _num(fuel.margin_laps)
    spoken = f"{laps:.1f} laps of fuel."
    if margin is not None:
        spoken += (
            f" {margin:.1f} laps spare to the flag."
            if margin >= 0
            else f" {-margin:.1f} laps short of the flag."
        )
    detail = (
        f"fuel.laps_remaining {fuel.laps_remaining} (level {fuel.level} L, "
        f"{fuel.per_lap} L per lap over {fuel.samples} laps)"
        + (f", fuel.margin_laps {fuel.margin_laps}" if margin is not None else "")
        + "."
    )
    return TemplateCall(spoken, detail, data={"laps_remaining": laps, "margin_laps": margin})


def _gap(state: RaceState, side: str) -> TemplateCall:
    no_field = _no_data(
        f"No gap data {side} in this session.",
        "The race state has no running order with gaps (no field data, e.g. an .ibt "
        "replay, which records no CarIdx channels).",
        side=side,
    )
    if not state.capabilities.standings:
        return no_field
    car = _player_car(state)
    if car is None or car.position is None:
        return no_field
    gap = _num(car.gap_ahead if side == "ahead" else car.gap_behind)
    if side == "ahead" and car.position == 1:
        return TemplateCall("You're leading, nobody ahead.", "Player is P1.", data={"side": side})
    others = [c for c in state.standings.cars if c.position is not None and not c.is_player]
    if side == "behind" and others and car.position >= max(c.position for c in others) + 1:
        return TemplateCall(
            "Nobody behind, you're last.",
            f"Player is P{car.position} of the field.",
            data={"side": side},
        )
    if gap is None:
        return _no_data(
            f"No gap {side} right now.",
            f"standings gap_{side} is unknown (gap basis {state.standings.gap_basis}).",
            side=side,
        )
    other_pos = car.position - 1 if side == "ahead" else car.position + 1
    spoken = (
        f"P{other_pos} is {gap:.1f} ahead."
        if side == "ahead"
        else f"P{other_pos} is {gap:.1f} behind."
    )
    return TemplateCall(
        spoken,
        f"standings player car gap_{side} {gap} s (basis {state.standings.gap_basis}).",
        data={"side": side, "gap": gap, "position": other_pos},
    )


def _answer_gap_ahead(state: RaceState) -> TemplateCall:
    return _gap(state, "ahead")


def _answer_gap_behind(state: RaceState) -> TemplateCall:
    return _gap(state, "behind")


def _answer_lap(state: RaceState, which: str) -> TemplateCall:
    value = _num(state.player.last_lap_time if which == "last" else state.player.best_lap_time)
    if value is None or value <= 0:
        return _no_data(
            f"No {which} lap time yet.", f"player.{which}_lap_time is not set.", which=which
        )
    spoken = f"{'Last' if which == 'last' else 'Best'} lap {lap_time(value)}."
    return TemplateCall(
        spoken, f"player.{which}_lap_time {value} s.", data={f"{which}_lap_time": value}
    )


def _answer_last_lap(state: RaceState) -> TemplateCall:
    return _answer_lap(state, "last")


def _answer_best_lap(state: RaceState) -> TemplateCall:
    return _answer_lap(state, "best")


def _answer_position(state: RaceState) -> TemplateCall:
    pos = state.player.position
    if not isinstance(pos, int) or pos <= 0:
        return _no_data("No position data in this session.", "player.position is not set.")
    field = (
        len([c for c in state.standings.cars if c.position]) if state.capabilities.standings else 0
    )
    spoken = f"P{pos}." if not field else f"P{pos} of {field}."
    detail = f"player.position {pos}" + (f", {field} cars in the running order." if field else ".")
    return TemplateCall(spoken, detail, data={"position": pos})


def _answer_laps_to_go(state: RaceState) -> TemplateCall:
    session = state.session
    laps = session.laps_remaining
    if isinstance(laps, int) and 0 <= laps < 10000:
        spoken = "Last lap." if laps == 1 else f"{laps} laps to go."
        return TemplateCall(
            spoken, f"session.laps_remaining {laps}.", data={"laps_remaining": laps}
        )
    to_finish = _num(state.fuel.laps_to_finish)
    if to_finish is not None:
        return TemplateCall(
            f"About {round(to_finish)} laps to go."
            if abs(to_finish - round(to_finish)) < 0.05
            else f"About {to_finish:.1f} laps to go.",
            f"fuel.laps_to_finish {state.fuel.laps_to_finish} (time left over lap pace).",
            data={"laps_to_finish": to_finish},
        )
    left = _num(session.time_remaining)
    if left is not None and 0 <= left < 86400:
        minutes = int(left // 60)
        return TemplateCall(
            f"{minutes} minutes to go." if minutes != 1 else "1 minute to go.",
            f"session.time_remaining {left} s.",
            data={"time_remaining": left},
        )
    return _no_data("No race length data in this session.", "No laps or time remaining.")


def _answer_incidents(state: RaceState) -> TemplateCall:
    count = state.player.incidents
    if not state.capabilities.incidents or not isinstance(count, int):
        return _no_data("No incident data in this session.", "player.incidents is not set.")
    rc = state.race_control
    limit, left = rc.incident_limit, rc.incidents_remaining
    if isinstance(limit, int) and isinstance(left, int):
        spoken = f"{count}x of {limit}, {left} to the limit."
        detail = f"player.incidents {count}, race_control.incident_limit {limit} ({left} left)."
    else:
        spoken = (
            f"{count}x, no incident limit this session."
            if rc.limit_source == "unlimited"
            else f"{count}x."
        )
        detail = f"player.incidents {count}; incident limit: {rc.limit_source}."
    return TemplateCall(
        spoken, detail, data={"incidents": count, "incident_limit": limit}
    )


_TYRE_NAMES = {"LF": "left front", "RF": "right front", "LR": "left rear", "RR": "right rear"}


def _answer_tyre_temps(state: RaceState) -> TemplateCall:
    temps = {c: v for c, v in state.tyres.temps.items() if _num(v) is not None}
    if not temps:
        return _no_data("No tyre temperatures in this session.", "tyres.temps is empty.")
    if all(c in temps for c in ("LF", "RF", "LR", "RR")):
        spoken = (
            f"Fronts {temps['LF']:.0f} and {temps['RF']:.0f}, "
            f"rears {temps['LR']:.0f} and {temps['RR']:.0f}."
        )
    else:
        spoken = ", ".join(f"{_TYRE_NAMES.get(c, c)} {v:.0f}" for c, v in temps.items()) + "."
        spoken = spoken[0].upper() + spoken[1:]
    detail = "tyres.temps (°C, middle of the tread): " + ", ".join(
        f"{c} {v}" for c, v in temps.items()
    )
    return TemplateCall(spoken, detail + ".", data={"temps": temps})


#: intent -> template over the engine state.
LOOKUPS = {
    "fuel_laps": _answer_fuel_laps,
    "gap_ahead": _answer_gap_ahead,
    "gap_behind": _answer_gap_behind,
    "last_lap": _answer_last_lap,
    "best_lap": _answer_best_lap,
    "position": _answer_position,
    "laps_to_go": _answer_laps_to_go,
    "tyre_temps": _answer_tyre_temps,
    "incidents": _answer_incidents,
}


def answer_lookup(intent: str, state: RaceState) -> TemplateCall | None:
    """The template answer for ``intent`` from ``state``; ``None`` for an unknown intent."""
    fn = LOOKUPS.get(intent)
    return fn(state) if fn is not None else None


def _question_template(event: RaceEvent, state: RaceState) -> TemplateCall | None:
    intent = event.payload.get("intent")
    if not intent:
        return None  # the owning agent answers this one
    call = answer_lookup(str(intent), state)
    if call is None:
        return None
    call.priority = RadioPriority.ADVISORY
    call.subject = f"answer:{event.payload.get('question_id')}"
    call.data = {**call.data, "intent": intent, "question": event.payload.get("text")}
    call.expires_after_s = ANSWER_EXPIRES_S
    return call


class QuestionCaller(DeterministicCaller):
    """Answers lookup questions from the engine state, in the owner's voice."""

    def __init__(self, *, max_age_s: float | None = ANSWER_MAX_AGE_S) -> None:
        super().__init__(
            "driver_questions",
            {EventType.DRIVER_QUESTION.value: _question_template},
            max_age_s=max_age_s,
        )

    def call(self, event: RaceEvent, state: RaceState) -> RadioMessage | None:
        message = super().call(event, state)
        if message is None:
            return None
        message.agent = str(event.payload.get("owner") or DEFAULT_OWNER)
        message.in_reply_to = event.payload.get("question_id")
        return message


def question_caller() -> QuestionCaller:
    return QuestionCaller()


# --------------------------------------------------------------------------
# the agents' side
# --------------------------------------------------------------------------
def question_trigger(agent: str) -> Trigger:
    """``Trigger("driver_question")`` for ``agent``: its own questions that need judgement."""

    def _mine(event: RaceEvent, state: RaceState) -> bool:
        p = event.payload
        return p.get("owner") == agent and not p.get("intent")

    return Trigger(
        EventType.DRIVER_QUESTION.value,
        predicate=_mine,
        label="the driver asked a question",
        # The driver asked; a cooldown would be silence.
        cooldown_s=0.0,
    )


def is_question(event: RaceEvent | None) -> bool:
    return event is not None and event.event_type is EventType.DRIVER_QUESTION


__all__ = [
    "ANSWER_MAX_AGE_S",
    "DEFAULT_OWNER",
    "LOOKUPS",
    "OWNERS",
    "QuestionCaller",
    "Route",
    "answer_lookup",
    "is_question",
    "lap_time",
    "normalise",
    "question_caller",
    "question_trigger",
    "route_question",
]
