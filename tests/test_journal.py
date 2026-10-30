"""The race journal: the recorder, the reader and the HTTP surface, all offline."""

from __future__ import annotations

import json
import time

import pytest

from rtv.store import journal
from rtv.store.journal import SessionRecorder


class _State:
    def __init__(self, t: float, tick: int) -> None:
        self.t, self.tick = t, tick

    def to_api(self) -> dict:
        return {"session_time": self.t, "tick": self.tick, "version": self.tick}


class _Event:
    def __init__(self, t: float, tick: int, key: str) -> None:
        self.t, self.tick, self.key = t, tick, key

    def to_api(self) -> dict:
        return {"session_time": self.t, "tick": self.tick, "key": self.key, "lap": 1}


def _recorder(tmp_path, clock):
    return SessionRecorder(
        tmp_path,
        state_interval_s=1.0,
        snapshot=lambda: _State(*clock),
        wall_clock=lambda: 1_790_000_000.0,
    )


def test_a_run_records_every_kind_with_one_monotonic_seq(tmp_path):
    clock = [0.0, 0]
    rec = _recorder(tmp_path, clock)
    run_id = rec.begin("s1", "replay", agents=[])
    assert rec.active == {"session_id": "s1", "run_id": run_id, "source": "replay"}
    for i in range(300):  # 5 s of 60 Hz frames
        clock[:] = [i / 60, i]
        rec.on_frame(i / 60, i, lap=1)
        if i == 120:
            rec.on_race_event(_Event(i / 60, i, "lockup"))
            rec.on_activity(
                {"seq": 7, "kind": "run_start", "agent": "coach", "run_id": "coach-1",
                 "session_time": i / 60, "tick": i, "wall_ms": 5, "data": {"model": "m"}}
            )
    rec.end(reason="done")
    assert rec.active is None

    records = list(journal.iter_records(journal.run_path(tmp_path, "s1", run_id)))
    seqs = [r["seq"] for r in records]
    assert seqs == list(range(1, len(records) + 1))
    kinds = [r["kind"] for r in records]
    assert kinds[0] == "lifecycle" and kinds[-1] == "lifecycle"
    assert records[0]["data"]["phase"] == "start"
    assert records[-1]["data"] == {"phase": "end", "reason": "done"}
    # One snapshot per session second (0..4) plus the final one.
    states = [r for r in records if r["kind"] == "state"]
    assert len(states) == 6 and states[-1]["final"] is True
    event = next(r for r in records if r["kind"] == "race_event")
    assert event["event_key"] == "lockup" and event["tick"] == 120
    activity = next(r for r in records if r["kind"] == "run_start")
    assert activity["agent"] == "coach" and activity["run_id"] == "coach-1"
    assert activity["data"] == {"model": "m", "activity_seq": 7}


def test_nothing_is_written_without_an_open_run(tmp_path):
    rec = _recorder(tmp_path, [0.0, 0])
    rec.on_frame(1.0, 60)
    rec.on_race_event(_Event(1.0, 60, "lockup"))
    rec.end()
    assert not any(tmp_path.iterdir())


def test_runs_never_overwrite_each_other(tmp_path):
    rec = _recorder(tmp_path, [0.0, 0])
    first = rec.begin("s1", "live")
    second = rec.begin("s1", "replay")  # closes the first
    third = rec.begin("s1", "replay")
    rec.end()
    assert len({first, second, third}) == 3
    runs = journal.list_runs(tmp_path, "s1")
    assert [r["run_id"] for r in runs] == sorted([first, second, third])
    assert all(r["complete"] for r in runs)


def test_default_run_prefers_the_newest_run_the_agents_spoke_in(tmp_path):
    rec = _recorder(tmp_path, [0.0, 0])
    spoke = rec.begin("s1", "live")
    rec.on_activity({"kind": "run_start", "agent": "spotter", "data": {}})
    rec.begin("s1", "replay")
    rec.end()
    runs = journal.list_runs(tmp_path, "s1")
    assert runs[0]["has_agents"] and not runs[1]["has_agents"]
    assert journal.default_run(runs) == spoke
    assert journal.default_run([]) is None


def test_a_torn_last_line_is_skipped(tmp_path):
    rec = _recorder(tmp_path, [0.0, 0])
    run_id = rec.begin("s1", "live")
    rec.end()
    path = journal.run_path(tmp_path, "s1", run_id)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 99, "kind": "rad')
    assert [r["kind"] for r in journal.iter_records(path)][-1] == "lifecycle"
    assert journal.summarize_run(path)["complete"]


def test_read_filters_and_state_lookup(tmp_path):
    clock = [0.0, 0]
    rec = _recorder(tmp_path, clock)
    run_id = rec.begin("s1", "replay")
    for i in range(0, 600, 6):
        clock[:] = [i / 60, i]
        rec.on_frame(i / 60, i)
        if i % 120 == 0:
            rec.on_race_event(_Event(i / 60, i, f"e{i}"))
    rec.end()
    path = journal.run_path(tmp_path, "s1", run_id)

    events = journal.read_records(path, kinds=["race_event"])
    assert [e["event_key"] for e in events] == ["e0", "e120", "e240", "e360", "e480"]
    window = journal.read_records(path, kinds=["race_event"], t_from=2.0, t_to=6.0)
    assert [e["event_key"] for e in window] == ["e120", "e240", "e360"]
    no_state = journal.read_records(path, exclude=["state"])
    assert all(r["kind"] != "state" for r in no_state)
    after = journal.read_records(path, after_seq=events[2]["seq"], kinds=["race_event"])
    assert [e["event_key"] for e in after] == ["e360", "e480"]
    assert len(journal.read_records(path, limit=3)) == 3

    assert journal.state_at(path, 3.5)["session_time"] == 3.0
    assert journal.state_at(path, -1)["session_time"] == 0.0  # before the start


@pytest.mark.parametrize("bad", ["..", "../x", "a/b", "", "x" * 300])
def test_ids_cannot_escape_the_journal_folder(tmp_path, bad):
    with pytest.raises(ValueError):
        journal.run_path(tmp_path, bad, "r")
    with pytest.raises(ValueError):
        journal.run_path(tmp_path, "s1", bad)


def test_lines_are_compact_json(tmp_path):
    rec = _recorder(tmp_path, [0.0, 0])
    run_id = rec.begin("s1", "live")
    rec.end()
    line = journal.run_path(tmp_path, "s1", run_id).read_text().splitlines()[0]
    assert json.loads(line)["kind"] == "lifecycle"
    assert '"kind":"lifecycle"' in line


# --------------------------------------------------------------------------
# the app records a replay, and serves it back
# --------------------------------------------------------------------------
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("fastapi")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_PITWALL", "true")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    with TestClient(create_app()) as c:
        from evals.synthetic_race import seed_scenario_session

        seed_scenario_session(c.app.state.services.writer, "scenario")
        yield c
    get_settings.cache_clear()


def _replay(client, session_id: str = "scenario") -> None:
    r = client.post("/api/v1/replay/start", json={"session_id": session_id, "speed": 0})
    assert r.status_code == 200
    deadline = time.time() + 30
    while time.time() < deadline:
        status = client.get("/api/v1/replay/status").json()
        if not status["running"]:
            break
        time.sleep(0.02)
    # The journal closes on the replay thread's lifecycle callback.
    while time.time() < deadline and client.app.state.services.recorder.active:
        time.sleep(0.02)


def test_no_journal_is_a_404(client):
    r = client.get("/api/v1/sessions/scenario/pitwall-runs")
    assert r.status_code == 200
    assert r.json()["runs"] == [] and r.json()["default_run"] is None
    assert client.get("/api/v1/sessions/scenario/pitwall-log").status_code == 404


def test_a_replay_is_journalled_and_served_back(client):
    _replay(client)
    runs = client.get("/api/v1/sessions/scenario/pitwall-runs").json()
    assert len(runs["runs"]) == 1 and runs["recording"] is None
    run = runs["runs"][0]
    assert run["source"] == "replay" and run["complete"]
    assert run["counts"]["race_event"] > 0 and run["counts"]["state"] > 10
    assert not run["has_agents"]  # no key, no agents: deterministic events only

    log = client.get("/api/v1/sessions/scenario/pitwall-log").json()
    assert log["run_id"] == run["run_id"]
    kinds = {r["kind"] for r in log["records"]}
    assert "state" not in kinds and {"lifecycle", "race_event"} <= kinds
    seqs = [r["seq"] for r in log["records"]]
    assert seqs == sorted(seqs)
    # The journal is the same event log the live bus produced.
    live = client.get("/api/v1/racestate/events", params={"limit": 500}).json()["events"]
    journalled = [r["data"]["key"] for r in log["records"] if r["kind"] == "race_event"]
    assert journalled[-len(live):] == [e["key"] for e in live][-len(journalled):]

    states = client.get(
        "/api/v1/sessions/scenario/pitwall-log", params={"kinds": "state", "limit": 5}
    ).json()
    assert states["count"] == 5 and states["truncated"]

    mid = (run["session_time_start"] + run["session_time_end"]) / 2
    snap = client.get("/api/v1/sessions/scenario/pitwall-log/state", params={"t": mid})
    assert snap.status_code == 200
    rec = snap.json()["record"]
    assert rec["kind"] == "state" and rec["session_time"] <= mid
    assert rec["data"]["session"]["session_id"] == "scenario"


def test_bad_run_ids_are_refused(client):
    r = client.get("/api/v1/sessions/scenario/pitwall-log", params={"run": "../../x"})
    assert r.status_code == 400
    r = client.get("/api/v1/sessions/scenario/pitwall-log", params={"run": "nope"})
    assert r.status_code == 404
