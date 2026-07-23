"""The pitwall agent HTTP + WebSocket surface, driven offline through TestClient.

The agent layer is not mounted without an API key, and that is deliberate: the
default test path must never depend on the network. So these tests cover both
shapes -- the honest "not available, here is why" response, and the full surface
with an orchestrator wired in behind a scripted provider.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("fastapi")

from rtv.pitwall.agents.strategist import STRATEGIST, PitCall  # noqa: E402
from rtv.pitwall.framework import RadioPriority  # noqa: E402
from rtv.pitwall.orchestrator import PitwallOrchestrator  # noqa: E402
from rtv.pitwall.provider import ScriptedProvider  # noqa: E402
from rtv.pitwall.radio import RadioFeed  # noqa: E402


def _answer(**kw) -> PitCall:
    return PitCall(
        priority=kw.pop("priority", RadioPriority.ADVISORY),
        spoken_text=kw.pop("spoken_text", "Window opens lap five."),
        detail_text="Fuel window is open.",
        recommendation="box_next_lap",
        confidence="high",
        rationale="No figures asserted.",
        **kw,
    )


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_PITWALL", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    return TestClient(create_app())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        yield c


@pytest.fixture()
def agent_client(tmp_path, monkeypatch):
    """A client with the agent layer wired to a scripted provider."""
    with _client(tmp_path, monkeypatch) as c:
        services = c.app.state.services
        services.pitwall = PitwallOrchestrator(
            services.engine,
            ScriptedProvider(lambda **_: _answer()),
            [STRATEGIST],
            feed=RadioFeed(),
            clock=None,
        )
        yield c


# --------------------------------------------------------------------------
# without a key, the layer is absent -- and says so
# --------------------------------------------------------------------------
def test_status_explains_why_the_agent_layer_is_not_running(client):
    body = client.get("/api/v1/pitwall/status").json()
    assert body["available"] is False
    assert "ANTHROPIC_API_KEY" in body["reason"]
    assert "strategist" in body["known_agents"]


def test_the_deterministic_pitwall_still_works_without_the_agent_layer(client):
    assert client.app.state.services.pitwall is None
    assert client.get("/api/v1/racestate").status_code == 200
    assert client.get("/api/v1/health").status_code == 200


def test_radio_and_switches_are_503_rather_than_silently_no_ops(client):
    assert client.get("/api/v1/pitwall/radio").status_code == 503
    assert client.post("/api/v1/pitwall/enabled", json={"enabled": False}).status_code == 503


# --------------------------------------------------------------------------
# with the layer mounted
# --------------------------------------------------------------------------
def test_status_reports_the_agents_and_their_triggers(agent_client):
    body = agent_client.get("/api/v1/pitwall/status").json()
    assert body["available"] is True and body["enabled"] is True
    agent = body["agents"][0]
    assert agent["name"] == "strategist"
    assert agent["output_contract"] == "PitCall"
    assert any(t["event_type"] == "pit_window_open" for t in agent["triggers"])
    assert "simulate_pit_outcome" in agent["tools"]


def test_the_kill_switch_is_reachable_over_http(agent_client):
    body = agent_client.post("/api/v1/pitwall/enabled", json={"enabled": False}).json()
    assert body["enabled"] is False
    assert agent_client.get("/api/v1/pitwall/status").json()["enabled"] is False

    agent_client.post("/api/v1/pitwall/enabled", json={"enabled": True})
    assert agent_client.get("/api/v1/pitwall/status").json()["enabled"] is True


def test_one_agent_can_be_disabled_without_touching_the_rest(agent_client):
    body = agent_client.post(
        "/api/v1/pitwall/agents/strategist/enabled", json={"enabled": False}
    ).json()
    assert body == {"agent": "strategist", "enabled": False}
    status = agent_client.get("/api/v1/pitwall/status").json()
    assert status["enabled"] is True  # the layer is still live
    assert status["agents"][0]["enabled"] is False


def test_an_unknown_agent_is_404(agent_client):
    r = agent_client.post("/api/v1/pitwall/agents/ghost/enabled", json={"enabled": True})
    assert r.status_code == 404


def test_the_radio_endpoint_serves_the_ring_buffer(agent_client):
    feed = agent_client.app.state.services.pitwall.feed
    empty = agent_client.get("/api/v1/pitwall/radio").json()
    assert empty == {"count": 0, "messages": [], "pending": []}

    from rtv.pitwall.framework import EventRef, RadioMessage
    from rtv.racestate.models import EventType, RaceEvent

    event = RaceEvent(event_type=EventType.PIT_WINDOW_OPEN, tick=1, session_time=1.0)
    feed.emit(
        RadioMessage(
            agent="strategist",
            priority=RadioPriority.CRITICAL,
            spoken_text="Box box box.",
            detail_text="Fuel is done.",
            event_ref=EventRef.of(event),
            data={"recommendation": "box_now"},
        )
    )
    body = agent_client.get("/api/v1/pitwall/radio").json()
    assert body["count"] == 1
    message = body["messages"][0]
    assert message["spoken_text"] == "Box box box."
    assert message["priority"] == "critical"
    assert message["event_ref"]["key"] == "pit_window_open"
    assert message["data"]["recommendation"] == "box_now"


def test_reset_clears_the_channel(agent_client):
    feed = agent_client.app.state.services.pitwall.feed
    from rtv.pitwall.framework import EventRef, RadioMessage
    from rtv.racestate.models import EventType, RaceEvent

    event = RaceEvent(event_type=EventType.PIT_WINDOW_OPEN, tick=1, session_time=1.0)
    feed.emit(
        RadioMessage(
            agent="strategist", priority=RadioPriority.INFO,
            spoken_text="x", detail_text="x", event_ref=EventRef.of(event),
        )
    )
    assert agent_client.get("/api/v1/pitwall/radio").json()["count"] == 1
    agent_client.post("/api/v1/pitwall/reset")
    assert agent_client.get("/api/v1/pitwall/radio").json()["count"] == 0


# --------------------------------------------------------------------------
# /ws/pitwall radio frames
# --------------------------------------------------------------------------
def test_the_pitwall_socket_pushes_radio_frames(agent_client):
    from rtv.pitwall.framework import EventRef, RadioMessage
    from rtv.racestate.models import EventType, RaceEvent

    feed = agent_client.app.state.services.pitwall.feed
    event = RaceEvent(event_type=EventType.PIT_WINDOW_OPEN, tick=1, session_time=1.0)

    with agent_client.websocket_connect("/ws/pitwall") as ws:
        assert ws.receive_json()["type"] == "state"
        feed.emit(
            RadioMessage(
                agent="strategist",
                priority=RadioPriority.CRITICAL,
                spoken_text="Box this lap, fuel only.",
                detail_text="Window is open.",
                event_ref=EventRef.of(event),
            )
        )
        for _ in range(20):
            msg = ws.receive_json()
            if msg["type"] == "radio":
                assert msg["message"]["agent"] == "strategist"
                assert msg["message"]["spoken_text"] == "Box this lap, fuel only."
                break
        else:  # pragma: no cover - contract failure
            raise AssertionError("no radio frame on /ws/pitwall")


def test_the_socket_still_works_when_the_agent_layer_is_absent(client):
    """v1 and stage-1 behaviour must not depend on the agent layer existing."""
    with client.websocket_connect("/ws/pitwall") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"op": "subscribe", "rate_hz": 5})
        for _ in range(20):
            msg = ws.receive_json()
            if msg["type"] == "ack":
                assert msg["rate_hz"] == 5
                break
        else:  # pragma: no cover - contract failure
            raise AssertionError("no ack for subscribe")
