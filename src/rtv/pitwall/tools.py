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

from rtv.logging import get_logger
from rtv.pitwall.framework import ToolContext, ToolSpec
from rtv.racestate import strategy_math as sm
from rtv.racestate.detectors import DEFAULT_CONFIG, corner_label
from rtv.racestate.models import RaceState

log = get_logger("pitwall.tools")

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


# --------------------------------------------------------------------------
# stage 3: the vehicle engineer / spotter / coach half of the registry
# --------------------------------------------------------------------------
def _buckets(ctx: ToolContext) -> int:
    try:
        return max(1, int(ctx.config.get("corner_buckets", DEFAULT_CONFIG.corner_buckets)))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return DEFAULT_CONFIG.corner_buckets


def _event_row(event: Any) -> dict[str, Any]:
    return {
        "key": event.key,
        "lap": event.lap,
        "session_time": round(event.session_time, 3),
        "severity": event.severity.value,
        "payload": event.payload,
    }


def _get_recent_detector_events(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    types = args.get("types")
    if isinstance(types, str):
        types = [types]
    wanted = set(types) if types else None
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    rows = [
        _event_row(e)
        for e in ctx.events
        if wanted is None or e.event_type.value in wanted
    ]
    out: dict[str, Any] = {
        "requested_types": sorted(wanted) if wanted else "all",
        "returned": len(rows[-limit:]),
        "available": len(rows),
        "events": rows[-limit:],
    }
    if not rows:
        out["unavailable"] = (
            "No events of those types are in the ring buffer for this session yet."
        )
    return out


def _get_car_health(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    health = ctx.state.car_health
    out: dict[str, Any] = {
        **health.model_dump(mode="json"),
        "thresholds": {
            "oil_temp_max_c": ctx.config.get("oil_temp_max_c", DEFAULT_CONFIG.oil_temp_max_c),
            "water_temp_max_c": ctx.config.get(
                "water_temp_max_c", DEFAULT_CONFIG.water_temp_max_c
            ),
        },
        "lap": ctx.state.player.lap,
    }
    if not ctx.state.capabilities.car_health:
        out["unavailable"] = "This session's catalog has no engine-health channels."
    return out


def _get_setup_snapshot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    engine = ctx.extras.get("engine")
    snapshot = getattr(engine, "setup_snapshot", None)
    if snapshot is None:
        return {
            "available": False,
            "unavailable": "No race-state engine is attached, so no session-info document.",
        }
    try:
        return snapshot()
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("setup_snapshot failed")
        return {"available": False, "unavailable": f"Setup could not be read: {exc}"}


# ---- corner detail ------------------------------------------------------
def _target_pct(ctx: ToolContext, args: dict[str, Any]) -> float | None:
    """Where on the lap the caller means, from an argument or the trigger."""
    raw = args.get("lap_dist_pct")
    if raw is not None:
        try:
            return float(raw) % 1.0
        except (TypeError, ValueError):
            pass
    wanted = args.get("corner")
    buckets = _buckets(ctx)
    if wanted:
        for i in range(buckets):
            mid = (i + 0.5) / buckets
            if corner_label(mid, buckets) == str(wanted):
                return mid
    payload = ctx.event.payload or {}
    for key in ("lap_dist_pct_from", "lap_dist_pct"):
        if payload.get(key) is not None:
            try:
                return float(payload[key]) % 1.0
            except (TypeError, ValueError):
                continue
    if payload.get("corner") and isinstance(payload["corner"], str):
        return _target_pct(ctx, {"corner": payload["corner"]})
    return None


def _live_corner_events(ctx: ToolContext, pct: float | None) -> dict[str, Any]:
    """What the deterministic detectors saw at this corner during the session."""
    buckets = _buckets(ctx)
    label = corner_label(pct, buckets) if pct is not None else None
    rows: list[dict[str, Any]] = []
    for event in ctx.events:
        kind = event.event_type.value
        if kind not in ("lockup", "wheelspin", "offtrack", "recurring_issue"):
            continue
        payload = event.payload or {}
        where = payload.get("lap_dist_pct")
        if where is None and payload.get("lap_dist_pct_from") is not None:
            where = payload["lap_dist_pct_from"]
        if label is not None and corner_label(where, buckets) != label:
            continue
        rows.append(_event_row(event))
    return {"corner": label, "count": len(rows), "events": rows[-12:]}


def _layer1_corner(ctx: ToolContext, pct: float | None) -> dict[str, Any]:
    """The Layer-1 coach's corner findings for the nearest stored corner.

    This is the same :class:`~rtv.coaching.features.CoachingService` the v1
    coaching endpoints use -- deterministic NumPy over stored telemetry, no LLM.
    It needs a *written* session, so it is unavailable during the opening laps and
    in any deployment that is not recording; that is reported, never papered over.
    """
    coaching = ctx.extras.get("coaching")
    repo = ctx.extras.get("repo")
    session_id = ctx.extras.get("session_id") or ctx.state.session.session_id
    if coaching is None or repo is None or not session_id:
        return {
            "available": False,
            "unavailable": "No stored session is attached, so no corner telemetry to read.",
        }
    try:
        laps = [
            row
            for row in repo.get_laps(session_id)
            if row.get("is_valid") and not row.get("is_out_lap") and not row.get("is_in_lap")
        ]
        timed = [row for row in laps if row.get("lap_time")]
        if len(timed) < 2:
            return {
                "available": False,
                "unavailable": f"Session {session_id} has fewer than two clean timed laps.",
            }
        main = max(timed, key=lambda r: r["lap"])["lap"]
        ref = min(timed, key=lambda r: r["lap_time"])["lap"]
        if main == ref:
            others = [r for r in timed if r["lap"] != ref]
            main = max(others, key=lambda r: r["lap"])["lap"]
        findings = coaching.lap_findings(session_id, main, ref)
    except Exception as exc:
        return {"available": False, "unavailable": f"Corner telemetry unavailable: {exc}"}

    length = findings.lap_length_m or 0.0
    corners = findings.corners
    if not corners:
        return {
            "available": False,
            "unavailable": f"No corners were detected on lap {main} of {session_id}.",
        }
    if pct is not None and length > 0:
        target_m = pct * length
        corner = min(corners, key=lambda c: abs(c.distance - target_m))
    else:
        corner = max(corners, key=lambda c: c.net_dt)
    return {
        "available": True,
        "session_id": session_id,
        "main_lap": main,
        "ref_lap": ref,
        "lap_length_m": length,
        "corner": {
            "label": corner.label,
            "type": corner.type,
            "distance_m": round(corner.distance, 1),
            "lap_dist_pct": round(corner.distance / length, 4) if length else None,
            "sector": corner.sector,
            "min_speed_kmh": round(corner.min_main, 2),
            "ref_min_speed_kmh": round(corner.min_ref, 2),
            "time_delta_s": round(corner.net_dt, 4),
            "diagnostics": [
                {
                    "agent": d.agent,
                    "text": d.text,
                    "time_loss_s": round(d.time_loss, 4),
                    "lockup": d.lockup,
                    "good": d.good,
                }
                for d in corner.diags
            ],
        },
        "notes": list(findings.notes),
    }


def _get_corner_detail(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    pct = _target_pct(ctx, args)
    out: dict[str, Any] = {
        "requested_lap_dist_pct": None if pct is None else round(pct, 4),
        "live": _live_corner_events(ctx, pct),
        "telemetry": _layer1_corner(ctx, pct),
    }
    if pct is None:
        out["note"] = (
            "No corner was named and the trigger carries no lap distance, so this "
            "covers the whole lap."
        )
    return out


GET_RECENT_DETECTOR_EVENTS = ToolSpec(
    name="get_recent_detector_events",
    description=(
        "Recent deterministic detector events from this session's ring buffer, "
        "newest last: lockup, wheelspin, offtrack, incident, recurring_issue, "
        "tyre_out_of_band, car_health_warning, traffic_close, lap_completed and the "
        "rest of the event catalog. Use it to see whether something is a one-off or "
        "a pattern."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Event types to keep. Omit for everything.",
            },
            "limit": {"type": "integer", "description": "Most recent N (1-100). Default 20."},
        },
    },
    fn=_get_recent_detector_events,
)

GET_CAR_HEALTH = ToolSpec(
    name="get_car_health",
    description=(
        "Oil and water temperature, oil and fuel pressure, voltage, tow time and the "
        "sim's own engine-warning bits, alongside the thresholds this pitwall is "
        "using. A null field means the channel is not in this session's catalog."
    ),
    input_schema={"type": "object", "properties": {}},
    fn=_get_car_health,
)

GET_SETUP_SNAPSHOT = ToolSpec(
    name="get_setup_snapshot",
    description=(
        "The car setup exactly as the session-info document reports it (brake bias, "
        "cold pressures, camber, wing, fuel capacity). Returns 'unavailable' when the "
        "sim supplied no CarSetup section - many sessions do not. Never assume a "
        "setup value that is not in here."
    ),
    input_schema={"type": "object", "properties": {}},
    fn=_get_setup_snapshot,
)

GET_CORNER_DETAIL = ToolSpec(
    name="get_corner_detail",
    description=(
        "Everything known about one corner: the detector events that fired there "
        "this session, plus the Layer-1 coaching analysis of it (minimum speed vs a "
        "reference lap, brake point, throttle application, lock-up, time lost). The "
        "'telemetry' half reports available=false with a reason when the session has "
        "not been recorded or has too few clean laps - say so rather than inferring."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "corner": {
                "type": "string",
                "description": "Corner label from a recurring_issue event, e.g. 'C08'.",
            },
            "lap_dist_pct": {
                "type": "number",
                "description": "Lap fraction 0-1, if you have one instead of a label.",
            },
        },
    },
    fn=_get_corner_detail,
)

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

#: The whole registry. Each agent picks a subset; a tool outside an agent's tuple
#: is unreachable *and* uncitable by it, which is how speech stays scoped.
ALL_TOOLS: tuple[ToolSpec, ...] = (
    GET_RACE_STATE_SLICE,
    GET_FUEL_PROJECTION,
    GET_TYRE_TREND,
    GET_STANDINGS_AROUND_PLAYER,
    GET_STINT_HISTORY,
    SIMULATE_PIT_OUTCOME,
    GET_RECENT_DETECTOR_EVENTS,
    GET_CAR_HEALTH,
    GET_SETUP_SNAPSHOT,
    GET_CORNER_DETAIL,
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}
