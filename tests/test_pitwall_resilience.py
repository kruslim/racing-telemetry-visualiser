"""Stage 5: what the pitwall does when the world stops cooperating.

Three failure modes the spec names, each of which is silent by default:

* **iRacing disconnects mid-session.** The engine has no clock of its own, so a
  disconnect does not corrupt the race state -- it freezes it, which is worse.
  "P4, 2.1 s behind" reads identically whether it is current or four minutes old.
* **The model API fails.** One retry, then say "Pitwall AI degraded" out loud and
  keep every deterministic layer running.
* **Cost runs away.** A hard per-session ceiling on billable model calls, with the
  counter, the per-agent call counts and the latencies on `/pitwall/status`.
"""

from __future__ import annotations

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


def _broken_provider(**_):
    raise RuntimeError("401 Unauthorized")


def _first_frames(n: int = 5):
    source = scenario_source(ScenarioSpec())
    frames = []
    for frame in source.frames:
        frames.append(frame)
        if len(frames) >= n:
            break
    return source.catalog, frames


@pytest.fixture(scope="module")
def engine() -> RaceStateEngine:
    eng = RaceStateEngine(source="replay")
    replay_scenario(eng, ScenarioSpec())
    return eng


def _said(orch) -> list:
    """Everything on the channel: aired plus still queued."""
    return orch.feed.history() + orch.feed.pending()


# --------------------------------------------------------------------------
# iRacing disconnecting mid-session
# --------------------------------------------------------------------------
def test_a_disconnect_flags_the_state_rather_than_freezing_it_silently():
    catalog, frames = _first_frames(3)
    eng = RaceStateEngine(source="live")
    for frame in frames:
        eng.on_frame(frame, catalog)
    before = eng.snapshot()

    eng.mark_stale("iRacing telemetry disconnected")

    after = eng.snapshot()
    assert after.stale is True
    assert after.stale_reason == "iRacing telemetry disconnected"
    # The numbers themselves are untouched -- they are the last known ones, and
    # discarding them would lose the picture a driver still needs.
    assert after.player.lap == before.player.lap
    assert after.version > before.version  # a subscriber sees a new snapshot
    assert eng.health()["stale"] is True


def test_marking_stale_twice_keeps_the_first_reason():
    eng = RaceStateEngine(source="live")
    eng.mark_stale("first")
    version = eng.snapshot().version
    eng.mark_stale("second")
    assert eng.snapshot().stale_reason == "first"
    assert eng.snapshot().version == version


def test_a_resume_clears_the_detector_window_across_the_gap():
    # The frame before a four-minute gap is not the predecessor of the one after
    # it. Comparing them would manufacture a lap, a pit stop or a wheel-lock.
    catalog, frames = _first_frames(5)
    eng = RaceStateEngine(source="live")
    for frame in frames:
        eng.on_frame(frame, catalog)
    assert len(eng._window) == len(frames)

    eng.mark_stale("gone")
    eng.mark_live()

    assert eng._window == []
    assert eng.snapshot().stale is False


def test_a_frame_is_its_own_announcement_of_a_resume():
    catalog, frames = _first_frames(3)
    eng = RaceStateEngine(source="live")
    eng.on_frame(frames[0], catalog)
    eng.mark_stale("gone")

    eng.on_frame(frames[1], catalog)

    assert eng.snapshot().stale is False
    assert len(eng._window) == 1  # the pre-gap frame was dropped, not extended


def test_mark_live_on_a_live_engine_does_not_discard_the_window():
    catalog, frames = _first_frames(4)
    eng = RaceStateEngine(source="live")
    for frame in frames:
        eng.on_frame(frame, catalog)
    eng.mark_live()
    assert len(eng._window) == len(frames)


def test_a_reconnect_produces_no_phantom_events():
    """A whole race, dropped and resumed mid-caution on lap 3.

    The gap is placed at a quiet moment on purpose. A disconnect that lands
    exactly on a lap boundary genuinely loses that lap's events -- the engine
    cannot know a lap completed while it was not watching, and inventing one
    would be the failure this whole discipline exists to prevent. What is
    asserted here is the other half: a gap over quiet track must produce the
    *same* log, not a longer one.
    """
    catalog, frames = _first_frames(3360)
    quiet, interrupted = RaceStateEngine(source="live"), RaceStateEngine(source="live")
    for frame in frames:
        quiet.on_frame(frame, catalog)
    for i, frame in enumerate(frames):
        if i == 1500:
            interrupted.mark_stale("iRacing telemetry disconnected")
        interrupted.on_frame(frame, catalog)

    assert len(quiet.bus.history()) == 19  # the whole scripted race
    assert [e.key for e in interrupted.bus.history()] == [
        e.key for e in quiet.bus.history()
    ]


def test_suspending_the_agents_is_not_the_kill_switch(engine):
    orch = _orch(engine)
    orch.suspend("iRacing telemetry disconnected")

    assert orch.suspended is True
    assert orch.enabled is True  # the operator never said stop
    assert orch.dispatch(_event(), engine.snapshot()) == []
    assert orch.status()["suspend_reason"] == "iRacing telemetry disconnected"

    orch.resume()
    assert orch.suspended is False
    assert orch.dispatch(_event(), engine.snapshot()) == ["strategist"]


def test_a_resume_does_not_override_an_operator_who_said_stop(engine):
    orch = _orch(engine)
    orch.set_enabled(False)
    orch.suspend("disconnected")
    orch.resume()
    assert orch.enabled is False
    assert orch.dispatch(_event(), engine.snapshot()) == []


def test_a_resume_clears_the_cooldowns_that_elapsed_during_the_gap(engine):
    orch = _orch(engine)
    state = engine.snapshot()
    assert orch.dispatch(_event(t=10.0), state) == ["strategist"]
    orch.suspend("disconnected")
    orch.resume()
    # Session time did not advance while we were gone, but the race did -- so a
    # cooldown armed before the gap must not silence the first call after it.
    assert orch.dispatch(_event(t=10.0), state) == ["strategist"]


def test_suspend_and_resume_are_idempotent(engine):
    orch = _orch(engine)
    orch.resume()  # not suspended: a no-op, and silent
    assert _said(orch) == []
    orch.suspend("a")
    orch.suspend("b")
    assert orch.status()["suspend_reason"] == "a"
    assert len(_said(orch)) == 1


def test_the_connection_notices_reach_the_radio(engine):
    orch = _orch(engine)
    orch.suspend("iRacing telemetry disconnected")
    aired = _said(orch)
    assert len(aired) == 1
    assert aired[0].priority is RadioPriority.INFO
    assert aired[0].subject == "pitwall_connection"
    assert aired[0].data["suspended"] is True
    orch.feed.pump(0.0)  # it goes to air

    orch.resume()

    said = _said(orch)
    assert [m.data["suspended"] for m in said] == [True, False]


def test_a_reconnect_supersedes_a_disconnect_nobody_heard(engine):
    # Both notices share (agent, subject), so the ordinary radio supersede rule
    # applies: "telemetry back" replaces a "telemetry lost" that never aired.
    # Reading out a disconnect the driver has already driven through would be
    # worse than saying nothing.
    orch = _orch(engine)
    orch.suspend("iRacing telemetry disconnected")
    orch.resume()

    said = _said(orch)
    assert len(said) == 1
    assert said[0].data["suspended"] is False


def test_agents_never_reason_over_a_stale_race_state(engine):
    orch = _orch(engine)
    state = engine.snapshot()
    state.stale = True
    state.stale_reason = "disconnected"
    assert orch.dispatch(_event(), state) == []


# --------------------------------------------------------------------------
# API-failure resilience
# --------------------------------------------------------------------------
async def test_a_failed_model_call_is_retried_exactly_once(engine):
    attempts = {"n": 0}

    def flaky(**_):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("502 Bad Gateway")
        return _answer("Recovered, stay out.")

    orch = _orch(engine, handler=flaky)
    messages = await orch.handle_event(_event(), engine.snapshot())

    assert attempts["n"] == 2
    assert orch.runtimes["strategist"].retries == 1
    assert [m.spoken_text for m in messages] == ["Recovered, stay out."]
    assert orch.status()["agents"][0]["degraded"] is False


async def test_it_is_not_retried_twice(engine):
    attempts = {"n": 0}

    def always_broken(**_):
        attempts["n"] += 1
        raise RuntimeError("502 Bad Gateway")

    orch = _orch(engine, handler=always_broken)
    assert await orch.handle_event(_event(), engine.snapshot()) == []
    # A pit call that lands three corners late is worse than no call, so the
    # policy is one retry, not an exponential backoff.
    assert attempts["n"] == 2


async def test_a_persistent_failure_says_pitwall_ai_degraded_on_the_radio(engine):
    orch = _orch(engine, handler=_broken_provider, failure_limit=0)
    await orch.handle_event(_event(), engine.snapshot())

    said = _said(orch)
    assert len(said) == 1
    assert "Pitwall AI degraded" in said[0].spoken_text
    assert said[0].priority is RadioPriority.INFO
    assert said[0].data["degraded"] is True
    assert said[0].data["agent"] == "strategist"
    assert said[0].subject == "pitwall_degraded"
    assert orch.status()["agents"][0]["degraded"] is True


async def test_the_degraded_notice_is_said_once_per_episode(engine):
    orch = _orch(engine, handler=_broken_provider, failure_limit=0)
    state = engine.snapshot()
    for i in range(4):
        await orch.handle_event(_event(t=100.0 + i * 100), state)

    assert len([m for m in _said(orch) if m.data.get("degraded")]) == 1


async def test_a_recovery_re_arms_the_notice(engine):
    broken = {"yes": True}

    def handler(**_):
        if broken["yes"]:
            raise RuntimeError("401 Unauthorized")
        return _answer()

    orch = _orch(engine, handler=handler, failure_limit=0)
    state = engine.snapshot()
    await orch.handle_event(_event(t=100.0), state)
    assert orch.status()["agents"][0]["degraded"] is True
    orch.feed.pump(100.0)

    broken["yes"] = False
    await orch.handle_event(_event(t=400.0), state)
    assert orch.status()["agents"][0]["degraded"] is False
    orch.feed.pump(400.0)

    broken["yes"] = True
    await orch.handle_event(_event(t=700.0), state)

    assert orch.status()["agents"][0]["degraded"] is True
    assert len([m for m in _said(orch) if m.data.get("degraded")]) == 2


async def test_the_deterministic_layers_keep_running_through_it(engine):
    orch = _orch(engine, handler=_broken_provider, failure_limit=0)
    published = engine.bus.stats()["published"]

    await orch.handle_event(_event(), engine.snapshot())

    # The engine, its event log and the race state are untouched by a dead
    # model. That is the whole point of doing the arithmetic upstream.
    assert engine.bus.stats()["published"] == published
    assert engine.snapshot().metrics.dropped_frames == 0
    assert engine.snapshot().stale is False


# --------------------------------------------------------------------------
# the session cost guard
# --------------------------------------------------------------------------
async def test_llm_calls_are_counted_per_agent_and_per_session(engine):
    orch = _orch(engine)
    await orch.handle_event(_event(), engine.snapshot())

    row = orch.status()["agents"][0]
    assert row["llm_calls"] >= 1
    assert row["invocations"] == 1
    assert row["last_latency_ms"] is not None
    assert row["avg_latency_ms"] is not None
    assert orch.status()["calls_used"] == row["llm_calls"]


async def test_the_cost_guard_is_a_hard_stop(engine):
    orch = _orch(engine, max_calls_per_session=1)
    state = engine.snapshot()

    await orch.handle_event(_event(t=100.0), state)
    assert orch.budget_exhausted is True
    spent = orch.calls_used

    accepted = await orch.handle_event(_event(t=500.0), state)

    assert accepted == []
    assert orch.calls_used == spent  # not one further token
    assert orch.status()["calls_remaining"] == 0
    assert orch.healthy() is False


async def test_the_cost_guard_says_so_on_the_radio_once(engine):
    orch = _orch(engine, max_calls_per_session=1)
    state = engine.snapshot()
    for i in range(4):
        await orch.handle_event(_event(t=100.0 + i * 100), state)

    notices = [m for m in _said(orch) if m.data.get("budget_exhausted")]
    assert len(notices) == 1
    assert "budget reached" in notices[0].spoken_text
    assert notices[0].data["max_calls"] == 1


async def test_zero_means_unlimited(engine):
    orch = _orch(engine, max_calls_per_session=0)
    state = engine.snapshot()
    for i in range(3):
        await orch.handle_event(_event(t=100.0 + i * 100), state)
    assert orch.budget_exhausted is False
    assert orch.calls_remaining is None
    assert orch.status()["max_calls_per_session"] is None


async def test_reset_gives_a_new_session_a_fresh_budget(engine):
    orch = _orch(engine, max_calls_per_session=1)
    state = engine.snapshot()
    await orch.handle_event(_event(t=100.0), state)
    assert orch.budget_exhausted is True
    lifetime = orch.runtimes["strategist"].turns

    orch.reset()

    assert orch.calls_used == 0
    assert orch.budget_exhausted is False
    # ...without discarding the lifetime counters the status endpoint reports.
    assert orch.runtimes["strategist"].turns == lifetime
    assert await orch.handle_event(_event(t=100.0), state)
