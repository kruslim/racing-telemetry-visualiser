"""Stage 07: the session lifecycle, one clean segment per session, autonomy and the hook.

All offline. The weekend stream (``evals/synthetic_weekend.py``) is practice,
qualifying and race of one event in one RTV session, the way the live poller
delivers it; agents are driven by ``ScriptedProvider``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace

import pytest
from evals.synthetic_race import ScenarioSpec, replay_scenario, scenario_source
from evals.synthetic_weekend import (
    PRACTICE,
    QUALIFY,
    RACE,
    WEEKEND_SESSION_ID,
    weekend_source,
)

from rtv.pitwall.agents.strategist import STRATEGIST, PitCall
from rtv.pitwall.framework import RadioPriority
from rtv.pitwall.orchestrator import PitwallOrchestrator
from rtv.pitwall.provider import ScriptedProvider
from rtv.pitwall.radio import RadioFeed
from rtv.racestate import RaceStateEngine, ReplayDriver
from rtv.racestate.lifecycle import normalise_session_type, session_type_from_info
from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.store import journal

LIFECYCLE = {
    "session_start",
    "session_state_change",
    "session_green",
    "session_checkered",
    "session_end",
}


def _answer(text="Stay out, no stop needed.") -> PitCall:
    return PitCall(
        priority=RadioPriority.ADVISORY,
        spoken_text=text,
        detail_text=text,
        recommendation="stay_out",
        confidence="high",
        rationale="No figures asserted.",
    )


def _orch(engine, agents=(STRATEGIST,), **kw) -> PitwallOrchestrator:
    return PitwallOrchestrator(
        engine,
        ScriptedProvider(lambda **_: _answer()),
        [replace(a) for a in agents],
        feed=kw.pop("feed", RadioFeed()),
        clock=None,
        **kw,
    )


def _lifecycle_events(engine: RaceStateEngine, source) -> list[RaceEvent]:
    seen: list[RaceEvent] = []
    engine.bus.subscribe_callback(
        lambda ev: seen.append(ev) if ev.event_type.value in LIFECYCLE else None
    )
    ReplayDriver(engine).run(source)
    engine.end_segment("replay_finished")
    return seen


# --------------------------------------------------------------------------
# the state machine
# --------------------------------------------------------------------------
def test_session_types_are_normalised_from_the_session_info():
    assert normalise_session_type("Race") == "race"
    assert normalise_session_type("Open Qualify") == "qualify"
    assert normalise_session_type("Lone Qualify") == "qualify"
    assert normalise_session_type("Offline Testing") == "practice"
    assert normalise_session_type("Warmup") == "practice"
    assert normalise_session_type("Hosted Something") is None
    info = {"SessionInfo": {"Sessions": [{"SessionNum": 3, "SessionType": "Race"}]}}
    assert session_type_from_info(info, 3) == ("race", "Race", None)
    kind, _, note = session_type_from_info(info, 1)
    assert kind is None and "not in the session-info" in note
    assert session_type_from_info(info, None)[2].startswith("SessionNum is not")


def test_a_weekend_stream_has_three_segments_with_their_own_lifecycle():
    engine = RaceStateEngine(source="replay")
    events = _lifecycle_events(engine, weekend_source())
    starts = [e for e in events if e.key == "session_start"]
    ends = [e for e in events if e.key == "session_end"]
    assert [e.payload["session_type"] for e in starts] == ["practice", "qualify", "race"]
    assert [e.payload["session_num"] for e in starts] == [0, 1, 2]
    assert [e.payload["segment"] for e in starts] == [1, 2, 3]
    assert [e.payload["reason"] for e in ends] == [
        "session_changed",
        "session_changed",
        "replay_finished",
    ]
    assert [e.payload.get("cause") for e in ends[:2]] == ["session_num_changed"] * 2

    race = [e for e in events if e.payload.get("segment") == 3]
    keys = [e.key for e in race]
    assert keys == [
        "session_start",
        "session_state_change",  # -> parade_laps
        "session_state_change",  # -> racing
        "session_green",
        "session_state_change",  # -> checkered (the leader)
        "session_checkered",  # the player crosses the line
        "session_state_change",  # -> cool_down
        "session_end",
    ]
    green = race[3]
    assert green.payload["from"] == "pre_green" and green.payload["to"] == "green"
    assert green.lap == 1 and green.session_time == pytest.approx(0.2 * RACE.lap_time, abs=0.05)
    flag = race[5]
    assert flag.payload["player_finished"] is True and flag.lap == RACE.laps
    for event in events:
        assert event.severity is Severity.INFO
        assert {"session_num", "session_type", "from", "to"} <= set(event.payload)

    snap = engine.snapshot().lifecycle
    assert snap.state == "ended" and snap.segment == 3 and snap.player_finished is True


def test_no_session_num_means_an_unknown_type_and_a_capability_that_says_so():
    engine = RaceStateEngine(source="replay")
    events = _lifecycle_events(engine, scenario_source(ScenarioSpec(laps=2)))
    state = engine.snapshot()
    assert state.capabilities.lifecycle is False
    assert "SessionNum" in state.capabilities.missing
    assert [e.key for e in events][:3] == ["session_start", "session_state_change", "session_green"]
    assert events[0].payload["session_type"] is None
    assert any("SessionNum" in n for n in state.lifecycle.notes)


def test_a_transition_the_catalog_cannot_back_stays_unknown():
    engine = RaceStateEngine(source="replay")
    spec = ScenarioSpec(laps=2, exclude=("SessionState",))
    events = _lifecycle_events(engine, scenario_source(spec))
    assert "session_green" not in {e.key for e in events}
    assert engine.lifecycle.green_at is None
    assert any("SessionState" in n for n in engine.lifecycle.notes)


# --------------------------------------------------------------------------
# one clean segment per session (engine)
# --------------------------------------------------------------------------
def test_the_race_segment_does_not_inherit_the_practice_fuel_model_or_laps():
    engine = RaceStateEngine(source="replay")
    at_start: dict[str, object] = {}

    def watch(ev: RaceEvent) -> None:
        if ev.key == "session_start" and ev.payload["session_type"] == "race":
            snap = engine.snapshot()
            at_start.update(
                fuel_samples=snap.fuel.samples,
                per_lap=snap.fuel.per_lap,
                best=snap.player.best_lap_time,
                track=snap.session.track_name,
                session_id=snap.session.session_id,
                caps=snap.capabilities.fuel,
            )

    engine.bus.subscribe_callback(watch)
    ReplayDriver(engine).run(weekend_source())
    assert at_start == {
        "fuel_samples": 0,
        "per_lap": None,
        "best": None,
        "track": "Synthetic Circuit",  # session info and binding survive the reset
        "session_id": WEEKEND_SESSION_ID,
        "caps": True,
    }
    # ... and the race builds its own model again.
    assert engine.snapshot().fuel.samples > 0


def test_a_new_session_id_ends_the_segment_and_the_next_frame_starts_clean():
    engine = RaceStateEngine(source="live")
    events = _lifecycle_events(engine, scenario_source(ScenarioSpec(laps=2)))
    engine.set_session_id("first")
    assert engine.snapshot().lifecycle.state == "ended"
    replay_scenario(engine, ScenarioSpec(laps=2))
    engine.set_session_id("second")
    ends = [e for e in engine.bus.history() if e.key == "session_end"]
    assert ends[-1].payload["reason"] == "session_changed"
    assert len(events) >= 3


# --------------------------------------------------------------------------
# autonomy (orchestrator)
# --------------------------------------------------------------------------
def _ev(kind: str, t: float = 10.0, **payload) -> RaceEvent:
    return RaceEvent(
        event_type=EventType(kind), tick=int(t * 60), session_time=t, lap=4, payload=payload
    )


def test_autonomy_holds_standby_until_green_and_stands_down_at_the_flag(tmp_path):
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    orch = _orch(engine, autonomous=True)
    window = _ev("pit_window_open", 30.0, lap=4)

    assert orch.mode == "standby" and orch.dispatch(window) == []
    orch.dispatch(_ev("session_start", 0.0, session_type="race", session_num=2))
    assert orch.dispatch(window) == []
    assert orch.status()["standby_reason"] == "waiting for the green flag"

    orch.dispatch(_ev("session_green", 1.0, session_type="race", session_num=2))
    assert orch.mode == "armed"
    assert orch.dispatch(window) == ["strategist"]

    # The leader's flag is not ours; the player's is.
    orch.dispatch(_ev("session_checkered", 50.0, session_type="race", player_finished=False))
    assert orch.mode == "armed"
    orch.dispatch(_ev("session_checkered", 52.0, session_type="race", player_finished=True))
    assert orch.mode == "standby"
    assert orch.dispatch(_ev("pit_window_open", 200.0, lap=4)) == []
    assert orch.last_autonomy["calls_used"] == orch.calls_used


def test_practice_arms_only_its_roster_and_nothing_by_default():
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    from rtv.pitwall.agents import build_agents

    agents = build_agents()
    window = _ev("pit_window_open", 30.0, lap=4)

    default = _orch(engine, agents, autonomous=True)
    default.dispatch(_ev("session_start", 0.0, session_type="practice"))
    default.dispatch(_ev("session_green", 1.0, session_type="practice"))
    assert default.mode == "standby"
    assert "practice roster" in default.standby_reason
    assert default.dispatch(window) == []

    roster = _orch(engine, agents, autonomous=True, rosters={"practice": ["coach"]})
    roster.dispatch(_ev("session_start", 0.0, session_type="practice"))
    roster.dispatch(_ev("session_green", 1.0, session_type="practice"))
    assert roster.mode == "armed" and roster.status()["armed_roster"] == ["coach"]
    assert roster.dispatch(window) == []  # the strategist is not on the roster
    trace = roster.status()
    assert trace["session_type"] == "practice"

    unknown = _orch(engine, agents, autonomous=True)
    unknown.dispatch(_ev("session_start", 0.0, session_type=None))
    unknown.dispatch(_ev("session_green", 1.0, session_type=None))
    assert unknown.mode == "standby" and "unknown" in unknown.standby_reason


def test_autonomy_never_overrides_the_kill_switch_or_an_agent_switch():
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    orch = _orch(engine, autonomous=True)
    orch.set_enabled(False)
    orch.dispatch(_ev("session_start", 0.0, session_type="race"))
    orch.dispatch(_ev("session_green", 1.0, session_type="race"))
    assert orch.enabled is False and orch.mode == "off"
    assert orch.dispatch(_ev("pit_window_open", 30.0, lap=4)) == []

    orch.set_enabled(True)
    orch.set_agent_enabled("strategist", False)
    orch.dispatch(_ev("session_green", 2.0, session_type="race"))
    assert orch.mode == "armed"
    assert orch.dispatch(_ev("pit_window_open", 31.0, lap=4)) == []
    assert orch.agent_enabled("strategist") is False


def test_standby_is_traced_as_the_dispatch_block_reason():
    from rtv.pitwall.activity import ActivityLog

    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    log = ActivityLog()
    orch = _orch(engine, autonomous=True, activity=log)
    orch.dispatch(_ev("pit_window_open", 30.0, lap=4))
    blocked = [r for r in log.history() if r["kind"] == "dispatch"]
    assert blocked[-1]["data"]["blocked"] == "standby"
    orch.dispatch(_ev("session_start", 0.0, session_type="race"))
    orch.dispatch(_ev("session_green", 1.0, session_type="race"))
    autonomy = [r for r in log.history() if r["kind"] == "autonomy"]
    assert [r["data"]["armed"] for r in autonomy] == [False, True]


def test_autonomy_off_dispatches_exactly_as_before():
    """Lifecycle events are the only addition, and they wake nobody."""
    from rtv.pitwall.agents import build_agents

    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    events = engine.bus.history()
    assert LIFECYCLE & {e.key for e in events}

    with_lifecycle = _orch(engine, build_agents())
    without = _orch(engine, build_agents())
    decisions = [(e.key, with_lifecycle.dispatch(e)) for e in events]
    assert all(accepted == [] for key, accepted in decisions if key in LIFECYCLE)
    assert [d for d in decisions if d[0] not in LIFECYCLE] == [
        (e.key, without.dispatch(e)) for e in events if e.key not in LIFECYCLE
    ]
    assert with_lifecycle.mode == "on" and not with_lifecycle.standby


# --------------------------------------------------------------------------
# the app: journal runs per segment, replays, the session-end hook
# --------------------------------------------------------------------------
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("fastapi")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_PITWALL", "true")
    monkeypatch.setenv("RTV_PITWALL_SESSION_END_NOTIFIER", "file")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    with TestClient(create_app()) as c:
        from evals.synthetic_race import seed_scenario_session

        seed_scenario_session(c.app.state.services.writer, "scenario")
        yield c
    get_settings.cache_clear()


def _mount(services, *, inline=True, **kw) -> PitwallOrchestrator:
    """A scripted agent layer, wired the way build_services wires a real one."""
    orch = _orch(services.engine, **kw)
    orch.attach_activity(services.activity)
    orch.feed.subscribe_callback(services.recorder.on_radio)
    services.pitwall = orch
    services.segments.pitwall = orch
    if inline:
        # Run each event to completion on the frame thread, in bus order.
        def run(ev: RaceEvent) -> None:
            asyncio.run(orch.handle_event(ev))
            orch.feed.flush()

        services.engine.bus.subscribe_callback(run)
    return orch


def _replay_source(services, source) -> None:
    services.replay.start(source)
    services.replay.join(timeout=60)
    assert services.on_session_end.wait()


def _runs(client, session_id) -> list[dict]:
    return client.get(f"/api/v1/sessions/{session_id}/pitwall-runs").json()["runs"]


def test_a_weekend_gives_three_runs_and_the_race_inherits_nothing(client):
    services = client.app.state.services
    orch = _mount(services, max_calls_per_session=1)
    at_race: dict[str, object] = {}

    def watch(ev):
        if ev.key == "session_start" and ev.payload["session_type"] == "race":
            at_race.update(
                calls_used=orch.calls_used,
                radio=len(orch.feed.history()) + len(orch.feed.pending()),
                cooldowns=dict(orch.runtimes["strategist"]._last_fired),
                exhausted=orch.budget_exhausted,
            )

    # Subscribed after the segment journal, so it sees the state after the reset.
    services.engine.bus.subscribe_callback(watch)
    _replay_source(services, weekend_source())

    runs = _runs(client, WEEKEND_SESSION_ID)
    assert [(r["session_num"], r["session_type"]) for r in runs] == [
        (0, "practice"),
        (1, "qualify"),
        (2, "race"),
    ]
    assert [r["end_reason"] for r in runs] == [
        "session_changed",
        "session_changed",
        "replay_finished",
    ]
    assert all(r["complete"] for r in runs)
    # Practice spent the whole (one-call) budget; the race started from zero.
    assert runs[0]["calls_used"] == 1
    assert at_race == {"calls_used": 0, "radio": 0, "cooldowns": {}, "exhausted": False}
    assert runs[2]["calls_used"] == 1  # and spent its own

    # Each run holds only its own segment's lifecycle events.
    for run, kind in zip(runs, ("practice", "qualify", "race"), strict=True):
        log = client.get(
            f"/api/v1/sessions/{WEEKEND_SESSION_ID}/pitwall-log",
            params={"run": run["run_id"], "kinds": "race_event"},
        ).json()["records"]
        starts = [r["data"] for r in log if r["data"]["key"] == "session_start"]
        assert [s["payload"]["session_type"] for s in starts] == [kind]
        assert log[-1]["data"]["key"] == "session_end"

    # The session-end hook stored one summary (and one notice) per segment.
    root = services.settings.journal_dir / WEEKEND_SESSION_ID / "runs"
    for run in runs:
        stored = json.loads((root / f"{run['run_id']}.summary.json").read_text())
        assert stored["run_id"] == run["run_id"]
        assert stored["session_type"] == run["session_type"]
        assert (root / f"{run['run_id']}.notice.txt").is_file()
    assert services.on_session_end.fired == 3


def test_two_replays_in_one_server_run_share_nothing(client):
    services = client.app.state.services
    orch = _mount(services)
    orch.director.reset = _count_resets = _Counter(orch.director.reset)  # type: ignore[method-assign]

    def replay():
        r = client.post("/api/v1/replay/start", json={"session_id": "scenario", "speed": 0})
        assert r.status_code == 200
        services.replay.join(timeout=60)
        assert services.on_session_end.wait()

    replay()
    first = {
        "calls": orch.calls_used,
        "radio": [m.spoken_text for m in orch.feed.history()],
        "dispatched": orch.dispatched,
    }
    assert first["calls"] > 0 and first["radio"]
    resets_before = _count_resets.n
    replay()

    runs = _runs(client, "scenario")
    assert len(runs) == 2 and all(r["complete"] for r in runs)
    # The second run's budget, radio and cooldowns started from nothing: it
    # spent and said exactly what the first did.
    assert runs[1]["calls_used"] == runs[0]["calls_used"] == first["calls"]
    assert [m.spoken_text for m in orch.feed.history()] == first["radio"]
    assert orch.dispatched == 2 * first["dispatched"]
    assert _count_resets.n > resets_before  # the director was reset as well
    log = [
        client.get(
            "/api/v1/sessions/scenario/pitwall-log", params={"run": r["run_id"], "kinds": "radio"}
        ).json()["count"]
        for r in runs
    ]
    assert log[0] == log[1] > 0


class _Counter:
    def __init__(self, fn) -> None:
        self.fn, self.n = fn, 0

    def __call__(self, *a, **kw):
        self.n += 1
        return self.fn(*a, **kw)


def test_a_raising_session_end_callback_is_contained(client):
    services = client.app.state.services
    seen: list = []

    def boom(ended):
        raise RuntimeError("debrief exploded")

    services.on_session_end.insert(0, boom)
    services.on_session_end.append(seen.append)
    _replay_source(services, weekend_source(practice=replace(PRACTICE, laps=2), qualify=QUALIFY))
    assert [e.session_type for e in seen] == ["practice", "qualify", "race"]
    assert services.on_session_end.errors == 3
    assert {e.reason for e in seen} == {"session_changed", "replay_finished"}
    assert seen[0].journal_path.is_file() and seen[0].calls_used is None
    # The frame path never waited for it: the replay finished normally.
    assert services.replay.status().error is None


def test_the_summary_endpoint_serves_the_stored_file(client):
    services = client.app.state.services
    r = client.post("/api/v1/replay/start", json={"session_id": "scenario", "speed": 0})
    assert r.status_code == 200
    services.replay.join(timeout=60)
    assert services.on_session_end.wait()
    run = _runs(client, "scenario")[0]
    path = services.settings.journal_dir / "scenario" / "runs" / f"{run['run_id']}.summary.json"
    stored = json.loads(path.read_text())
    served = client.get("/api/v1/sessions/scenario/summary", params={"run": run["run_id"]}).json()
    served.pop("vision", None)
    assert served == stored
    # Without the stored file it is computed, same shape.
    path.unlink()
    computed = client.get(
        "/api/v1/sessions/scenario/summary", params={"run": run["run_id"]}
    ).json()
    computed.pop("vision", None)
    assert {k: v for k, v in computed.items() if k not in {"session_num", "session_type"}} == {
        k: v for k, v in stored.items() if k not in {"session_num", "session_type"}
    }


def test_a_run_summary_counts_only_its_own_laps():
    laps = [
        {"lap": n, "start_session_time": t, "lap_time": 10.0, "is_valid": True}
        for n, t in ((1, 0.0), (2, 10.0), (3, 20.0), (7, 70.0))
    ]
    records = [
        {"kind": "state", "session_time": t, "data": {"player": {"lap": lap}}}
        for t, lap in ((0.5, 1), (12.0, 2), (25.0, 3))
    ]
    assert [lap["lap"] for lap in journal.laps_for_run(laps, records)] == [1, 2, 3]
    # A lap number the run saw, but from outside its time window (a practice row).
    laps.append({"lap": 3, "start_session_time": 900.0, "lap_time": 10.0, "is_valid": True})
    assert [lap["lap"] for lap in journal.laps_for_run(laps[-1:], records)] == []
    assert journal.laps_for_run(laps, []) == laps


def test_the_frame_path_never_waits_for_the_hook(client):
    services = client.app.state.services
    release = threading.Event()
    services.on_session_end.append(lambda ended: release.wait(30))
    services.replay.start(weekend_source(practice=replace(PRACTICE, laps=2)))
    services.replay.join(timeout=60)
    # The replay finished while the first callback was still blocked.
    assert not services.replay.status().running
    assert services.on_session_end.wait(timeout=0.05) is False
    release.set()
    assert services.on_session_end.wait()
    assert services.on_session_end.fired == 3
