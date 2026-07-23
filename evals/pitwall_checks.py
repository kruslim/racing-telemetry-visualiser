"""Deterministic ground-truth checks for the pitwall agents — no LLM, no network.

The same idea as :mod:`evals.checks`, applied to a decision instead of a lap
report. The race-state engine already knows the truth: what the pit window is,
what a stop on a given lap would actually cost, whether a caution is out, which
corner the lockups were at. So an agent's call is scoreable arithmetic, not a vibe.

The strategist's checks are below; the vehicle engineer's, the spotter's and the
coach's are at the bottom of the file, dispatched by :func:`check_for_agent`.
Grounding and radio discipline are identical for all four; everything else is
scored against what that role's deterministic trigger actually said.

For the strategist:

===========================  ================================================
grounding                    every figure it said traces to the state or a tool
window_ok                    the lap it named lies inside the fuel window
rejoin_ok                    its rejoin position matches simulate_pit_outcome
decision_ok                  the call matches what the state actually demands
radio_ok                     spoken_text is short enough to be radio
refusal_ok                   it held rather than guessed when data was missing
===========================  ================================================

``decision_ok`` is a *band*, not a single right answer -- there are usually two
defensible calls (box now vs box next lap) and one indefensible one (stay out on
an empty tank). The band is what we score.
"""

from __future__ import annotations

from typing import Any

from rtv.pitwall.validator import FactSet, validate_output
from rtv.racestate.models import FlagPhase, RaceState
from rtv.racestate.strategy_math import simulate_pit_outcome

#: Laps of tolerance when checking a called stop lap against the window.
LAP_TOL = 0
#: Positions of tolerance when checking a claimed rejoin against the projection.
POSITION_TOL = 1

BOXING = {"box_now", "box_next_lap", "pit_under_yellow"}


def expected_calls(state: RaceState) -> set[str]:
    """The set of defensible recommendations for this race state.

    Deliberately generous where the data genuinely allows a judgement call, and
    strict where it does not: with no fuel model there is exactly one honest
    answer, and it is 'hold'.
    """
    fuel = state.fuel
    lap = state.player.lap
    if not fuel.per_lap or lap is None:
        return {"hold"}

    latest = fuel.pit_window_latest_lap
    if latest is not None and lap >= latest - 1:
        # At or past the run-dry bound: stopping is the only defensible call.
        return {"box_now", "box_next_lap", "pit_under_yellow"}

    stop_required = fuel.margin_laps is not None and fuel.margin_laps < 0
    under_yellow = state.flags.phase is FlagPhase.YELLOW
    if stop_required and fuel.window_open:
        allowed = {"box_now", "box_next_lap"}
        if under_yellow:
            allowed.add("pit_under_yellow")
        return allowed
    if stop_required:
        # A stop is needed but the window has not opened: extending is correct,
        # and taking a cheap caution is defensible.
        allowed = {"extend", "stay_out"}
        if under_yellow:
            allowed |= {"pit_under_yellow", "box_now"}
        return allowed
    return {"stay_out", "extend"}


def cross_check(
    result: dict[str, Any],
    state: RaceState,
    *,
    facts: FactSet | None = None,
    pit_lane_loss_s: float = 25.0,
) -> dict:
    """Score one strategist call against the deterministic race state."""
    issues: list[str] = []
    data = result.get("data") or result
    recommendation = data.get("recommendation")
    spoken = result.get("spoken_text", "") or ""
    detail = result.get("detail_text", "") or ""

    # --- grounding: every number traceable to what the agent was shown -----
    if facts is None:
        facts = FactSet()
        facts.add("race_state", state.to_api())
        facts.add(
            "simulate_pit_outcome",
            simulate_pit_outcome(state, pit_lane_loss_s=pit_lane_loss_s).to_tool(),
        )
    report = validate_output(spoken, detail, facts)
    grounding_ok = not report.unsupported
    if not grounding_ok:
        issues.append(
            "ungrounded figures: " + ", ".join(report.unsupported) + " (not in the race state)"
        )

    # --- radio discipline --------------------------------------------------
    radio_ok = not report.over_word_limit
    if not radio_ok:
        issues.append(f"spoken_text is {report.word_count} words; radio limit is 25")

    # --- the decision itself ----------------------------------------------
    allowed = expected_calls(state)
    decision_ok = recommendation in allowed
    if not decision_ok:
        issues.append(
            f"recommendation {recommendation!r} is outside the defensible set "
            f"{sorted(allowed)} for this state"
        )

    # --- the stop lap has to be inside the window -------------------------
    window_ok = True
    target = data.get("target_lap")
    if recommendation in BOXING and target is not None:
        earliest = state.fuel.pit_window_earliest_lap
        latest = state.fuel.pit_window_latest_lap
        if latest is not None and target > latest + LAP_TOL:
            window_ok = False
            issues.append(f"target_lap {target} is past the run-dry lap {latest}")
        elif earliest is not None and target < earliest - LAP_TOL:
            window_ok = False
            issues.append(f"target_lap {target} is before the window opens at {earliest}")

    # --- the rejoin claim has to match the deterministic projection --------
    rejoin_ok = True
    claimed = data.get("rejoin_position")
    if claimed is not None:
        outcome = simulate_pit_outcome(
            state, stop_lap=target, pit_lane_loss_s=pit_lane_loss_s
        )
        if not outcome.grounded or outcome.rejoin_position is None:
            rejoin_ok = False
            issues.append(
                f"claimed rejoin P{claimed} but the projection is not grounded "
                f"({outcome.reason})"
            )
        elif abs(int(claimed) - outcome.rejoin_position) > POSITION_TOL:
            rejoin_ok = False
            issues.append(
                f"claimed rejoin P{claimed} but the projection says "
                f"P{outcome.rejoin_position}"
            )

    # --- refusing when the data is absent is a *pass*, not a failure -------
    data_missing = not state.fuel.per_lap or state.player.lap is None
    refusal_ok = (recommendation == "hold") if data_missing else True
    if data_missing and not refusal_ok:
        issues.append("data for a pit call is missing but the agent still made one")

    checks = {
        "grounding_ok": grounding_ok,
        "radio_ok": radio_ok,
        "decision_ok": decision_ok,
        "window_ok": window_ok,
        "rejoin_ok": rejoin_ok,
        "refusal_ok": refusal_ok,
    }
    return {
        **checks,
        "score": sum(1 for v in checks.values() if v) / len(checks),
        "numbers": len(report.numbers),
        "ungrounded": list(report.unsupported),
        "allowed_calls": sorted(allowed),
        "recommendation": recommendation,
        "issues": issues,
    }


def aggregate(rows: list[dict]) -> dict:
    """Roll per-case results up into headline metrics.

    The check keys differ per agent, so they are discovered from the rows rather
    than hard-coded -- a mixed run reports the union and simply omits a rate for
    an agent that does not carry that check.
    """
    if not rows:
        return {"cases": 0}
    n = len(rows)
    keys = [k for k in dict.fromkeys(k for r in rows for k in r) if k.endswith("_ok")]
    out: dict[str, Any] = {"cases": n}
    for key in keys:
        scored = [r for r in rows if key in r]
        out[key.replace("_ok", "_rate")] = (
            sum(1 for r in scored if r[key]) / len(scored) if scored else 0.0
        )
    out["mean_score"] = sum(r["score"] for r in rows) / n
    out["total_ungrounded"] = sum(len(r["ungrounded"]) for r in rows)
    out["clean_cases"] = sum(1 for r in rows if not r["issues"])
    return out


# ==========================================================================
# stage 3: the vehicle engineer / spotter / coach
# ==========================================================================
#
# Same idea, different ground truth. A pit call is scored against arithmetic; a
# role call is scored against *what the deterministic trigger actually said*. The
# engine already decided that three lockups happened at C08 and that the LF trend
# is 2.0 C/lap, so an engineer that answers "rears are graining at C13" is wrong
# by comparison, not by opinion.

#: What each agent is allowed to conclude from each kind of trigger. A band, not
#: one right answer -- the same recurrence is legitimately a brake or a tyre
#: finding -- but "the fronts are cooking" is not a defensible answer to a tow.
ENGINEER_FINDINGS: dict[str, set[str]] = {
    "lockup": {"brake_lockup", "tyre_temps", "none"},
    "wheelspin": {"traction", "tyre_temps", "none"},
    "offtrack": {"traction", "brake_lockup", "none"},
    "tyre_temp": {"tyre_temps", "none"},
    "tyre_temp_trend": {"tyre_temps", "none"},
    "tyre_axle_imbalance": {"tyre_temps", "none"},
    "tyre_pressure": {"tyre_pressures", "none"},
    "tyre_pressure_trend": {"tyre_pressures", "none"},
    "engine_warning": {"car_health", "damage"},
    "tow": {"damage", "car_health"},
    "oil_temp": {"car_health"},
    "water_temp": {"car_health"},
}

SPOTTER_THREATS: dict[str, set[str]] = {
    "traffic_close": {"car_closing", "clear"},
    "blue_flag": {"lapped_traffic", "clear"},
    "flag_change": {"hazard", "clear"},
    "incident": {"hazard", "clear"},
}

COACH_THEMES: dict[str, set[str]] = {
    "lockup": {"braking", "line", "tyre_management", "none"},
    "wheelspin": {"throttle", "line", "tyre_management", "none"},
    "offtrack": {"line", "braking", "consistency", "none"},
    "stint_lap_milestone": {
        "braking",
        "throttle",
        "line",
        "consistency",
        "tyre_management",
        "none",
    },
}

#: A spotter call is shorter than radio generally: ten words is already long.
SPOTTER_WORD_LIMIT = 14


def _base_checks(result: dict, facts: FactSet | None, state: RaceState) -> tuple[dict, list]:
    """Grounding and radio discipline, which every agent is held to identically."""
    issues: list[str] = []
    spoken = result.get("spoken_text", "") or ""
    detail = result.get("detail_text", "") or ""
    if facts is None:
        facts = FactSet()
        facts.add("race_state", state.to_api())
    report = validate_output(spoken, detail, facts)
    if report.unsupported:
        issues.append(
            "ungrounded figures: " + ", ".join(report.unsupported) + " (not in the race state)"
        )
    if report.over_word_limit:
        issues.append(f"spoken_text is {report.word_count} words; radio limit is 25")
    return (
        {
            "grounding_ok": not report.unsupported,
            "radio_ok": not report.over_word_limit,
            "_report": report,
            "_spoken": spoken,
        },
        issues,
    )


def _finish(checks: dict, issues: list[str], extra: dict) -> dict:
    report = checks.pop("_report")
    checks.pop("_spoken", None)
    scored = {k: v for k, v in checks.items() if k.endswith("_ok")}
    return {
        **scored,
        "score": sum(1 for v in scored.values() if v) / len(scored),
        "numbers": len(report.numbers),
        "ungrounded": list(report.unsupported),
        "issues": issues,
        **extra,
    }


def engineer_check(
    result: dict[str, Any],
    state: RaceState,
    event,
    *,
    facts: FactSet | None = None,
    setup_available: bool = False,
) -> dict:
    """Score one vehicle-engineer advisory against the trigger that caused it."""
    checks, issues = _base_checks(result, facts, state)
    data = result.get("data") or result
    payload = getattr(event, "payload", {}) or {}
    finding = data.get("finding")

    kind = payload.get("issue") or payload.get("measure") or ""
    allowed = ENGINEER_FINDINGS.get(kind)
    checks["finding_ok"] = allowed is None or finding in allowed
    if not checks["finding_ok"]:
        issues.append(
            f"finding {finding!r} is outside the defensible set {sorted(allowed)} "
            f"for a {kind!r} trigger"
        )

    # A corner is a fact the event carries. Naming a different one is invention.
    corner = data.get("corner")
    event_corner = payload.get("corner")
    checks["corner_ok"] = (
        corner is None or event_corner is None or str(corner) == str(event_corner)
    )
    if not checks["corner_ok"]:
        issues.append(f"named corner {corner!r} but the event is at {event_corner!r}")

    # Setup advice without a setup sheet is the classic ungrounded engineer.
    checks["setup_ok"] = setup_available or not data.get("setup_note")
    if not checks["setup_ok"]:
        issues.append("recommended a setup change but no CarSetup was available")

    # With no tyre channels, a tyre finding cannot be backed by anything.
    tyre_blind = not state.capabilities.tyres
    checks["refusal_ok"] = not (
        tyre_blind and finding in ("tyre_temps", "tyre_pressures")
    )
    if not checks["refusal_ok"]:
        issues.append("claimed a tyre finding but this session has no tyre channels")

    return _finish(
        checks, issues, {"finding": finding, "allowed_findings": sorted(allowed or [])}
    )


def spotter_check(
    result: dict[str, Any],
    state: RaceState,
    event,
    *,
    facts: FactSet | None = None,
) -> dict:
    """Score one spotter call: right threat, right car, right side, few words."""
    checks, issues = _base_checks(result, facts, state)
    spoken = checks["_spoken"]
    data = result.get("data") or result
    payload = getattr(event, "payload", {}) or {}
    threat = data.get("threat")

    allowed = SPOTTER_THREATS.get(event.event_type.value)
    checks["threat_ok"] = allowed is None or threat in allowed
    if not checks["threat_ok"]:
        issues.append(
            f"threat {threat!r} is outside the defensible set {sorted(allowed)} "
            f"for {getattr(event, 'key', '?')}"
        )

    claimed = data.get("car_idx")
    known = {c.idx for c in state.standings.cars}
    event_car = payload.get("car_idx")
    if claimed is None:
        checks["car_ok"] = True
    elif event_car is not None:
        checks["car_ok"] = int(claimed) == int(event_car)
    else:
        checks["car_ok"] = int(claimed) in known
    if not checks["car_ok"]:
        issues.append(f"named car {claimed} but the event is about car {event_car}")

    side = data.get("side", "unknown")
    event_side = payload.get("side")
    checks["side_ok"] = (
        side == "unknown" or event_side is None or side == event_side
    )
    if not checks["side_ok"]:
        issues.append(f"said {side!r} but the event says {event_side!r}")

    words = len(spoken.split())
    checks["brevity_ok"] = words <= SPOTTER_WORD_LIMIT
    if not checks["brevity_ok"]:
        issues.append(f"spotter call is {words} words; the limit is {SPOTTER_WORD_LIMIT}")

    # No gap basis means the seconds do not exist for this session.
    checks["refusal_ok"] = bool(state.standings.gap_basis) or not _quotes_a_gap(result)
    if not checks["refusal_ok"]:
        issues.append("quoted a gap in seconds but this session has no gap basis")

    return _finish(checks, issues, {"threat": threat, "allowed_threats": sorted(allowed or [])})


def _quotes_a_gap(result: dict[str, Any]) -> bool:
    text = f"{result.get('spoken_text', '')} {result.get('detail_text', '')}".lower()
    return any(word in text for word in ("second", "sec ", "secs", "s behind", "s back"))


def coach_check(
    result: dict[str, Any],
    state: RaceState,
    event,
    *,
    facts: FactSet | None = None,
    telemetry_available: bool = False,
) -> dict:
    """Score one coaching cue: right theme, right corner, one instruction."""
    checks, issues = _base_checks(result, facts, state)
    data = result.get("data") or result
    payload = getattr(event, "payload", {}) or {}
    theme = data.get("theme")

    kind = payload.get("issue") or event.event_type.value
    allowed = COACH_THEMES.get(kind)
    checks["theme_ok"] = allowed is None or theme in allowed
    if not checks["theme_ok"]:
        issues.append(
            f"theme {theme!r} is outside the defensible set {sorted(allowed)} for {kind!r}"
        )

    corner = data.get("corner")
    event_corner = payload.get("corner")
    checks["corner_ok"] = (
        corner is None
        or event_corner is None
        or str(corner) == str(event_corner)
        or str(corner).startswith("T")  # a Layer-1 label from get_corner_detail
    )
    if not checks["corner_ok"]:
        issues.append(f"coached corner {corner!r} but the pattern is at {event_corner!r}")

    checks["telemetry_ok"] = telemetry_available or not data.get("from_telemetry")
    if not checks["telemetry_ok"]:
        issues.append("claimed a telemetry-backed cue but no corner trace was available")

    cue = (data.get("cue") or "").strip()
    checks["one_cue_ok"] = bool(cue) if theme != "none" else True
    if not checks["one_cue_ok"]:
        issues.append("gave a theme but no cue the driver can act on")

    return _finish(checks, issues, {"theme": theme, "allowed_themes": sorted(allowed or [])})


#: agent name -> its scorer. ``cross_check`` keeps the strategist's signature so
#: the stage-2 harness and its tests are untouched.
ROLE_CHECKS = {
    "vehicle_engineer": engineer_check,
    "spotter": spotter_check,
    "coach": coach_check,
}


def check_for_agent(
    agent: str,
    result: dict[str, Any],
    state: RaceState,
    event,
    **kwargs: Any,
) -> dict:
    """Score one answer with whichever checker that agent's role calls for."""
    if agent in ROLE_CHECKS:
        return ROLE_CHECKS[agent](result, state, event, **kwargs)
    kwargs.pop("setup_available", None)
    kwargs.pop("telemetry_available", None)
    return cross_check(result, state, **kwargs)
