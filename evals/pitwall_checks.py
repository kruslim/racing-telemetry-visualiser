"""Deterministic ground-truth checks for the STRATEGIST — no LLM, no network.

The same idea as :mod:`evals.checks`, applied to a decision instead of a lap
report. The race-state engine already knows the truth: what the pit window is,
what a stop on a given lap would actually cost, whether a caution is out. So a
strategist's call is scoreable arithmetic, not a vibe:

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
    """Roll per-case results up into headline metrics."""
    if not rows:
        return {"cases": 0}
    n = len(rows)
    keys = (
        "grounding_ok",
        "radio_ok",
        "decision_ok",
        "window_ok",
        "rejoin_ok",
        "refusal_ok",
    )
    out: dict[str, Any] = {"cases": n}
    for key in keys:
        out[key.replace("_ok", "_rate")] = sum(1 for r in rows if r[key]) / n
    out["mean_score"] = sum(r["score"] for r in rows) / n
    out["total_ungrounded"] = sum(len(r["ungrounded"]) for r in rows)
    out["clean_cases"] = sum(1 for r in rows if not r["issues"])
    return out
