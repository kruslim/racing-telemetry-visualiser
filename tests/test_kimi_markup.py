"""Kimi tool-call markup never becomes content (R2).

On the A2 coach eval Kimi twice returned its own chat-template tokens
(``<|close|><|cues|>...``, ``<|oopen|>argument key=...``) inside the orchestrator's
``corner`` field: a garbled respond call that still passed the schema. The fixture
holds those two strings as recorded (``tests/fixtures/kimi_markup/``). Every layer
reaches Kimi through ``KimiProvider`` or the graph coach, so the guard is tested at
both, plus the orchestrator's corner contract on top. All offline.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rtv.coaching.agent.prose import result_prose
from rtv.coaching.orchestrator import (
    CoachOrchestrator,
    CornerAdvice,
    Priority,
    SessionPlan,
    VerdictReport,
)
from rtv.llm import find_markup
from rtv.pitwall.provider import (
    RESPOND_TOOL,
    KimiProvider,
    ProviderResponse,
    ScriptedProvider,
    structured_call,
)

FIXTURE = Path(__file__).parent / "fixtures" / "kimi_markup" / "a2_coach_eval.json"
LEAKS = [leak["value"] for leak in json.loads(FIXTURE.read_text(encoding="utf-8"))["leaks"]]


# ----------------------------------------------------------------- the detector
def test_the_fixture_holds_both_recorded_leaks():
    assert len(LEAKS) == 2
    assert LEAKS[0].startswith("T3<|close|><|cues|>")
    assert LEAKS[1].startswith("T2<|/open|>")


@pytest.mark.parametrize("leak", LEAKS)
def test_each_recorded_leak_is_detected_and_located(leak):
    found = find_markup({"corner": leak, "diagnosis": "fine", "cues": ["ok"]})
    assert found is not None and found.startswith("corner: ")


@pytest.mark.parametrize(
    "text",
    [
        "Brake 5 m later into T3 <- that is the gain; throttle |0.8| on exit.",
        "Lost <0.2 s at T7 | Pouhon, a < b and c > d.",
        "Les Combes is your biggest loss on the lap (+0.321 s).",
    ],
)
def test_ordinary_coaching_text_is_not_markup(text):
    assert find_markup({"cues": [text], "n": 3, "ok": True}) is None


def test_markup_is_found_in_nested_lists():
    payload = {"priorities": [{"corner": "T1", "why": "ok"}, {"corner": "T2", "why": "x<|sep|>"}]}
    assert find_markup(payload).startswith("priorities[1].why: ")


# ----------------------------------------------------------------- KimiProvider
class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, content):
        self.content = content
        self.usage = None


class _Messages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


class _Client:
    def __init__(self, *responses):
        self.messages = _Messages(responses)


def _respond(payload: dict) -> _Response:
    return _Response([_Block("tool_use", id="toolu_1", name=RESPOND_TOOL, input=payload)])


def _advice(corner: str) -> dict:
    return {
        "corner": corner,
        "diagnosis": "Bruxelles cost 0.179 s.",
        "cues": ["Get back to full throttle decisively at apex."],
        "est_gain_s": 0.179,
    }


async def _complete(provider, output_model=CornerAdvice):
    return await provider.complete(
        agent="orchestrator.CornerAdvice",
        model="kimi-k3",
        system=[{"type": "text", "text": "role"}],
        messages=[{"role": "user", "content": "go"}],
        tools=(),
        output_model=output_model,
        max_tokens=800,
    )


@pytest.mark.parametrize("leak", LEAKS)
async def test_a_recorded_leak_is_a_malformed_turn_not_an_answer(leak):
    # The schema alone accepts it: corner is a plain string.
    CornerAdvice.model_validate(_advice(leak))
    response = await _complete(KimiProvider(client=_Client(_respond(_advice(leak)))))
    assert response.output is None
    assert response.tool_calls == []
    assert response.malformed and response.malformed.startswith(f"{RESPOND_TOOL}: corner: ")


async def test_markup_in_a_real_tool_call_spoils_the_whole_turn():
    content = [
        _Block("tool_use", id="toolu_1", name="get_fuel_projection", input={}),
        _Block("tool_use", id="toolu_2", name="get_corner_detail", input={"corner": LEAKS[1]}),
    ]
    response = await _complete(KimiProvider(client=_Client(_Response(content))))
    assert response.tool_calls == [] and response.output is None
    assert response.malformed.startswith("get_corner_detail: corner: ")


async def test_markup_in_the_text_is_dropped():
    content = [_Block("text", text='<|open|>call tool="rtv_final_answer"<|sep|>')]
    response = await _complete(KimiProvider(client=_Client(_Response(content))))
    assert response.text == "" and response.malformed


async def test_a_clean_answer_still_parses():
    response = await _complete(KimiProvider(client=_Client(_respond(_advice("T3")))))
    assert response.malformed is None
    assert response.output.corner == "T3"


# ----------------------------------------------------------------- structured_call
async def _structured(client, check=None):
    return await structured_call(
        KimiProvider(client=client),
        agent="orchestrator.CornerAdvice",
        model="kimi-k3",
        system=[],
        prompt="go",
        output_model=CornerAdvice,
        max_tokens=800,
        check=check,
    )


async def test_a_garbled_answer_is_asked_for_once_more():
    client = _Client(_respond(_advice(LEAKS[0])), _respond(_advice("T3")))
    answer = await _structured(client)
    assert answer.corner == "T3"
    assert client.messages.calls == 2


async def test_two_garbled_answers_give_no_answer():
    client = _Client(_respond(_advice(LEAKS[0])), _respond(_advice(LEAKS[1])))
    assert await _structured(client) is None


async def test_the_callers_check_rejects_like_an_invalid_payload():
    client = _Client(_respond(_advice("T9")), _respond(_advice("T9")))

    def check(a):
        return None if a.corner == "T3" else "wrong corner"

    assert await _structured(client, check=check) is None
    assert client.messages.calls == 2


# ----------------------------------------------------------------- the orchestrator
FINDINGS = {
    "corners": [
        {"index": 1, "label": "T2", "name": "Les Combes", "net_dt": 0.321, "diags": []},
        {"index": 2, "label": "T3", "name": "Bruxelles", "net_dt": 0.179, "diags": []},
    ],
    "chief": {
        "top3": [
            {"index": 1, "label": "T2", "gain": 0.321, "why": "brake"},
            {"index": 2, "label": "T3", "gain": 0.179, "why": "line"},
        ],
        "net_lap": 0.5, "lost": 0.5, "losing_n": 2, "total": 2,
    },
}


class _Kimi:
    """A KimiProvider over a fake client that answers per schema, like the A2 run did."""

    def __init__(self, advice_for):
        self.advice_for = advice_for
        self.provider = KimiProvider(client=self)
        self.messages = self
        self.prompts: list[str] = []

    async def create(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        self.prompts.append(prompt)
        schema = kwargs["tools"][-1]["input_schema"]["title"]
        if schema == "CornerAdvice":
            asked = "T2" if "corner T2" in prompt else "T3"
            return _respond(_advice(self.advice_for(asked, prompt)))
        if schema == "SessionPlan":
            return _respond(SessionPlan(
                headline="T2 first", one_lap_focus="T2 brakes",
                priorities=[Priority(corner="T2", why="brake", gain_s=0.321)],
            ).model_dump())
        return _respond(VerdictReport(claims=[]).model_dump())


def test_the_specialist_is_asked_about_its_corner_by_label():
    kimi = _Kimi(lambda asked, prompt: asked)
    asyncio.run(CoachOrchestrator(kimi.provider, model="kimi-k3").coach(FINDINGS))
    specialist = [p for p in kimi.prompts if p.startswith("Focus only")]
    assert specialist[0].startswith("Focus only on corner T2 ")
    assert "corner None" not in " ".join(specialist)


def test_the_a2_leak_never_reaches_the_coaching_result():
    # T3's specialist garbles both attempts, exactly as recorded; T2 answers cleanly.
    kimi = _Kimi(lambda asked, prompt: LEAKS[0] if asked == "T3" else asked)
    result = asyncio.run(CoachOrchestrator(kimi.provider, model="kimi-k3").coach(FINDINGS))
    assert [a.corner for a in result.advices] == ["T2"]
    dumped = json.dumps(result.model_dump())
    assert "<|" not in dumped
    assert all("<|" not in text for _w, _c, text in result_prose(result.model_dump()))


def test_advice_about_another_corner_is_rejected():
    kimi = _Kimi(lambda asked, prompt: "T7")
    result = asyncio.run(CoachOrchestrator(kimi.provider, model="kimi-k3").coach(FINDINGS))
    assert result.advices == []


def test_a_plan_with_a_corner_the_findings_lack_is_no_plan():
    def handler(*, output_model, **_):
        if output_model is CornerAdvice:
            return ProviderResponse(output=CornerAdvice.model_validate(_advice("T2")))
        if output_model is SessionPlan:
            return SessionPlan(
                headline="x", one_lap_focus="y",
                priorities=[Priority(corner="T9", why="z", gain_s=0.1)],
            )
        return VerdictReport(claims=[])

    result = asyncio.run(
        CoachOrchestrator(ScriptedProvider(handler), model="kimi-k3").coach(FINDINGS)
    )
    assert result.plan is None and result.clean is False
    assert "could not produce" in result.message


# ----------------------------------------------------------------- pitwall agents
async def test_a_pitwall_agent_retries_a_malformed_turn_once():
    from evals.synthetic_race import ScenarioSpec, replay_scenario

    from rtv.pitwall.agents.strategist import STRATEGIST, PitCall
    from rtv.pitwall.framework import AgentRuntime, RadioPriority
    from rtv.racestate import RaceStateEngine
    from rtv.racestate.models import EventType, RaceEvent, Severity

    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    state = engine.snapshot()
    event = RaceEvent(
        event_type=EventType("pit_window_open"), tick=1000, session_time=42.0, lap=5,
        severity=Severity.ADVISORY, payload={"lap": 5},
    )
    answer = PitCall(
        priority=RadioPriority.ADVISORY, spoken_text="Stay out, no stop needed.",
        detail_text="Nothing to cite.", recommendation="stay_out", confidence="high",
        rationale="No figures asserted.",
    )

    def once(*, call_index, **_):
        return ProviderResponse(malformed="corner: '<|close|>'") if call_index == 0 else answer

    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(once))
    message = await runtime.invoke(event, state)
    assert message is not None and message.spoken_text == answer.spoken_text
    assert runtime.malformed == 1 and runtime.stats()["malformed"] == 1

    def always(**_):
        return ProviderResponse(malformed="corner: '<|close|>'")

    runtime = AgentRuntime(STRATEGIST, ScriptedProvider(always))
    assert await runtime.invoke(event, state) is None
    assert runtime.turns == 2  # one retry, then no output: never the markup


# ----------------------------------------------------------------- the graph coach
def test_the_graph_coach_bounces_markup_then_refuses(make_model, ai):
    pytest.importorskip("langgraph")
    from evals.fixtures import build_fixture
    from langchain_core.messages import ToolMessage

    from rtv.coaching.agent import REFUSE_TOOL, SUBMIT_COACHING_TOOL, run_coach

    garbled = {
        "headline": "Biggest gain is in T4.",
        "priorities": [{"corner": LEAKS[1], "why": "early braking", "gain_s": 0.30}],
        "one_lap_focus": "Brake later into T4.",
        "claims": [],
        "corners_examined": ["T4"],
    }
    model = make_model(
        [
            ai("get_lap_findings", {"session_id": "synthetic", "main_lap": 5, "ref_lap": 3}),
            ai(SUBMIT_COACHING_TOOL, garbled),
            ai(REFUSE_TOOL, {"reason": "insufficient_data", "channels_required": [],
                             "suggestion": "x<|sep|>y"}),
        ]
    )
    state = run_coach("Where am I losing time?", build_fixture("t4_brake_loss"), model)
    feedback = [
        m for m in state.messages
        if isinstance(m, ToolMessage) and "raw tool-call markup" in str(m.content)
    ]
    assert feedback, "the markup must bounce like any invalid answer"
    assert state.answer is None and state.refusal is not None
    assert "<|" not in (state.refusal.suggestion or "")
