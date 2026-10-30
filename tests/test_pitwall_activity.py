"""The activity trace: every record names a code path that actually ran.

Offline: the agents are driven by ``ScriptedProvider``, so these tests check the
trace the live view draws without a key or the network.
"""

from __future__ import annotations

import pytest

from rtv.pitwall.activity import KINDS, ActivityLog, summarize
from rtv.pitwall.agents.strategist import STRATEGIST, PitCall
from rtv.pitwall.framework import RadioPriority
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ScriptedProvider, tool_use_turn
from rtv.pitwall.radio import RadioFeed
from rtv.racestate import RaceStateEngine, replay_scenario
from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.racestate.scenario import ScenarioSpec


@pytest.fixture(scope="module")
def engine() -> RaceStateEngine:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    return engine


def _event(t=42.0, event_type="pit_window_open", **payload) -> RaceEvent:
    return RaceEvent(
        event_type=EventType(event_type),
        tick=int(t * 60),
        session_time=t,
        lap=5,
        severity=Severity.ADVISORY,
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


def _orch(engine, handler, activity) -> PitwallOrchestrator:
    return PitwallOrchestrator(
        engine,
        ScriptedProvider(handler),
        [STRATEGIST],
        feed=RadioFeed(),
        clock=None,
        activity=activity,
    )


def _kinds(log: ActivityLog) -> list[str]:
    return [r["kind"] for r in log.history()]


async def test_one_run_traces_dispatch_model_tool_validate_and_radio(engine):
    log = ActivityLog(wall_clock=None)

    def handler(*, turn, **_):
        if turn == 0:
            return tool_use_turn([("get_fuel_projection", {})])
        return _answer()

    orch = _orch(engine, handler, log)
    messages = await orch.handle_event(_event())
    assert len(messages) == 1
    orch.feed.flush()

    assert _kinds(log) == [
        "dispatch",
        "run_start",
        "llm_call_start",
        "llm_call_end",
        "tool_call",
        "llm_call_start",
        "llm_call_end",
        "validate",
        "run_end",
        "radio_queued",
        "radio_aired",
    ]
    assert set(_kinds(log)) <= set(KINDS)
    records = log.history()
    dispatch = records[0]
    assert dispatch["data"]["accepted"] == ["strategist"]
    assert dispatch["event_key"] == "pit_window_open"
    run_ids = {r["run_id"] for r in records[1:9]}
    assert run_ids == {"strategist-1"}
    first_call_end = records[3]["data"]
    assert first_call_end["tool_calls"] == ["get_fuel_projection"]
    assert first_call_end["ok"] is True and first_call_end["answered"] is False
    tool = records[4]["data"]
    assert tool["tool"] == "get_fuel_projection" and tool["ok"] is True
    assert tool["result"]["type"] == "object"
    end = records[8]["data"]
    assert end["outcome"] == "radio" and end["turns"] == 2
    assert end["message_id"] == messages[0].message_id
    assert records[-1]["data"]["message_id"] == messages[0].message_id
    # Ordered, unique sequence numbers: the review rebuilds state from them.
    seqs = [r["seq"] for r in records]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


async def test_radio_history_in_context_is_recorded_as_the_agent_to_agent_edge(engine):
    log = ActivityLog(wall_clock=None)
    orch = _orch(engine, lambda **_: _answer(), log)
    first = await orch.handle_event(_event(t=42.0))
    orch.feed.flush()
    await orch.handle_event(_event(t=500.0))

    starts = [r for r in log.history() if r["kind"] == "run_start"]
    assert starts[0]["data"]["context_radio"] == []
    assert starts[1]["data"]["context_radio"] == [
        {"message_id": first[0].message_id, "agent": "strategist"}
    ]


async def test_cooldown_and_kill_switch_show_up_as_skips_without_a_model_call(engine):
    log = ActivityLog(wall_clock=None)
    calls = []
    orch = _orch(engine, lambda **kw: calls.append(kw) or _answer(), log)
    rival = dict(gap_to_player=2.0)
    await orch.handle_event(_event(t=10.0, event_type="rival_pitted", **rival))
    # Inside the strategist's rival_pitted cooldown.
    await orch.handle_event(_event(t=20.0, event_type="rival_pitted", **rival))
    orch.set_enabled(False)
    await orch.handle_event(_event(t=900.0))

    dispatches = [r["data"] for r in log.history() if r["kind"] == "dispatch"]
    assert dispatches[1]["accepted"] == [] and dispatches[1]["skipped"] == {
        "strategist": "cooldown"
    }
    assert dispatches[2]["blocked"] == "disabled"
    assert len(calls) == 1


async def test_a_refused_answer_is_traced_as_a_failed_validation(engine):
    log = ActivityLog(wall_clock=None)
    orch = _orch(engine, lambda **_: _answer("Box now, we gain 7.3 seconds."), log)
    await orch.handle_event(_event())

    validates = [r["data"] for r in log.history() if r["kind"] == "validate"]
    assert validates[0]["ok"] is False and "7.3" in validates[0]["unsupported"]
    assert validates[-1]["repaired"] is True
    end = next(r for r in log.history() if r["kind"] == "run_end")
    assert end["data"]["outcome"] == "refused"


async def test_a_failing_model_call_is_traced_and_the_run_ends_in_error(engine):
    log = ActivityLog(wall_clock=None)

    def boom(**_):
        raise RuntimeError("provider down")

    orch = PitwallOrchestrator(
        engine, ScriptedProvider(boom), [STRATEGIST], feed=RadioFeed(), clock=None,
        activity=log, retry_backoff_s=0.0,
    )
    await orch.handle_event(_event())
    ends = [r["data"] for r in log.history() if r["kind"] == "llm_call_end"]
    assert ends and all(e["ok"] is False and e["error"] == "RuntimeError" for e in ends)
    run_end = next(r for r in log.history() if r["kind"] == "run_end")
    assert run_end["data"]["outcome"] == "error"


def test_records_without_a_time_take_the_engine_clock_and_subscribers_get_them():
    log = ActivityLog(clock=lambda: (12.5, 750), wall_clock=lambda: 1.0)
    with log.subscribe(name="test") as sub:
        log.record("radio_aired", agent="spotter", message_id="abc")
        (record,) = sub.drain()
    assert record["session_time"] == 12.5 and record["tick"] == 750
    assert record["wall_ms"] == 1000 and record["data"] == {"message_id": "abc"}
    assert log.since(0) == [record] and log.since(record["seq"]) == []


def test_summarize_keeps_the_shape_not_the_payload():
    assert summarize({"a": 1, "b": [1, 2]}) == {"type": "object", "keys": ["a", "b"], "size": 2}
    assert summarize([1, 2, 3]) == {"type": "array", "size": 3}
    assert summarize({"error": "nope"})["error"] == "nope"
    assert summarize(None) == {"type": "null"}


def test_a_feed_without_a_tracer_records_nothing():
    feed = RadioFeed()
    assert feed.tracer is None


# --------------------------------------------------------------------------
# the HTTP and WebSocket surface
# --------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("pyarrow")
    pytest.importorskip("duckdb")
    pytest.importorskip("fastapi")
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_PITWALL", "true")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_activity_endpoint_pages_by_seq(client):
    activity = client.app.state.services.activity
    assert activity is not None
    first = activity.record("dispatch", event_key="lap_completed", accepted=[])
    activity.record("radio_aired", agent="spotter", message_id="m1")

    body = client.get("/api/v1/pitwall/activity").json()
    assert [r["kind"] for r in body["records"]] == ["dispatch", "radio_aired"]
    assert body["last_seq"] == body["records"][-1]["seq"]
    later = client.get(f"/api/v1/pitwall/activity?since={first['seq']}").json()
    assert [r["kind"] for r in later["records"]] == ["radio_aired"]


def test_activity_records_are_pushed_on_the_pitwall_socket(client):
    activity = client.app.state.services.activity
    with client.websocket_connect("/ws/pitwall") as ws:
        assert ws.receive_json()["type"] == "state"
        activity.record("dispatch", event_key="flag_change:yellow", accepted=["spotter"])
        for _ in range(20):
            msg = ws.receive_json()
            if msg["type"] == "activity":
                assert msg["activity"]["kind"] == "dispatch"
                assert msg["activity"]["data"]["accepted"] == ["spotter"]
                break
        else:  # pragma: no cover - contract failure
            raise AssertionError("no activity frame on /ws/pitwall")
