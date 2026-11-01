"""Stage 09 part A: the race-control detectors, read-outs, agent and checks. Offline."""

from __future__ import annotations

from dataclasses import replace

import pytest
from evals.pitwall_cases import (
    RACE_CONTROL_SCENARIO,
    ROLE_DETECTOR_CONFIG,
    build_agent_cases,
)
from evals.pitwall_checks import race_control_check
from evals.synthetic_race import ScenarioSpec, replay_scenario

from rtv.pitwall import decisions
from rtv.pitwall.agents.race_control import (
    RACE_CONTROL,
    RaceControlCall,
    race_control_policy,
    race_control_subject,
)
from rtv.pitwall.callers import (
    RACE_CONTROL_TEMPLATES,
    race_control_caller,
    spotter_caller,
)
from rtv.pitwall.framework import AgentRuntime, EventRef, RadioMessage, RadioPriority
from rtv.pitwall.provider import ScriptedProvider
from rtv.pitwall.questions import answer_lookup, route_question
from rtv.pitwall.radio import RadioFeed
from rtv.pitwall.validator import FactSet, validate_output
from rtv.racestate import RaceStateEngine
from rtv.racestate.detectors import DEFAULT_CONFIG
from rtv.racestate.models import EventType, RaceEvent, RaceState
from rtv.racestate.race_control import RaceControlMonitor, incident_limit_from, stage_for

RC_KINDS = {"incident_limit", "track_limits", "penalty", "blue_flag_ignored"}


def _run(spec: ScenarioSpec, config=ROLE_DETECTOR_CONFIG) -> RaceStateEngine:
    engine = RaceStateEngine(source="replay", config=config)
    replay_scenario(engine, spec)
    return engine


@pytest.fixture(scope="module")
def scripted() -> RaceStateEngine:
    return _run(RACE_CONTROL_SCENARIO)


def _rc_events(engine: RaceStateEngine) -> list[RaceEvent]:
    return [e for e in engine.bus.history() if e.event_type.value in RC_KINDS]


# --------------------------------------------------------------------------
# RC1: deterministic detectors
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"IncidentLimit": "17"}, (17, "session_info")),
        ({"IncidentLimit": 25}, (25, "session_info")),
        ({"IncidentLimit": "unlimited"}, (None, "unlimited")),
        ({"IncidentLimit": "0"}, (None, "unlimited")),
        ({"IncidentLimit": "lots"}, (None, "unreadable")),
        ({}, (None, "absent")),
    ],
)
def test_the_incident_limit_comes_from_the_session_info(options, expected):
    assert incident_limit_from({"WeekendInfo": {"WeekendOptions": options}}) == expected
    assert incident_limit_from(None) == (None, "absent")


def test_stages_are_two_heavy_contacts_one_and_none_left():
    assert [stage_for(n) for n in (None, 9, 8, 5, 4, 1, 0)] == [
        None, None, "approaching", "approaching", "final", "final", "reached",
    ]


def test_the_scripted_race_produces_each_race_control_event_once(scripted):
    got = [
        (e.lap, e.event_type.value, e.payload.get("stage") or e.payload.get("state"))
        for e in _rc_events(scripted)
    ]
    assert got == [
        (2, "incident_limit", "approaching"),
        (2, "track_limits", None),
        (3, "incident_limit", "final"),
        (4, "penalty", "shown"),
        (5, "penalty", "cleared"),
    ]
    limits = next(e for e in _rc_events(scripted) if e.event_type is EventType.TRACK_LIMITS)
    assert limits.payload["offs"] == 3 and limits.payload["incidents_remaining"] == 7
    final = next(e for e in _rc_events(scripted) if e.payload.get("stage") == "final")
    assert (final.payload["incidents"], final.payload["incident_limit"]) == (14, 17)
    assert final.payload["incidents_remaining"] == 3


def test_a_known_limit_rides_on_every_incident_event(scripted):
    incidents = [e for e in scripted.bus.history() if e.event_type is EventType.INCIDENT]
    assert [e.payload["incidents"] for e in incidents] == [4, 8, 10, 14]
    assert [e.payload["incidents_remaining"] for e in incidents] == [13, 9, 7, 3]
    assert [e.payload["incident_stage"] for e in incidents] == [
        None, None, "approaching", "final",
    ]


def test_the_state_carries_the_race_control_section(scripted):
    state = scripted.snapshot()
    rc = state.race_control
    assert (rc.incidents, rc.incident_limit, rc.incidents_remaining) == (14, 17, 3)
    assert rc.limit_source == "session_info" and rc.incident_stage == "final"
    assert rc.penalty == [] and rc.serve_in_pit is False
    caps = state.capabilities
    assert caps.incident_limit and caps.track_limits and caps.penalties
    assert caps.blue_flag_compliance and rc.missing == {}


def test_the_ground_truth_race_has_no_race_control_events():
    engine = _run(ScenarioSpec(), DEFAULT_CONFIG)
    assert _rc_events(engine) == []
    rc = engine.snapshot().race_control
    assert rc.limit_source == "absent" and rc.incident_limit is None
    assert rc.missing["incident_limit"] == ["WeekendInfo.WeekendOptions.IncidentLimit: absent"]


def test_a_detector_the_catalog_cannot_back_says_so():
    spec = replace(
        RACE_CONTROL_SCENARIO,
        exclude=("CarIdxLapDistPct", "PlayerCarMyIncidentCount", "PlayerTrackSurface"),
    )
    engine = _run(spec)
    state = engine.snapshot()
    caps, missing = state.capabilities, state.race_control.missing
    assert not caps.incident_limit and not caps.track_limits and not caps.blue_flag_compliance
    assert missing["incident_limit"] == ["PlayerCarMyIncidentCount"]
    assert missing["track_limits"] == ["PlayerTrackSurface"]
    assert "CarIdxLapDistPct" in missing["blue_flag_compliance"]
    # The penalty flags are still read: SessionFlags is there.
    assert caps.penalties
    assert {e.event_type.value for e in _rc_events(engine)} == {"penalty"}


def test_track_limits_re_arm_once_the_window_clears():
    monitor = RaceControlMonitor()
    fired = [
        monitor.observe_off(lap=lap, session_time=float(i), lap_dist_pct=0.5)
        for i, lap in enumerate((1, 1, 1, 1))
    ]
    assert [bool(f) for f in fired] == [False, False, True, False]  # latched
    monitor.roll_lap(5)  # every off is now outside the 3-lap window
    assert monitor.offs == []
    again = [monitor.observe_off(lap=5, session_time=10.0 + i, lap_dist_pct=0.2) for i in range(3)]
    assert [bool(f) for f in again] == [False, False, True]


def test_a_car_held_up_needs_both_corners_and_seconds_inside_the_gap():
    monitor = RaceControlMonitor()
    blue = {"car_idx": 7, "gap": 0.6, "position": 1, "laps_ahead": 1.0}
    assert monitor.observe_blue(blue, 3.00, 100.0) is None  # the episode starts
    assert monitor.observe_blue(blue, 3.20, 102.0) is None  # four corners, two seconds
    held = monitor.observe_blue(blue, 3.25, 106.0)
    assert held and held["corners"] == 5 and held["held_s"] == 6.0
    assert monitor.observe_blue(blue, 3.30, 108.0) is None  # once per car
    # A car that is merely passing (gap over a second) is not being held up.
    fresh = RaceControlMonitor()
    wide = {**blue, "car_idx": 3, "gap": 1.8}
    assert all(fresh.observe_blue(wide, 3.0 + i / 10, 100.0 + 3 * i) is None for i in range(6))


def test_a_lapping_car_that_drives_off_is_not_flagged():
    from tests.helpers import field_spec

    engine = _run(field_spec(), DEFAULT_CONFIG)
    kinds = [e.event_type for e in engine.bus.history()]
    assert EventType.BLUE_FLAG in kinds and EventType.BLUE_FLAG_IGNORED not in kinds


# --------------------------------------------------------------------------
# RC2: the template read-outs
# --------------------------------------------------------------------------
def _event(kind: str, payload: dict, t: float = 50.0) -> RaceEvent:
    return RaceEvent(event_type=EventType(kind), tick=int(t * 60), session_time=t, payload=payload)


@pytest.mark.parametrize(
    ("kind", "payload", "spoken"),
    [
        (
            "incident",
            {"incidents": 9, "delta": 1, "incident_limit": 17, "incidents_remaining": 8,
             "incident_stage": "approaching", "lap_dist_pct": 0.4},
            "Incident, 9 of 17, 8 to the limit.",
        ),
        (
            "incident",
            {"incidents": 4, "delta": 4, "incident_limit": 17, "incidents_remaining": 13,
             "incident_stage": None, "lap_dist_pct": 0.4},
            "Incident, 4 of 17.",
        ),
        (
            "incident",
            {"incidents": 17, "delta": 2, "incident_limit": 17, "incidents_remaining": 0,
             "incident_stage": "reached", "lap_dist_pct": 0.4},
            "Incident, 17 of 17, that's the limit.",
        ),
        ("penalty", {"flag": "black", "state": "shown", "active": ["black"]},
         "Black flag. Serve it in the pit lane."),
        ("penalty", {"flag": "repair", "state": "shown", "active": ["repair"]},
         "Meatball flag, pit for repairs."),
        ("penalty", {"flag": "black", "state": "cleared", "active": []}, "Black flag cleared."),
        ("track_limits", {"offs": 3, "window_laps": 3, "threshold": 3, "laps": [4, 5]},
         "Track limits, 3 offs in 3 laps."),
        ("blue_flag_ignored",
         {"car_idx": 7, "position": 1, "gap": 0.6, "laps_ahead": 1.0, "corners": 4,
          "held_s": 6.0, "threshold_corners": 3},
         "Blue flag, P1 held up 4 corners, let them by."),
    ],
)
def test_each_read_out_is_exact_and_grounded_by_its_event_alone(kind, payload, spoken):
    message = race_control_caller().call(_event(kind, payload), RaceState())
    assert message is not None and message.spoken_text == spoken
    assert message.agent == "race_control" and message.source == "template"
    facts = FactSet()
    facts.add("event", payload)
    assert validate_output(message.spoken_text, message.detail_text, facts).ok


def test_incident_read_outs_need_a_limit_and_then_the_spotter_stays_quiet():
    plain = {"incidents": 4, "delta": 2, "lap_dist_pct": 0.31}
    limited = {**plain, "incident_limit": 17, "incidents_remaining": 13, "incident_stage": None}
    assert race_control_caller().call(_event("incident", plain), RaceState()) is None
    assert spotter_caller().call(_event("incident", plain), RaceState()).spoken_text == (
        "Incident, that's 4x."
    )
    assert spotter_caller().call(_event("incident", limited), RaceState()) is None
    assert race_control_caller().call(_event("incident", limited), RaceState()) is not None


def test_the_incident_limit_stage_has_no_read_out_of_its_own():
    assert EventType.INCIDENT_LIMIT.value not in RACE_CONTROL_TEMPLATES


# --------------------------------------------------------------------------
# RC2: the LLM agent wakes only on a consequence
# --------------------------------------------------------------------------
def test_the_agent_is_woken_only_by_a_consequence(scripted):
    state = scripted.snapshot()
    runtime = AgentRuntime(RACE_CONTROL, provider=None)
    woken = []
    for event in scripted.bus.history():
        trigger = runtime.match(event, state)
        if trigger is not None:
            runtime.arm(trigger, event)
            woken.append((event.event_type.value, event.payload.get("stage")))
    # Not the incidents themselves, not "approaching", not the cleared flag.
    assert woken == [("track_limits", None), ("incident_limit", "final"), ("penalty", None)]


def test_its_golden_set_has_a_case_per_trigger():
    cases = build_agent_cases("race_control")
    names = [c.name for c in cases]
    assert names[:3] == [
        "scenario-race_control-01-track_limits",
        "scenario-race_control-02-incident_limit",
        "scenario-race_control-03-penalty",
    ]
    assert {c.event.event_type.value for c in cases} >= {
        t.event_type for t in RACE_CONTROL.triggers
    }


def test_the_slice_is_flags_incidents_penalties_and_never_fuel_or_plan(scripted):
    slice_ = RACE_CONTROL.state_slice(scripted.snapshot())
    assert set(slice_) == {
        "version", "flags", "race_control", "player", "session", "standings", "capabilities",
    }
    assert slice_["race_control"]["incident_limit"] == 17
    assert "fuel" not in slice_ and "plan" not in slice_ and "tyres" not in slice_


def _call(**over) -> RaceControlCall:
    base = {
        "priority": "critical",
        "spoken_text": "Three incidents left, keep it clean.",
        "detail_text": "14 of 17.",
        "consequence": "incident_limit",
        "incidents": 14,
        "incident_limit": 17,
        "confidence": "high",
    }
    return RaceControlCall(**{**base, **over})


def test_the_policy_holds_counts_exact_and_penalties_to_the_flags(scripted):
    state = scripted.snapshot()
    event = _event("incident_limit", {"stage": "final"})
    assert race_control_policy(_call(), state, event) is None
    assert "13" in race_control_policy(_call(incidents=13), state, event)
    assert "limit 25" in race_control_policy(_call(incident_limit=25), state, event)
    no_flag = race_control_policy(_call(consequence="penalty", penalty="black"), state, event)
    assert "no penalty flag" in no_flag
    assert "box_now" in race_control_policy(_call(pit_request="box_now"), state, event)
    shown = state.model_copy(deep=True)
    shown.race_control.penalty, shown.race_control.serve_in_pit = ["black"], True
    serve = _call(consequence="penalty", penalty="black", pit_request="box_now")
    assert race_control_policy(serve, shown, event) is None
    assert "repair" in race_control_policy(_call(penalty="repair"), shown, event)


async def test_a_misreported_count_is_withheld_not_aired(scripted):
    cases = build_agent_cases("race_control")
    case = next(c for c in cases if c.event.payload.get("stage") == "final")

    def handler(**_):
        return _call(incidents=15, spoken_text="Fifteen of seventeen, careful.").model_dump()

    message = await AgentRuntime(RACE_CONTROL, ScriptedProvider(handler)).invoke(
        case.event, case.state, events=case.events
    )
    assert message.refused and not message.speak
    assert message.spoken_text == RACE_CONTROL.withheld_text
    assert "15" in message.data["withheld"]


async def test_a_faithful_call_airs_with_its_counts(scripted):
    cases = build_agent_cases("race_control")
    case = next(c for c in cases if c.event.payload.get("stage") == "final")

    def handler(**_):
        return _call().model_dump()

    message = await AgentRuntime(RACE_CONTROL, ScriptedProvider(handler)).invoke(
        case.event, case.state, events=case.events
    )
    assert message.grounded and not message.refused and message.speak
    assert (message.data["incidents"], message.data["incident_limit"]) == (14, 17)
    assert message.subject == "race_control:incident_limit"


# --------------------------------------------------------------------------
# RC2: a penalty served in the pit lane is a pit-stop decision
# --------------------------------------------------------------------------
def _radio(agent: str, subject: str, t: float, priority=RadioPriority.ADVISORY, **data):
    return RadioMessage(
        agent=agent,
        priority=priority,
        spoken_text=f"{agent} {subject} {t}",
        detail_text="d",
        data=data,
        event_ref=EventRef(event_type="x", key=f"x:{agent}:{t}", tick=int(t * 60),
                           session_time=t),
        session_time=t,
        subject=subject,
    )


def test_a_penalty_stop_joins_the_pit_stop_group():
    event = _event("penalty", {"flag": "black", "state": "shown"})
    box = _call(consequence="penalty", penalty="black", pit_request="box_now")
    assert race_control_subject(event, box) == "penalty_stop"
    assert race_control_subject(event, _call()) == "race_control:incident_limit"
    serve = _radio("race_control", "penalty_stop", 100.0, pit_request="box_now")
    assert decisions.group_of(serve) == "pit_stop" and decisions.action_of(serve) == "box_now"
    note = _radio("race_control", "race_control:incident_limit", 100.0)
    assert decisions.group_of(note) is None


def test_the_strategists_stay_out_cannot_contradict_a_queued_critical_penalty_stop():
    feed = RadioFeed()
    serve = _radio(
        "race_control", "penalty_stop", 100.0, RadioPriority.CRITICAL, pit_request="box_now"
    )
    feed.publish(serve)
    stay = _radio("strategist", "pit_stop", 105.0, recommendation="stay_out")
    feed.publish(stay)
    assert stay.superseded and not serve.superseded
    assert [m.agent for m in feed.flush()] == ["race_control"]


# --------------------------------------------------------------------------
# RC4: the eval checks
# --------------------------------------------------------------------------
def _result(**data) -> dict:
    spoken = data.pop("spoken_text", "Three incidents left, keep it clean.")
    return {"spoken_text": spoken, "detail_text": "14 of 17.", "data": data}


def test_a_faithful_race_control_call_scores_clean():
    case = next(
        c for c in build_agent_cases("race_control") if c.event.payload.get("stage") == "final"
    )
    score = race_control_check(
        _result(consequence="incident_limit", incidents=14, incident_limit=17),
        case.state,
        case.event,
    )
    assert score["issues"] == [] and score["score"] == 1.0


def test_the_checks_catch_a_wrong_count_a_needless_call_and_an_invented_penalty():
    cases = {c.name: c for c in build_agent_cases("race_control")}
    final = cases["scenario-race_control-02-incident_limit"]
    wrong = race_control_check(
        _result(consequence="incident_limit", incidents=13, spoken_text="13x of 17."),
        final.state, final.event,
    )
    assert wrong["count_ok"] is False
    approaching = cases["race-control-approaching-only"]
    needless = race_control_check(
        _result(consequence="incident_limit"), approaching.state, approaching.event
    )
    assert needless["consequence_ok"] is False
    blind = cases["race-control-no-flags-channel"]
    invented = race_control_check(
        _result(consequence="track_limits", penalty="black", pit_request="box_now"),
        blind.state, blind.event,
    )
    assert invented["refusal_ok"] is False
    honest = race_control_check(
        _result(consequence="track_limits", spoken_text="No penalty showing, 3 incidents left."),
        blind.state, blind.event,
    )
    assert honest["refusal_ok"] and honest["count_ok"]


# --------------------------------------------------------------------------
# driver questions
# --------------------------------------------------------------------------
def test_incident_questions_are_answered_from_the_engine(scripted):
    route = route_question("How many incidents do I have?")
    assert (route.owner, route.intent) == ("race_control", "incidents")
    call = answer_lookup("incidents", scripted.snapshot())
    assert call.spoken_text == "14x of 17, 3 to the limit."
    unlimited = RaceState()
    unlimited.capabilities.incidents = True
    unlimited.player.incidents = 4
    unlimited.race_control.limit_source = "unlimited"
    assert answer_lookup("incidents", unlimited).spoken_text == (
        "4x, no incident limit this session."
    )
    assert answer_lookup("incidents", RaceState()).data.get("no_data") is True
