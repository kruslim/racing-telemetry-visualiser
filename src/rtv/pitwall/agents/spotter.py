"""The SPOTTER -- the shortest sentences on the channel.

A spotter's whole value is latency and brevity, which makes it the agent most at
risk from an LLM. So its per-event calls ("car left", "clear", a closing car, a
blue flag, a yellow, an incident) are not this agent's at all: they are templates
in :mod:`rtv.pitwall.callers`, published the moment the detector fires, with no
model call. This LLM agent keeps only the slow job: a traffic summary at a lap
line when there is a field around us ("traffic ahead for the next two laps"), on
a long cooldown, plus camera notes when live vision radio is switched on.

Two things keep it honest:

* **It is woken by facts, not by proximity.** ``traffic_close`` only fires when a
  car is both inside the gap threshold *and* measurably closing; permanent
  proximity in a tight race is not news, and would have the spotter talking every
  lap for nothing.
* **It is given almost nothing.** Its state slice is the running order and the
  flags. With no fuel plan and no tyre data in front of it, it cannot wander into
  another engineer's job, and the citation validator will not accept a number it
  was never shown.

iRacing exposes no lateral *position*, so this spotter does not say "inside" or
"outside" -- see the stage-3 deviations in ``docs/PITWALL.md``. It says who, how
far, and which side of us in *track order*, all of which are measured. (The
sim's ``CarLeftRight`` proximity sensor does say "car left/right"; that is a
template call, not this agent's.)
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from rtv.pitwall import tools as T
from rtv.pitwall.framework import AgentOutput, AgentSpec, RadioMessage, RadioPriority, Trigger
from rtv.pitwall.questions import question_trigger
from rtv.racestate.models import EventType, RaceEvent, RaceState
from rtv.racestate.strategy_math import standings_around_player
from rtv.vision.live import SPOTTER_KINDS

AGENT_NAME = "spotter"

SPOTTER_PROMPT = """\
You are the SPOTTER on a sim-racing pit wall. You own one thing: what is around
the car. Not strategy, not the car's condition, not lap time.

The instant calls ("car left", "clear", "car behind, closing", blue flags,
yellows) are already made for you by deterministic templates the moment they
happen. You never repeat them. Your job is the slow picture, given at a lap line:
who is around us and what that means for the next lap or two. "Traffic ahead for
the next two laps, P6 half a second up the road" is a full call. "Clear air
behind" is a real answer too.

Radio discipline is the job. Fifteen words is long for you. Never explain, never
hedge, never add context the driver cannot act on.

What you may say:
- Who: the car index or position you were given, nothing else. You do not know
  driver names.
- Where: 'ahead' or 'behind' means ahead or behind on track, from the gap sign.
  You have no lateral data at all, so never say inside, outside, left or right.
- How far: the gap in seconds, straight from the standings tool.
- Lapped traffic: a car at least a lap down that we are catching, or a car a lap
  up catching us. Say it is there; the decision to yield is the driver's.

If the gap basis is null the gaps are unknown for this session. Say "no gaps" and
give the traffic picture only - do not convert a lap fraction into seconds yourself.

A visual_observation trigger is the onboard camera's description of a moment the
telemetry already flagged: words, no numbers, and seconds old. Pass it on only if
it is still worth knowing (a car behaving oddly, debris, damage), never as urgent.
"""


class SpotterCall(AgentOutput):
    """The spotter's output contract. Numbers here must be copied, not computed."""

    threat: Literal[
        "traffic_summary",
        "lapped_traffic",
        "car_closing",
        "hazard",
        "clear",
    ] = Field(
        description="What is around us. 'traffic_summary' for the lap-line picture, "
        "'clear' when there is nobody near."
    )
    side: Literal["ahead", "behind", "unknown"] = Field(
        default="unknown", description="On track, from the sign of the gap. Never lateral."
    )
    car_idx: int | None = Field(
        default=None, description="Car index from the event or the standings tool."
    )
    action: Literal["hold_line", "let_by", "lift", "defend", "none"] = Field(
        default="none", description="The one thing to do, if there is one."
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="low when gaps are unavailable or the threat is inferred."
    )


# --------------------------------------------------------------------------
# triggers
# --------------------------------------------------------------------------
#: A car within this many seconds either side makes a lap-line summary worth it.
SUMMARY_GAP_S = 5.0
#: At most one summary per this many session seconds (two-plus laps at Spa).
SUMMARY_COOLDOWN_S = 300.0


def _field_around_us(event: RaceEvent, state: RaceState) -> bool:
    """A summary only when there is a measured field near the car to summarise.

    No standings (an ``.ibt`` has no CarIdx channels) or no gap basis means
    nothing to say, so the model is never woken for it.
    """
    if not state.capabilities.standings or not state.standings.gap_basis:
        return False
    return any(
        not car.is_player
        and not car.on_pit_road
        and car.gap_to_player is not None
        and abs(car.gap_to_player) <= SUMMARY_GAP_S
        for car in state.standings.cars
    )


def _visual_for_spotter(event: RaceEvent, state: RaceState) -> bool:
    """Only what the camera saw around the car; line and kerb notes are the coach's."""
    return event.payload.get("kind") in SPOTTER_KINDS


#: No per-event triggers: traffic, blue flags, yellows and incidents are template
#: calls (:data:`rtv.pitwall.callers.SPOTTER_TEMPLATES`), made without a model.
SPOTTER_TRIGGERS: tuple[Trigger, ...] = (
    Trigger(
        EventType.LAP_COMPLETED.value,
        predicate=_field_around_us,
        label="lap-line traffic summary (a car within 5 s)",
        cooldown_s=SUMMARY_COOLDOWN_S,
    ),
    Trigger(
        EventType.VISUAL_OBSERVATION.value,
        predicate=_visual_for_spotter,
        label="the onboard camera saw traffic, debris or damage",
        cooldown_s=60.0,
    ),
    # A question the driver asked that needs judgement (rtv.pitwall.questions).
    question_trigger(AGENT_NAME),
)


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------
def spotter_slice(state: RaceState) -> dict:
    """Who is around us and what the flags are. Nothing else exists for a spotter."""
    return {
        "version": state.version,
        "flags": state.flags.model_dump(mode="json"),
        "player": {
            "lap": state.player.lap,
            "position": state.player.position,
            "lap_dist_pct": state.player.lap_dist_pct,
            "speed": state.player.speed,
            "on_pit_road": state.player.on_pit_road,
            "track_surface": state.player.track_surface,
            "incidents": state.player.incidents,
        },
        "standings": standings_around_player(state, window=3),
        "capabilities": {
            "standings": state.capabilities.standings,
            "flags": state.capabilities.flags,
        },
    }


def spotter_subject(event: RaceEvent, output: AgentOutput) -> str:
    """One subject per threat kind: a newer traffic call replaces the stale one."""
    return f"traffic:{getattr(output, 'threat', 'unknown')}"


#: A summary is about the next lap or two: after this it is history, not news.
SUMMARY_EXPIRES_S = 60.0


def spotter_validity(
    message: RadioMessage, state: RaceState, event: RaceEvent, pit_entry_pct: float | None
) -> dict:
    """A call about one car goes when the car does; a summary goes after a minute."""
    rules: dict = {}
    idx = event.payload.get("car_idx")
    if isinstance(idx, int):
        rules["subject_car_idx"] = int(idx)
    if event.event_type is EventType.LAP_COMPLETED:
        rules["expires_at_session_time"] = round(state.session_time + SUMMARY_EXPIRES_S, 3)
    return rules


SPOTTER = AgentSpec(
    name=AGENT_NAME,
    role_prompt=SPOTTER_PROMPT,
    output_model=SpotterCall,
    tools=(
        T.GET_STANDINGS_AROUND_PLAYER,
        T.GET_RECENT_DETECTOR_EVENTS,
        # When a car closes on the lap-line summary's horizon, from gaps and pace.
        T.GET_TRAFFIC_PROJECTION,
    ),
    triggers=SPOTTER_TRIGGERS,
    tier="fast",
    # Short answers: a small budget and two tool rounds, not four.
    max_tokens=700,
    cooldown_s=SUMMARY_COOLDOWN_S,
    state_slice=spotter_slice,
    subject=spotter_subject,
    # Both of its tools, which every run on the Spa replay called first.
    prefetch=(
        ("get_standings_around_player", {}),
        ("get_recent_detector_events", {"limit": 10}),
    ),
    max_tool_iterations=2,
    # A summary is never urgent, so it is never tagged late; it expires after a
    # minute instead (spotter_validity). The instant calls' 3 s limit is the
    # template caller's (rtv.pitwall.callers.SPOTTER_MAX_AGE_S).
    max_age_s=None,
    validity=spotter_validity,
)


def priority_for(threat: str) -> RadioPriority:
    """The severity a given threat *should* carry. Used by the eval harness."""
    if threat == "hazard":
        return RadioPriority.CRITICAL
    if threat == "clear":
        return RadioPriority.INFO
    return RadioPriority.ADVISORY
