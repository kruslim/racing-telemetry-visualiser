"""Stage-3 role agents: the vehicle engineer, the spotter and the coach.

All offline, with a scripted provider standing in for Claude. What is under test
is that adding three agents really was configuration -- that they route, that
their tools work, and above all that each one is *scoped*: shown only the state
its job needs, given only the tools that answer its question, and refused when it
says a number neither one supports.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from evals.pitwall_cases import build_agent_cases, build_all_cases
from evals.pitwall_checks import (
    aggregate,
    check_for_agent,
    coach_check,
    engineer_check,
    spotter_check,
)

from rtv.pitwall.agents import AGENT_REGISTRY, build_agents
from rtv.pitwall.agents.coach import COACH, CoachCall
from rtv.pitwall.agents.spotter import SPOTTER, SpotterCall
from rtv.pitwall.agents.strategist import STRATEGIST
from rtv.pitwall.agents.vehicle_engineer import VEHICLE_ENGINEER, EngineerCall
from rtv.pitwall.framework import AgentRuntime, RadioPriority, ToolContext
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ScriptedProvider, tool_use_turn
from rtv.pitwall.radio import RadioFeed
from rtv.pitwall.tools import ALL_TOOLS, TOOLS_BY_NAME
from rtv.racestate import RaceStateEngine
from rtv.racestate.detectors import DEFAULT_CONFIG
from rtv.racestate.models import EventType, RaceEvent, RaceState, Severity
from rtv.racestate.scenario import (
    ScenarioSpec,
    scenario_catalog,
    scenario_frames,
    scenario_session_info,
)

ROLE_SPEC = ScenarioSpec(lockup_laps=(1, 2), corner_pcts=(0.44,))
ROLE_CONFIG = replace(
    DEFAULT_CONFIG, stint_milestone_laps=2, tyre_temp_trend_c_per_lap=1.0
)
ROLES = (VEHICLE_ENGINEER, SPOTTER, COACH)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine() -> RaceStateEngine:
    catalog = scenario_catalog(ROLE_SPEC)
    engine = RaceStateEngine(source="replay", config=ROLE_CONFIG)
    engine.bind_catalog(catalog, session_id="roles")
    engine.set_session_info(scenario_session_info(ROLE_SPEC))
    for frame in scenario_frames(ROLE_SPEC):
        engine.on_frame(frame, catalog)
    return engine


@pytest.fixture()
def state(engine) -> RaceState:
    return engine.snapshot()


@pytest.fixture(scope="module")
def events(engine) -> list[RaceEvent]:
    return engine.bus.history(200)


@pytest.fixture(scope="module")
def recurrence(events) -> RaceEvent:
    return next(e for e in events if e.event_type is EventType.RECURRING_ISSUE)


def _event(event_type: str, payload: dict | None = None, **kw) -> RaceEvent:
    return RaceEvent(
        event_type=EventType(event_type),
        tick=kw.get("tick", 2000),
        session_time=kw.get("session_time", 40.0),
        lap=kw.get("lap", 4),
        severity=kw.get("severity", Severity.ADVISORY),
        payload=payload or {},
    )


def _ctx(state, event, events=(), **extras) -> ToolContext:
    return ToolContext(
        state=state,
        event=event,
        events=list(events),
        config={"corner_buckets": ROLE_CONFIG.corner_buckets},
        extras=extras,
    )


# --------------------------------------------------------------------------
# the registry is still just configuration
# --------------------------------------------------------------------------
def test_the_registry_holds_four_distinct_agents():
    names = [spec.name for spec in AGENT_REGISTRY]
    assert names == ["strategist", "vehicle_engineer", "spotter", "coach"]
    assert len(set(names)) == len(names)


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.name)
def test_every_trigger_names_a_real_event_type(spec):
    known = {e.value for e in EventType}
    assert {t.event_type for t in spec.triggers} <= known


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.name)
def test_every_tool_comes_from_the_shared_registry(spec):
    registry = {t.name for t in ALL_TOOLS}
    assert {t.name for t in spec.tools} <= registry
    assert all(TOOLS_BY_NAME[t.name] is t for t in spec.tools)


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.name)
def test_every_agent_describes_itself_for_the_status_endpoint(spec):
    described = spec.describe()
    assert described["name"] == spec.name
    assert described["triggers"] and described["tools"]


def test_build_agents_can_mount_a_subset_and_retier_the_models():
    agents = build_agents(
        models={"spotter": "claude-test-1"}, only=["spotter", "coach"]
    )
    assert [a.name for a in agents] == ["spotter", "coach"]
    assert agents[0].model == "claude-test-1"


# --------------------------------------------------------------------------
# scoping: the state slice is a permission
# --------------------------------------------------------------------------
def test_the_spotter_cannot_see_fuel_or_tyres_at_all(state):
    shown = set(SPOTTER.state_slice(state))
    assert "fuel" not in shown and "tyres" not in shown and "car_health" not in shown
    assert "standings" in shown and "flags" in shown


def test_the_vehicle_engineer_is_not_shown_the_race_it_is_not_running(state):
    shown = set(VEHICLE_ENGINEER.state_slice(state))
    assert "fuel" not in shown and "standings" not in shown
    assert {"tyres", "car_health"} <= shown


def test_the_coach_is_not_shown_strategy(state):
    shown = set(COACH.state_slice(state))
    assert "fuel" not in shown and "standings" not in shown
    assert {"player", "tyres"} <= shown


@pytest.mark.parametrize("spec", ROLES, ids=lambda s: s.name)
def test_no_role_agent_can_reach_the_strategists_pit_projection(spec):
    assert "simulate_pit_outcome" not in {t.name for t in spec.tools}
    assert "get_fuel_projection" not in {t.name for t in spec.tools}


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------
def _routes(engine, event, state):
    orch = PitwallOrchestrator(
        engine, ScriptedProvider(lambda **_: None), list(AGENT_REGISTRY),
        feed=RadioFeed(), clock=None,
    )
    return set(orch.dispatch(event, state))


def test_a_recurrence_wakes_the_car_and_the_driver_but_not_strategy(
    engine, state, recurrence
):
    assert _routes(engine, recurrence, state) == {"vehicle_engineer", "coach"}


def test_a_tyre_finding_is_the_engineers_alone(engine, state):
    event = _event("tyre_out_of_band", {"measure": "tyre_temp_trend", "corner": "LF"})
    assert _routes(engine, event, state) == {"vehicle_engineer"}


def test_a_traffic_call_is_the_spotters_alone(engine, state):
    event = _event("traffic_close", {"car_idx": 1, "gap": 0.5, "side": "behind"})
    assert _routes(engine, event, state) == {"spotter"}


def test_a_caution_wakes_both_the_strategist_and_the_spotter(engine, state):
    event = _event("flag_change", {"from": "green", "to": "yellow"})
    assert _routes(engine, event, state) == {"strategist", "spotter"}


def test_a_single_lockup_wakes_nobody(engine, state):
    event = _event("lockup", {"slip": 0.4, "wheel": "LF", "lap_dist_pct": 0.40})
    assert _routes(engine, event, state) == set()


def test_the_coach_speaks_a_quarter_as_often_as_the_engineer():
    """Both take the same recurrence; the cooldowns keep it from becoming a chat."""

    def cooldown(spec, event_type):
        return next(t.cooldown_s for t in spec.triggers if t.event_type == event_type)

    engineer = cooldown(VEHICLE_ENGINEER, "recurring_issue")
    coach = cooldown(COACH, "recurring_issue")
    assert coach >= 2 * engineer


def test_the_coach_stays_quiet_under_a_caution(state, recurrence):
    yellow = state.model_copy(deep=True)
    yellow.flags.phase = yellow.flags.phase.__class__.YELLOW
    runtime = AgentRuntime(COACH, provider=None)
    off = _event("offtrack", {"surface": "off_track", "lap_dist_pct": 0.5})
    assert runtime.match(off, yellow) is None
    assert runtime.match(off, state) is not None


# --------------------------------------------------------------------------
# the new tools
# --------------------------------------------------------------------------
def test_recent_detector_events_filters_and_bounds(state, events, recurrence):
    tool = TOOLS_BY_NAME["get_recent_detector_events"]
    out = tool(_ctx(state, recurrence, events), {"types": ["lockup"], "limit": 2})
    assert out["available"] == 3
    assert out["returned"] == 2
    assert all(row["key"] == "lockup" for row in out["events"])


def test_recent_detector_events_says_so_when_there_are_none(state, recurrence):
    tool = TOOLS_BY_NAME["get_recent_detector_events"]
    out = tool(_ctx(state, recurrence, []), {"types": ["incident"]})
    assert out["events"] == []
    assert "unavailable" in out


def test_car_health_reports_the_thresholds_it_was_judged_against(state, recurrence):
    out = TOOLS_BY_NAME["get_car_health"](_ctx(state, recurrence), {})
    assert out["thresholds"]["oil_temp_max_c"] == DEFAULT_CONFIG.oil_temp_max_c
    assert out["oil_temp"] == state.car_health.oil_temp


def test_car_health_says_so_when_the_catalog_has_no_health_channels(state, recurrence):
    blind = state.model_copy(deep=True)
    blind.capabilities.car_health = False
    out = TOOLS_BY_NAME["get_car_health"](_ctx(blind, recurrence), {})
    assert "unavailable" in out


def test_setup_snapshot_reads_through_to_the_engine(engine, state, recurrence):
    out = TOOLS_BY_NAME["get_setup_snapshot"](
        _ctx(state, recurrence, engine=engine), {}
    )
    assert out["available"] is True
    assert "Chassis" in out["setup"]


def test_setup_snapshot_refuses_rather_than_guesses_without_an_engine(state, recurrence):
    out = TOOLS_BY_NAME["get_setup_snapshot"](_ctx(state, recurrence), {})
    assert out["available"] is False
    assert "unavailable" in out


def test_corner_detail_finds_the_live_events_at_that_corner(state, events, recurrence):
    out = TOOLS_BY_NAME["get_corner_detail"](_ctx(state, recurrence, events), {})
    assert out["live"]["corner"] == "C08"
    assert out["live"]["count"] >= 3
    assert out["telemetry"]["available"] is False  # nothing recorded here
    assert "unavailable" in out["telemetry"]


def test_corner_detail_accepts_an_explicit_corner_label(state, events, recurrence):
    out = TOOLS_BY_NAME["get_corner_detail"](
        _ctx(state, recurrence, events), {"corner": "C13"}
    )
    assert out["live"]["corner"] == "C13"
    assert out["live"]["count"] == 0


def test_corner_detail_reuses_the_layer_1_coaching_service(tmp_path, state, events, recurrence):
    """The same deterministic corner analysis the v1 coaching endpoints serve."""
    from rtv.coaching.features import CoachingService
    from rtv.racestate.scenario import seed_scenario_session
    from rtv.store.duck import Database
    from rtv.store.repository import Repository
    from rtv.store.writer import TelemetryWriter

    parquet = tmp_path / "parquet"
    parquet.mkdir()
    db = Database(tmp_path / "telemetry.duckdb")
    seed_scenario_session(TelemetryWriter(db, parquet), "stored", ROLE_SPEC)
    repo = Repository(db, parquet)
    try:
        out = TOOLS_BY_NAME["get_corner_detail"](
            _ctx(
                state, recurrence, events,
                coaching=CoachingService(repo), repo=repo, session_id="stored",
            ),
            {},
        )
    finally:
        db.close()
    trace = out["telemetry"]
    assert trace["available"] is True
    assert trace["session_id"] == "stored"
    assert trace["corner"]["min_speed_kmh"] > 0
    # The scripted corner sits just after the braking zone the lockups are in.
    assert 0.40 <= trace["corner"]["lap_dist_pct"] <= 0.50


# --------------------------------------------------------------------------
# the runtime: grounding is enforced identically for every role
# --------------------------------------------------------------------------
def _engineer(**kw) -> EngineerCall:
    base = dict(
        priority=RadioPriority.ADVISORY,
        spoken_text="Fronts are taking it at the same corner.",
        detail_text="No figures asserted.",
        finding="brake_lockup",
        confidence="medium",
        rationale="Pattern only.",
    )
    base.update(kw)
    return EngineerCall(**base)


async def test_an_engineer_that_cites_a_tool_result_is_grounded(state, recurrence, events):
    def handler(*, turn, **_):
        if turn == 0:
            return tool_use_turn([("get_tyre_trend", {})])
        trend = state.tyres.temp_trend.get("LF")
        return _engineer(
            spoken_text=f"LF climbing {trend} degrees a lap.",
            detail_text=f"get_tyre_trend reports {trend} C per lap on the LF.",
            finding="tyre_temps",
        )

    runtime = AgentRuntime(VEHICLE_ENGINEER, ScriptedProvider(handler))
    message = await runtime.invoke(recurrence, state, events=events)
    assert message.grounded and not message.refused
    assert "get_tyre_trend" in message.tools_used


async def test_an_invented_tyre_temperature_becomes_a_grounded_refusal(
    state, recurrence, events
):
    def handler(**_):
        return _engineer(
            spoken_text="Right front is 143.7 degrees, twenty over the window.",
            detail_text="Pressure has gone to 218.4 kPa.",
            finding="tyre_temps",
        )

    runtime = AgentRuntime(VEHICLE_ENGINEER, ScriptedProvider(handler))
    message = await runtime.invoke(recurrence, state, events=events)
    assert message.refused
    assert message.priority is RadioPriority.INFO
    assert "143.7" in message.ungrounded and "218.4" in message.ungrounded


async def test_a_tool_outside_an_agents_list_is_unreachable_and_uncitable(state, events):
    """Scoping tools scopes speech: the spotter cannot borrow a fuel number."""

    def handler(*, turn, **_):
        if turn == 0:
            return tool_use_turn([("get_fuel_projection", {})])
        return SpotterCall(
            priority=RadioPriority.ADVISORY,
            spoken_text="Car behind, and we have 1234.5 litres of fuel.",
            detail_text="Fuel is not the spotter's to quote.",
            threat="car_closing",
            side="behind",
            confidence="low",
        )

    event = _event("traffic_close", {"car_idx": 1, "gap": 0.5, "side": "behind"})
    provider = ScriptedProvider(handler)
    runtime = AgentRuntime(SPOTTER, provider)
    message = await runtime.invoke(event, state, events=events)

    # The call was rejected at dispatch, so nothing it would have returned ever
    # reached the fact set -- and the figure the agent then asserted is unbacked.
    assert "get_fuel_projection" not in message.tools_used
    results = [
        block
        for call in provider.calls
        for msg in call["messages"]
        if isinstance(msg.get("content"), list)
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert results and all(r["is_error"] for r in results)
    assert "Unknown tool" in results[0]["content"]
    assert message.refused and "1234.5" in message.ungrounded


async def test_a_spotter_car_index_must_come_from_the_event(state, events):
    def handler(**_):
        return SpotterCall(
            priority=RadioPriority.ADVISORY,
            spoken_text="Car alongside.",
            detail_text="No figures asserted.",
            threat="car_closing",
            side="behind",
            car_idx=57,  # nothing in the state or the event says 57
            confidence="low",
        )

    event = _event("traffic_close", {"car_idx": 3, "gap": 0.5, "side": "behind"})
    runtime = AgentRuntime(SPOTTER, ScriptedProvider(handler))
    message = await runtime.invoke(event, state, events=events)
    assert message.refused
    assert "car_idx=57" in message.ungrounded


async def test_a_coach_cue_with_no_numbers_at_all_is_grounded(state, recurrence, events):
    def handler(**_):
        return CoachCall(
            priority=RadioPriority.ADVISORY,
            spoken_text="Same corner keeps catching you. Brake a touch earlier.",
            detail_text="Three repeats at the same corner, from the aggregate.",
            theme="braking",
            corner="C08",
            cue="Brake a touch earlier there.",
            from_telemetry=False,
            confidence="low",
            rationale="Pattern only; no corner trace was available.",
        )

    runtime = AgentRuntime(COACH, ScriptedProvider(handler))
    message = await runtime.invoke(recurrence, state, events=events)
    assert message.grounded and not message.refused
    assert message.data["theme"] == "braking"


async def test_the_role_agents_share_one_channel_and_one_runtime(engine, state, recurrence):
    """No per-agent branch anywhere: the same orchestrator merges all of them."""

    def handler(*, agent, **_):
        if agent == "vehicle_engineer":
            return _engineer()
        return CoachCall(
            priority=RadioPriority.ADVISORY,
            spoken_text="Brake a touch earlier there.",
            detail_text="Pattern only.",
            theme="braking",
            cue="Brake earlier.",
            confidence="low",
            rationale="Pattern only.",
        )

    feed = RadioFeed()
    orch = PitwallOrchestrator(
        engine, ScriptedProvider(handler), [VEHICLE_ENGINEER, COACH],
        feed=feed, clock=None,
    )
    messages = await orch.handle_event(recurrence, state)
    assert {m.agent for m in messages} == {"vehicle_engineer", "coach"}
    assert all(m.grounded for m in messages)


def test_the_orchestrator_hands_every_tool_the_engine(engine):
    orch = PitwallOrchestrator(
        engine, ScriptedProvider(lambda **_: None), list(AGENT_REGISTRY),
        feed=RadioFeed(), clock=None,
    )
    assert orch.tool_extras["engine"] is engine
    assert all(rt.tool_extras["engine"] is engine for rt in orch.runtimes.values())


# --------------------------------------------------------------------------
# the golden sets and their ground truth
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def role_cases() -> dict:
    return build_all_cases()


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda s: s.name)
def test_every_agent_has_a_case_for_every_trigger_it_declares(spec, role_cases):
    covered = {c.event.event_type.value for c in role_cases[spec.name]}
    declared = {t.event_type for t in spec.triggers}
    assert declared <= covered, f"no eval case for {sorted(declared - covered)}"


def test_the_role_golden_sets_are_reproducible():
    def signature(cases):
        return [(c.name, c.event.tick, c.state.version) for c in cases]

    for agent in ("vehicle_engineer", "spotter", "coach"):
        assert signature(build_agent_cases(agent)) == signature(build_agent_cases(agent))


def test_the_strategists_golden_set_is_unchanged_by_stage_3(role_cases):
    """Stage 2's harness must keep scoring exactly what it scored before."""
    names = [c.name for c in role_cases["strategist"]]
    assert names[-4:] == [
        "degraded-no-fuel-model",
        "degraded-no-gap-basis",
        "degraded-yellow-opportunity",
        "degraded-rival-pitted-no-need",
    ]
    assert sum(1 for n in names if n.startswith("scenario-")) == 6


# ---- the engineer's checks ----------------------------------------------
def _engineer_result(**data):
    payload = {
        "finding": "brake_lockup",
        "corner": "C08",
        "affected": ["LF"],
        "trend": "worsening",
        "confidence": "high",
        "rationale": "grounded",
    }
    payload.update(data)
    return {
        "spoken_text": data.pop("spoken_text", "Fronts are taking it at that corner."),
        "detail_text": "Three repeats at the same corner.",
        "data": payload,
    }


def test_a_faithful_engineer_advisory_scores_clean(role_cases):
    case = next(
        c for c in role_cases["vehicle_engineer"] if c.event.key == "recurring_issue"
    )
    score = engineer_check(_engineer_result(), case.state, case.event, setup_available=True)
    assert score["issues"] == []
    assert score["score"] == 1.0


def test_an_engineer_that_answers_the_wrong_question_is_flagged(role_cases):
    case = next(
        c for c in role_cases["vehicle_engineer"] if c.event.key == "car_health_warning"
    )
    score = engineer_check(_engineer_result(finding="tyre_temps"), case.state, case.event)
    assert score["finding_ok"] is False
    assert any("outside the defensible set" in i for i in score["issues"])


def test_an_engineer_that_moves_the_corner_is_flagged(role_cases):
    case = next(
        c for c in role_cases["vehicle_engineer"] if c.event.key == "recurring_issue"
    )
    score = engineer_check(_engineer_result(corner="C13"), case.state, case.event)
    assert score["corner_ok"] is False


def test_setup_advice_without_a_setup_sheet_is_flagged(role_cases):
    case = next(
        c for c in role_cases["vehicle_engineer"] if c.event.key == "recurring_issue"
    )
    score = engineer_check(
        _engineer_result(setup_note="Two clicks of rear wing."),
        case.state, case.event, setup_available=False,
    )
    assert score["setup_ok"] is False


def test_a_tyre_finding_without_tyre_channels_is_flagged(role_cases):
    case = next(
        c for c in role_cases["vehicle_engineer"] if c.name == "engineer-no-tyre-channels"
    )
    score = engineer_check(
        _engineer_result(finding="tyre_temps"), case.state, case.event
    )
    assert score["refusal_ok"] is False


# ---- the spotter's checks ------------------------------------------------
def _spotter_result(**data):
    payload = {
        "threat": "car_closing",
        "side": "behind",
        "car_idx": 1,
        "action": "hold_line",
        "confidence": "high",
    }
    payload.update(data)
    return {
        "spoken_text": data.pop("spoken_text", "Car behind, closing."),
        "detail_text": "",
        "data": payload,
    }


def test_a_faithful_spotter_call_scores_clean(role_cases):
    case = next(c for c in role_cases["spotter"] if c.name == "spotter-car-closing")
    result = _spotter_result(car_idx=case.event.payload["car_idx"])
    score = spotter_check(result, case.state, case.event)
    assert score["issues"] == []


def test_a_spotter_naming_the_wrong_car_is_flagged(role_cases):
    case = next(c for c in role_cases["spotter"] if c.name == "spotter-car-closing")
    score = spotter_check(_spotter_result(car_idx=63), case.state, case.event)
    assert score["car_ok"] is False


def test_a_spotter_calling_a_hazard_traffic_is_flagged(role_cases):
    case = next(c for c in role_cases["spotter"] if c.event.key == "blue_flag")
    score = spotter_check(_spotter_result(threat="hazard"), case.state, case.event)
    assert score["threat_ok"] is False


def test_a_spotter_that_writes_an_essay_is_flagged(role_cases):
    case = next(c for c in role_cases["spotter"] if c.name == "spotter-car-closing")
    result = _spotter_result(car_idx=case.event.payload["car_idx"])
    result["spoken_text"] = " ".join(["car"] * 20)
    score = spotter_check(result, case.state, case.event)
    assert score["brevity_ok"] is False


def test_a_spotter_quoting_seconds_with_no_gap_basis_is_flagged(role_cases):
    case = next(c for c in role_cases["spotter"] if c.name == "spotter-no-gap-basis")
    result = _spotter_result(threat="hazard", car_idx=None)
    result["spoken_text"] = "Incident ahead, two seconds."
    score = spotter_check(result, case.state, case.event)
    assert score["refusal_ok"] is False


# ---- the coach's checks --------------------------------------------------
def _coach_result(**data):
    payload = {
        "theme": "braking",
        "corner": "C08",
        "cue": "Brake a touch earlier there.",
        "from_telemetry": False,
        "confidence": "low",
        "rationale": "grounded",
    }
    payload.update(data)
    return {
        "spoken_text": data.pop("spoken_text", "Same corner. Brake earlier."),
        "detail_text": "",
        "data": payload,
    }


def test_a_faithful_coaching_cue_scores_clean(role_cases):
    case = next(c for c in role_cases["coach"] if c.event.key == "recurring_issue")
    score = coach_check(_coach_result(), case.state, case.event)
    assert score["issues"] == []
    assert score["score"] == 1.0


def test_a_coach_claiming_a_trace_it_never_saw_is_flagged(role_cases):
    case = next(c for c in role_cases["coach"] if c.event.key == "recurring_issue")
    score = coach_check(
        _coach_result(from_telemetry=True), case.state, case.event,
        telemetry_available=False,
    )
    assert score["telemetry_ok"] is False


def test_a_coach_coaching_the_wrong_corner_is_flagged(role_cases):
    case = next(c for c in role_cases["coach"] if c.event.key == "recurring_issue")
    score = coach_check(_coach_result(corner="C02"), case.state, case.event)
    assert score["corner_ok"] is False


def test_a_coach_with_a_theme_and_no_cue_is_flagged(role_cases):
    case = next(c for c in role_cases["coach"] if c.event.key == "recurring_issue")
    score = coach_check(_coach_result(cue="   "), case.state, case.event)
    assert score["one_cue_ok"] is False


def test_a_layer_1_corner_label_is_accepted(role_cases):
    case = next(c for c in role_cases["coach"] if c.event.key == "recurring_issue")
    score = coach_check(_coach_result(corner="T4"), case.state, case.event)
    assert score["corner_ok"] is True


# ---- dispatch + aggregation ---------------------------------------------
def test_check_for_agent_picks_the_right_scorer(role_cases):
    case = next(c for c in role_cases["spotter"] if c.name == "spotter-car-closing")
    score = check_for_agent(
        "spotter",
        _spotter_result(car_idx=case.event.payload["car_idx"]),
        case.state,
        case.event,
    )
    assert "threat_ok" in score and "decision_ok" not in score


def test_check_for_agent_falls_back_to_the_strategists_scorer(role_cases):
    case = role_cases["strategist"][0]
    score = check_for_agent(
        STRATEGIST.name,
        {"spoken_text": "Hold.", "detail_text": "", "data": {"recommendation": "extend"}},
        case.state,
        case.event,
    )
    assert "decision_ok" in score


def test_aggregate_reports_a_rate_only_for_the_checks_that_were_run(role_cases):
    engineer_case = next(
        c for c in role_cases["vehicle_engineer"] if c.event.key == "recurring_issue"
    )
    coach_case = next(c for c in role_cases["coach"] if c.event.key == "recurring_issue")
    rows = [
        engineer_check(_engineer_result(), engineer_case.state, engineer_case.event,
                       setup_available=True),
        coach_check(_coach_result(), coach_case.state, coach_case.event),
    ]
    agg = aggregate(rows)
    assert agg["cases"] == 2
    assert agg["grounding_rate"] == 1.0
    assert agg["finding_rate"] == 1.0  # only the engineer row carries it
    assert agg["theme_rate"] == 1.0  # only the coach row carries it
    assert agg["clean_cases"] == 2
