"""The live vision seam on the pitwall: who wakes on visual_observation, and how loud.

Offline: a scripted provider stands in for the model.
"""

from __future__ import annotations

import pytest
from evals.synthetic_race import ScenarioSpec, replay_scenario

from rtv.pitwall.agents import AGENT_REGISTRY
from rtv.pitwall.agents.spotter import SPOTTER, SpotterCall
from rtv.pitwall.framework import AgentRuntime, RadioPriority
from rtv.pitwall.provider import ScriptedProvider
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, RaceState, Severity


@pytest.fixture(scope="module")
def state() -> RaceState:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    return engine.snapshot()


def _visual(kind: str) -> RaceEvent:
    return RaceEvent(
        event_type=EventType.VISUAL_OBSERVATION,
        tick=1000,
        session_time=420.0,
        lap=5,
        severity=Severity.INFO,
        payload={"source": "vision", "kind": kind, "text": "words", "confidence": "medium"},
    )


@pytest.mark.parametrize(
    ("kind", "woken"),
    [
        ("traffic", {"spotter"}),
        ("damage", {"spotter"}),
        ("track_state", {"spotter"}),
        ("kerb_use", {"coach"}),
        ("line", {"coach"}),
        ("car_behaviour", {"coach"}),
        ("other", set()),
    ],
)
def test_visual_observations_wake_only_the_spotter_or_the_coach(state, kind, woken):
    if state.flags.phase.value not in ("green", "unknown"):
        pytest.skip("the scenario's final state is under a caution")
    matched = {
        spec.name
        for spec in AGENT_REGISTRY
        if any(t.matches(_visual(kind), state) for t in spec.triggers)
    }
    assert matched == woken


async def test_a_visual_call_always_airs_at_the_lowest_priority(state):
    def handler(**_):
        return SpotterCall(
            priority=RadioPriority.CRITICAL,
            spoken_text="Car behind looks wild under braking.",
            detail_text="From the onboard camera.",
            threat="car_closing",
            side="behind",
            confidence="low",
        )

    message = await AgentRuntime(SPOTTER, ScriptedProvider(handler)).invoke(
        _visual("traffic"), state
    )
    assert message is not None
    assert message.priority is RadioPriority.INFO
