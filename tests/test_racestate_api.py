"""Pitwall HTTP + WebSocket surface, driven offline through the TestClient."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("fastapi")

MILESTONES = ["flag_change:yellow", "pit_window_open", "lockup", "pit_entry", "pit_exit"]


def _make_client(tmp_path, monkeypatch, *, pitwall: bool = True):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_PITWALL", "true" if pitwall else "false")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    return TestClient(create_app())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as c:
        # The app only replays stored sessions, so the scripted race goes into
        # the store first, exactly as an import would put it there.
        from evals.synthetic_race import seed_scenario_session

        seed_scenario_session(c.app.state.services.writer, "scenario")
        yield c


def _await_replay(client, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    status = {}
    while time.time() < deadline:
        status = client.get("/api/v1/replay/status").json()
        if status["finished"] or not status["running"]:
            return status
        time.sleep(0.02)
    raise AssertionError(f"Replay did not finish within {timeout}s: {status}")


# --------------------------------------------------------------------------
# race state
# --------------------------------------------------------------------------
def test_racestate_available_before_any_telemetry(client):
    r = client.get("/api/v1/racestate")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 0
    assert body["capabilities"]["standings"] is False  # no catalog bound yet
    assert body["metrics"]["frames"] == 0


def test_events_endpoint_is_empty_initially(client):
    r = client.get("/api/v1/racestate/events")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "events": []}


# --------------------------------------------------------------------------
# replay control
# --------------------------------------------------------------------------
def test_scenario_replay_drives_state_and_events_over_http(client):
    r = client.post("/api/v1/replay/start", json={"session_id": "scenario", "speed": 0})
    assert r.status_code == 200
    assert r.json()["session_id"] == "scenario"

    status = _await_replay(client)
    assert status["error"] is None
    assert status["frames"] > 3000

    state = client.get("/api/v1/racestate").json()
    assert state["session"]["source"] == "replay"
    assert state["player"]["stint"] == 2
    assert state["capabilities"]["fuel"] is True
    assert state["fuel"]["per_lap"] == pytest.approx(0.5, abs=1e-6)

    events = client.get("/api/v1/racestate/events", params={"limit": 500}).json()
    keys = [e["key"] for e in events["events"]]
    assert [k for k in keys if k in set(MILESTONES)] == MILESTONES


def test_replay_rejects_a_second_concurrent_run(client):
    client.post("/api/v1/replay/start", json={"session_id": "scenario", "speed": 1})
    try:
        second = client.post(
            "/api/v1/replay/start", json={"session_id": "scenario", "speed": 1}
        )
        assert second.status_code == 409
    finally:
        client.post("/api/v1/replay/stop")


def test_replay_stop_halts_a_running_replay(client):
    client.post("/api/v1/replay/start", json={"session_id": "scenario", "speed": 1})
    body = client.post("/api/v1/replay/stop").json()
    assert body["running"] is False


def test_replay_requires_a_session_id(client):
    # There is no built-in synthetic race to fall back on any more.
    assert client.post("/api/v1/replay/start", json={}).status_code == 422


def test_replay_of_an_unknown_session_is_404(client):
    r = client.post("/api/v1/replay/start", json={"session_id": "nope", "speed": 0})
    assert r.status_code == 404


def test_replay_from_the_store_reproduces_the_scenario(client):
    """The engine cannot tell a Parquet round-trip from the in-memory script."""
    from evals.synthetic_race import seed_scenario_session

    seed_scenario_session(client.app.state.services.writer, "stored-scenario")

    r = client.post(
        "/api/v1/replay/start", json={"session_id": "stored-scenario", "speed": 0}
    )
    assert r.status_code == 200
    status = _await_replay(client, timeout=60.0)
    assert status["error"] is None

    events = client.get("/api/v1/racestate/events", params={"limit": 500}).json()
    keys = [e["key"] for e in events["events"]]
    assert [k for k in keys if k in set(MILESTONES)] == MILESTONES

    state = client.get("/api/v1/racestate").json()
    assert state["session"]["session_id"] == "stored-scenario"
    assert state["fuel"]["per_lap"] == pytest.approx(0.5, abs=1e-3)


# --------------------------------------------------------------------------
# /ws/pitwall contract
# --------------------------------------------------------------------------
def test_pitwall_ws_opens_with_state_and_acks_a_rate_change(client):
    with client.websocket_connect("/ws/pitwall") as ws:
        first = ws.receive_json()
        assert first["type"] == "state"
        assert first["state"]["version"] == 0

        ws.send_json({"op": "subscribe", "rate_hz": 10})
        for _ in range(20):
            msg = ws.receive_json()
            if msg["type"] == "ack":
                assert msg["rate_hz"] == 10
                break
        else:  # pragma: no cover - contract failure
            raise AssertionError("no ack for subscribe")


def test_pitwall_ws_rejects_an_unknown_op(client):
    with client.websocket_connect("/ws/pitwall") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"op": "wat"})
        for _ in range(20):
            msg = ws.receive_json()
            if msg["type"] == "error":
                assert msg["code"] == "unknown_op"
                break
        else:  # pragma: no cover - contract failure
            raise AssertionError("no error for unknown op")


def test_pitwall_ws_pushes_events_as_they_happen(client):
    with client.websocket_connect("/ws/pitwall") as ws:
        assert ws.receive_json()["type"] == "state"
        client.post(
            "/api/v1/replay/start", json={"session_id": "scenario", "speed": 0}
        )

        seen: list[str] = []
        for _ in range(600):
            msg = ws.receive_json()
            if msg["type"] == "event":
                seen.append(msg["event"]["key"])
                if msg["event"]["key"] == "pit_exit":
                    break
            elif msg["type"] == "state":
                assert "capabilities" in msg["state"]

        # Events are never coalesced away, so the full ordered set must be here.
        assert [k for k in seen if k in set(MILESTONES)] == MILESTONES


# --------------------------------------------------------------------------
# feature flag
# --------------------------------------------------------------------------
def test_pitwall_routes_absent_when_disabled(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, pitwall=False) as c:
        assert c.get("/api/v1/racestate").status_code == 404
        assert c.post("/api/v1/replay/start", json={}).status_code == 404
        assert c.app.state.services.engine is None
        # v1 endpoints are untouched.
        assert c.get("/api/v1/health").status_code == 200
        assert c.get("/api/v1/sessions").status_code == 200


# --------------------------------------------------------------------------
# a replay is indistinguishable from live on /ws/live
# --------------------------------------------------------------------------
def test_replay_streams_over_ws_live_like_a_live_session(client):
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"op": "subscribe", "channels": ["Speed", "Lap"], "rate_hz": 60})
        client.post("/api/v1/replay/start", json={"session_id": "scenario", "speed": 0})
        seen: dict[str, dict] = {}
        for _ in range(2000):
            msg = ws.receive_json()
            seen.setdefault(msg["type"], msg)
            if msg["type"] == "state" and msg["connection"] == "replay":
                seen["replay_state"] = msg
            if {"replay_state", "catalog", "data"} <= seen.keys():
                break
    client.post("/api/v1/replay/stop")

    assert seen["replay_state"]["session_id"] == "scenario"
    assert "Speed" in {v["name"] for v in seen["catalog"]["variables"]}
    assert "Speed" in seen["data"]["v"]


def test_hub_goes_back_to_disconnected_when_a_replay_ends(client):
    client.post("/api/v1/replay/start", json={"session_id": "scenario", "speed": 0})
    assert _await_replay(client)["finished"] is True
    for _ in range(100):  # the lifecycle sink runs just after the status flips
        state, session_id, _seq = client.app.state.services.hub.state()
        if state.value == "disconnected":
            break
        time.sleep(0.01)
    assert state.value == "disconnected" and session_id is None


# --------------------------------------------------------------------------
# .ibt upload
# --------------------------------------------------------------------------
def test_upload_rejects_a_non_ibt_file(client):
    r = client.post("/api/v1/import/upload", params={"filename": "lap.csv"}, content=b"x")
    assert r.status_code == 400


def test_upload_rejects_an_empty_body(client):
    r = client.post("/api/v1/import/upload", params={"filename": "lap.ibt"}, content=b"")
    assert r.status_code == 400


def test_upload_stores_the_file_and_starts_an_import_job(client):
    r = client.post(
        "/api/v1/import/upload", params={"filename": "spa race.ibt"}, content=b"not-ibt"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bytes"] == 7
    assert body["path"].endswith("_spa race.ibt")
    # The bytes are not a real .ibt, so the job ends in an error, not a session.
    job = {}
    for _ in range(200):
        job = client.get(f"/api/v1/import/{body['job_id']}").json()
        if job["status"] in ("complete", "error"):
            break
        time.sleep(0.02)
    assert job["status"] == "error"
