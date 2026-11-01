"""RACE CONTROL -- compliance and consequence, the quietest voice on the channel.

Stage 09 part A. Nobody on the pit wall said "one more incident ends the race" or
"serve the drive-through before the stop" before this agent, because no agent was
shown the incident limit or the player's driver flags. Race control is.

It works like the spotter after B2: two paths.

* **Read-outs are templates.** "Incident, 14 of 17.", "Black flag. Serve it in the
  pit lane.", "Track limits, 3 offs in 3 laps." have one right wording and every
  figure is in the event, so :data:`rtv.pitwall.callers.RACE_CONTROL_TEMPLATES`
  says them with no model call, under this agent's name.
* **This LLM agent is woken only by a consequence** to reason about, which the
  engine has already aggregated (:mod:`rtv.racestate.race_control`): the incident
  count is one heavy contact from the limit, a penalty has to be served in the pit
  lane, repeated offs while close to the limit, or a car we have held up behind a
  blue flag for several corners. A clean race wakes it zero times.

Three rules keep it honest:

* **Counts are exact.** Its structured ``incidents`` and ``incident_limit`` must be
  the counter's and the session's figures, checked by a deterministic policy
  (:func:`race_control_policy`); a race-control call that misreports the incident
  count is withheld, because that is worse than silence.
* **No penalty without a penalty channel.** It may only speak of a penalty, or ask
  for a stop to serve one, when SessionFlags shows a driver flag right now.
* **A stop to serve a penalty is a pit-stop decision.** ``pit_request: box_now``
  gives the subject ``penalty_stop``, in the B3 ``pit_stop`` group
  (:mod:`rtv.pitwall.decisions`), so it and the strategist's stop call cannot
  contradict each other on the radio.

Its state slice is flags, incidents and limit, penalty state, the player and the
cars around us. Not fuel, not tyres, not the race plan: the strategist owns those.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from rtv.director.engine import INJECTED_KEY
from rtv.pitwall import tools as T
from rtv.pitwall.framework import AgentOutput, AgentSpec, RadioMessage, RadioPriority, Trigger
from rtv.pitwall.questions import question_trigger
from rtv.pitwall.validity import box_call_rules
from rtv.racestate.models import EventType, RaceEvent, RaceState
from rtv.racestate.race_control import SERVE_IN_PIT
from rtv.racestate.strategy_math import standings_around_player

AGENT_NAME = "race_control"

RACE_CONTROL_PROMPT = """\
You are RACE CONTROL on a sim-racing pit wall. You own compliance and its
consequences: the incident count against the session's limit, penalty flags
shown to our car, track limits and blue flags. Not fuel, not tyres, not pace,
not traffic calls - other engineers own those.

The read-outs are already made for you by deterministic templates the moment
they happen ("Incident, 14 of 17.", "Black flag. Serve it in the pit lane.").
You never repeat them. You are only woken when there is a consequence to reason
about, and your job is to say what it means and the one thing to do about it:
- incident_limit, stage final: one heavy contact (a 4x) would take us to the
  limit. Say how many incidents are left and what that means for the driving.
- penalty shown (black flag, meatball): it has to be served in the pit lane.
  If the driver should come in now, set pit_request to box_now; the strategist
  sees that, and your call and theirs share one decision on the radio.
- track_limits: repeated offs while close to the incident limit. Each off is an
  incident in iRacing; say how many are left before the limit.
- blue_flag_ignored: we have held up a faster car for several corners. Tell the
  driver to let it through.
- driver_question: the driver asked about incidents, a penalty or track limits.
  Answer from the data you were given, or say you do not have it.

Numbers are copied, never computed:
- incidents and incident_limit in your answer must be exactly the figures in the
  state's race_control section (or the event). If you are not sure, leave them
  null. A misreported incident count is withheld and never aired.
- iRacing reports no track-limit warning count and no penalty timer. Never say
  "one more and it's a drive-through" or "you have N laps to serve it": you were
  not given that. Say what is known.
- Speak of a penalty only if race_control.penalty lists one right now. With no
  penalty showing, penalty is "none" and pit_request is "none".

You are the quietest voice on the channel. Twelve words is a full call. 'none'
is a real answer when the consequence turned out not to be one.
"""


class RaceControlCall(AgentOutput):
    """Race control's output contract. Counts here must be copied exactly."""

    consequence: Literal["incident_limit", "penalty", "track_limits", "blue_flag", "none"] = (
        Field(description="What the consequence is about. 'none' if there is none.")
    )
    incidents: int | None = Field(
        default=None,
        description="The player's incident count, copied from race_control.incidents.",
    )
    incident_limit: int | None = Field(
        default=None,
        description="The session's limit, copied from race_control.incident_limit; null if none.",
    )
    penalty: Literal["none", "black", "repair", "furled", "disqualify"] = Field(
        default="none", description="A driver flag listed in race_control.penalty, else none."
    )
    pit_request: Literal["none", "box_now"] = Field(
        default="none",
        description="box_now only to serve a black flag or a meatball in the pit lane now.",
    )
    driver_action: str | None = Field(
        default=None, description="The one thing the driver should do. None if nothing."
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="low when anything this rests on was missing."
    )


# --------------------------------------------------------------------------
# triggers: a consequence, never a read-out
# --------------------------------------------------------------------------
def _one_from_limit(event: RaceEvent, state: RaceState) -> bool:
    """One heavy contact from the limit: the only incident stage worth a model call."""
    return event.payload.get("stage") == "final"


def _penalty_to_serve(event: RaceEvent, state: RaceState) -> bool:
    """A black flag or a meatball was shown: it is served in the pit lane."""
    return (
        state.capabilities.penalties
        and event.payload.get("state") == "shown"
        and event.payload.get("flag") in SERVE_IN_PIT
    )


def _offs_near_limit(event: RaceEvent, state: RaceState) -> bool:
    """Repeated offs only matter to race control when the limit is close."""
    left = event.payload.get("incidents_remaining")
    margin = event.payload.get("approach_margin")
    return isinstance(left, int) and isinstance(margin, int) and left <= margin


def _blue_flag_backed(event: RaceEvent, state: RaceState) -> bool:
    """Needs CarIdx standings; on a replayed .ibt this is False and it never wakes."""
    return state.capabilities.blue_flag_compliance


RACE_CONTROL_TRIGGERS: tuple[Trigger, ...] = (
    Trigger(
        EventType.INCIDENT_LIMIT.value,
        predicate=_one_from_limit,
        label="one heavy contact from the incident limit",
        cooldown_s=0.0,
    ),
    Trigger(
        EventType.PENALTY.value,
        predicate=_penalty_to_serve,
        label="a penalty to serve in the pit lane (black flag, meatball)",
        cooldown_s=0.0,
    ),
    Trigger(
        EventType.TRACK_LIMITS.value,
        predicate=_offs_near_limit,
        label="repeated off-tracks close to the incident limit",
        cooldown_s=120.0,
    ),
    Trigger(
        EventType.BLUE_FLAG_IGNORED.value,
        predicate=_blue_flag_backed,
        label="a car lapping us held up for several corners",
        cooldown_s=300.0,
    ),
    # A question about a penalty or the limit that no lookup answers (rtv.pitwall.questions).
    question_trigger(AGENT_NAME),
)


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------
def race_control_slice(state: RaceState) -> dict:
    """Flags, incidents and limit, penalty state, the player, the cars around us."""
    caps = state.capabilities
    return {
        "version": state.version,
        "flags": state.flags.model_dump(mode="json"),
        "race_control": state.race_control.model_dump(mode="json"),
        "player": {
            "lap": state.player.lap,
            "position": state.player.position,
            "lap_dist_pct": state.player.lap_dist_pct,
            "on_pit_road": state.player.on_pit_road,
            "track_surface": state.player.track_surface,
            "incidents": state.player.incidents,
        },
        "session": {
            "state": state.session.state,
            "laps_remaining": state.session.laps_remaining,
            "time_remaining": state.session.time_remaining,
        },
        "standings": standings_around_player(state, window=2),
        "capabilities": {
            "incidents": caps.incidents,
            "incident_limit": caps.incident_limit,
            "track_limits": caps.track_limits,
            "penalties": caps.penalties,
            "blue_flag_compliance": caps.blue_flag_compliance,
            "standings": caps.standings,
        },
    }


def race_control_subject(event: RaceEvent, output: AgentOutput) -> str:
    """A stop to serve a penalty is a pit-stop decision; the rest supersede per kind."""
    if getattr(output, "pit_request", "none") == "box_now":
        return "penalty_stop"
    return f"race_control:{getattr(output, 'consequence', None) or event.event_type.value}"


def race_control_policy(output: AgentOutput, state: RaceState, event: RaceEvent) -> str | None:
    """Counts exact, and no penalty claim the flags do not show.

    A race-director event (eval rehearsals only, :mod:`rtv.director`) carries its own
    scripted count and limit; those figures are the event's, with provenance, so a
    call that copies them exactly passes too. Telemetry is never rewritten, so the
    counter would otherwise reject every rehearsed incident-limit call.
    """
    rc = state.race_control
    payload = event.payload if event.payload.get(INJECTED_KEY) else {}
    incidents = getattr(output, "incidents", None)
    if incidents is not None and incidents not in (rc.incidents, payload.get("incidents")):
        return f"incident count {incidents} is not the counter's {rc.incidents}"
    limit = getattr(output, "incident_limit", None)
    if limit is not None and limit not in (rc.incident_limit, payload.get("incident_limit")):
        return f"incident limit {limit} is not the session's {rc.incident_limit}"
    penalty = getattr(output, "penalty", "none")
    claims_penalty = penalty != "none" or getattr(output, "consequence", "") == "penalty"
    if claims_penalty and not (state.capabilities.penalties and rc.penalty):
        return "no penalty flag is showing (or there is no SessionFlags channel)"
    if penalty != "none" and penalty not in rc.penalty:
        return f"the {penalty} flag is not showing (showing: {', '.join(rc.penalty)})"
    if getattr(output, "pit_request", "none") == "box_now" and not rc.serve_in_pit:
        return "box_now needs a penalty served in the pit lane (black flag or meatball)"
    return None


def race_control_validity(
    message: RadioMessage, state: RaceState, event: RaceEvent, pit_entry_pct: float | None
) -> dict:
    """A call to box and serve stops being true once this lap's pit entry is past."""
    if (message.data or {}).get("pit_request") != "box_now":
        return {}
    return box_call_rules(state.player.lap, pit_entry_pct)


RACE_CONTROL = AgentSpec(
    name=AGENT_NAME,
    role_prompt=RACE_CONTROL_PROMPT,
    output_model=RaceControlCall,
    tools=(
        T.GET_RECENT_DETECTOR_EVENTS,
        T.GET_STANDINGS_AROUND_PLAYER,
    ),
    triggers=RACE_CONTROL_TRIGGERS,
    tier="fast",
    max_tokens=800,
    cooldown_s=60.0,
    state_slice=race_control_slice,
    subject=race_control_subject,
    policy=race_control_policy,
    prefetch=(("get_recent_detector_events", {"limit": 10}),),
    max_tool_iterations=2,
    max_age_s=15.0,
    validity=race_control_validity,
    withheld_text="Call withheld: it does not match the race-control data.",
)


def priority_for(consequence: str) -> RadioPriority:
    """The severity a given consequence *should* carry. Used by the eval harness."""
    if consequence in ("penalty", "incident_limit"):
        return RadioPriority.CRITICAL
    if consequence == "none":
        return RadioPriority.INFO
    return RadioPriority.ADVISORY
