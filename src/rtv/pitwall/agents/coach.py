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
from rtv.pitwall.questions import question_trigger
from rtv.racestate.models import EventType, FlagPhase, RaceEvent, RaceState
from rtv.racestate.plan import plan_view
from rtv.vision.live import COACH_KINDS

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
- Every event that happened somewhere carries a 'place': {label, name,
  lap_dist_pct, provisional}. Call the corner by its place: label and name when
  there is a name ("T1 La Source"), the label alone when name is null ("T4").
  These are the post-race review's own corner labels; the state's 'track' section
  lists them all. When provisional is true the label is a lap-distance bucket
  (C00-C19) because no clean lap has been mapped yet: say "the same corner" and
  never turn a bucket into a turn number. Never invent a corner name.
- A recurring_issue's 'corner' field is the bucket it was counted in; its place
  is what you say.
- get_corner_detail's 'telemetry' half is the deterministic corner analysis:
  minimum speed against a reference lap, brake point, throttle application, time
  lost. Quote those figures; they are measured. When it reports available=false,
  you still have the live detector events, but say your cue is from the pattern
  rather than from the trace.
- The state's 'plan' carries the strategist's tyre posture and pace target. A
  cue should fit them: under "conserve", a cue that saves the tyres ("off the
  kerbs in T4") beats one that finds a tenth. You cannot change the plan.
- A visual_observation trigger is the onboard camera's description of a moment the
  telemetry flagged (kerb use, the line, a slide): words only, no figures. Use it
  for the *how* of a cue, mark confidence low, and never take a number from it.
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
        default=None,
        description="The event's place: label and name ('T1 La Source'), or the label alone.",
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


def _visual_for_coach(event: RaceEvent, state: RaceState) -> bool:
    """Line, kerb and car-behaviour notes from the camera, and never under a caution."""
    return event.payload.get("kind") in COACH_KINDS and _under_green(event, state)


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
    Trigger(
        EventType.VISUAL_OBSERVATION.value,
        predicate=_visual_for_coach,
        label="the onboard camera saw the line or kerb use",
        cooldown_s=120.0,
    ),
    # A question the driver asked that needs judgement (rtv.pitwall.questions).
    question_trigger(AGENT_NAME),
)


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------
def coach_slice(state: RaceState) -> dict:
    """Pace, the current stint, the tyres under it and the corner map. No fuel, no standings.

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
        "track": state.track.model_dump(mode="json"),
        # Read-only: the tyre posture and pace target the stint is driven to.
        "plan": plan_view(state, "posture"),
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
        T.GET_RACE_PLAN,
        T.GET_LAP_TIME_DEGRADATION,
    ),
    triggers=COACH_TRIGGERS,
    tier="fast",
    max_tokens=1200,
    cooldown_s=120.0,
    state_slice=coach_slice,
    subject=coach_subject,
    # Every run on the Spa replay called these two first; with no corner named,
    # get_corner_detail covers the trigger's own lap distance.
    prefetch=(
        ("get_recent_detector_events", {"limit": 10}),
        ("get_corner_detail", {}),
    ),
    max_tool_iterations=3,
    max_age_s=30.0,
    # Held to the Layer-1 prose check whenever get_corner_detail returned a corner.
    prose_check=True,
)


def priority_for(theme: str) -> RadioPriority:
    """The severity a given cue *should* carry. Used by the eval harness.

    Nothing a coach says is critical. A driving cue that pre-empts a fuel call
    would be the wrong trade every single time.
    """
    return RadioPriority.INFO if theme == "none" else RadioPriority.ADVISORY
