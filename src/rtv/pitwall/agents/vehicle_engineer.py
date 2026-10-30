"""The VEHICLE / PERFORMANCE ENGINEER -- the car's advocate on the radio.

The strategist owns *when the car stops*; this one owns *what state the car is
in*. Same framework, no new code paths: a prompt, a tool subset, trigger
predicates and an output contract.

The interesting design decision is what wakes it. A single lockup is a driver
having a moment, and paying for an LLM call every time a front wheel steps out
would be both noisy and expensive. So the aggregation happens deterministically
first -- :class:`~rtv.racestate.detectors.CornerRecurrence` counts repeats of the
same issue at the same corner and only the *third* one inside five laps becomes a
``recurring_issue`` event. The engineer never sees the singles, which is exactly
how a real one works: they watch the trace, and they key the mic when it is a
pattern.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from rtv.pitwall import tools as T
from rtv.pitwall.framework import AgentOutput, AgentSpec, RadioPriority, Trigger
from rtv.racestate.models import EventType, RaceEvent, RaceState

AGENT_NAME = "vehicle_engineer"

VEHICLE_ENGINEER_PROMPT = """\
You are the VEHICLE ENGINEER on a sim-racing pit wall. You own the condition of
the car: tyres, brakes, traction, engine health, damage. You do not call pit
strategy, you do not call traffic, and you do not coach lap time - other
engineers own those. If the answer is "box now", say what the car needs and let
the strategist decide when.

You are talking to a driver at racing speed. Lead with the part of the car, then
the number, then at most one instruction. "Fronts are up twelve degrees over the
stint - ease the entry into turn eight" - not a briefing.

How to read what you are given:
- A recurring_issue event has already been filtered: it means the same problem at
  the same corner three times inside five laps. Treat it as a pattern, never as a
  single mistake, and quote its occurrence count and corner from the event.
- Corner labels are lap-distance buckets (C00-C19), not the circuit's own corner
  numbers. Say "the same corner" or use the label; never invent "turn 4".
- tyre_out_of_band tells you which measure went out and against which threshold.
  A trend is degrees or kPa *per lap* across the current stint - multiply it out
  before you say what it means by the end of the stint, and say you did.
- car_health_warning carries the sim's own warning bits when it has them. Those
  are the car speaking; a temperature over a configured threshold is us judging.
  Say which it is.
- On pit entry you are doing a stint review: what the set that just came off tells
  us about the next one.

Rules that matter more than being helpful:
- get_setup_snapshot is frequently unavailable. If it is, you may still describe
  the symptom, but you must not name a setup value or recommend a click of
  anything you were not shown.
- Never say a temperature, pressure, trend or occurrence count you were not given.
  If the tyre channels are missing for this session, say that.
- 'none' is a real finding. Use it when the trigger turned out to be benign.
"""


class EngineerCall(AgentOutput):
    """The vehicle engineer's output contract.

    Deliberately carries no numeric fields. Everything quantitative belongs in the
    prose, where the validator scores it tolerantly; a structured number here would
    have to match a tool result to the sixth decimal to be allowed through, and
    there is no number the pitwall UI needs that badly.
    """

    finding: Literal[
        "tyre_temps",
        "tyre_pressures",
        "brake_lockup",
        "traction",
        "car_health",
        "damage",
        "none",
    ] = Field(description="What the car is actually doing. 'none' if benign.")
    corner: str | None = Field(
        default=None,
        description="Corner label from the event (C00-C19), when it is corner-specific.",
    )
    affected: list[str] = Field(
        default_factory=list,
        description="Corners or systems involved, e.g. ['LF','RF'] or ['oil'].",
    )
    trend: Literal["worsening", "stable", "improving", "unknown"] = Field(
        default="unknown", description="Direction of travel, from the trend data only."
    )
    driver_action: str | None = Field(
        default=None, description="One thing the driver can do now. None if nothing helps."
    )
    setup_note: str | None = Field(
        default=None,
        description="For the next stop, only if get_setup_snapshot gave you the setup.",
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="low when anything this rests on was missing or assumed."
    )
    rationale: str = Field(description="One or two sentences, every figure cited.")


# --------------------------------------------------------------------------
# triggers
# --------------------------------------------------------------------------
def _is_car_issue(event: RaceEvent, state: RaceState) -> bool:
    """Every recurrence tells the engineer something about the car.

    Lockups and wheelspin are brake bias / diff / tyre questions as much as driver
    ones; the coach takes the same event from the driver's side, on a much longer
    cooldown, so the two do not race each other onto the channel.
    """
    return event.payload.get("issue") in ("lockup", "wheelspin", "offtrack")


def _tyres_are_readable(event: RaceEvent, state: RaceState) -> bool:
    """A stint review is only worth a call when there is tyre data to review."""
    return bool(state.capabilities.tyres and (state.tyres.temps or state.tyres.pressures))


VEHICLE_ENGINEER_TRIGGERS: tuple[Trigger, ...] = (
    Trigger(
        EventType.RECURRING_ISSUE.value,
        predicate=_is_car_issue,
        label="the same issue at the same corner, repeatedly",
        cooldown_s=45.0,
    ),
    Trigger(
        EventType.TYRE_OUT_OF_BAND.value,
        label="tyre temperature or pressure out of band",
        cooldown_s=60.0,
    ),
    Trigger(
        EventType.CAR_HEALTH_WARNING.value,
        label="engine temperature, warning light or tow",
        cooldown_s=30.0,
    ),
    # There is no separate stint_end event: pit entry *is* the end of the stint,
    # and it already carries the lap, the stint number and the fuel remaining.
    Trigger(
        EventType.PIT_ENTRY.value,
        predicate=_tyres_are_readable,
        label="stint end (post-stint tyre and brake review)",
        cooldown_s=0.0,
    ),
)


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------
def vehicle_engineer_slice(state: RaceState) -> dict:
    """The car, and only the car.

    No standings and no fuel plan: the engineer has no business quoting a gap or a
    pit window, and every section left out is a number it cannot say.
    """
    return {
        "version": state.version,
        "session": state.session.model_dump(mode="json"),
        "flags": state.flags.model_dump(mode="json"),
        "player": state.player.model_dump(mode="json"),
        "tyres": state.tyres.model_dump(mode="json"),
        "car_health": state.car_health.model_dump(mode="json"),
        "conditions": state.conditions.model_dump(mode="json"),
        "capabilities": state.capabilities.model_dump(mode="json"),
    }


def vehicle_engineer_subject(event: RaceEvent, output: AgentOutput) -> str:
    """Supersede per system, not per agent.

    Two tyre-temperature advisories collapse into the newest one, but a tyre note
    and an oil-temperature note are different problems and must both be heard.
    """
    finding = getattr(output, "finding", None) or event.event_type.value
    if finding in ("tyre_temps", "tyre_pressures"):
        return "tyres"
    if finding in ("brake_lockup", "traction"):
        return "grip"
    if finding in ("car_health", "damage"):
        return "car_health"
    return f"engineer:{finding}"


VEHICLE_ENGINEER = AgentSpec(
    name=AGENT_NAME,
    role_prompt=VEHICLE_ENGINEER_PROMPT,
    output_model=EngineerCall,
    tools=(
        T.GET_TYRE_TREND,
        T.GET_RECENT_DETECTOR_EVENTS,
        T.GET_CORNER_DETAIL,
        T.GET_CAR_HEALTH,
        T.GET_SETUP_SNAPSHOT,
        T.GET_RACE_STATE_SLICE,
    ),
    triggers=VEHICLE_ENGINEER_TRIGGERS,
    # The numbers are all computed upstream; this is pattern-reading, not
    # reasoning about a decision tree, so it sits on the fast model.
    model="claude-haiku-4-5-20251001",
    max_tokens=1200,
    cooldown_s=45.0,
    state_slice=vehicle_engineer_slice,
    subject=vehicle_engineer_subject,
    max_tool_iterations=3,
)


def priority_for(finding: str, severity: str = "advisory") -> RadioPriority:
    """The severity a given finding *should* carry. Used by the eval harness."""
    if finding in ("car_health", "damage"):
        return RadioPriority.CRITICAL if severity == "critical" else RadioPriority.ADVISORY
    if finding == "none":
        return RadioPriority.INFO
    return RadioPriority.ADVISORY
