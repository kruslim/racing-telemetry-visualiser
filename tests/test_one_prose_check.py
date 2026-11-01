"""One prose fact check for all three coaches (task A2, REVIEW.md P2-4).

The orchestrator already ran :func:`rtv.coaching.agent.prose.check_prose` on its plan. These
tests hold the other two coaches to it: the LangGraph coach's validate node (a wrong brake
direction or a figure in the wrong unit goes through the repair and refusal path), and the
live pitwall coach whenever ``get_corner_detail`` handed it a Layer-1 corner. Hermetic: a
scripted model and a scripted provider, no network, no key.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace

import pytest
from evals.fixtures import build_fixture
from evals.synthetic_race import (
    ScenarioSpec,
    scenario_catalog,
    scenario_frames,
    scenario_session_info,
)

from rtv.coaching.agent.prose import check_prose
from rtv.pitwall import tools as T
from rtv.pitwall.agents.coach import COACH, CoachCall
from rtv.pitwall.framework import AgentRuntime, RadioPriority, ToolSpec
from rtv.pitwall.provider import ScriptedProvider
from rtv.pitwall.validator import FactSet, layer1_corner, validate_output
from rtv.racestate import RaceStateEngine
from rtv.racestate.detectors import DEFAULT_CONFIG
from rtv.racestate.models import EventType, RaceEvent, RaceState

FIND_ARGS = {"session_id": "synthetic", "main_lap": 5, "ref_lap": 3}


def _answer(**over) -> dict:
    """A faithful answer for t4_brake_loss: T4 loses 0.30 s, the driver brakes 9 m early."""
    base = {
        "headline": "Biggest gain is in T4, where you brake 9 m early.",
        "priorities": [{"corner": "T4", "why": "early braking", "gain_s": 0.30}],
        "one_lap_focus": "Brake later into T4.",
        "claims": [
            {
                "statement": "T4 costs you 0.30 s against the reference.",
                "corner": "T4",
                "citations": [{"corner": "T4", "figure": "net_dt", "value": 0.30, "unit": "s"}],
                "confidence": "high",
            }
        ],
        "corners_examined": ["T1", "T4", "T7"],
        "could_not_determine": [],
    }
    return {**base, **over}


def _feedback(state) -> list[str]:
    from langchain_core.messages import ToolMessage

    return [
        str(m.content)
        for m in state.messages
        if isinstance(m, ToolMessage) and "failed validation" in str(m.content)
    ]


# ── the graph coach ─────────────────────────────────────────────────────────────────────────


def test_graph_coach_accepts_a_faithful_answer_through_the_prose_check(make_model, ai):
    pytest.importorskip("langgraph")
    from rtv.coaching.agent import SUBMIT_COACHING_TOOL, run_coach

    model = make_model([ai("get_lap_findings", FIND_ARGS), ai(SUBMIT_COACHING_TOOL, _answer())])
    state = run_coach("Where am I losing time?", build_fixture("t4_brake_loss"), model)
    assert state.answer is not None
    assert state.validation_retries == 0


def test_graph_coach_bounces_a_wrong_brake_direction_and_takes_the_fix(make_model, ai):
    """The structured figures are all right; the sentence tells the driver the opposite."""
    pytest.importorskip("langgraph")
    from rtv.coaching.agent import SUBMIT_COACHING_TOOL, run_coach

    wrong = _answer(one_lap_focus="Brake earlier into T4.")
    model = make_model(
        [
            ai("get_lap_findings", FIND_ARGS),
            ai(SUBMIT_COACHING_TOOL, wrong),
            ai(SUBMIT_COACHING_TOOL, _answer()),
        ]
    )
    state = run_coach("Where am I losing time?", build_fixture("t4_brake_loss"), model)

    assert state.answer is not None
    assert state.answer.one_lap_focus == "Brake later into T4."
    assert state.validation_retries == 1
    feedback = _feedback(state)
    assert feedback, "the prose failure must re-enter the conversation"
    assert "prose: one_lap_focus" in feedback[0]
    assert "brake earlier at T4" in feedback[0] and "9 m early" in feedback[0]
    assert "could_not_determine" in feedback[0]  # the escape instruction


def test_graph_coach_bounces_a_figure_in_the_wrong_unit(make_model, ai):
    """T4's 0.30 s said as km/h is not T4's speed loss (that is 3 km/h)."""
    pytest.importorskip("langgraph")
    from rtv.coaching.agent import REFUSE_TOOL, SUBMIT_COACHING_TOOL, run_coach

    claims = [
        {
            "statement": "You are 0.30 km/h slower than the reference at the T4 apex.",
            "corner": "T4",
            "citations": [{"corner": "T4", "figure": "net_dt", "value": 0.30, "unit": "s"}],
            "confidence": "high",
        }
    ]
    model = make_model(
        [
            ai("get_lap_findings", FIND_ARGS),
            ai(SUBMIT_COACHING_TOOL, _answer(claims=claims)),
            ai(REFUSE_TOOL, {"reason": "insufficient_data", "channels_required": []}),
        ]
    )
    state = run_coach("How slow am I at T4?", build_fixture("t4_brake_loss"), model)

    feedback = _feedback(state)
    assert feedback and "0.30 km/h at T4 matches no speed" in feedback[0]
    assert state.answer is None and state.refusal is not None


def test_graph_coach_degrades_when_the_prose_stays_wrong(make_model, ai):
    pytest.importorskip("langgraph")
    from rtv.coaching.agent import SUBMIT_COACHING_TOOL, run_coach
    from rtv.coaching.agent.contracts import DEGRADED_HEADLINE

    wrong = _answer(headline="T4: you brake 9 m late.")
    model = make_model(
        [ai("get_lap_findings", FIND_ARGS)] + [ai(SUBMIT_COACHING_TOOL, wrong)] * 3
    )
    state = run_coach("What should I fix?", build_fixture("t4_brake_loss"), model)
    assert state.validation_retries == 3
    assert state.answer is not None and state.answer.headline == DEGRADED_HEADLINE


def test_the_prose_check_imports_without_langgraph():
    """The pitwall validator and the orchestrator share it without the ``agent`` extra."""
    code = (
        "import sys; sys.modules['langgraph'] = None\n"
        "import rtv.pitwall.validator, rtv.coaching.agent.prose\n"
        "assert 'rtv.coaching.agent.graph' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# ── the live mode ───────────────────────────────────────────────────────────────────────────


T4_LATE = {
    "label": "T4",
    "distance": 1200.0,
    "min_main": 92.0,
    "min_ref": 99.0,
    "net_dt": 0.21,
    "diags": [{"text": "Brake point 14 m late", "distance": 1100.0, "time_loss": 0.12}],
}


def test_live_mode_checks_brake_direction_only():
    findings = {"corners": [T4_LATE]}
    wrong = check_prose([("cue", "T4", "Brake later, 30 km/h faster.")], findings, mode="live")
    assert len(wrong.issues) == 1 and "14 m late" in wrong.issues[0]
    # Figures are the typed fact set's job, and another corner of the map is not invented.
    right = check_prose(
        [("cue", "T4", "Brake earlier into T4, then T5 is flat. 30 km/h down.")],
        findings,
        mode="live",
    )
    assert right.issues == []
    # No brake-point difference: the live detector events can be the cue's basis.
    plain = {"corners": [{**T4_LATE, "diags": []}]}
    assert check_prose([("cue", "T4", "Brake earlier.")], plain, mode="live").issues == []
    assert check_prose([("cue", "T4", "Brake earlier.")], plain).issues  # lap mode: flagged


def _detail(corner: dict | None = None) -> dict:
    """A ``get_corner_detail`` result with the Layer-1 telemetry half available."""
    corner = corner or {
        "label": "T4",
        "type": "Slow corner",
        "distance_m": 1200.0,
        "lap_dist_pct": 0.3,
        "sector": 1,
        "min_speed_kmh": 92.0,
        "ref_min_speed_kmh": 99.0,
        "time_delta_s": 0.21,
        "diagnostics": [
            {
                "agent": "brake",
                "text": "Brake point 14 m late",
                "distance_m": 1100.0,
                "time_loss_s": 0.12,
                "lockup": False,
                "good": False,
            }
        ],
    }
    return {
        "requested_lap_dist_pct": 0.3,
        "live": {},
        "telemetry": {"available": True, "session_id": "s", "main_lap": 5, "ref_lap": 3,
                      "lap_length_m": 4000.0, "corner": corner, "notes": []},
    }


def test_layer1_corner_maps_the_tool_result_back_to_finding_fields():
    corner = layer1_corner(_detail())
    assert corner is not None
    assert (corner["label"], corner["min_main"], corner["net_dt"]) == ("T4", 92.0, 0.21)
    assert corner["diags"][0] == {
        "text": "Brake point 14 m late", "distance": 1100.0, "time_loss": 0.12
    }
    assert layer1_corner({"telemetry": {"available": False, "unavailable": "x"}}) is None
    assert layer1_corner({"fuel": 3}) is None


def test_validate_output_holds_the_coach_to_its_corner():
    facts = FactSet()
    facts.add("get_corner_detail", _detail())
    assert set(facts.corners) == {"T4"}
    payload = {"theme": "braking", "corner": "T4 Speaker's Corner", "cue": "Brake later."}

    wrong = validate_output("Brake later into T4.", "", facts, payload, prose_check=True)
    assert not wrong.ok and wrong.contradictions and not wrong.unsupported
    # Without the flag (every other agent) the same answer only has its figures checked.
    assert validate_output("Brake later into T4.", "", facts, payload).ok

    right = {**payload, "cue": "Brake earlier."}
    assert validate_output("Brake earlier into T4.", "", facts, right, prose_check=True).ok
    # A cue about a corner the Layer-1 result is not about has nothing to be held to.
    other = {**payload, "corner": "T9"}
    assert validate_output("Brake later.", "", facts, other, prose_check=True).ok


# ── the pitwall coach, end to end ───────────────────────────────────────────────────────────

SPEC = ScenarioSpec(lockup_laps=(1, 2), corner_pcts=(0.44,))


@pytest.fixture(scope="module")
def run() -> tuple[RaceState, list[RaceEvent], RaceEvent]:
    config = replace(DEFAULT_CONFIG, stint_milestone_laps=2, tyre_temp_trend_c_per_lap=1.0)
    catalog = scenario_catalog(SPEC)
    engine = RaceStateEngine(source="replay", config=config)
    engine.bind_catalog(catalog, session_id="a2")
    engine.set_session_info(scenario_session_info(SPEC))
    for frame in scenario_frames(SPEC):
        engine.on_frame(frame, catalog)
    events = engine.bus.history(200)
    recurrence = next(e for e in events if e.event_type is EventType.RECURRING_ISSUE)
    return engine.snapshot(), events, recurrence


def _coach_with_telemetry():
    fake = ToolSpec(
        name=T.GET_CORNER_DETAIL.name,
        description=T.GET_CORNER_DETAIL.description,
        input_schema=T.GET_CORNER_DETAIL.input_schema,
        fn=lambda ctx, args: _detail(),
    )
    tools = tuple(fake if t.name == fake.name else t for t in COACH.tools)
    return replace(COACH, tools=tools)


def _cue(text: str) -> CoachCall:
    return CoachCall(
        priority=RadioPriority.ADVISORY,
        spoken_text=text,
        detail_text="From the corner analysis.",
        theme="braking",
        corner="T4",
        cue=text,
        from_telemetry=True,
        confidence="medium",
        rationale="get_corner_detail: brake point late at T4.",
    )


async def test_pitwall_coach_withholds_a_cue_its_corner_analysis_contradicts(run):
    state, events, recurrence = run
    provider = ScriptedProvider(lambda **_: _cue("Brake later into T4."))
    runtime = AgentRuntime(_coach_with_telemetry(), provider)
    message = await runtime.invoke(recurrence, state, events=events)

    assert message.refused and not message.grounded
    assert message.priority is RadioPriority.INFO
    assert "contradicts the corner analysis" in message.detail_text
    assert "14 m late" in message.detail_text
    # The repair turn told the model what was wrong before the refusal.
    repair = provider.calls[-1]["messages"][-1]["content"]
    assert "contradict the corner analysis" in repair


async def test_pitwall_coach_repairs_into_the_right_direction(run):
    state, events, recurrence = run

    answers = iter(["Brake later into T4.", "Brake earlier into T4."])

    def handler(**_):
        return _cue(next(answers))

    runtime = AgentRuntime(_coach_with_telemetry(), ScriptedProvider(handler))
    message = await runtime.invoke(recurrence, state, events=events)
    assert message.grounded and not message.refused
    assert message.spoken_text == "Brake earlier into T4."
