"""B3: calls from different agents on one decision share a supersede group.

The feed rules (newer replaces queued, critical replaces queued advisory, a
contradiction of a higher-priority standing call is dropped), what consumers and
the journal see, the one-line note the losing agent gets on its next run, and an
end-to-end run of two scripted agents through the orchestrator. Offline.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from rtv.pitwall import agent_input, decisions
from rtv.pitwall.activity import ActivityLog
from rtv.pitwall.agents import STRATEGIST, VEHICLE_ENGINEER
from rtv.pitwall.agents.strategist import PitCall
from rtv.pitwall.agents.vehicle_engineer import EngineerCall, vehicle_engineer_subject
from rtv.pitwall.framework import EventRef, RadioMessage, RadioPriority
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ScriptedProvider
from rtv.pitwall.radio import RadioFeed
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.store.run_stats import compare_markdown, run_stats

ADV, CRIT, INFO = RadioPriority.ADVISORY, RadioPriority.CRITICAL, RadioPriority.INFO


def _msg(
    agent: str,
    text: str,
    *,
    priority: RadioPriority = ADV,
    subject: str | None = None,
    t: float = 100.0,
    **data,
) -> RadioMessage:
    default = {"strategist": "pit_stop", "vehicle_engineer": "damage_stop"}.get(agent, agent)
    return RadioMessage(
        agent=agent,
        priority=priority,
        spoken_text=text,
        detail_text=text,
        data=data,
        event_ref=EventRef(event_type="x", key=f"x:{agent}:{text}", tick=int(t * 60),
                           session_time=t),
        session_time=t,
        subject=subject if subject is not None else default,
    )


def _box(priority=ADV, t=100.0, text="Box this lap, the front-left is losing pressure."):
    return _msg("vehicle_engineer", text, priority=priority, t=t, finding="damage",
                pit_request="box_now")


def _stay(priority=ADV, t=100.0, text="Stay out, no stop needed."):
    return _msg("strategist", text, priority=priority, t=t, recommendation="stay_out")


def _feed():
    log = ActivityLog(wall_clock=None)
    feed = RadioFeed(tracer=log)
    seen: list[RadioMessage] = []
    feed.subscribe_callback(seen.append)
    return feed, log, seen


def _kinds(log, kind):
    return [r for r in log.history() if r["kind"] == kind]


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------
def test_the_pit_stop_group_spans_strategist_and_engineer_box_calls():
    assert decisions.group_of(_stay()) == "pit_stop"
    assert decisions.group_of(_box()) == "pit_stop"
    tyres = _msg("vehicle_engineer", "Fronts are hot.", subject="tyres")
    assert decisions.group_of(tyres) is None
    assert decisions.group_of(_msg("coach", "Brake earlier.", subject="coaching")) is None
    assert decisions.action_of(_box()) == "box_now"
    assert decisions.action_of(_stay()) == "stay_out"
    plan = _msg("strategist", "Plan to box.", recommendation="plan_stop")
    assert decisions.action_of(plan) == "stop_later"
    assert decisions.action_of(_msg("strategist", "No data.", recommendation="hold")) is None
    assert decisions.opposed("box_now", "stay_out") and decisions.opposed("box_now", "stop_later")
    assert not decisions.opposed("box_now", "box_now") and not decisions.opposed("box_now", None)


def test_the_engineer_joins_the_decision_only_when_it_asks_to_box_now():
    event = RaceEvent(event_type=EventType.CAR_HEALTH_WARNING, tick=1, session_time=1.0)
    base = {"priority": ADV, "spoken_text": "s", "detail_text": "d", "confidence": "high",
            "rationale": "r"}
    assert vehicle_engineer_subject(event, EngineerCall(**base, finding="damage")) == "car_health"
    box = EngineerCall(**base, finding="damage", pit_request="box_now")
    assert vehicle_engineer_subject(event, box) == "damage_stop"
    tyre = EngineerCall(**base, finding="tyre_pressures", pit_request="box_now")
    assert vehicle_engineer_subject(event, tyre) == "tyre_stop"


# --------------------------------------------------------------------------
# the feed rules
# --------------------------------------------------------------------------
def test_engineer_box_vs_strategist_stay_out_the_newer_replaces_the_queued_one():
    feed, log, seen = _feed()
    box = _box()
    feed.publish(box)
    stay = _stay(t=105.0)
    assert feed.publish(stay) is box
    assert [m.spoken_text for m in feed.flush()] == [stay.spoken_text]
    assert box.superseded and not box.speak and box.superseded_by == stay.message_id
    assert "strategist" in box.superseded_reason
    [rec] = _kinds(log, "radio_superseded")
    assert rec["data"]["by_message_id"] == stay.message_id
    assert rec["data"]["rule"] == "group" and rec["data"]["group"] == "pit_stop"
    assert seen[0] is box  # consumers are told, so the journal and the review show it
    assert feed.take_superseded("vehicle_engineer")[0]["by_agent"] == "strategist"


def test_a_critical_box_replaces_a_queued_advisory_from_another_agent():
    feed, log, _ = _feed()
    plan = _msg("strategist", "Plan to box at the end of the stint.",
                recommendation="plan_stop")
    feed.publish(plan)
    box = _box(priority=CRIT, t=101.0)
    feed.publish(box)
    assert feed.pending() == [box]
    assert plan.superseded and plan.superseded_by == box.message_id
    assert _kinds(log, "radio_superseded")[0]["data"]["rule"] == "critical"
    assert "critical" in plan.superseded_reason


def test_a_contradiction_of_a_queued_critical_call_is_dropped_and_journalled():
    feed, log, seen = _feed()
    box = _box(priority=CRIT)
    feed.publish(box)
    stay = _stay(t=110.0)
    assert feed.publish(stay) is None
    assert [m.spoken_text for m in feed.flush()] == [box.spoken_text]
    assert stay.superseded and stay.superseded_by == box.message_id
    assert "still queued" in stay.superseded_reason
    [rec] = _kinds(log, "radio_superseded")
    assert rec["data"]["message_id"] == stay.message_id
    assert rec["data"]["by_message_id"] == box.message_id and rec["data"]["rule"] == "conflict"
    queued = [r["data"]["message_id"] for r in _kinds(log, "radio_queued")]
    assert stay.message_id not in queued
    assert feed.stats()["conflicts"] == 1
    assert stay in seen and stay not in feed.history()


def test_a_contradiction_of_an_aired_critical_call_inside_the_window_is_dropped():
    feed, _, _ = _feed()
    feed.publish(_box(priority=CRIT, t=100.0))
    feed.flush()
    soon = _stay(t=100.0 + decisions.CONFLICT_WINDOW_S - 1)
    feed.publish(soon)
    assert soon.superseded and "aired 59 s before" in soon.superseded_reason
    later = _stay(t=100.0 + decisions.CONFLICT_WINDOW_S + 1, text="Stay out, it held.")
    feed.publish(later)
    assert not later.superseded
    assert [m.spoken_text for m in feed.flush()] == ["Stay out, it held."]


def test_an_equal_or_higher_priority_contradiction_is_a_revision_and_airs():
    feed, _, _ = _feed()
    feed.publish(_box())
    feed.flush()
    stay = _stay(t=110.0)
    feed.publish(stay)
    assert not stay.superseded and feed.pending() == [stay]


def test_a_call_that_takes_no_side_is_not_a_contradiction():
    feed, _, _ = _feed()
    feed.publish(_box(priority=CRIT))
    hold = _msg("strategist", "Hold, no fuel figure yet.", recommendation="hold", t=101.0)
    feed.publish(hold)
    assert not hold.superseded and len(feed.pending()) == 2


def test_a_lower_priority_call_never_displaces_a_higher_one_in_the_group():
    feed, _, _ = _feed()
    box = _box()
    feed.publish(box)
    agree = _msg("strategist", "Box now, fuel only.", priority=INFO, recommendation="box_now")
    feed.publish(agree)
    assert not box.superseded and not agree.superseded and len(feed.pending()) == 2


def test_a_queued_critical_call_is_never_superseded():
    feed, _, _ = _feed()
    box = _box(priority=CRIT)
    feed.publish(box)
    feed.publish(_msg("strategist", "Box now under the yellow.", priority=CRIT,
                      recommendation="pit_under_yellow"))
    assert not box.superseded and len(feed.pending()) == 2


def test_same_agent_rules_are_unchanged():
    feed, log, _ = _feed()
    first = _msg("strategist", "Plan lap 12.", recommendation="plan_stop")
    second = _msg("strategist", "Plan lap 13.", recommendation="plan_stop")
    feed.publish(first)
    assert feed.publish(second) is first
    assert feed.take_superseded("strategist") == []  # its own revision: no note
    crit = _msg("strategist", "Box now, out of fuel.", priority=CRIT, recommendation="box_now")
    feed.publish(crit)
    assert not second.superseded  # a critical call replaces nothing of its own agent's
    assert [m.spoken_text for m in feed.flush()] == ["Box now, out of fuel.", "Plan lap 13."]
    # Outside any group: per agent and subject, as before, and a contradiction
    # between subjects is not this module's business.
    coach = [_msg("coach", "Brake earlier.", subject="coaching"),
             _msg("coach", "Brake later.", subject="coaching")]
    tyres = _msg("vehicle_engineer", "Fronts are hot.", subject="tyres")
    for m in [*coach, tyres]:
        feed.publish(m)
    assert [m.spoken_text for m in feed.pending()] == ["Brake later.", "Fronts are hot."]
    assert _kinds(log, "radio_superseded")[-1]["data"]["rule"] == "same_agent"


def test_the_note_is_handed_out_once_and_cleared_with_the_feed():
    feed, _, _ = _feed()
    feed.publish(_box(priority=CRIT))
    feed.publish(_stay(t=101.0))
    [note] = feed.take_superseded("strategist")
    assert note["by_agent"] == "vehicle_engineer" and note["by_priority"] == "critical"
    assert feed.take_superseded("strategist") == []
    feed.publish(_stay(t=102.0, text="Stay out, really."))
    feed.clear()
    assert feed.take_superseded("strategist") == []


# --------------------------------------------------------------------------
# the agent's next context
# --------------------------------------------------------------------------
NOTE = {
    "message_id": "a1",
    "spoken_text": "Stay out, no stop needed.",
    "by_message_id": "b2",
    "by_agent": "vehicle_engineer",
    "by_priority": "critical",
    "by_spoken_text": "Box this lap, the front-left is losing pressure.",
    "reason": "contradicts the vehicle_engineer's critical pit_stop call (still queued)",
}
EVENT = {"key": "pit_window_open:L7", "severity": "advisory", "session_time": 842.5, "lap": 7,
         "payload": {}}


def test_the_note_is_one_line_in_the_instruction_and_absent_without_one():
    plain = agent_input.instruction(EVENT)
    noted = agent_input.instruction(EVENT, superseded=[NOTE])
    assert plain in noted.replace("\n\n" + agent_input.superseded_line(NOTE), "")
    line = agent_input.superseded_line(NOTE)
    assert "\n" not in line and line.startswith("NOT AIRED:")
    assert NOTE["spoken_text"] in line and NOTE["by_spoken_text"] in line


def test_the_note_is_recorded_on_run_start_and_rebuilds_the_first_call():
    kw = {"system_prompt": "p", "state_slice": {}, "event": EVENT}
    without = agent_input.build_input(STRATEGIST, **kw)
    assert "superseded" not in without
    record = agent_input.build_input(STRATEGIST, **kw, superseded=[NOTE])
    assert record["superseded"] == [NOTE]
    content = agent_input.model_input(record)["messages"][0]["content"]
    assert content == agent_input.instruction(EVENT, superseded=[NOTE])


# --------------------------------------------------------------------------
# end to end: two scripted agents through the orchestrator
# --------------------------------------------------------------------------
def _answer(output_model, **_):
    if output_model is EngineerCall:
        return EngineerCall(
            priority=CRIT,
            spoken_text="Box this lap, the front-left is losing pressure.",
            detail_text="Front-left pressure falling.",
            finding="damage",
            pit_request="box_now",
            confidence="high",
            rationale="The car reports damage.",
        )
    return PitCall(
        priority=ADV,
        spoken_text="Stay out, no stop needed.",
        detail_text="Stay out.",
        recommendation="stay_out",
        confidence="high",
        rationale="No figures asserted.",
    )


def _event(event_type: EventType, t: float) -> RaceEvent:
    return RaceEvent(event_type=event_type, tick=int(t * 60), session_time=t, lap=7,
                     severity=Severity.ADVISORY, payload={})


@pytest.mark.asyncio
async def test_scripted_engineer_box_beats_strategist_stay_out_and_strategist_is_told():
    engine = RaceStateEngine(source="replay")
    log = ActivityLog(wall_clock=None)
    provider = ScriptedProvider(_answer)
    agents = [replace(VEHICLE_ENGINEER, prefetch=()), replace(STRATEGIST, prefetch=())]
    orch = PitwallOrchestrator(engine, provider, agents, feed=RadioFeed(tracer=log), clock=None,
                               activity=log)
    box = await orch._run_agent("vehicle_engineer", _event(EventType.CAR_HEALTH_WARNING, 100.0))
    orch.publish(box)
    stay = await orch._run_agent("strategist", _event(EventType.PIT_WINDOW_OPEN, 105.0))
    orch.publish(stay)
    assert box.subject == "damage_stop" and box.priority is CRIT
    assert stay.superseded and stay.superseded_by == box.message_id
    assert [m.message_id for m in orch.feed.flush()] == [box.message_id]

    await orch._run_agent("strategist", _event(EventType.PIT_WINDOW_OPEN, 130.0))
    last = provider.calls[-1]["messages"][0]["content"]
    assert 'NOT AIRED: your call "Stay out, no stop needed." was dropped' in last
    starts = [r for r in log.history() if r["kind"] == "run_start"]
    assert starts[-1]["data"]["input"]["superseded"][0]["by_message_id"] == box.message_id
    assert "superseded" not in starts[1]["data"]["input"]  # the first strategist run had none
    # The engine's run_stats count it, and compare_runs prints it.
    stats = run_stats(log.history())
    assert stats["radio_superseded"] == 1 and stats["radio_conflicts"] == 1
    assert stats["radio_superseded_cross_agent"] == 1
    assert "radio conflicts dropped" in compare_markdown([stats, stats], ["a", "b"])
