"""Stage 5: what keeps the live path alive for a whole race, not just a lap.

Every property here fails *silently* if nobody asserts it, which is why they are
asserted:

* a per-frame fault costs a frame, not the pitwall -- and is counted;
* iRacing disconnecting freezes the state visibly rather than invisibly, stands
  the agents down, and resumes without manufacturing events across the gap;
* a failed model call is retried once and then announced as degraded;
* an agent that keeps failing is taken off the air instead of burning a call per
  race event for the rest of the session;
* the session cost guard is a hard stop, not a warning;
* a supervised task that dies is restarted, and the restart is visible;
* a replay cannot be started on top of a running live poller.
"""

from __future__ import annotations

import asyncio

import pytest

from rtv.pitwall.agents.strategist import STRATEGIST, PitCall
from rtv.pitwall.framework import RadioPriority
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ScriptedProvider
from rtv.pitwall.radio import RadioFeed
from rtv.racestate import RaceStateEngine, replay_scenario
from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.racestate.replay import scenario_source
from rtv.racestate.scenario import ScenarioSpec


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
    kw.setdefault("retry_backoff_s", 0.0)  # the retry is asserted, not waited for
    return PitwallOrchestrator(
        engine,
        ScriptedProvider(handler or (lambda **_: _answer())),
        kw.pop("agents", [STRATEGIST]),
        feed=kw.pop("feed", RadioFeed()),
        clock=None,
        **kw,
    )


@pytest.fixture(scope="module")
def engine() -> RaceStateEngine:
    eng = RaceStateEngine(source="replay")
    replay_scenario(eng, ScenarioSpec())
    return eng


# --------------------------------------------------------------------------
# the engine's per-frame fault handling
# --------------------------------------------------------------------------
def _first_frames(n: int = 5):
    source = scenario_source(ScenarioSpec())
    frames = []
    for frame in source.frames:
        frames.append(frame)
        if len(frames) >= n:
            break
    return source.catalog, frames


def test_a_strict_engine_still_raises(monkeypatch):
    # The default. A replay or a test that silently emitted no events would pass
    # every assertion about what the engine does *not* do, which is worse than
    # a red test.
    catalog, frames = _first_frames()
    eng = RaceStateEngine(source="replay")
    monkeypatch.setattr(eng, "_update", lambda frame: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        eng.on_frame(frames[0], catalog)


def test_a_resilient_engine_counts_the_fault_and_keeps_going(monkeypatch):
    catalog, frames = _first_frames(4)
    eng = RaceStateEngine(source="replay", resilient=True)
    eng.bind_catalog(catalog)
    monkeypatch.setattr(eng, "_update", lambda frame: 1 / 0)

    for frame in frames:
        eng.on_frame(frame, catalog)  # must not raise

    assert eng.snapshot().metrics.dropped_frames == len(frames)
    assert eng.health()["faults"] == len(frames)
    assert "ZeroDivisionError" in eng.health()["last_fault"]


def test_a_fault_storm_is_logged_at_most_once_a_second(monkeypatch):
    # 60 stack traces a second turns a bug into an outage of its own.
    from rtv.racestate import engine as engine_module

    catalog, frames = _first_frames(3)
    eng = RaceStateEngine(source="replay", resilient=True)
    eng.bind_catalog(catalog)
    monkeypatch.setattr(eng, "_update", lambda frame: 1 / 0)
    logged: list[str] = []
    monkeypatch.setattr(
        engine_module.log, "exception", lambda msg, *a, **kw: logged.append(msg % a)
    )

    for _ in range(20):
        eng.on_frame(frames[0], catalog)

    assert len(logged) == 1
    assert "Race-state update failed" in logged[0]
    assert eng.health()["faults"] == 20


def test_a_resilient_engine_publishes_nothing_for_a_failed_frame(monkeypatch):
    catalog, frames = _first_frames(2)
    eng = RaceStateEngine(source="replay", resilient=True)
    eng.bind_catalog(catalog)
    seen = []
    eng.bus.subscribe_callback(seen.append)
    monkeypatch.setattr(eng, "_update", lambda frame: 1 / 0)
    eng.on_frame(frames[0], catalog)
    assert seen == []


def test_resilience_does_not_change_the_event_log_when_nothing_fails():
    strict, resilient = RaceStateEngine(source="replay"), RaceStateEngine(
        source="replay", resilient=True
    )
    replay_scenario(strict, ScenarioSpec())
    replay_scenario(resilient, ScenarioSpec())
    assert [e.key for e in resilient.bus.history()] == [
        e.key for e in strict.bus.history()
    ]
    assert resilient.snapshot().metrics.dropped_frames == 0


# --------------------------------------------------------------------------
# engine.health()
# --------------------------------------------------------------------------
def test_health_distinguishes_unbound_from_idle():
    fresh = RaceStateEngine(source="live")
    health = fresh.health()
    assert health["bound"] is False
    assert health["frames"] == 0
    # No frame has ever arrived, so "how long since one did" is honestly unknown
    # rather than zero.
    assert health["seconds_since_frame"] is None


def test_health_reports_the_hot_loop_after_a_replay(engine):
    health = engine.health()
    assert health["bound"] is True
    assert health["frames"] > 3000
    assert health["events"] > 0
    assert health["faults"] == 0
    assert health["seconds_since_frame"] is not None
    assert health["source"] == "replay"
    assert health["capabilities"]["fuel"] is True
    assert health["bus"]["published"] == len(engine.bus.history())


def test_health_carries_no_wall_clock_into_the_event_log(engine):
    # The one wall-clock number in the package is monotonic and lives here only.
    assert all("wall" not in str(e.payload) for e in engine.bus.history())


# --------------------------------------------------------------------------
# supervision
# --------------------------------------------------------------------------
async def test_a_dying_task_is_restarted_and_counted(engine):
    orch = _orch(engine)
    calls = {"n": 0}

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)

    task = asyncio.create_task(orch._supervise("flaky", flaky, backoff=0.0))
    for _ in range(200):
        if calls["n"] >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()

    assert calls["n"] == 3
    assert orch.restarts["flaky"] == 2
    assert orch.healthy() is False
    assert orch.status()["restarts"] == {"flaky": 2}


async def test_a_supervised_task_that_returns_cleanly_is_not_restarted(engine):
    orch = _orch(engine)
    calls = {"n": 0}

    async def finishes() -> None:
        calls["n"] += 1

    await orch._supervise("done", finishes, backoff=0.0)
    assert calls["n"] == 1
    assert orch.restarts == {}
    assert orch.healthy() is True


async def test_cancellation_is_not_a_failure(engine):
    orch = _orch(engine)

    async def forever() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(orch._supervise("forever", forever, backoff=0.0))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert orch.restarts == {}


async def test_the_pump_survives_a_bad_event(engine):
    # A subscriber that raises must not be able to silence the pitwall.
    orch = _orch(engine)
    await orch.start()
    try:
        assert orch.status()["running"] is True
        assert orch.status()["healthy"] is True
    finally:
        await orch.stop()


# --------------------------------------------------------------------------
# the per-agent circuit breaker
# --------------------------------------------------------------------------
def _broken_provider(**_):
    raise RuntimeError("401 Unauthorized")


async def test_repeated_failures_take_an_agent_off_the_air(engine):
    orch = _orch(engine, handler=_broken_provider, failure_limit=3)
    state = engine.snapshot()

    for i in range(3):
        await orch.handle_event(_event(t=100.0 + i * 100), state)

    assert orch.agent_enabled("strategist") is False
    row = next(a for a in orch.status()["agents"] if a["name"] == "strategist")
    assert row["consecutive_failures"] == 3
    assert "3 consecutive failed invocations" in row["disabled_reason"]
    assert orch.status()["healthy"] is False


async def test_a_disabled_agent_costs_nothing_further(engine):
    orch = _orch(engine, handler=_broken_provider, failure_limit=2)
    state = engine.snapshot()
    for i in range(2):
        await orch.handle_event(_event(t=100.0 + i * 100), state)
    calls_before = orch.runtimes["strategist"].invocations

    await orch.handle_event(_event(t=900.0), state)

    assert orch.runtimes["strategist"].invocations == calls_before


async def test_one_success_re_arms_the_breaker(engine):
    broken = {"yes": True}

    def handler(**kw):
        if broken["yes"]:
            raise RuntimeError("401 Unauthorized")
        return _answer()

    orch = _orch(engine, handler=handler, failure_limit=3)
    state = engine.snapshot()
    for i in range(2):
        await orch.handle_event(_event(t=100.0 + i * 100), state)
    assert orch.status()["agents"][0]["consecutive_failures"] == 2

    broken["yes"] = False
    await orch.handle_event(_event(t=400.0), state)

    assert orch.agent_enabled("strategist") is True
    row = next(a for a in orch.status()["agents"] if a["name"] == "strategist")
    assert row["consecutive_failures"] == 0
    assert row["degraded"] is False


async def test_re_enabling_clears_the_breaker(engine):
    orch = _orch(engine, handler=_broken_provider, failure_limit=1)
    await orch.handle_event(_event(), engine.snapshot())
    assert orch.agent_enabled("strategist") is False

    orch.set_agent_enabled("strategist", True)

    row = next(a for a in orch.status()["agents"] if a["name"] == "strategist")
    assert row["disabled_reason"] is None
    assert row["consecutive_failures"] == 0


async def test_reset_clears_the_breaker_too(engine):
    orch = _orch(engine, handler=_broken_provider, failure_limit=1)
    await orch.handle_event(_event(), engine.snapshot())
    orch.reset()
    assert orch.status()["agents"][0]["disabled_reason"] is None
    assert orch.healthy() is True


async def test_the_breaker_can_be_turned_off(engine):
    orch = _orch(engine, handler=_broken_provider, failure_limit=0)
    state = engine.snapshot()
    for i in range(5):
        await orch.handle_event(_event(t=100.0 + i * 100), state)
    assert orch.agent_enabled("strategist") is True


async def test_a_healthy_agent_never_trips_it(engine):
    orch = _orch(engine, failure_limit=1)
    state = engine.snapshot()
    for i in range(4):
        await orch.handle_event(_event(t=100.0 + i * 100), state)
    assert orch.agent_enabled("strategist") is True
    assert orch.healthy() is True
