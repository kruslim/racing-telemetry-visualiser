"""The STRATEGIST — the reference agent, and the shape every later one copies.

Everything below is data: a prompt, a tool subset, a list of trigger predicates
and an output contract. There is no strategist-specific code path in the runtime,
which is the test of whether the framework actually generalises.

What it is for: the deterministic engine already knows *when* the pit window is
open and *what* a stop would cost. It cannot decide whether to take it. That
judgement -- weigh a cheap yellow-flag stop against track position, decide whether
to cover a rival, decide whether to stretch a stint -- is the part worth an LLM
call, and it happens a handful of times a race rather than sixty times a second.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from rtv.pitwall import tools as T
from rtv.pitwall.framework import AgentOutput, AgentSpec, RadioPriority, Trigger
from rtv.racestate.models import EventType, FlagPhase, RaceEvent, RaceState
from rtv.racestate.strategy_math import standings_around_player

AGENT_NAME = "strategist"

STRATEGIST_PROMPT = """\
You are the STRATEGIST on a sim-racing pit wall. You own one decision: when the
car stops, and what it takes on when it does. You do not coach driving, you do
not call traffic, you do not comment on car setup - other engineers own those.

You are talking to a driver at racing speed. Say the call first, then at most one
reason. "Box this lap, fuel only, we come out P4" - not a briefing.

How to think about a stop:
- Fuel sets the boundaries. get_fuel_projection gives the earliest lap a stop can
  reach the flag from and the latest lap before the tank runs dry. Outside those
  bounds there is no decision to make.
- Track position decides inside them. simulate_pit_outcome converts a stop lap
  into a rejoin position from the live gaps. Quote its rejoin_position; never
  estimate one yourself.
- A yellow is cheap. Under a full-course caution the field is slowed, so the same
  stop costs far less track position. If a stop is needed at all and a caution is
  out, that is usually the moment - price it with simulate_pit_outcome and say so.
- A rival stopping only matters if covering them changes our finishing position.
  Undercut or overcut on the numbers, not on reflex.
- Tyres extend or shorten a stint. A rising temperature trend across a stint is a
  reason to bring a stop forward; flat trends are a reason to stretch it.

Recommendation values:
- box_now: pit at the end of this lap.
- box_next_lap: pit at the end of the lap after this one.
- pit_under_yellow: take the current caution, opportunistically.
- extend: stay out longer than the obvious lap; say how many laps.
- stay_out: no stop is called for.
- hold: you do not have the data to decide. Use it. It is a real answer.

If a tool reports grounded=false or unavailable, choose 'hold' and say which
number you are missing. Never invent a rejoin position, a lap number or a fuel
figure - the pit wall would rather hear nothing than hear something wrong."""


class PitCall(AgentOutput):
    """The strategist's output contract."""

    recommendation: Literal[
        "box_now",
        "box_next_lap",
        "pit_under_yellow",
        "extend",
        "stay_out",
        "hold",
    ] = Field(description="The call. Use 'hold' when the data does not support one.")
    target_lap: int | None = Field(
        default=None, description="Lap to stop on, when a stop is being called."
    )
    fuel_to_add_l: float | None = Field(
        default=None, description="Litres to add, straight from simulate_pit_outcome."
    )
    rejoin_position: int | None = Field(
        default=None, description="Projected position after the stop, from the tool."
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="low when anything the call rests on is missing or assumed."
    )
    rationale: str = Field(description="One or two sentences, every figure cited.")
    risks: list[str] = Field(
        default_factory=list, description="What would make this call wrong."
    )


# --------------------------------------------------------------------------
# triggers
# --------------------------------------------------------------------------
def _yellow_or_red(event: RaceEvent, state: RaceState) -> bool:
    """Only flag changes worth a strategy call.

    Green-to-yellow opens an opportunistic stop; yellow-to-green closes it. A
    change into white or checkered is not a strategy decision.
    """
    to = event.payload.get("to")
    frm = event.payload.get("from")
    return to in (FlagPhase.YELLOW.value, FlagPhase.RED.value) or frm in (
        FlagPhase.YELLOW.value,
        FlagPhase.RED.value,
    )


def _rival_is_close(event: RaceEvent, state: RaceState) -> bool:
    """A rival's stop only matters when we could actually be traded with them."""
    gap = event.payload.get("gap_to_player")
    if gap is None:
        return True  # position-filtered by the detector already; let it through
    return abs(float(gap)) <= 30.0


def _stop_is_needed(event: RaceEvent, state: RaceState) -> bool:
    """Don't wake the strategist on a milestone when no stop is in prospect."""
    margin = state.fuel.margin_laps
    return margin is None or margin < 3.0


STRATEGIST_TRIGGERS: tuple[Trigger, ...] = (
    Trigger(EventType.PIT_WINDOW_OPEN.value, label="pit window opens", cooldown_s=0.0),
    Trigger(
        EventType.PIT_WINDOW_CLOSING.value, label="pit window closing", cooldown_s=0.0
    ),
    Trigger(EventType.FUEL_CRITICAL.value, label="fuel critical", cooldown_s=0.0),
    Trigger(
        EventType.FLAG_CHANGE.value,
        predicate=_yellow_or_red,
        label="yellow/red flag transition",
        cooldown_s=20.0,
    ),
    Trigger(
        EventType.FUEL_MARGIN_LOW.value, label="fuel margin below threshold", cooldown_s=60.0
    ),
    Trigger(
        EventType.RIVAL_PITTED.value,
        predicate=_rival_is_close,
        label="a nearby rival pitted",
        cooldown_s=45.0,
    ),
    Trigger(
        EventType.STINT_LAP_MILESTONE.value,
        predicate=_stop_is_needed,
        label="stint lap milestone",
        cooldown_s=60.0,
    ),
)


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------
def strategist_slice(state: RaceState) -> dict:
    """The strategist's view of the world -- and the numbers it may cite.

    Car health, conditions and the raw 64-car array are left out: not the
    strategist's decision, and every field included widens what the citation
    validator will accept.
    """
    return {
        "version": state.version,
        "session": state.session.model_dump(mode="json"),
        "flags": state.flags.model_dump(mode="json"),
        "player": state.player.model_dump(mode="json"),
        "fuel": state.fuel.model_dump(mode="json"),
        "tyres": state.tyres.model_dump(mode="json"),
        "standings": standings_around_player(state, window=3),
        "capabilities": state.capabilities.model_dump(mode="json"),
    }


def strategist_subject(event: RaceEvent, output: AgentOutput) -> str:
    """Supersede key: every pit call is about the same thing -- the next stop.

    So two pit-window updates collapse to the newest, but a rival-driven note and
    a fuel-driven note about that same stop also collapse, which is right: the
    driver needs one current answer to "when am I stopping", not a log of them.
    """
    return "pit_stop"


STRATEGIST = AgentSpec(
    name=AGENT_NAME,
    role_prompt=STRATEGIST_PROMPT,
    output_model=PitCall,
    tools=(
        T.GET_RACE_STATE_SLICE,
        T.GET_FUEL_PROJECTION,
        T.GET_TYRE_TREND,
        T.GET_STANDINGS_AROUND_PLAYER,
        T.GET_STINT_HISTORY,
        T.SIMULATE_PIT_OUTCOME,
    ),
    triggers=STRATEGIST_TRIGGERS,
    # Strategy is the one place in the pitwall worth a reasoning model: it is
    # low-frequency, and getting it wrong costs the race.
    model="claude-sonnet-4-6",
    max_tokens=2000,
    cooldown_s=30.0,
    state_slice=strategist_slice,
    subject=strategist_subject,
    max_tool_iterations=4,
)


def priority_for(recommendation: str) -> RadioPriority:
    """The severity a given call *should* carry. Used by the eval harness."""
    if recommendation in ("box_now", "pit_under_yellow"):
        return RadioPriority.CRITICAL
    if recommendation in ("box_next_lap", "extend"):
        return RadioPriority.ADVISORY
    return RadioPriority.INFO
