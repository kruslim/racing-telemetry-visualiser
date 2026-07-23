"""The SPOTTER -- the shortest sentences on the channel.

A spotter's whole value is latency and brevity, which makes it the agent most at
risk from an LLM. Two things keep it honest:

* **It is woken by facts, not by proximity.** ``traffic_close`` only fires when a
  car is both inside the gap threshold *and* measurably closing; permanent
  proximity in a tight race is not news, and would have the spotter talking every
  lap for nothing.
* **It is given almost nothing.** Its state slice is the running order and the
  flags. With no fuel plan and no tyre data in front of it, it cannot wander into
  another engineer's job, and the citation validator will not accept a number it
  was never shown.

iRacing exposes no lateral position, so this spotter does not say "inside" or
"outside" -- see the stage-3 deviations in ``docs/PITWALL.md``. It says who, how
far, and which side of us in *track order*, all of which are measured.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from rtv.pitwall import tools as T
from rtv.pitwall.framework import AgentOutput, AgentSpec, RadioPriority, Trigger
from rtv.racestate.models import EventType, FlagPhase, RaceEvent, RaceState
from rtv.racestate.strategy_math import standings_around_player

AGENT_NAME = "spotter"

SPOTTER_PROMPT = """\
You are the SPOTTER on a sim-racing pit wall. You own one thing: what is around
the car right now. Not strategy, not the car's condition, not lap time.

Radio discipline is the job. Ten words is long for you. "Car behind, half a
second, closing" is a full call. Never explain, never hedge, never add context
the driver cannot act on in the next corner.

What you may say:
- Who: the car index or position you were given, nothing else. You do not know
  driver names.
- Where: 'ahead' or 'behind' means ahead or behind on track, from the gap sign.
  You have no lateral data at all, so never say inside, outside, left or right.
- How far: the gap in seconds, straight from the event or the standings tool.
- Blue flags: a car at least a lap up and closing. Tell the driver it is coming
  and which side of them it is; the decision to yield is theirs.
- Hazards: a yellow, a red, an incident or an off. Say what changed and where, if
  you were told where.

If the gap basis is null the gaps are unknown for this session. Say "no gaps" and
give the threat type only - do not convert a lap fraction into seconds yourself.
Threat 'clear' is a real answer: use it when the trigger has resolved.
"""


class SpotterCall(AgentOutput):
    """The spotter's output contract. Numbers here must be copied, not computed."""

    threat: Literal[
        "lapped_traffic",
        "car_closing",
        "hazard",
        "clear",
    ] = Field(description="What is around us. 'clear' when the trigger has resolved.")
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
def _hazard_flag(event: RaceEvent, state: RaceState) -> bool:
    """Only flag changes a driver has to react to in the next few seconds."""
    to = event.payload.get("to")
    return to in (FlagPhase.YELLOW.value, FlagPhase.RED.value)


SPOTTER_TRIGGERS: tuple[Trigger, ...] = (
    Trigger(
        EventType.TRAFFIC_CLOSE.value,
        label="a car is close and closing",
        cooldown_s=15.0,
    ),
    Trigger(EventType.BLUE_FLAG.value, label="lapped traffic closing", cooldown_s=10.0),
    Trigger(
        EventType.FLAG_CHANGE.value,
        predicate=_hazard_flag,
        label="yellow or red thrown",
        cooldown_s=10.0,
    ),
    Trigger(EventType.INCIDENT.value, label="incident", cooldown_s=10.0),
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


SPOTTER = AgentSpec(
    name=AGENT_NAME,
    role_prompt=SPOTTER_PROMPT,
    output_model=SpotterCall,
    tools=(T.GET_STANDINGS_AROUND_PLAYER, T.GET_RECENT_DETECTOR_EVENTS),
    triggers=SPOTTER_TRIGGERS,
    model="claude-haiku-4-5-20251001",
    # Short answers, and latency is the whole point: a small budget and two tool
    # rounds, not four. A spotter call that lands after the corner is worthless.
    max_tokens=700,
    cooldown_s=15.0,
    state_slice=spotter_slice,
    subject=spotter_subject,
    max_tool_iterations=2,
)


def priority_for(threat: str) -> RadioPriority:
    """The severity a given threat *should* carry. Used by the eval harness."""
    if threat == "hazard":
        return RadioPriority.CRITICAL
    if threat == "clear":
        return RadioPriority.INFO
    return RadioPriority.ADVISORY
