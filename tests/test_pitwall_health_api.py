"""``GET /api/v1/pitwall/health`` and the live/replay interlock (stage 5).

``/racestate`` says what the race is doing. This says whether anything is still
watching it -- a distinction that only matters when something has gone wrong,
which is exactly when nobody can afford to go and read the logs.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("fastapi")


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
        yield c


def _await_replay(client, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    status: dict = {}
    while time.time() < deadline:
        status = client.get("/api/v1/replay/status").json()
        if status["finished"] or not status["running"]:
            return status
        time.sleep(0.02)
    raise AssertionError(f"Replay did not finish within {timeout}s: {status}")


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------
def test_health_answers_before_any_telemetry(client):
    body = client.get("/api/v1/pitwall/health").json()
    assert body["ok"] is True  # idle is healthy: nothing is meant to be producing
    assert body["pitwall"] is True
    assert body["engine"]["bound"] is False
    assert body["engine"]["frames"] == 0
    assert body["live"]["running"] is False
    assert body["live"]["state"] == "disconnected"
    assert body["replay"]["running"] is False
    assert body["agents"]["mounted"] is False


def test_health_reports_the_engine_after_a_replay(client):
    client.post("/api/v1/replay/start", json={"session_id": "scenario", "speed": 0})
    _await_replay(client)

    body = client.get("/api/v1/pitwall/health").json()
    assert body["ok"] is True
    assert body["engine"]["bound"] is True
    assert body["engine"]["frames"] > 3000
    assert body["engine"]["faults"] == 0
    assert body["engine"]["source"] == "replay"
    assert body["engine"]["seconds_since_frame"] >= 0
    assert body["engine"]["capabilities"]["standings"] is True
    assert body["engine"]["bus"]["published"] > 0
    assert body["replay"]["finished"] is True


def test_health_is_the_only_pitwall_route_that_never_errors(client):
    # The agent layer is not mounted offline, so /pitwall/radio 503s -- but the
    # health of the deterministic path is still a question with an answer.
    assert client.get("/api/v1/pitwall/radio").status_code == 503
    assert client.get("/api/v1/pitwall/health").status_code == 200


def test_health_is_absent_with_the_pitwall_disabled(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, pitwall=False) as client:
        assert client.get("/api/v1/pitwall/health").status_code == 404
        assert client.get("/api/v1/health").json()["status"] == "ok"


def test_the_engine_is_built_resilient_on_the_live_path(client):
    assert client.get("/api/v1/pitwall/health").json()["engine"]["resilient"] is True


# --------------------------------------------------------------------------
# the live/replay interlock
# --------------------------------------------------------------------------
def test_a_replay_is_refused_while_the_live_poller_runs(client, monkeypatch):
    services = client.app.state.services
    monkeypatch.setattr(type(services.poller), "running", property(lambda self: True))

    response = client.post("/api/v1/replay/start", json={"session_id": "scenario"})

    assert response.status_code == 409
    assert "live poller" in response.json()["detail"]
    assert services.replay.status().running is False


def test_a_second_replay_is_still_refused(client):
    client.post("/api/v1/replay/start", json={"session_id": "scenario", "speed": 1.0})
    try:
        second = client.post("/api/v1/replay/start", json={"session_id": "scenario"})
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]
    finally:
        client.post("/api/v1/replay/stop")


def test_an_unknown_session_is_still_a_404(client):
    response = client.post("/api/v1/replay/start", json={"session_id": "nope"})
    assert response.status_code == 404


# --------------------------------------------------------------------------
# the director script is validated at boot, and a bad one is not fatal
# --------------------------------------------------------------------------
def _settings(**kw):
    from rtv.config import Settings

    return Settings(**kw)


def test_no_script_configured_gives_a_bare_noop_director():
    from rtv.services import build_director

    director = build_director(_settings())
    assert director.describe()["script"] is None
    assert director.describe()["implemented"] is False


def test_a_configured_script_is_loaded_and_validated():
    from pathlib import Path

    from rtv.services import build_director

    example = Path(__file__).resolve().parents[1] / "docs" / "director_scenario.example.json"
    described = build_director(
        _settings(pitwall_director_script=str(example))
    ).describe()
    assert described["injections"] == 5
    # ...and still injects nothing: the layer is interfaces only.
    assert described["implemented"] is False
    assert described["fired"] == 0


def test_a_missing_script_is_a_warning_not_a_boot_failure(tmp_path):
    from rtv.services import build_director

    director = build_director(
        _settings(pitwall_director_script=str(tmp_path / "ghost.json"))
    )
    assert director.describe()["script"] is None


def test_a_malformed_script_is_a_warning_not_a_boot_failure(tmp_path):
    from rtv.services import build_director

    bad = tmp_path / "bad.json"
    bad.write_text('{"name": "no when clause", "injections": [{"id": "a"}]}', "utf-8")

    director = build_director(_settings(pitwall_director_script=str(bad)))

    # Losing a rehearsal is a smaller failure than refusing to start the pitwall.
    assert director.describe()["script"] is None
