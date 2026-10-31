"""The deterministic stop plan, and the strategist policy that holds calls to it.

The Spa replay with Kimi had the strategist calling "box this lap" from lap 2 of a
window that ran to lap 13, and again right after the stop with 0.0 L to add. Two
of those calls came from the data: a margin that sagged by up to a lap inside
every lap, and a run-dry bound a lap late. These pin the fixes, offline.
"""

from __future__ import annotations

import pytest
from evals.synthetic_race import ScenarioSpec, scenario_source

from rtv.pitwall.agents.strategist import STRATEGIST, PitCall, strategist_policy
from rtv.pitwall.framework import POLICY_WITHHELD_SPOKEN, AgentRuntime, RadioPriority
from rtv.pitwall.provider import ScriptedProvider
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import (
    EventType,
    FlagPhase,
    FuelState,
    PlayerState,
    RaceEvent,
    RaceState,
    Severity,
)
from rtv.racestate.strategy_math import stop_plan


def _state(
    *,
    lap: int = 2,
    pct: float | None = 0.0,
    level: float = 47.7,
    per_lap: float = 3.7,
    capacity: float = 120.0,
    laps_to_finish: float = 16.0,
    samples: int = 5,
    active: tuple[str, ...] = (),
) -> RaceState:
    """A fuel picture shaped like the Spa race at the lap-2 line."""
    if per_lap <= 0:
        return RaceState(
            player=PlayerState(lap=lap, lap_dist_pct=pct), fuel=FuelState(level=level)
        )
    laps_remaining = level / per_lap
    margin = laps_remaining - laps_to_finish
    left = 1.0 if pct is None else 1.0 - pct
    shortfall = laps_to_finish - left - capacity / per_lap
    state = RaceState(
        player=PlayerState(lap=lap, lap_dist_pct=pct),
        fuel=FuelState(
            level=level,
            per_lap=per_lap,
            capacity=capacity,
            samples=samples,
            laps_remaining=laps_remaining,
            laps_to_finish=laps_to_finish,
            margin_laps=margin,
            pit_window_earliest_lap=lap + max(0, int(shortfall + 0.999)),
            pit_window_latest_lap=lap + int(laps_remaining - left + 1e-3),
            window_open=margin < 0,
        ),
    )
    state.flags.phase = FlagPhase.GREEN
    state.flags.active = list(active)
    return state


def _event(event_type: EventType = EventType.PIT_WINDOW_OPEN, **payload) -> RaceEvent:
    return RaceEvent(
        event_type=event_type,
        tick=1,
        session_time=230.0,
        lap=2,
        severity=Severity.ADVISORY,
        payload=payload,
    )


def _call(recommendation: str, target_lap: int | None = None) -> PitCall:
    return PitCall(
        priority=RadioPriority.ADVISORY,
        spoken_text="Copy.",
        detail_text="No figures.",
        recommendation=recommendation,
        target_lap=target_lap,
        confidence="high",
        rationale="No figures.",
    )


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------
def test_an_open_window_plans_the_latest_lap_with_a_reserve():
    """12.9 laps aboard at the lap-2 line reach the end of lap 13 with 0.9 spare."""
    plan = stop_plan(_state())
    assert plan.grounded and plan.stop_required
    assert plan.latest_lap == 13
    assert plan.target_lap == 13
    assert plan.call == "stop_later"
    assert plan.laps_until_target == 11
    assert plan.fuel_to_add_l and plan.fuel_to_add_l > 0


def test_the_plan_counts_down_to_box_this_lap():
    """8.45 L at 3.975 L/lap on the lap-12 line: lap 12 is the stop (Spa's in-lap)."""
    state = _state(lap=12, level=8.45, per_lap=3.975, laps_to_finish=6.0)
    assert stop_plan(state).call == "box_this_lap"
    assert stop_plan(state).target_lap == 12
    earlier = _state(lap=11, level=12.44, per_lap=3.965, laps_to_finish=7.0)
    assert stop_plan(earlier).call == "box_next_lap"


def test_a_tank_that_reaches_the_flag_needs_no_stop():
    plan = stop_plan(_state(lap=14, level=21.0, per_lap=3.975, laps_to_finish=4.0))
    assert plan.call == "no_stop_needed"
    assert plan.stop_required is False
    assert plan.target_lap is None


def test_no_fuel_model_is_no_data_not_a_guess():
    plan = stop_plan(_state(per_lap=0.0))
    assert plan.grounded is False and plan.call == "no_data"
    assert plan.reason


def test_few_fuel_samples_lower_the_confidence():
    assert stop_plan(_state(samples=1)).confidence == "low"
    assert stop_plan(_state(samples=5)).confidence == "high"


# --------------------------------------------------------------------------
# the engine's fuel distances
# --------------------------------------------------------------------------
def test_the_fuel_margin_holds_steady_through_a_lap():
    """It used to fall by up to a lap between crossings, then jump back."""
    source = scenario_source(ScenarioSpec())
    engine = RaceStateEngine(source="replay")
    margins: list[float] = []
    for frame in source.frames:
        engine.on_frame(frame, source.catalog)
        state = engine.snapshot()
        # Lap 2 is green and clean, with a learned per-lap figure.
        if state.player.lap == 2 and state.fuel.margin_laps is not None:
            margins.append(state.fuel.margin_laps)
    assert len(margins) > 10
    assert max(margins) - min(margins) < 0.05


# --------------------------------------------------------------------------
# the policy
# --------------------------------------------------------------------------
def test_box_now_eleven_laps_before_the_planned_stop_is_ruled_out():
    reason = strategist_policy(_call("box_now"), _state(), _event())
    assert reason and "target_lap is 13" in reason


def test_the_planned_lap_and_the_lap_before_it_are_allowed():
    state = _state(lap=12, level=8.45, per_lap=3.975, laps_to_finish=6.0)
    assert strategist_policy(_call("box_now", 12), state, _event()) is None
    assert strategist_policy(_call("plan_stop", 13), _state(), _event()) is None
    state11 = _state(lap=11, level=12.44, per_lap=3.965, laps_to_finish=7.0)
    assert strategist_policy(_call("box_now"), state11, _event()) is None


def test_a_stop_is_ruled_out_when_the_tank_reaches_the_flag():
    state = _state(lap=13, level=24.96, per_lap=3.975, laps_to_finish=5.0)
    event = _event(EventType.FUEL_MARGIN_LOW, margin_laps=1.28)
    reason = strategist_policy(_call("box_now"), state, event)
    assert reason and "no stop is needed" in reason
    assert strategist_policy(_call("stay_out"), state, event) is None


def test_a_stop_lap_past_the_run_dry_lap_is_ruled_out():
    reason = strategist_policy(_call("plan_stop", 15), _state(), _event())
    assert reason and "run-dry" in reason


def test_a_full_course_caution_justifies_an_early_stop():
    state = _state(active=("caution",))
    assert strategist_policy(_call("pit_under_yellow"), state, _event()) is None
    assert strategist_policy(_call("box_now"), state, _event()) is None
    # A local yellow is not a caution.
    local = _state(active=("yellow_waving",))
    assert strategist_policy(_call("pit_under_yellow"), local, _event())


def test_a_positive_margin_does_not_wake_the_strategist():
    state = _state(lap=13, level=24.96, per_lap=3.975, laps_to_finish=5.0)
    low = _event(EventType.FUEL_MARGIN_LOW, margin_laps=0.998)
    trigger = next(t for t in STRATEGIST.triggers if t.event_type == "fuel_margin_low")
    assert trigger.matches(low, state) is False
    short = _event(EventType.FUEL_MARGIN_LOW, margin_laps=-3.06)
    assert trigger.matches(short, _state()) is True


def test_the_strategist_sees_the_stop_plan():
    assert STRATEGIST.state_slice(_state())["stop_plan"]["target_lap"] == 13


# --------------------------------------------------------------------------
# the runtime: one repair, then withheld rather than aired
# --------------------------------------------------------------------------
async def test_a_policy_violation_is_repaired_with_the_reason():
    seen: list[str] = []

    def handler(*, call_index, messages, **_):
        if call_index == 0:
            return _call("box_now")
        seen.append(messages[-1]["content"])
        return _call("plan_stop", 13)

    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(handler))
    message = await runtime.invoke(_event(), _state())

    assert "target_lap is 13" in seen[0]
    assert message.refused is False and message.speak is True
    assert message.data["recommendation"] == "plan_stop"


async def test_an_unrepaired_violation_is_withheld_not_spoken():
    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(lambda **_: _call("box_now")))
    message = await runtime.invoke(_event(), _state())

    assert message.refused is True
    assert message.speak is False
    assert message.priority is RadioPriority.INFO
    assert message.spoken_text == POLICY_WITHHELD_SPOKEN
    assert "target_lap is 13" in message.data["withheld"]
    # Its own subject: it cannot supersede the standing pit call.
    assert message.subject.endswith(":withheld")
    assert runtime.refusals == 1


@pytest.mark.parametrize("rec", ["stay_out", "hold", "extend"])
async def test_calls_without_a_stop_are_never_policed(rec):
    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(lambda **_: _call(rec)))
    message = await runtime.invoke(_event(), _state())
    assert message.refused is False
