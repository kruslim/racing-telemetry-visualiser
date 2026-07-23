"""The deterministic tools agents may call.

Every tool is a thin, side-effect-free wrapper over :mod:`rtv.racestate` — the
state snapshot, the event ring buffer and :mod:`rtv.racestate.strategy_math`. No
tool talks to the sim, the store or the network, so a tool call costs a few
microseconds and the whole agent layer stays replayable.

Tools are also the *grounding surface*: whatever a tool returns becomes citable,
and nothing else is. Scoping an agent's tool list is therefore also scoping which
numbers it is allowed to say out loud.
"""

from __future__ import annotations

from typing import Any

from rtv.pitwall.framework import ToolContext, ToolSpec
from rtv.racestate import strategy_math as sm
from rtv.racestate.models import RaceState

#: Sections of RaceState a slice tool will hand over. ``metrics`` is excluded --
#: engine self-timing is an operations concern, not a race one.
STATE_SECTIONS = (
    "session",
    "flags",
    "player",
    "fuel",
    "tyres",
    "standings",
    "car_health",
    "conditions",
    "capabilities",
)


def _section(state: RaceState, name: str) -> Any:
    if name == "standings":
        # The full 64-car array is mostly noise; the neighbours are the story.
        return sm.standings_around_player(state, window=5)
    return getattr(state, name).model_dump(mode="json")


def _get_race_state_slice(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    sections = args.get("sections") or list(STATE_SECTIONS)
    if isinstance(sections, str):
        sections = [sections]
    unknown = [s for s in sections if s not in STATE_SECTIONS]
    out: dict[str, Any] = {
        s: _section(ctx.state, s) for s in sections if s in STATE_SECTIONS
    }
    out["version"] = ctx.state.version
    out["session_time"] = ctx.state.session_time
    if unknown:
        out["unknown_sections"] = unknown
    return out


def _get_fuel_projection(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return sm.fuel_projection(ctx.state)


def _get_tyre_trend(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return sm.tyre_trend(ctx.state)


def _get_standings_around_player(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    window = args.get("window")
    try:
        window = int(window) if window is not None else int(ctx.config.get("standings_window", 3))
    except (TypeError, ValueError):
        window = 3
    return sm.standings_around_player(ctx.state, window=max(1, min(window, 10)))


def _get_stint_history(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return sm.stint_history(ctx.state, ctx.events)


def _simulate_pit_outcome(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    stop_lap = args.get("stop_lap")
    try:
        stop_lap = int(stop_lap) if stop_lap is not None else None
    except (TypeError, ValueError):
        stop_lap = None
    under_yellow = args.get("under_yellow")
    if under_yellow is not None:
        under_yellow = bool(under_yellow)
    loss = ctx.config.get("pit_lane_loss_s", sm.DEFAULT_PIT_LANE_LOSS_S)
    return sm.simulate_pit_outcome(
        ctx.state,
        stop_lap=stop_lap,
        pit_lane_loss_s=float(loss),
        under_yellow=under_yellow,
    ).to_tool()


GET_RACE_STATE_SLICE = ToolSpec(
    name="get_race_state_slice",
    description=(
        "Read named sections of the current deterministic race state. Use it when you "
        "need a figure that is not in the state block you were given. A field that is "
        "null is genuinely unknown for this session - do not substitute a value."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {"type": "string", "enum": list(STATE_SECTIONS)},
                "description": "Sections to return. Omit for all of them.",
            }
        },
    },
    fn=_get_race_state_slice,
)

GET_FUEL_PROJECTION = ToolSpec(
    name="get_fuel_projection",
    description=(
        "Fuel level, learned consumption per lap and its spread, laps remaining, laps "
        "still to run, margin to the finish, and the pit window bounds. Returns an "
        "'unavailable' note instead of numbers when consumption has not been learned."
    ),
    input_schema={"type": "object", "properties": {}},
    fn=_get_fuel_projection,
)

GET_TYRE_TREND = ToolSpec(
    name="get_tyre_trend",
    description=(
        "Per-corner tyre temperatures and pressures with their per-lap trend across the "
        "current stint. Trends are cleared on pit exit, so they always describe the set "
        "that is on the car now."
    ),
    input_schema={"type": "object", "properties": {}},
    fn=_get_tyre_trend,
)

GET_STANDINGS_AROUND_PLAYER = ToolSpec(
    name="get_standings_around_player",
    description=(
        "The cars either side of us in the running order with gaps in seconds. Check "
        "gap_basis: when it is null the gaps are unknown and you must not quote any."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "window": {
                "type": "integer",
                "description": "How many cars each side (1-10). Defaults to 3.",
            }
        },
    },
    fn=_get_standings_around_player,
)

GET_STINT_HISTORY = ToolSpec(
    name="get_stint_history",
    description=(
        "Stints so far, rebuilt from the deterministic event log: lap times per stint, "
        "which laps were green, and where the stops fell."
    ),
    input_schema={"type": "object", "properties": {}},
    fn=_get_stint_history,
)

SIMULATE_PIT_OUTCOME = ToolSpec(
    name="simulate_pit_outcome",
    description=(
        "Project a stop on a given lap: rejoin position from the current gaps and the "
        "pit-lane time loss, how much fuel to add, and whether that reaches the flag. "
        "Under yellow the loss is discounted. Check 'grounded': when it is false the "
        "inputs were missing and every projected number is null - say so rather than "
        "guessing. The 'assumptions' list is what you are entitled to hedge with."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "stop_lap": {
                "type": "integer",
                "description": "Lap to pit on. Omit to model stopping this lap.",
            },
            "under_yellow": {
                "type": "boolean",
                "description": "Override the flag state, e.g. to price an opportunistic stop.",
            },
        },
    },
    fn=_simulate_pit_outcome,
)

#: The strategist's scoped subset. Later agents pick their own from this registry.
ALL_TOOLS: tuple[ToolSpec, ...] = (
    GET_RACE_STATE_SLICE,
    GET_FUEL_PROJECTION,
    GET_TYRE_TREND,
    GET_STANDINGS_AROUND_PLAYER,
    GET_STINT_HISTORY,
    SIMULATE_PIT_OUTCOME,
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}
