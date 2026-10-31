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
from rtv.pitwall.framework import (
    AgentOutput,
    AgentSpec,
    RadioMessage,
    RadioPriority,
    Trigger,
)
from rtv.pitwall.validity import box_call_rules
from rtv.racestate.models import EventType, FlagPhase, RaceEvent, RaceState
from rtv.racestate.strategy_math import (
    CAUTION_FLAGS,
    standings_around_player,
    stop_plan,
)

AGENT_NAME = "strategist"

STRATEGIST_PROMPT = """\
You are the STRATEGIST on a sim-racing pit wall. You own one decision: when the
car stops, and what it takes on when it does. You do not coach driving, you do
not call traffic, you do not comment on car setup - other engineers own those.

You are talking to a driver at racing speed. Say the call first, then at most one
reason. "Box this lap, fuel only, we come out P4" - not a briefing.

The RACE STATE carries a stop_plan, worked out deterministically from the fuel
window. It is your starting point:
- stop_plan.call is "no_stop_needed": the tank reaches the flag. The answer is
  stay_out. A low fuel margin that is still positive is not a reason to stop.
- stop_plan.call is "stop_later": a stop is needed, but not yet. Answer plan_stop
  with target_lap = stop_plan.target_lap, so the driver knows the lap. An open
  window means a stop is possible, not that it is due.
- stop_plan.call is "box_next_lap" or "box_this_lap": answer box_next_lap or
  box_now, with stop_plan.fuel_to_add_l.
- stop_plan.call is "no_data": answer hold and say which number is missing.

Reasons to move away from the plan, each priced with simulate_pit_outcome:
- A full-course caution (stop_plan.caution_stop_available) makes a stop cheap.
  If a stop is needed and the caution is out, pit_under_yellow is usually right.
  A local yellow is not a caution.
- A rival stopping only matters if covering them changes our finishing position.
  Undercut or overcut on the numbers, not on reflex.
- A rising tyre temperature trend across a stint can bring a stop forward; flat
  trends are a reason to stretch it.
Never call a stop later than stop_plan.latest_lap: the tank runs dry after it.
When stop_plan.confidence is low, say the fuel figure may still move.

Recommendation values:
- box_now: pit at the end of this lap.
- box_next_lap: pit at the end of the lap after this one.
- pit_under_yellow: take the current full-course caution.
- plan_stop: a stop is needed later; target_lap says which lap.
- extend: stay out past the planned lap; say how many laps.
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
        "plan_stop",
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

    Green-to-caution opens an opportunistic stop; yellow-to-green closes it. A
    local yellow, or a change into white or checkered, is not a strategy decision.
    """
    to = event.payload.get("to")
    frm = event.payload.get("from")
    if to == FlagPhase.YELLOW.value:
        # A local yellow slows one sector, not the field: no cheap stop in it.
        return bool(CAUTION_FLAGS.intersection(event.payload.get("active") or ()))
    return to == FlagPhase.RED.value or frm in (
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


def _short_of_the_flag(event: RaceEvent, state: RaceState) -> bool:
    """A low margin that still reaches the flag is a number to watch, not a call.

    On the Spa replay the margin read 0.998 laps right after the stop and the
    strategist answered "box this lap" with 0.0 L to add.
    """
    margin = event.payload.get("margin_laps", state.fuel.margin_laps)
    return margin is not None and float(margin) < 0


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
        EventType.FUEL_MARGIN_LOW.value,
        predicate=_short_of_the_flag,
        label="fuel short of the flag",
        cooldown_s=60.0,
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
        "stop_plan": stop_plan(state).to_tool(),
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


# --------------------------------------------------------------------------
# policy: the calls the fuel picture rules out
# --------------------------------------------------------------------------
_BOX_CALLS = frozenset({"box_now", "box_next_lap", "pit_under_yellow"})


def strategist_policy(output: AgentOutput, state: RaceState, event: RaceEvent) -> str | None:
    """Why a call contradicts the deterministic stop plan, or ``None``.

    The validator checks that every number is real; this checks that the
    decision is. It rules out only what the fuel arithmetic settles: stopping
    when the tank reaches the flag, boxing laps before the planned stop without
    a caution to justify it, and stop laps outside the window.
    """
    rec = getattr(output, "recommendation", None)
    target = getattr(output, "target_lap", None)
    plan = stop_plan(state)
    lap = plan.current_lap
    if rec not in _BOX_CALLS and rec != "plan_stop":
        return None
    if not plan.grounded:
        return f"a stop was called but the stop plan has no data ({plan.reason})"
    if not plan.stop_required:
        return (
            f"the tank reaches the flag (stop_plan.call is no_stop_needed, margin "
            f"{state.fuel.margin_laps} laps), so no stop is needed"
        )
    if target is not None:
        if plan.latest_lap is not None and target > plan.latest_lap:
            return f"target_lap {target} is after the run-dry lap {plan.latest_lap}"
        if plan.earliest_lap is not None and target < plan.earliest_lap:
            return f"target_lap {target} is before the window opens at {plan.earliest_lap}"
    if rec == "pit_under_yellow" and not plan.caution_stop_available:
        return "pit_under_yellow needs a full-course caution inside the window"
    # Covering a rival's stop is a real reason to box early, when there are live
    # gaps to price it against.
    covering = (
        event.event_type is EventType.RIVAL_PITTED and state.capabilities.standings
    )
    if rec in ("box_now", "box_next_lap") and not (plan.caution_stop_available or covering):
        stop_lap = lap if rec == "box_now" else (lap or 0) + 1
        if plan.target_lap is not None and stop_lap < plan.target_lap - 1:
            return (
                f"{rec} stops on lap {stop_lap}, but stop_plan.target_lap is "
                f"{plan.target_lap}: answer plan_stop with that lap, or price a "
                "caution or a rival's stop to stop early"
            )
    return None


def strategist_validity(
    message: RadioMessage, state: RaceState, event: RaceEvent, pit_entry_pct: float | None
) -> dict:
    """When a pit call stops being true: its stop lap is over, or the stop happened.

    Read from the structured answer (``recommendation``, ``target_lap``), never
    from the words. "Box this lap" stops at this lap's pit entry; a plan for lap
    12 stands until lap 12 is done. stay_out, extend and hold have no lap to miss.
    """
    data = message.data or {}
    rec = data.get("recommendation")
    lap = state.player.lap
    if rec in ("box_now", "pit_under_yellow"):
        stop_lap = lap
    elif rec == "box_next_lap":
        stop_lap = lap + 1 if lap is not None else None
    elif rec == "plan_stop":
        target = data.get("target_lap")
        stop_lap = int(target) if isinstance(target, int | float) else None
    else:
        return {}
    return box_call_rules(stop_lap, pit_entry_pct)


def _simulate_planned_stop(state: RaceState, event: RaceEvent) -> dict:
    """Price the planned stop lap (or this lap, when there is no plan)."""
    target = stop_plan(state).target_lap
    return {"stop_lap": target} if target is not None else {}


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
    tier="reasoning",
    max_tokens=2000,
    cooldown_s=30.0,
    state_slice=strategist_slice,
    subject=strategist_subject,
    policy=strategist_policy,
    # Every run on the Spa replay opened with these two calls.
    prefetch=(
        ("get_fuel_projection", {}),
        ("simulate_pit_outcome", _simulate_planned_stop),
    ),
    max_tool_iterations=4,
    # A pit call is about a lap, not a moment: it airs late after this, and is
    # dropped only when its stop lap has gone (strategist_validity).
    max_age_s=30.0,
    validity=strategist_validity,
)


def priority_for(recommendation: str) -> RadioPriority:
    """The severity a given call *should* carry. Used by the eval harness."""
    if recommendation in ("box_now", "pit_under_yellow"):
        return RadioPriority.CRITICAL
    if recommendation in ("box_next_lap", "extend", "plan_stop"):
        return RadioPriority.ADVISORY
    return RadioPriority.INFO
