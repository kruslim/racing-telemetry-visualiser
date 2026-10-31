"""Live visual notes are review material by default, and share the session call budget.

Task G4 (REVIEW.md P2-5). A live ``visual_observation`` still goes on the bus (the
recorder journals it, the timeline and the review show it) but wakes the spotter or
coach only with ``RTV_VISION_LIVE_RADIO=true``. Every live VLM call counts against
the pitwall's session budget, so its "max calls" is every model call in the session.

Offline: scripted providers stand in for both models.
"""

from __future__ import annotations

import json
import time

import pytest
from evals.synthetic_race import ScenarioSpec, replay_scenario

from rtv.config import Settings
from rtv.pitwall.activity import ActivityLog
from rtv.pitwall.agents import AGENT_REGISTRY
from rtv.pitwall.agents.strategist import STRATEGIST, PitCall
from rtv.pitwall.framework import RadioPriority
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ScriptedProvider
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.vision.analyzer import VisionAnalyzer
from rtv.vision.live_worker import LiveSource, LiveVisionWorker
from rtv.vision.models import VisionAnswer
from rtv.vision.store import VisionStore

JPEG = b"\xff\xd8\xff\xe0frame\xff\xd9"


def _until(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def _visual(kind: str = "traffic") -> RaceEvent:
    return RaceEvent(
        event_type=EventType.VISUAL_OBSERVATION, tick=1000, session_time=420.0, lap=5,
        severity=Severity.INFO,
        payload={"source": "vision", "kind": kind, "text": "words", "confidence": "medium"},
    )  # fmt: skip


def _never(**_):  # pragma: no cover - routing tests never reach the model
    raise AssertionError("no model call expected")


# ---- routing ------------------------------------------------------------------


@pytest.fixture(scope="module")
def raced() -> RaceStateEngine:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    return engine


def _pitwall(engine, **kw) -> PitwallOrchestrator:
    return PitwallOrchestrator(
        engine, ScriptedProvider(kw.pop("handler", _never)), kw.pop("agents", AGENT_REGISTRY),
        clock=None, retry_backoff_s=0.0, **kw,
    )  # fmt: skip


def test_the_flag_defaults_off():
    assert Settings().vision_live_radio is False


def test_a_visual_note_stays_off_the_radio_by_default(raced):
    state = raced.snapshot()
    if state.flags.phase.value not in ("green", "unknown"):
        pytest.skip("the scenario's final state is under a caution")
    activity = ActivityLog()
    pitwall = _pitwall(raced, activity=activity)
    assert pitwall.vision_radio is False
    assert pitwall.dispatch(_visual("traffic"), state) == []
    assert pitwall.dispatch(_visual("line"), state) == []
    # The timeline still has it: a dispatch record that says why nobody woke.
    blocked = [r for r in activity.history() if r["kind"] == "dispatch"]
    assert [r["data"]["blocked"] for r in blocked] == ["vision_review_only"] * 2
    assert blocked[0]["data"]["event_type"] == "visual_observation"
    assert pitwall.dispatched == 0


def test_the_flag_puts_visual_notes_back_on_the_radio(raced):
    state = raced.snapshot()
    if state.flags.phase.value not in ("green", "unknown"):
        pytest.skip("the scenario's final state is under a caution")
    pitwall = _pitwall(raced, vision_radio=True)
    assert pitwall.dispatch(_visual("traffic"), state) == ["spotter"]
    assert pitwall.dispatch(_visual("line"), state) == ["coach"]


def test_the_flag_can_be_turned_on_from_the_env(monkeypatch):
    monkeypatch.setenv("RTV_VISION_LIVE_RADIO", "true")
    assert Settings().vision_live_radio is True


# ---- the shared budget ---------------------------------------------------------


def test_vision_calls_count_in_the_session_budget(raced):
    pitwall = _pitwall(raced, agents=[STRATEGIST], max_calls_per_session=3)
    pitwall.record_call("vision")
    pitwall.record_call("vision")
    status = pitwall.status()
    assert status["calls_used"] == 2
    assert status["calls_by_layer"] == {"agents": 0, "vision": 2}
    assert status["calls_remaining"] == 1
    pitwall.record_call("vision")
    assert pitwall.budget_exhausted is True
    # Spent by vision alone, the agents stand down like any spent budget.
    assert pitwall.dispatch(_pit_window(), raced.snapshot()) == []
    pitwall.reset()
    assert pitwall.calls_by_layer == {"agents": 0}
    assert pitwall.budget_exhausted is False


def _pit_window() -> RaceEvent:
    return RaceEvent(
        event_type=EventType.PIT_WINDOW_OPEN, tick=6000, session_time=100.0, lap=5,
        severity=Severity.ADVISORY, payload={"lap": 5},
    )  # fmt: skip


async def test_agents_and_vision_share_one_budget(raced):
    def handler(**_):
        return PitCall(
            priority=RadioPriority.ADVISORY, spoken_text="Stay out, no stop needed.",
            detail_text="Stay out.", recommendation="stay_out", confidence="high",
            rationale="No figures asserted.",
        )  # fmt: skip

    pitwall = _pitwall(raced, agents=[STRATEGIST], handler=handler, max_calls_per_session=0)
    await pitwall.handle_event(_pit_window(), raced.snapshot())
    agents = pitwall.calls_by_layer["agents"]
    assert agents >= 1
    pitwall.record_call("vision")
    assert pitwall.calls_by_layer == {"agents": agents, "vision": 1}
    assert pitwall.calls_used == agents + 1


class Source(LiveSource):
    kind = "capture"
    video_id = "cap1"

    def __init__(self) -> None:
        self.gate: list[bool] = [True]

    async def ready(self, t_end, timeout_s):
        return self.gate[0]

    def grab(self):
        async def g(st):
            return JPEG, st - 90.0

        return g


class Budget:
    """The orchestrator's budget surface, with a hook to spend it mid-flight."""

    def __init__(self, remaining: int | None) -> None:
        self.remaining = remaining
        self.recorded: list[str] = []

    @property
    def calls_remaining(self) -> int | None:
        return self.remaining

    def record_call(self, layer: str) -> None:
        self.recorded.append(layer)
        if self.remaining is not None:
            self.remaining -= 1


def _answer(**_):
    return VisionAnswer.model_validate({
        "trigger_visible": "yes",
        "observations": [{"kind": "line", "text": "Ran wide over the exit kerb.",
                          "confidence": "high"}],
    })  # fmt: skip


def _worker(tmp_path, budget, *, source=None, max_calls=10, calls=None):
    engine = RaceStateEngine(source="replay")
    engine.set_session_id("s1")
    store = VisionStore(tmp_path / "sessions")

    def handler(**kw):
        if calls is not None:
            calls.append(kw)
        return _answer()

    worker = LiveVisionWorker(
        engine=engine, store=store,
        analyzer_factory=lambda: VisionAnalyzer(ScriptedProvider(handler), None, store,
                                                model="kimi-k3", frames_per_moment=2),
        source_for=lambda sid: source or Source(),
        enabled=lambda: True, triggers=frozenset({"offtrack"}),
        max_calls=max_calls, min_gap_s=5.0, budget=budget,
    )  # fmt: skip
    worker.start()
    return engine, store, worker


def _off(t: float) -> RaceEvent:
    return RaceEvent(
        event_type=EventType.OFFTRACK,
        tick=int(t * 60),
        session_time=t,
        lap=3,
        severity=Severity.ADVISORY,
        payload={},
    )


def _journal(store: VisionStore, analysis_id: str) -> list[dict]:
    path = store.run_path("s1", analysis_id)
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_vision_call_over_the_shared_budget_is_skipped_and_recorded(tmp_path):
    budget, calls = Budget(remaining=2), []
    engine, store, worker = _worker(tmp_path, budget, calls=calls)
    try:
        for t in (100.0, 120.0, 140.0):
            engine.bus.publish(_off(t))
        _until(lambda: worker.pending == 0 and worker.status()["calls"] == 2)
        status = worker.status()
        assert status["skipped"] == {"over_budget": 1}
        assert status["shared_budget"] is True
        assert len(calls) == 2 and budget.recorded == ["vision", "vision"]
        assert budget.remaining == 0
        aid = status["analysis_id"]
    finally:
        worker.stop()
    records = _journal(store, aid)
    skips = [r for r in records if r["kind"] == "skip"]
    assert [(r["session_time"], r["data"]["reason"]) for r in skips] == [(140.0, "over_budget")]


def test_the_budget_is_rechecked_when_the_frames_arrive(tmp_path):
    """The agents can spend the last call while a moment waits for its post-frames."""
    budget, calls = Budget(remaining=1), []
    source = Source()

    async def ready(t_end, timeout_s):
        budget.remaining = 0  # an agent call landed in the meantime
        return True

    source.ready = ready
    engine, store, worker = _worker(tmp_path, budget, source=source, calls=calls)
    try:
        engine.bus.publish(_off(100.0))
        _until(lambda: worker.pending == 0 and worker.status()["skipped"])
        assert worker.status()["skipped"] == {"over_budget": 1}
        assert calls == [] and budget.recorded == []
    finally:
        worker.stop()


def test_the_vision_cap_is_still_an_inner_limit(tmp_path):
    budget = Budget(remaining=None)  # an uncapped session budget
    engine, _store, worker = _worker(tmp_path, budget, max_calls=1)
    try:
        engine.bus.publish(_off(100.0))
        engine.bus.publish(_off(120.0))
        _until(lambda: worker.pending == 0 and worker.status()["calls"] == 1)
        assert worker.status()["skipped"] == {"over_cap": 1}
        assert budget.recorded == ["vision"]
    finally:
        worker.stop()


def test_the_real_orchestrator_is_a_budget_the_worker_can_spend(tmp_path, raced):
    pitwall = _pitwall(raced, agents=[STRATEGIST], max_calls_per_session=1)
    engine, _store, worker = _worker(tmp_path, pitwall)
    try:
        engine.bus.publish(_off(100.0))
        engine.bus.publish(_off(120.0))
        _until(lambda: worker.pending == 0 and worker.status()["calls"] == 1
               and worker.status()["skipped"])  # fmt: skip
        assert worker.status()["skipped"] == {"over_budget": 1}
    finally:
        worker.stop()
    assert pitwall.calls_by_layer == {"agents": 0, "vision": 1}
    assert pitwall.budget_exhausted is True
