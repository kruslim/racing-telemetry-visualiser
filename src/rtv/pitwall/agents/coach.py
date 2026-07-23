"""The COACH -- the live, one-cue-at-a-time half of the v1 coaching stack.

v1 already has a coach: Layer 1 extracts ~20 corner findings from a stored lap,
Layer 3b writes them up with an adversarial verify pass. That coach is *post-hoc*
and thorough, and it should stay that way. This one is its live sibling, and it is
built on the opposite constraint: a driver mid-stint can act on exactly one thing,
and only between corners.

So the two share a source of truth rather than a shape. ``get_corner_detail``
calls the same :class:`~rtv.coaching.features.CoachingService` the v1 endpoints
call, and the live event stream supplies what the store cannot yet. What differs
is the budget: one cue, under twenty-five words, and only when a *pattern* has
already been established deterministically.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from rtv.pitwall import tools as T
from rtv.pitwall.framework import AgentOutput, AgentSpec, RadioPriority, Trigger
from rtv.racestate.models import EventType, FlagPhase, RaceEvent, RaceState

AGENT_NAME = "coach"

COACH_PROMPT = """\
You are the DRIVER COACH on a sim-racing pit wall. You own how the car is being
driven: braking, throttle application, line, consistency, tyre management. You do
not call strategy, you do not call traffic, and you do not diagnose the car - the
strategist, the spotter and the vehicle engineer own those.

The driver is mid-stint at racing speed and can hold exactly one instruction.
Give one cue, in the imperative, for one corner. "Brake ten metres later into the
same corner, you're scrubbing the fronts" - never a list, never two corners.

How to think:
- A recurring_issue event has already been filtered: the same problem at the same
  corner three times inside five laps. That is a habit, not a mistake, and it is
  the only thing worth interrupting a driver for.
- Corner labels are lap-distance buckets (C00-C19), not the circuit's corner
  numbers. Use the label or say "the same corner". Never invent "turn 7".
- get_corner_detail's 'telemetry' half is the deterministic corner analysis:
  minimum speed against a reference lap, brake point, throttle application, time
  lost. Quote those figures; they are measured. When it reports available=false,
  you still have the live detector events, but say your cue is from the pattern
  rather than from the trace.
- get_stint_history gives lap times for the stint. Slower laps late in a stint on
  rising tyre temperatures is a tyre-management cue, not a technique one.

Rules:
- Never invent a distance, a speed or a time delta. If you have no trace, coach
  the pattern in words and mark confidence low.
- One cue. If nothing is worth saying, theme 'none' and say the driver is on it.
  A quiet radio is a legitimate outcome; filling airtime is not.
"""


class CoachCall(AgentOutput):
    """The coach's output contract. One theme, one corner, one cue."""

    theme: Literal[
        "braking",
        "throttle",
        "line",
        "consistency",
        "tyre_management",
        "none",
    ] = Field(description="The single thing this call is about.")
    corner: str | None = Field(
        default=None, description="Corner label (C00-C19) or the Layer-1 label (T4)."
    )
    cue: str = Field(
        description="The instruction, imperative and specific. Empty when theme is 'none'."
    )
    from_telemetry: bool = Field(
        default=False,
        description="True only when get_corner_detail returned a corner trace you used.",
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="low when the cue rests on the event pattern alone."
    )
    rationale: str = Field(description="One or two sentences, every figure cited.")


# --------------------------------------------------------------------------
# triggers
# --------------------------------------------------------------------------
def _is_driving_pattern(event: RaceEvent, state: RaceState) -> bool:
    """Only recurrences a driver can do something about."""
    return event.payload.get("issue") in ("lockup", "wheelspin", "offtrack")


def _under_green(event: RaceEvent, state: RaceState) -> bool:
    """Never coach under a caution: the driver has other things to think about."""
    return state.flags.phase in (FlagPhase.GREEN, FlagPhase.UNKNOWN)


def _has_pace_history(event: RaceEvent, state: RaceState) -> bool:
    """A milestone is only a coaching moment when there are laps to compare."""
    return _under_green(event, state) and state.player.laps_on_tyres >= 2


COACH_TRIGGERS: tuple[Trigger, ...] = (
    # The same event the vehicle engineer takes, read from the driver's side --
    # and on a cooldown nearly three times as long, so the car's advocate speaks
    # first and the coach does not turn one pattern into a conversation.
    Trigger(
        EventType.RECURRING_ISSUE.value,
        predicate=_is_driving_pattern,
        label="a repeated mistake at the same corner",
        cooldown_s=120.0,
    ),
    Trigger(
        EventType.OFFTRACK.value,
        predicate=_under_green,
        label="off track",
        cooldown_s=90.0,
    ),
    Trigger(
        EventType.STINT_LAP_MILESTONE.value,
        predicate=_has_pace_history,
        label="stint pace review",
        cooldown_s=180.0,
    ),
)


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------
def coach_slice(state: RaceState) -> dict:
    """Pace, the current stint and the tyres under it. No fuel, no standings.

    Tyres are in because tyre management is a coaching theme; the fuel plan and
    the running order are out, because "you're a lap short" is not the coach's
    sentence to say and would only widen what it may cite.
    """
    return {
        "version": state.version,
        "session": state.session.model_dump(mode="json"),
        "flags": state.flags.model_dump(mode="json"),
        "player": state.player.model_dump(mode="json"),
        "tyres": state.tyres.model_dump(mode="json"),
        "capabilities": state.capabilities.model_dump(mode="json"),
    }


def coach_subject(event: RaceEvent, output: AgentOutput) -> str:
    """One coaching cue stands at a time; a newer one replaces it."""
    return "coaching"


COACH = AgentSpec(
    name=AGENT_NAME,
    role_prompt=COACH_PROMPT,
    output_model=CoachCall,
    tools=(
        T.GET_CORNER_DETAIL,
        T.GET_RECENT_DETECTOR_EVENTS,
        T.GET_STINT_HISTORY,
        T.GET_TYRE_TREND,
    ),
    triggers=COACH_TRIGGERS,
    model="claude-haiku-4-5-20251001",
    max_tokens=1200,
    cooldown_s=120.0,
    state_slice=coach_slice,
    subject=coach_subject,
    max_tool_iterations=3,
)


def priority_for(theme: str) -> RadioPriority:
    """The severity a given cue *should* carry. Used by the eval harness.

    Nothing a coach says is critical. A driving cue that pre-empts a fuel call
    would be the wrong trade every single time.
    """
    return RadioPriority.INFO if theme == "none" else RadioPriority.ADVISORY
