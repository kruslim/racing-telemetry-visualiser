"""Stage 08: the race plan. Versioning, proposals, the FactSet rules, scoping and radio.

A revision is a proposal, not a write: these tests pin that a bogus proposal changes
nothing and leaves nothing quotable, that an earlier plan value cannot back a new
proposal, that a plan figure said with the wrong unit is refused, that only the
strategist can propose and that the others see the posture only. Offline, no model.
"""

from __future__ import annotations

import pytest
from evals.synthetic_race import ScenarioSpec, replay_scenario

from rtv.grounding.units import Figure
from rtv.pitwall import tools as T
from rtv.pitwall.activity import ActivityLog
from rtv.pitwall.agents import COACH, SPOTTER, STRATEGIST, VEHICLE_ENGINEER
from rtv.pitwall.agents.strategist import strategist_slice, strategist_validity
from rtv.pitwall.agents.vehicle_engineer import vehicle_engineer_slice
from rtv.pitwall.framework import EventRef, RadioMessage, RadioPriority, ToolContext
from rtv.pitwall.radio import RadioFeed
from rtv.pitwall.validator import FactSet, validate_output
from rtv.racestate import RaceStateEngine
from rtv.racestate import plan as rp
from rtv.racestate.models import EventType, PlanElement, RaceEvent, RacePlan, RaceState

RACE = ScenarioSpec(session_num=0, sessions=("Race",))


def _engine(spec: ScenarioSpec = RACE, **kw) -> tuple[RaceStateEngine, list[RaceEvent]]:
    engine = RaceStateEngine(source="replay")
    seen: list[RaceEvent] = []
    engine.bus.subscribe_callback(seen.append)
    replay_scenario(engine, ScenarioSpec(**{**spec.__dict__, **kw}))
    return engine, seen


def _at_window() -> RaceStateEngine:
    """Mid lap 4: the window is open (laps 4-6), the plan's stop is lap 5."""
    return _engine(laps=4, end_pct=0.5)[0]


def _plan_events(events: list[RaceEvent]) -> list[RaceEvent]:
    return [e for e in events if e.event_type is EventType.PLAN_REVISED]


# --------------------------------------------------------------------------
# the baseline and versioning
# --------------------------------------------------------------------------
def test_a_baseline_plan_is_derived_as_soon_as_the_fuel_model_exists():
    engine, events = _engine(laps=4, end_pct=0.5)
    plan = engine.snapshot().plan
    assert plan is not None and plan.version == 1 and plan.segment == 1
    assert plan.stop_count.value == 1 and plan.next_stop_lap.value == 5
    assert plan.fuel_to_add_l.value == pytest.approx(3.25)
    assert plan.fuel_per_lap_target_l.value == pytest.approx(0.5)
    for name in ("stop_count", "next_stop_lap", "fuel_to_add_l", "fuel_per_lap_target_l"):
        element = getattr(plan, name)
        assert element.provenance == "derived" and element.version == 1 and element.rationale
    # Too few clean laps for a degradation fit: no pace target, no posture, a reason.
    assert plan.pace_target_s is None and plan.tyre_posture is None
    assert "clean laps" in plan.missing["pace_target_s"]
    [baseline] = _plan_events(events)
    assert baseline.lap == 2 and baseline.payload["old_version"] is None
    assert baseline.payload["new_version"] == 1 and baseline.payload["provenance"] == "derived"


def test_the_plan_survives_the_yellow_and_versions_only_move_forward():
    engine, events = _engine(laps=8)
    revised = _plan_events(events)
    versions = [e.payload["new_version"] for e in revised]
    assert versions == sorted(versions) and len(set(versions)) == len(versions)
    for before, after in zip(revised, revised[1:], strict=False):
        assert after.payload["old_version"] == before.payload["new_version"]
    # Lap 3 runs under the full-course yellow: the plan is there and unchanged through it.
    assert not [e for e in revised if e.lap == 3]
    assert engine.snapshot().plan.version == versions[-1]
    # The stop consumed the plan's stop, then the fuel reaches the flag.
    plan = engine.snapshot().plan
    assert plan.stops_done == 1 and plan.stop_count.value == 0 and plan.next_stop_lap is None
    stop = next(e for e in revised if e.payload["rationale"].startswith("Pit stop"))
    assert stop.payload["changes"]["next_stop_lap"]["to"] is None


def test_no_plan_outside_a_race_segment():
    engine, events = _engine(ScenarioSpec())  # no session type: not a race
    assert engine.snapshot().plan is None and not _plan_events(events)
    practice, events = _engine(ScenarioSpec(session_num=0, sessions=("Practice",)))
    assert practice.snapshot().plan is None and not _plan_events(events)
    view = rp.plan_view(practice.snapshot())
    assert view == {"available": False, "unavailable": view["unavailable"]}
    assert "race" in view["unavailable"]


def test_the_segment_reset_clears_the_plan():
    engine = _at_window()
    assert engine.snapshot().plan is not None
    engine.reset_segment()
    assert engine.snapshot().plan is None and engine.snapshot().laps == []


# --------------------------------------------------------------------------
# proposals
# --------------------------------------------------------------------------
def test_a_grounded_feasible_proposal_lands_as_a_new_version():
    engine = _at_window()
    events: list[RaceEvent] = []
    engine.bus.subscribe_callback(events.append)
    outcome = engine.propose_plan_revision(
        {"next_stop_lap": 4, "rationale": "Take the open window a lap early."},
        state=engine.snapshot(),
    )
    assert outcome.accepted
    plan = engine.snapshot().plan
    assert plan.version == 2
    assert plan.next_stop_lap.value == 4 and plan.next_stop_lap.provenance == "agent_proposed"
    assert plan.next_stop_lap.set_by == "strategist" and plan.next_stop_lap.version == 2
    # The refuel follows the lap, re-priced by simulate_pit_outcome (derived).
    assert plan.fuel_to_add_l.value == pytest.approx(3.0)
    assert plan.fuel_to_add_l.provenance == "derived"
    [event] = _plan_events(events)
    assert event.payload["old_version"] == 1 and event.payload["new_version"] == 2
    assert event.payload["provenance"] == "agent_proposed"
    assert event.payload["changes"]["next_stop_lap"] == {"from": 5, "to": 4}
    result = outcome.to_tool(engine.snapshot())
    assert result["accepted"] and result["plan"]["next_stop_lap"] == 4


@pytest.mark.parametrize(
    "proposal, why",
    [
        ({"next_stop_lap": 9}, "run-dry lap 6"),
        ({"next_stop_lap": 3}, "behind the car"),
        ({"stop_count": 0}, "fewer stops"),
        ({"fuel_to_add_l": 0.5}, "does not reach the flag"),
        ({"tyre_posture": "flat out"}, "tyre_posture must be"),
        ({"fuel_per_lap_target_l": 0.4123}, "not backed"),
    ],
)
def test_an_infeasible_or_ungrounded_proposal_changes_nothing(proposal, why):
    engine = _at_window()
    before = engine.snapshot().plan
    outcome = engine.propose_plan_revision({**proposal, "rationale": "r"}, state=engine.snapshot())
    assert not outcome.accepted and why in outcome.revision.reason
    after = engine.snapshot().plan
    assert after.model_dump(exclude={"revisions"}) == before.model_dump(exclude={"revisions"})
    # The log records the rejection (for the live view's history); agents never see it.
    assert after.revisions[:-1] == before.revisions
    assert not after.revisions[-1].accepted and after.revisions[-1].reason
    assert "revisions" not in rp.plan_view(engine.snapshot())


def test_a_bogus_proposal_is_not_quotable_by_the_next_agent():
    """Rejected: the plan is unchanged and neither the tool result nor the next slice
    carries the bogus figures, so the next run's validator refuses them."""
    engine = _at_window()
    log = ActivityLog(wall_clock=None)
    state = engine.snapshot()
    ctx = ToolContext(
        state=state,
        event=RaceEvent(event_type=EventType.PIT_WINDOW_OPEN, tick=1, session_time=30.0, lap=4),
        extras={"engine": engine, "activity": log},
        agent="strategist",
    )
    result = T.PROPOSE_PLAN_REVISION(
        ctx, {"next_stop_lap": 11, "fuel_to_add_l": 3.77, "rationale": "Stretch to 11."}
    )
    assert result["accepted"] is False and result["plan_version"] == 1
    # The rejection never repeats the proposed numbers (a tool result is citable).
    shown = FactSet(typed=True)
    shown.add("propose_plan_revision", result)
    assert not shown.supports_figure(Figure(11.0, "11", "lap", "lap"))
    assert not shown.supports_figure(Figure(3.77, "3.77", "litres", "l"))
    # It is in the audit trail, with the proposal.
    [rec] = [r for r in log.history() if r["kind"] == "plan_proposal_rejected"]
    assert rec["agent"] == "strategist" and rec["data"]["proposal"]["next_stop_lap"] == 11
    assert rec["data"]["plan_version"] == 1 and rec["data"]["reason"]
    # The next agent's facts: its slice of the state after the rejection.
    facts = FactSet(typed=True)
    facts.add("race_state", strategist_slice(engine.snapshot()))
    report = validate_output("Box on lap 11, add 3.77 litres.", "", facts)
    assert not report.ok and set(report.unsupported) >= {"11", "3.77"}


def test_an_earlier_plan_value_cannot_back_a_new_proposal():
    """No laundering: a figure that exists only in the plan is not a deterministic fact."""
    engine = _at_window()
    with engine._lock:  # an operator-set refuel nothing in the data derives
        engine.state.plan.fuel_to_add_l = PlanElement(
            value=2.6, unit="L", provenance="operator_set", rationale="op", version=1
        )
    assert rp.plan_view(engine.snapshot())["fuel_to_add_l"] == pytest.approx(2.6)
    outcome = engine.propose_plan_revision(
        {"fuel_per_lap_target_l": 2.6, "rationale": "Copy the plan."}, state=engine.snapshot()
    )
    assert not outcome.accepted and "earlier plan value does not count" in outcome.revision.reason


def test_a_plan_figure_said_with_the_wrong_unit_is_refused():
    """A1-style swap: the pace target is seconds, the next stop a lap number."""
    state = RaceState()
    state.plan = RacePlan(
        version=4,
        next_stop_lap=PlanElement(value=13, unit="lap", provenance="agent_proposed", version=4),
        pace_target_s=PlanElement(value=141.18, unit="s", version=3),
    )
    state.lifecycle.session_type = "race"
    facts = FactSet(typed=True)
    facts.add("race_state", strategist_slice(state))
    assert ("race_state.plan.next_stop_lap" in {p for _, _, p in facts.facts("lap")})
    assert validate_output("Box on lap 13, pace 141.18 seconds.", "", facts).ok
    assert not validate_output("Thirteen laps of fuel.", "", facts).ok
    assert not validate_output("We have 13 laps of fuel left.", "", facts).ok
    report = validate_output("Pace target 141.18 laps.", "", facts)
    assert not report.ok and "141.18" in report.unsupported


def test_refresh_reverts_an_agent_element_that_stopped_being_feasible():
    engine = _at_window()
    assert engine.propose_plan_revision(
        {"next_stop_lap": 6, "rationale": "Run to the end of the window."},
        state=engine.snapshot(),
    ).accepted
    state = engine.snapshot()
    state.fuel.pit_window_latest_lap = 5  # the fuel picture got worse
    state.fuel.laps_remaining = 1.4
    revision = rp.refresh_plan(state)
    assert revision is not None and "next_stop_lap reverted" in revision.rationale
    assert state.plan.next_stop_lap.provenance == "derived"


# --------------------------------------------------------------------------
# scoping: slices and tools, not prompt text
# --------------------------------------------------------------------------
def test_only_the_strategist_can_propose_and_the_others_read_the_posture():
    names = lambda spec: {t.name for t in spec.tools}  # noqa: E731
    assert "propose_plan_revision" in names(STRATEGIST)
    for spec in (VEHICLE_ENGINEER, COACH, SPOTTER):
        assert "propose_plan_revision" not in names(spec)
    assert "get_race_plan" in names(VEHICLE_ENGINEER) and "get_race_plan" in names(COACH)

    engine, _ = _engine(laps=11, end_pct=0.5)  # four clean laps on the new set: a posture
    state = engine.snapshot()
    view = vehicle_engineer_slice(state)["plan"]
    assert view["tyre_posture"] == "neutral" and view["pace_target_s"] == pytest.approx(10.0)
    assert view["available"] and set(view) <= {
        "available", "version", "tyre_posture", "pace_target_s", "provenance", "missing"
    }
    assert "next_stop_lap" not in view and "fuel_to_add_l" not in view
    full = strategist_slice(state)["plan"]
    assert "next_stop_lap" in full and full["version"] == view["version"]

    event = RaceEvent(event_type=EventType.LOCKUP, tick=1, session_time=1.0)
    for agent, scope in (("vehicle_engineer", "posture"), ("coach", "posture"),
                         ("strategist", "full")):
        ctx = ToolContext(state=state, event=event, agent=agent)
        assert T.GET_RACE_PLAN(ctx, {}) == rp.plan_view(state, scope)


def test_plan_revisions_are_not_detector_events():
    engine, events = _engine(laps=4, end_pct=0.5)
    ctx = ToolContext(state=engine.snapshot(), event=events[-1], events=events)
    rows = T.GET_RECENT_DETECTOR_EVENTS(ctx, {"limit": 100})["events"]
    assert rows and not [r for r in rows if r["key"] == "plan_revised"]


def test_the_projection_tools_answer_from_the_snapshot():
    engine, events = _engine(laps=11, end_pct=0.5)
    ctx = ToolContext(state=engine.snapshot(), event=events[-1], events=events)
    deg = T.GET_LAP_TIME_DEGRADATION(ctx, {"horizon_laps": 3})
    assert deg["grounded"] and deg["horizon_laps"] == 3
    assert T.GET_FUEL_SAVE_OPTIONS(ctx, {"extra_laps": 2})["options"][0]["extra_laps"] == 2
    assert "ahead" in T.GET_TRAFFIC_PROJECTION(ctx, {})
    assert "favours" in T.GET_UNDERCUT_OVERCUT(ctx, {})
    no_engine = ToolContext(state=engine.snapshot(), event=events[-1])
    assert "unavailable" in T.PROPOSE_PLAN_REVISION(no_engine, {"rationale": "r"})


# --------------------------------------------------------------------------
# radio: the pit_stop decision group and B1 validity
# --------------------------------------------------------------------------
def _call(agent, text, *, t, priority=RadioPriority.ADVISORY, subject="pit_stop", **data):
    return RadioMessage(
        agent=agent,
        priority=priority,
        spoken_text=text,
        detail_text=text,
        data=data,
        event_ref=EventRef(event_type="x", key=f"x:{text}", tick=int(t * 60), session_time=t),
        session_time=t,
        subject=subject,
    )


def test_a_revised_box_lap_supersedes_the_queued_pit_call():
    feed = RadioFeed()
    old = _call("strategist", "Plan to box on lap five.", t=30.0, recommendation="plan_stop",
                target_lap=5)
    new = _call("strategist", "Revised: box on lap four.", t=31.0, recommendation="plan_stop",
                target_lap=4)
    feed.publish(old)
    assert feed.publish(new) is old and old.superseded
    assert [m.spoken_text for m in feed.flush()] == [new.spoken_text]


def test_a_critical_damage_stop_supersedes_a_queued_plan_advisory():
    feed = RadioFeed()
    plan = _call("strategist", "Plan to box on lap five.", t=30.0, recommendation="plan_stop",
                 target_lap=5)
    damage = _call("vehicle_engineer", "Box now, front-left damage.", t=31.0,
                   priority=RadioPriority.CRITICAL, subject="damage_stop", finding="damage",
                   pit_request="box_now")
    feed.publish(plan)
    assert feed.publish(damage) is plan and plan.superseded
    assert [m.spoken_text for m in feed.flush()] == [damage.spoken_text]


def test_a_plan_stop_with_no_lap_of_its_own_is_valid_until_the_plans_lap():
    engine = _at_window()
    state = engine.snapshot()
    message = _call("strategist", "Stop is planned.", t=state.session_time,
                    recommendation="plan_stop")
    event = RaceEvent(event_type=EventType.PIT_WINDOW_OPEN, tick=1, session_time=1.0)
    rules = strategist_validity(message, state, event, 0.9)
    assert rules["valid_until_lap"] == 5 and rules["invalid_if_lap_dist_past"] == 0.9
