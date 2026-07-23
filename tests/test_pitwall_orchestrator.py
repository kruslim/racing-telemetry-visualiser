"""Orchestrator tests: routing, per-agent serialisation, the cap and the switches.

These are the properties that stop the agent layer from either flooding the API
or flooding the driver, so they are asserted rather than assumed.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from rtv.pitwall.agents.strategist import STRATEGIST, PitCall
from rtv.pitwall.framework import RadioPriority, Trigger
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ScriptedProvider
from rtv.pitwall.radio import RadioFeed
from rtv.racestate import RaceStateEngine, replay_scenario
from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.racestate.scenario import ScenarioSpec


@pytest.fixture(scope="module")
def engine() -> RaceStateEngine:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    return engine


def _event(event_type="pit_window_open", *, t=42.0, severity=Severity.ADVISORY, **payload):
    return RaceEvent(
        event_type=EventType(event_type),
        tick=int(t * 60),
        session_time=t,
        lap=5,
        severity=severity,
        payload=payload or {"lap": 5},
    )


def _answer(text="Stay out, no stop needed.") -> PitCall:
    return PitCall(
        priority=RadioPriority.ADVISORY,
        spoken_text=text,
        detail_text=text,
        recommendation="stay_out",
        confidence="high",
        rationale="No figures asserted.",
    )


def _orch(engine, handler=None, **kw) -> PitwallOrchestrator:
    provider = ScriptedProvider(handler or (lambda **_: _answer()))
    orch = PitwallOrchestrator(
        engine, provider, kw.pop("agents", [STRATEGIST]),
        feed=kw.pop("feed", RadioFeed()), clock=None, **kw,
    )
    orch.provider = provider  # convenience handle for assertions
    return orch


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------
def test_an_event_is_routed_only_to_agents_that_declare_it(engine):
    orch = _orch(engine)
    assert orch.dispatch(_event("pit_window_open")) == ["strategist"]
    assert orch.dispatch(_event("lockup", t=100.0)) == []


def test_a_second_agent_is_pure_configuration(engine):
    """Adding an agent means adding a spec, not touching the orchestrator."""
    spotter = replace(
        STRATEGIST,
        name="spotter",
        triggers=(Trigger(EventType.LOCKUP.value, cooldown_s=0.0),),
    )
    orch = _orch(engine, agents=[STRATEGIST, spotter])
    assert orch.dispatch(_event("lockup")) == ["spotter"]
    assert orch.dispatch(_event("pit_window_open", t=100.0)) == ["strategist"]


def test_the_cooldown_suppresses_a_repeat_without_a_model_call(engine):
    orch = _orch(engine)
    rival = dict(gap_to_player=2.0)
    assert orch.dispatch(_event("rival_pitted", t=10.0, **rival)) == ["strategist"]
    assert orch.dispatch(_event("rival_pitted", t=20.0, **rival)) == []
    assert orch.dispatch(_event("rival_pitted", t=80.0, **rival)) == ["strategist"]


# --------------------------------------------------------------------------
# kill switches
# --------------------------------------------------------------------------
async def test_the_kill_switch_prevents_the_model_call_not_just_the_message(engine):
    orch = _orch(engine, enabled=False)
    assert await orch.handle_event(_event()) == []
    assert orch.provider.calls == []  # nothing was spent

    orch.set_enabled(True)
    assert await orch.handle_event(_event(t=200.0))
    assert orch.provider.calls


async def test_a_disabled_agent_is_skipped_and_can_be_re_enabled(engine):
    orch = _orch(engine)
    assert orch.set_agent_enabled("strategist", False) is True
    assert await orch.handle_event(_event()) == []
    assert orch.provider.calls == []

    orch.set_agent_enabled("strategist", True)
    assert await orch.handle_event(_event(t=200.0))


def test_enabling_an_unknown_agent_is_reported(engine):
    assert _orch(engine).set_agent_enabled("nobody", True) is False


def test_reset_clears_cooldowns_and_the_channel(engine):
    orch = _orch(engine)
    orch.dispatch(_event("rival_pitted", t=10.0, gap_to_player=2.0))
    orch.feed.publish(_answer_message())
    orch.reset()
    assert orch.feed.pending() == []
    # The cooldown is gone, so the same event fires again.
    assert orch.dispatch(_event("rival_pitted", t=12.0, gap_to_player=2.0)) == ["strategist"]


def _answer_message():
    from rtv.pitwall.framework import EventRef, RadioMessage

    return RadioMessage(
        agent="strategist",
        priority=RadioPriority.ADVISORY,
        spoken_text="x",
        detail_text="x",
        event_ref=EventRef.of(_event()),
    )


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------
async def test_an_agent_never_overlaps_itself(engine):
    """Two events in flight; the agent runs them one at a time."""
    concurrent, peak = 0, 0
    release = asyncio.Event()

    async def slow_handler(**_):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await release.wait()
        concurrent -= 1
        return _answer()

    orch = PitwallOrchestrator(
        engine, _AsyncProvider(slow_handler), [STRATEGIST], feed=RadioFeed(), clock=None
    )
    await orch.start()
    try:
        orch.dispatch(_event("pit_window_open", t=10.0))
        orch.dispatch(_event("fuel_critical", t=11.0))
        await asyncio.sleep(0.05)
        assert peak == 1, "the strategist overlapped itself"
        release.set()
        await asyncio.sleep(0.05)
    finally:
        await orch.stop()


async def test_the_global_cap_bounds_in_flight_model_calls(engine):
    concurrent, peak = 0, 0
    release = asyncio.Event()

    async def slow_handler(**_):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await release.wait()
        concurrent -= 1
        return _answer()

    agents = [
        replace(
            STRATEGIST,
            name=f"agent-{i}",
            triggers=(Trigger(EventType.PIT_WINDOW_OPEN.value, cooldown_s=0.0),),
        )
        for i in range(4)
    ]
    orch = PitwallOrchestrator(
        engine, _AsyncProvider(slow_handler), agents,
        feed=RadioFeed(), max_inflight=2, clock=None,
    )
    await orch.start()
    try:
        assert len(orch.dispatch(_event("pit_window_open"))) == 4
        await asyncio.sleep(0.05)
        assert peak == 2, f"cap of 2 in-flight calls not held (peak {peak})"
        release.set()
        await asyncio.sleep(0.05)
    finally:
        await orch.stop()


async def test_a_busy_agent_keeps_a_queued_critical_over_a_later_advisory(engine):
    """A 'box now' must not be displaced by a routine milestone behind it."""
    release = asyncio.Event()
    seen: list[str] = []

    async def slow_handler(*, messages, **_):
        seen.append(messages[0]["content"].splitlines()[0])
        await release.wait()
        return _answer()

    orch = PitwallOrchestrator(
        engine, _AsyncProvider(slow_handler), [STRATEGIST], feed=RadioFeed(), clock=None
    )
    await orch.start()
    try:
        orch.dispatch(_event("fuel_margin_low", t=10.0))  # occupies the worker
        await asyncio.sleep(0.02)
        orch.dispatch(_event("fuel_critical", t=11.0, severity=Severity.CRITICAL))
        orch.dispatch(_event("stint_lap_milestone", t=12.0))  # must not displace it
        release.set()
        await asyncio.sleep(0.05)
    finally:
        await orch.stop()

    assert any("fuel_critical" in line for line in seen), seen
    assert not any("stint_lap_milestone" in line for line in seen), seen
    assert orch.skipped_busy == 1


# --------------------------------------------------------------------------
# the merged feed
# --------------------------------------------------------------------------
async def test_agent_output_lands_on_the_shared_radio_feed(engine):
    orch = _orch(engine)
    messages = await orch.handle_event(_event())
    assert len(messages) == 1
    assert orch.feed.pending() == messages
    assert orch.feed.flush()[0].agent == "strategist"


async def test_two_agents_merge_into_one_prioritised_channel(engine):
    def handler(*, agent, **_):
        if agent == "spotter":
            return _spotter_answer()
        return _answer("Window opens lap five.")

    spotter = replace(
        STRATEGIST,
        name="spotter",
        triggers=(Trigger(EventType.PIT_WINDOW_OPEN.value, cooldown_s=0.0),),
        subject=lambda event, out: "traffic",
    )
    orch = _orch(engine, handler, agents=[STRATEGIST, spotter])
    await orch.handle_event(_event())

    aired = orch.feed.flush()
    assert [m.agent for m in aired] == ["spotter", "strategist"]  # critical first
    assert aired[0].priority is RadioPriority.CRITICAL


def _spotter_answer() -> PitCall:
    return PitCall(
        priority=RadioPriority.CRITICAL,
        spoken_text="Car inside, car inside.",
        detail_text="Overlap on the left.",
        recommendation="hold",
        confidence="high",
        rationale="No figures asserted.",
    )


def test_status_reports_every_agent_and_the_radio_counters(engine):
    status = _orch(engine).status()
    assert status["enabled"] is True
    assert status["max_inflight"] >= 1
    agent = status["agents"][0]
    assert agent["name"] == "strategist"
    assert "simulate_pit_outcome" in agent["tools"]
    assert agent["output_contract"] == "PitCall"
    assert {"invocations", "refusals", "errors"} <= set(agent)
    assert "published" in status["radio"]


# --------------------------------------------------------------------------
# an async provider, for the concurrency tests
# --------------------------------------------------------------------------
class _AsyncProvider:
    """A provider whose handler is a coroutine, so overlap is observable."""

    def __init__(self, handler):
        self._handler = handler

    async def complete(self, **kwargs):
        from rtv.pitwall.provider import ProviderResponse

        result = await self._handler(**kwargs)
        return (
            result
            if isinstance(result, ProviderResponse)
            else ProviderResponse(output=result)
        )
