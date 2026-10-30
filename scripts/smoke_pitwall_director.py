"""Offline smoke test of the stage-5 seam and the hardened live path.

No iRacing, no API key, no network. It answers the two questions stage 5 exists
to answer:

    1. Is the race-director seam real? A scripted full-course yellow, loaded from
       docs/director_scenario.example.json, is fed through the director hook and
       compared -- routing, agents, priority, radio -- against the yellow the
       scripted race actually threw. If anything downstream branched on "was this
       real?", these would diverge.

    2. Does the live path survive things going wrong? A frame that raises,
       iRacing disconnecting mid-session, a model API that fails, an agent whose
       provider is dead, a session that runs away with the budget, a supervised
       task that dies, and a replay attempted on top of a live poller.

Run:  python scripts/smoke_pitwall_director.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtv.director import (  # noqa: E402
    DIRECTOR_ID_KEY,
    DIRECTOR_KIND_KEY,
    INJECTED_KEY,
    DirectorEngine,
    NoopDirector,
    ScenarioScript,
    injected_event,
    is_injected,
)
from rtv.pitwall.agents import AGENT_REGISTRY  # noqa: E402
from rtv.pitwall.agents.strategist import PitCall  # noqa: E402
from rtv.pitwall.framework import RadioPriority  # noqa: E402
from rtv.pitwall.orchestrator import PitwallOrchestrator  # noqa: E402
from rtv.pitwall.provider import ScriptedProvider  # noqa: E402
from rtv.pitwall.radio import RadioFeed  # noqa: E402
from rtv.racestate import RaceStateEngine, replay_scenario  # noqa: E402
from rtv.racestate.replay import scenario_source  # noqa: E402
from rtv.racestate.scenario import ScenarioSpec  # noqa: E402

PASS, FAIL = "  ok  ", " FAIL "
failures: list[str] = []

EXAMPLE = ROOT / "docs" / "director_scenario.example.json"


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{PASS if condition else FAIL}] {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def _answer(text="Yellow out, hold station, we stay out.") -> PitCall:
    return PitCall(
        priority=RadioPriority.ADVISORY,
        spoken_text=text,
        detail_text=text,
        recommendation="hold",
        confidence="medium",
        rationale="No figures asserted.",
    )


def _orch(engine, handler=None, **kw) -> PitwallOrchestrator:
    return PitwallOrchestrator(
        engine,
        ScriptedProvider(handler or (lambda **_: _answer())),
        kw.pop("agents", list(AGENT_REGISTRY)),
        feed=kw.pop("feed", RadioFeed()),
        clock=None,
        **kw,
    )


class ScriptDirector:
    """A director in a dozen lines -- the point being that that is all it takes.

    It handles one condition (``at_session_time``) and one entry. Everything a
    real implementation adds is scheduling; the seam is already the same.
    """

    name = "smoke-script"

    def __init__(self, script: ScenarioScript, injection_id: str, *, at: float) -> None:
        self.injection = script.injection(injection_id)
        self.at = at
        self.fired = False

    def bind(self, script):  # pragma: no cover - the script arrives in __init__
        pass

    def poll(self, state):
        if self.fired or state.session_time < self.at:
            return ()
        self.fired = True
        return (injected_event(self.injection, state),)

    def reset(self):
        self.fired = False

    def describe(self):
        return {"director": self.name, "fired": self.fired}


def main() -> int:  # noqa: PLR0915 - a smoke script is a checklist
    print("\n== stage 5: the director seam and the hardened live path ==\n")

    # -- the script -------------------------------------------------------
    script = ScenarioScript.load(EXAMPLE)
    kinds = sorted({i.kind.value for i in script.injections})
    check(
        "the shipped example script loads and validates",
        len(script.injections) == 5,
        f"{script.name!r}: {len(script.injections)} injections",
    )
    check(
        "it covers a flag, a weather step and a regulation change",
        {"flag", "weather", "regulation"} <= set(kinds),
        ", ".join(kinds),
    )

    # -- a real race, and its real yellow ---------------------------------
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    real = next(e for e in engine.bus.history() if e.key == "flag_change:yellow")
    check("the scripted race threw a real full-course yellow", real is not None,
          f"tick {real.tick}, lap {real.lap}")

    state = engine.snapshot()
    scripted = injected_event(
        script.injection("fcy_lap_3"),
        state,
        tick=real.tick,
        session_time=real.session_time,
        lap=real.lap,
    )

    # -- 1. the seam ------------------------------------------------------
    check(
        "an injected yellow is the same event type, key and severity",
        (scripted.event_type, scripted.key, scripted.severity)
        == (real.event_type, real.key, real.severity),
        scripted.key,
    )
    extra = set(scripted.payload) - set(real.payload)
    check(
        "it carries the real detector's payload keys, plus provenance",
        set(real.payload) <= set(scripted.payload)
        and extra == {INJECTED_KEY, DIRECTOR_KIND_KEY, DIRECTOR_ID_KEY},
        "extra: " + ", ".join(sorted(extra)),
    )
    check(
        "provenance is honest in both directions",
        is_injected(scripted) and not is_injected(real),
    )

    detected_orch, injected_orch = _orch(engine), _orch(engine)
    routed_real = detected_orch.dispatch(real, state)
    routed_scripted = injected_orch.dispatch(scripted, state)
    check(
        "both wake exactly the same agents",
        routed_scripted == routed_real and routed_real,
        ", ".join(routed_real),
    )

    detected_orch.reset()
    injected_orch.reset()
    from_detector = asyncio.run(detected_orch.handle_event(real, state))
    from_director = asyncio.run(injected_orch.handle_event(scripted, state))
    check(
        "and produce the same radio, call for call",
        [(m.agent, m.priority, m.subject, m.spoken_text) for m in from_director]
        == [(m.agent, m.priority, m.subject, m.spoken_text) for m in from_detector]
        and bool(from_director),
        f"{len(from_director)} calls",
    )

    # -- the injection travels the ordinary bus ---------------------------
    engine_2 = RaceStateEngine(source="replay")
    replay_scenario(engine_2, ScenarioSpec())
    seen = []
    engine_2.bus.subscribe_callback(seen.append)
    seam_orch = _orch(engine_2, director=ScriptDirector(script, "fcy_lap_3", at=0.0))
    injected = seam_orch.poll_director()
    check(
        "an injected event goes out on the engine's own event bus",
        seen == injected and engine_2.bus.history(1)[0] is injected[0],
        f"{len(injected)} published, ring buffer updated",
    )
    check("once means once", seam_orch.poll_director() == [] and seam_orch.injected == 1)

    # -- the default is silent, and says so -------------------------------
    default = NoopDirector(script)
    check(
        "the default director never injects and reports itself as planned",
        default.poll(state) == ()
        and default.describe()["implemented"] is False
        and default.describe()["status"] == "planned",
        default.describe()["reason"][:60] + "...",
    )
    check(
        "both directors satisfy the published protocol",
        isinstance(default, DirectorEngine)
        and isinstance(ScriptDirector(script, "fcy_lap_3", at=0.0), DirectorEngine),
    )

    # -- 2. the hardened live path ----------------------------------------
    print()
    source = scenario_source(ScenarioSpec())
    frames = []
    for frame in source.frames:
        frames.append(frame)
        if len(frames) >= 5:
            break

    strict = RaceStateEngine(source="replay")
    strict.bind_catalog(source.catalog)
    strict._update = lambda frame: 1 / 0  # noqa: SLF001 - deliberate sabotage
    try:
        strict.on_frame(frames[0], source.catalog)
        raised = False
    except ZeroDivisionError:
        raised = True
    check("a strict engine still fails loudly (the test/replay default)", raised)

    live = RaceStateEngine(source="live", resilient=True)
    live.bind_catalog(source.catalog)
    live._update = lambda frame: 1 / 0  # noqa: SLF001
    for frame in frames:
        live.on_frame(frame, source.catalog)
    check(
        "a resilient engine counts the fault and keeps running",
        live.health()["faults"] == len(frames)
        and live.snapshot().metrics.dropped_frames == len(frames),
        live.health()["last_fault"],
    )

    healthy = RaceStateEngine(source="replay")
    replay_scenario(healthy, ScenarioSpec())
    check(
        "health() tells 'never started' apart from 'running'",
        RaceStateEngine(source="live").health()["seconds_since_frame"] is None
        and healthy.health()["seconds_since_frame"] is not None
        and healthy.health()["frames"] > 3000,
        f"{healthy.health()['frames']} frames, avg "
        f"{healthy.health()['avg_update_ms']} ms",
    )

    # -- iRacing disconnecting mid-session ---------------------------------
    live.mark_stale("iRacing telemetry disconnected")
    check(
        "a disconnect flags the state instead of freezing it silently",
        live.snapshot().stale is True and live.health()["stale"] is True,
        live.snapshot().stale_reason,
    )
    # A whole race, dropped and resumed mid-caution on lap 3. The resume must
    # invent nothing: the frame before a gap is not the predecessor of the one
    # after it, so the detector window is thrown away rather than straddled.
    quiet, interrupted = RaceStateEngine(source="live"), RaceStateEngine(source="live")
    all_frames = list(scenario_source(ScenarioSpec()).frames)
    for frame in all_frames:
        quiet.on_frame(frame, source.catalog)
    for i, frame in enumerate(all_frames):
        if i == 1500:
            interrupted.mark_stale("iRacing telemetry disconnected")
        interrupted.on_frame(frame, source.catalog)
    check(
        "a reconnect manufactures no phantom events across the gap",
        [e.key for e in interrupted.bus.history()] == [e.key for e in quiet.bus.history()],
        f"{len(quiet.bus.history())} events either way, over a whole race",
    )

    conn_orch = _orch(healthy, agents=[AGENT_REGISTRY[0]], retry_backoff_s=0.0)
    conn_orch.suspend("iRacing telemetry disconnected")
    suspended_routed = conn_orch.dispatch(real, state)
    conn_orch.resume()
    check(
        "agents stand down on a disconnect without touching the kill switch",
        suspended_routed == []
        and conn_orch.enabled is True
        and conn_orch.dispatch(real, state) == ["strategist"],
    )

    # -- a model API that fails --------------------------------------------
    attempts = {"n": 0}

    def flaky(**_):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("502 Bad Gateway")
        return _answer("Recovered, hold station.")

    retry_orch = _orch(
        healthy, handler=flaky, agents=[AGENT_REGISTRY[0]], retry_backoff_s=0.0
    )
    recovered = asyncio.run(retry_orch.handle_event(real, state))
    check(
        "a failed model call is retried exactly once, then succeeds",
        attempts["n"] == 2 and len(recovered) == 1,
        f"{retry_orch.runtimes['strategist'].retries} retry",
    )

    def dead_provider(**_):
        raise RuntimeError("401 Unauthorized")

    degraded_orch = _orch(
        healthy, handler=dead_provider, agents=[AGENT_REGISTRY[0]],
        failure_limit=0, retry_backoff_s=0.0,
    )
    asyncio.run(degraded_orch.handle_event(real, state))
    notice = (degraded_orch.feed.history() + degraded_orch.feed.pending())[0]
    check(
        "a persistent failure says so on the radio, once",
        "Pitwall AI degraded" in notice.spoken_text
        and notice.priority is RadioPriority.INFO,
        notice.spoken_text,
    )
    check(
        "...and the deterministic layers are untouched by it",
        healthy.snapshot().metrics.dropped_frames == 0
        and healthy.snapshot().stale is False,
    )

    breaker_orch = _orch(
        healthy, handler=dead_provider, agents=[AGENT_REGISTRY[0]],
        failure_limit=3, retry_backoff_s=0.0,
    )
    for i in range(4):
        asyncio.run(breaker_orch.handle_event(real.model_copy(
            update={"session_time": 100.0 + i * 100}
        ), state))
    row = next(a for a in breaker_orch.status()["agents"] if a["name"] == "strategist")
    check(
        "a dead provider takes the agent off the air after 3 tries",
        breaker_orch.agent_enabled("strategist") is False
        and breaker_orch.runtimes["strategist"].invocations == 3,
        row["disabled_reason"],
    )

    # -- the session cost guard --------------------------------------------
    budget_orch = _orch(
        healthy, agents=[AGENT_REGISTRY[0]], max_calls_per_session=1,
        retry_backoff_s=0.0,
    )
    asyncio.run(budget_orch.handle_event(real.model_copy(
        update={"session_time": 100.0}), state))
    spent = budget_orch.calls_used
    after = asyncio.run(budget_orch.handle_event(real.model_copy(
        update={"session_time": 900.0}), state))
    check(
        "the session cost guard is a hard stop, not a warning",
        budget_orch.budget_exhausted and after == [] and budget_orch.calls_used == spent,
        f"{spent} model call(s) used, limit 1, then silence",
    )
    status = budget_orch.status()
    check(
        "per-agent call counts and latencies are on /pitwall/status",
        status["agents"][0]["llm_calls"] >= 1
        and status["agents"][0]["last_latency_ms"] is not None
        and status["calls_remaining"] == 0,
        f"llm_calls={status['agents'][0]['llm_calls']}, "
        f"last={status['agents'][0]['last_latency_ms']}ms",
    )

    async def supervision() -> tuple[int, dict]:
        orch = _orch(healthy)
        calls = {"n": 0}

        async def flaky() -> None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            await asyncio.sleep(3600)

        task = asyncio.create_task(orch._supervise("flaky", flaky, backoff=0.0))  # noqa: SLF001
        for _ in range(200):
            if calls["n"] >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        return calls["n"], orch.status()

    attempts, status = asyncio.run(supervision())
    check(
        "a dying background task is restarted and the restart is visible",
        attempts == 3 and status["restarts"] == {"flaky": 2} and status["healthy"] is False,
        f"restarts: {status['restarts']}",
    )

    # -- the live/replay interlock, through the real HTTP surface ---------
    try:
        interlock = _http_interlock()
    except ImportError as exc:  # pragma: no cover - fastapi/duckdb are optional
        print(f"[ skip ] the live/replay interlock ({exc})")
    else:
        check(
            "a replay is refused while the live poller is running",
            interlock == 409,
            f"HTTP {interlock}",
        )

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("Director seam and live-path hardening verified. Offline, no key, no network.")
    print(
        "\nThe director itself is PLANNED: NoopDirector injects nothing. "
        "See docs/PITWALL.md (stage 5)."
    )
    return 0


def _http_interlock() -> int:
    """Start the real app, pretend the poller is live, and ask for a replay."""
    import tempfile

    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["RTV_DATA_DIR"] = tmp
        os.environ["RTV_AUTOSTART_LIVE"] = "false"
        os.environ["RTV_PITWALL"] = "true"
        from rtv.config import get_settings
        from rtv.main import create_app

        get_settings.cache_clear()
        with TestClient(create_app()) as client:
            poller = type(client.app.state.services.poller)
            original = poller.running
            poller.running = property(lambda self: True)
            try:
                return client.post(
                    "/api/v1/replay/start", json={"session_id": "scenario"}
                ).status_code
            finally:
                poller.running = original


if __name__ == "__main__":
    raise SystemExit(main())
