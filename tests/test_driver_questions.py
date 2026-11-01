"""C1: the driver asks the pitwall a question and gets an answer.

Covers the keyword router (:func:`rtv.pitwall.questions.route_question`), the
template answers read from the engine state, the honest "no data" answers, the
orchestrator path (transcript, ``driver_question`` event, answer ``in_reply_to``),
an agent-answered question driven by a scripted model, and the template-only
mount that answers lookups with no LLM key. All offline.
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest
from evals.pitwall_cases import QUESTIONS, _end_of_scenario_state

from rtv.pitwall.agents import AGENT_REGISTRY, STRATEGIST
from rtv.pitwall.agents.strategist import PitCall
from rtv.pitwall.framework import RadioPriority
from rtv.pitwall.orchestrator import PitwallOrchestrator, _holds_slot
from rtv.pitwall.provider import ScriptedProvider
from rtv.pitwall.questions import (
    LOOKUPS,
    QuestionCaller,
    answer_lookup,
    lap_time,
    route_question,
)
from rtv.pitwall.radio import RadioFeed
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, RaceState, Severity

# --------------------------------------------------------------------------
# the router
# --------------------------------------------------------------------------
#: (question, owner, intent) -- intent None means the owning agent answers.
ROUTES = [
    # fuel lookups
    ("How many laps of fuel do I have?", "strategist", "fuel_laps"),
    ("fuel?", "strategist", "fuel_laps"),
    ("How much fuel is left", "strategist", "fuel_laps"),
    ("Do I have enough fuel to finish?", "strategist", "fuel_laps"),
    ("what's the fuel situation", "strategist", "fuel_laps"),
    # strategy, judgement
    ("Should I save fuel?", "strategist", None),
    ("Should I box this lap?", "strategist", None),
    ("What's the plan for our stop?", "strategist", None),
    ("Is the undercut on?", "strategist", None),
    ("Do we pit under this yellow?", "strategist", None),
    # laps to go
    ("How many laps to go?", "strategist", "laps_to_go"),
    ("laps left?", "strategist", "laps_to_go"),
    ("How much time is left", "strategist", "laps_to_go"),
    # tyres and the car
    ("Tyre temps?", "vehicle_engineer", "tyre_temps"),
    ("What are my tire temperatures", "vehicle_engineer", "tyre_temps"),
    ("How hot are the tyres", "vehicle_engineer", "tyre_temps"),
    ("How are the tyres holding up?", "vehicle_engineer", None),
    ("Why are my fronts overheating?", "vehicle_engineer", None),
    ("Is there any damage on the car?", "vehicle_engineer", None),
    ("Oil temp looks high, is the engine ok?", "vehicle_engineer", None),
    ("What are the pressures doing", "vehicle_engineer", None),
    ("The car is understeering a lot", "vehicle_engineer", None),
    # gaps and traffic
    ("What's the gap ahead?", "spotter", "gap_ahead"),
    ("gap to the car in front", "spotter", "gap_ahead"),
    ("How far is the car ahead", "spotter", "gap_ahead"),
    ("Who's ahead?", "spotter", "gap_ahead"),
    ("Gap behind?", "spotter", "gap_behind"),
    ("How close is the car behind me", "spotter", "gap_behind"),
    ("who is behind", "spotter", "gap_behind"),
    ("Is the car behind closing?", "spotter", None),
    ("Any traffic coming up?", "spotter", None),
    ("Anyone close enough to attack me?", "spotter", None),
    ("Blue flags?", "spotter", None),
    # position
    ("What position am I in?", "spotter", "position"),
    ("what place am I", "spotter", "position"),
    # lap times
    ("What was my last lap?", "coach", "last_lap"),
    ("lap time?", "coach", "last_lap"),
    ("What's my best lap", "coach", "best_lap"),
    ("fastest lap so far?", "coach", "best_lap"),
    # driving
    ("Where am I losing time?", "coach", None),
    ("How do I take Eau Rouge faster?", "coach", None),
    ("Am I braking too late into T1?", "coach", None),
    ("Why was that last lap slow?", "coach", None),
    ("How's my line through Pouhon", "coach", None),
    # nothing matched: the strategist, flagged unmatched
    ("hello?", "strategist", None),
]


@pytest.mark.parametrize(("text", "owner", "intent"), ROUTES, ids=[r[0] for r in ROUTES])
def test_the_router_sends_each_question_to_its_owner(text, owner, intent):
    route = route_question(text)
    assert (route.owner, route.intent) == (owner, intent)


def test_the_router_table_is_big_enough_and_covers_every_lookup_and_owner():
    assert len(ROUTES) >= 40
    assert {i for _, _, i in ROUTES if i} == set(LOOKUPS)
    assert {o for _, o, _ in ROUTES} == {spec.name for spec in AGENT_REGISTRY}


def test_an_unmatched_question_is_flagged():
    assert route_question("hello?").matched is False
    assert route_question("gap ahead?").matched is True


def test_the_eval_questions_need_their_agent():
    for agent, text in QUESTIONS.items():
        route = route_question(text)
        assert (route.owner, route.intent) == (agent, None), text


# --------------------------------------------------------------------------
# template answers from the engine state
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def state() -> RaceState:
    """The scripted race's last state: a field of 8 with gaps, fuel and tyres."""
    return _end_of_scenario_state()


def _figures(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]


def test_fuel_answer_matches_the_engine(state):
    call = answer_lookup("fuel_laps", state)
    assert _figures(call.spoken_text) == [
        round(state.fuel.laps_remaining, 1),
        round(abs(state.fuel.margin_laps), 1),
    ]
    assert ("spare" in call.spoken_text) == (state.fuel.margin_laps >= 0)


@pytest.mark.parametrize("side", ["ahead", "behind"])
def test_gap_answers_match_the_engine(state, side):
    car = next(c for c in state.standings.cars if c.is_player)
    gap = car.gap_ahead if side == "ahead" else car.gap_behind
    other = car.position - 1 if side == "ahead" else car.position + 1
    call = answer_lookup(f"gap_{side}", state)
    assert call.spoken_text == f"P{other} is {gap:.1f} {side}."
    assert call.data["gap"] == gap


def test_position_laps_and_lap_time_answers_match_the_engine(state):
    field = len([c for c in state.standings.cars if c.position])
    assert answer_lookup("position", state).spoken_text == f"P{state.player.position} of {field}."
    assert (
        answer_lookup("laps_to_go", state).spoken_text
        == f"{state.session.laps_remaining} laps to go."
    )
    assert answer_lookup("last_lap", state).spoken_text == (
        f"Last lap {lap_time(state.player.last_lap_time)}."
    )
    assert answer_lookup("best_lap", state).spoken_text == (
        f"Best lap {lap_time(state.player.best_lap_time)}."
    )


def test_tyre_answer_matches_the_engine(state):
    t = state.tyres.temps
    assert answer_lookup("tyre_temps", state).spoken_text == (
        f"Fronts {t['LF']:.0f} and {t['RF']:.0f}, rears {t['LR']:.0f} and {t['RR']:.0f}."
    )


def test_lap_times_read_as_a_driver_says_them():
    assert lap_time(138.42) == "2:18.4"
    assert lap_time(59.96) == "1:00.0"
    assert lap_time(58.3) == "58.3"


@pytest.mark.parametrize("intent", sorted(LOOKUPS))
def test_no_data_gets_an_honest_answer_with_no_figure(intent):
    call = answer_lookup(intent, RaceState())
    assert call.data.get("no_data") is True
    assert not re.search(r"\d", call.spoken_text), call.spoken_text
    assert call.spoken_text.startswith("No ")


def test_an_ibt_replay_without_field_data_says_so(state):
    no_field = state.model_copy(deep=True)
    no_field.capabilities.standings = False
    for side in ("ahead", "behind"):
        call = answer_lookup(f"gap_{side}", no_field)
        assert call.data["no_data"] is True
        assert "No gap data" in call.spoken_text


def test_a_leader_is_told_nobody_is_ahead(state):
    leading = state.model_copy(deep=True)
    car = next(c for c in leading.standings.cars if c.is_player)
    car.position = 1
    assert answer_lookup("gap_ahead", leading).spoken_text == "You're leading, nobody ahead."


# --------------------------------------------------------------------------
# the orchestrator: transcript, event, answer
# --------------------------------------------------------------------------
def _engine(state: RaceState) -> RaceStateEngine:
    engine = RaceStateEngine(source="replay")
    engine._state = state.model_copy(deep=True)  # noqa: SLF001 - a fixed race for the test
    return engine


def _no_model(**_):
    raise AssertionError("a template answer must never reach the model")


def _orch(engine, agents=(), handler=_no_model, **kw) -> PitwallOrchestrator:
    return PitwallOrchestrator(
        engine, ScriptedProvider(handler), list(agents), feed=RadioFeed(), clock=None, **kw
    )


def _pending(orch) -> list:
    return [m for m in orch.feed.pending() if m.agent != "driver"]


def test_a_lookup_is_answered_at_once_with_no_model(state):
    engine = _engine(state)
    orch = _orch(engine, agents=[STRATEGIST])
    transcript = orch.ask("How many laps of fuel?", source="text")

    assert transcript.agent == "driver" and transcript.speak is False
    assert transcript.data["question"] == {
        "owner": "strategist", "intent": "fuel_laps", "matched": True, "answered_by": "template",
    }
    event = engine.bus.history()[-1]
    assert event.event_type is EventType.DRIVER_QUESTION
    assert event.payload["question_id"] == transcript.message_id
    assert event.payload["text"] == "How many laps of fuel?"

    [answer] = _pending(orch)
    assert answer.agent == "strategist"
    assert answer.source == "template"
    assert answer.in_reply_to == transcript.message_id
    assert answer.subject == f"answer:{transcript.message_id}"
    assert answer.priority is RadioPriority.ADVISORY
    assert answer.spoken_text == answer_lookup("fuel_laps", state).spoken_text
    assert orch.runtimes["strategist"].turns == 0


def test_a_question_with_no_agent_to_answer_gets_an_honest_reply(state):
    orch = _orch(_engine(state))  # template-only: no agents at all
    transcript = orch.ask("Where am I losing time?")
    [reply] = _pending(orch)
    assert reply.agent == "pitwall"
    assert reply.in_reply_to == transcript.message_id
    assert reply.data["unanswered"] == "no_agent"
    assert reply.spoken_text == "No coach on the pitwall to answer that."


def test_with_the_kill_switch_off_the_reply_is_shown_not_spoken(state):
    orch = _orch(_engine(state), agents=[STRATEGIST], enabled=False)
    orch.ask("fuel?")
    reply = next(m for m in orch.feed.history(10) if m.agent == "pitwall")
    assert reply.speak is False
    assert reply.data["unanswered"] == "disabled"


def test_a_queued_question_is_not_displaced_by_a_detector_event():
    question = RaceEvent(
        event_type=EventType.DRIVER_QUESTION, tick=1, session_time=1.0,
        severity=Severity.ADVISORY, payload={"owner": "strategist"},
    )
    lap = RaceEvent(event_type=EventType.LAP_COMPLETED, tick=2, session_time=2.0)
    flag = RaceEvent(
        event_type=EventType.FLAG_CHANGE, tick=2, session_time=2.0, severity=Severity.CRITICAL
    )
    assert _holds_slot(question, lap)
    assert not _holds_slot(question, flag)
    assert not _holds_slot(question, question.model_copy())


# --------------------------------------------------------------------------
# an agent answers, through the validator, tied to the question
# --------------------------------------------------------------------------
def _plan(**_) -> PitCall:
    return PitCall(
        priority=RadioPriority.ADVISORY,
        spoken_text="We stop in the window, fuel only.",
        detail_text="Stop plan from get_fuel_projection.",
        recommendation="stay_out",
        confidence="high",
        rationale="No figures asserted.",
    )


def test_the_owning_agent_answers_a_judgement_question(state):
    engine = _engine(state)
    seen: list[dict] = []

    def handler(**kw):
        seen.append(kw)
        return _plan()

    orch = _orch(engine, agents=[STRATEGIST], handler=handler)
    transcript = orch.ask("What's the plan for our stop?")
    assert _pending(orch) == []  # no template, no "unanswered" reply
    event = engine.bus.history()[-1]
    assert event.payload["intent"] is None

    [answer] = asyncio.run(orch.handle_event(event))
    assert answer.agent == "strategist"
    assert answer.source == "model"
    assert answer.grounded is True
    assert answer.in_reply_to == transcript.message_id
    assert answer.subject == f"answer:{transcript.message_id}"
    prompt = str(seen[0]["messages"][0]["content"])
    assert 'THE DRIVER ASKED: "What\'s the plan for our stop?"' in prompt


def test_every_agent_declares_a_question_trigger_for_its_own_questions(state):
    for spec in AGENT_REGISTRY:
        [trigger] = [t for t in spec.triggers if t.event_type == "driver_question"]
        mine = RaceEvent(
            event_type=EventType.DRIVER_QUESTION, tick=1, session_time=1.0,
            payload={"owner": spec.name, "intent": None},
        )
        other = mine.model_copy(update={"payload": {"owner": "nobody", "intent": None}})
        lookup = mine.model_copy(update={"payload": {"owner": spec.name, "intent": "fuel_laps"}})
        assert trigger.matches(mine, state)
        assert not trigger.matches(other, state)
        assert not trigger.matches(lookup, state)
        assert trigger.cooldown_s == 0.0


# --------------------------------------------------------------------------
# no LLM key: templates still answer
# --------------------------------------------------------------------------
def test_without_a_key_the_template_callers_still_mount(monkeypatch):
    for var in ("KIMI_CODE_API_KEY", "KIMI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from rtv.config import Settings
    from rtv.services import build_pitwall

    settings = Settings(_env_file=None, pitwall_agents=True)
    orch = build_pitwall(RaceStateEngine(source="replay"), settings)
    assert orch is not None
    assert orch.runtimes == {}
    assert orch.status()["agents_mounted"] is False
    agents = {c.agent for c in orch.callers}
    assert "spotter" in agents
    assert any(isinstance(c, QuestionCaller) for c in orch.callers)


def test_without_a_key_the_api_answers_a_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_PITWALL", "true")
    monkeypatch.setenv("RTV_PITWALL_AGENTS", "true")
    for var in ("KIMI_CODE_API_KEY", "KIMI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(var, "")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    try:
        with TestClient(create_app()) as client:
            status = client.get("/api/v1/pitwall/status").json()
            assert status["available"] is True and status["agents_mounted"] is False
            body = client.post(
                "/api/v1/pitwall/driver-message", json={"text": "gap behind?", "source": "text"}
            ).json()
            assert body["question"]["intent"] == "gap_behind"
            qid = body["message"]["message_id"]
            answer = None
            for _ in range(50):  # the running pump answers it
                radio = client.get("/api/v1/pitwall/radio", params={"limit": 20}).json()
                found = [
                    m for m in radio["messages"] + radio["pending"] if m.get("in_reply_to") == qid
                ]
                if found:
                    answer = found[0]
                    break
                time.sleep(0.05)
            assert answer is not None, "no answer to the question"
            assert answer["agent"] == "spotter"
            assert answer["source"] == "template"
            # A fresh store has no race: the honest answer, not a number.
            assert answer["data"]["no_data"] is True
    finally:
        get_settings.cache_clear()
