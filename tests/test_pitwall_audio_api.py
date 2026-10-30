"""The audio surface: /pitwall/tts, /pitwall/audio/{id} and /pitwall/driver-message.

The default deployment has no backend voice at all -- the browser speaks -- so the
contract that matters most is the *honest 503*: the frontend has to be able to
tell "no backend audio, speak it yourself" from "something is broken".
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("fastapi")

from rtv.pitwall.agents.strategist import STRATEGIST, PitCall  # noqa: E402
from rtv.pitwall.framework import EventRef, RadioMessage, RadioPriority  # noqa: E402
from rtv.pitwall.orchestrator import PitwallOrchestrator  # noqa: E402
from rtv.pitwall.provider import ScriptedProvider  # noqa: E402
from rtv.pitwall.radio import RadioFeed  # noqa: E402
from rtv.pitwall.tts import (  # noqa: E402
    NullProvider,
    RadioTTS,
    RestTTSProvider,
    TTSConfig,
    TTSResponse,
)
from rtv.racestate.models import EventType, RaceEvent  # noqa: E402


class FakeTransport:
    def __init__(self, *, status: int = 200, content: bytes = b"ID3-fake-audio") -> None:
        self.requests = []
        self.status = status
        self.content = content

    async def __call__(self, request):
        self.requests.append(request)
        return TTSResponse(status=self.status, content=self.content, media_type="audio/mpeg")


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


def _mount_agents(client, tts: RadioTTS | None = None) -> PitwallOrchestrator:
    services = client.app.state.services
    orchestrator = PitwallOrchestrator(
        services.engine,
        ScriptedProvider(lambda **_: _answer()),
        [STRATEGIST],
        feed=RadioFeed(),
        clock=None,
        tts=tts,
    )
    services.pitwall = orchestrator
    if tts is not None:
        services.tts = tts
    return orchestrator


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        yield c


@pytest.fixture()
def agent_client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        _mount_agents(c)
        yield c


@pytest.fixture()
def voiced_client(tmp_path, monkeypatch):
    """The premium path, with the vendor call replaced by a fake transport."""
    with _client(tmp_path, monkeypatch) as c:
        transport = FakeTransport()
        provider = RestTTSProvider(
            TTSConfig(
                provider="rest",
                url="https://example.invalid/v1/audio/speech",
                api_key="k-123",
            ),
            transport=transport,
        )
        orchestrator = _mount_agents(c, RadioTTS(provider))
        c.transport_spy = transport
        c.orchestrator = orchestrator
        yield c


def _message(agent: str = "strategist", text: str = "Box this lap, fuel only.") -> RadioMessage:
    event = RaceEvent(event_type=EventType.PIT_WINDOW_OPEN, tick=1, session_time=1.0)
    return RadioMessage(
        agent=agent,
        priority=RadioPriority.CRITICAL,
        spoken_text=text,
        detail_text="Window is open.",
        event_ref=EventRef.of(event),
    )


# --------------------------------------------------------------------------
# /pitwall/tts -- the frontend's first question
# --------------------------------------------------------------------------
def test_the_tts_endpoint_answers_even_with_no_agent_layer(client):
    body = client.get("/api/v1/pitwall/tts").json()
    assert body["available"] is False
    assert body["engine"] == "webspeech"
    assert "Web Speech" in body["reason"]
    # The browser still needs the role voices to sound like a pitwall.
    assert set(body["voices"]) >= {"strategist", "vehicle_engineer", "spotter", "coach"}
    assert body["voices"]["spotter"]["rate"] != body["voices"]["strategist"]["rate"]


def test_the_tts_endpoint_reports_a_configured_backend_voice(voiced_client):
    body = voiced_client.get("/api/v1/pitwall/tts").json()
    assert body["available"] is True
    assert body["engine"] == "backend"
    assert body["provider"] == "rest"
    assert body["url_prefix"] == "/api/v1/pitwall/audio"


# --------------------------------------------------------------------------
# /pitwall/audio/{message_id}
# --------------------------------------------------------------------------
def test_audio_is_503_without_the_agent_layer(client):
    assert client.get("/api/v1/pitwall/audio/whatever").status_code == 503


def test_audio_is_503_with_no_backend_provider_and_says_to_use_web_speech(agent_client):
    feed = agent_client.app.state.services.pitwall.feed
    message = _message()
    feed.emit(message)
    response = agent_client.get(f"/api/v1/pitwall/audio/{message.message_id}")
    assert response.status_code == 503
    assert "Web Speech" in response.json()["detail"]


def test_audio_serves_the_synthesised_bytes_once_and_then_from_cache(voiced_client):
    feed = voiced_client.app.state.services.pitwall.feed
    message = _message()
    feed.emit(message)

    response = voiced_client.get(f"/api/v1/pitwall/audio/{message.message_id}")
    assert response.status_code == 200
    assert response.content == b"ID3-fake-audio"
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-rtv-voice"] == "onyx"
    assert "max-age" in response.headers["cache-control"]
    assert len(voiced_client.transport_spy.requests) == 1

    again = voiced_client.get(f"/api/v1/pitwall/audio/{message.message_id}")
    assert again.content == b"ID3-fake-audio"
    assert len(voiced_client.transport_spy.requests) == 1, "served from the clip cache"


def test_audio_can_be_fetched_while_the_message_is_still_queued(voiced_client):
    """A browser may ask for the clip before the call reaches the front."""
    orchestrator = voiced_client.orchestrator
    message = _message()
    orchestrator.publish(message)  # queued, not yet aired
    assert orchestrator.feed.history() == []
    assert voiced_client.get(f"/api/v1/pitwall/audio/{message.message_id}").status_code == 200


def test_an_unknown_message_id_is_404_not_an_invitation_to_synthesise_anything(voiced_client):
    response = voiced_client.get("/api/v1/pitwall/audio/deadbeefdeadbeef")
    assert response.status_code == 404
    assert len(voiced_client.transport_spy.requests) == 0


def test_a_vendor_failure_is_502_so_the_frontend_falls_back_rather_than_going_quiet(
    tmp_path, monkeypatch
):
    with _client(tmp_path, monkeypatch) as c:
        provider = RestTTSProvider(
            TTSConfig(provider="rest", url="https://x.invalid", api_key="k"),
            transport=FakeTransport(status=503),
        )
        orchestrator = _mount_agents(c, RadioTTS(provider))
        message = _message()
        orchestrator.feed.emit(message)
        response = c.get(f"/api/v1/pitwall/audio/{message.message_id}")
        assert response.status_code == 502


# --------------------------------------------------------------------------
# audio_url on the wire
# --------------------------------------------------------------------------
def test_a_published_message_carries_no_audio_url_without_a_provider(agent_client):
    orchestrator = agent_client.app.state.services.pitwall
    message = orchestrator.publish(_message())
    assert message.audio_url is None
    assert message.to_api()["audio_url"] is None


def test_a_published_message_carries_its_audio_url_when_a_provider_can_speak(voiced_client):
    message = voiced_client.orchestrator.publish(_message())
    assert message.audio_url == f"/api/v1/pitwall/audio/{message.message_id}"
    body = voiced_client.get("/api/v1/pitwall/radio").json()
    assert body["pending"][0]["audio_url"] == message.audio_url


def test_the_radio_payload_carries_everything_the_audio_manager_needs(agent_client):
    feed = agent_client.app.state.services.pitwall.feed
    feed.emit(_message())
    payload = agent_client.get("/api/v1/pitwall/radio").json()["messages"][0]
    for key in ("message_id", "agent", "priority", "spoken_text", "subject", "speak", "audio_url"):
        assert key in payload, key
    assert payload["speak"] is True


# --------------------------------------------------------------------------
# push to talk
# --------------------------------------------------------------------------
def test_a_driver_message_joins_the_log_but_is_never_read_back(agent_client):
    response = agent_client.post(
        "/api/v1/pitwall/driver-message",
        json={"text": "  Fronts are gone, I need tyres.  "},
    )
    assert response.status_code == 200
    message = response.json()["message"]
    assert message["agent"] == "driver"
    assert message["spoken_text"] == "Fronts are gone, I need tyres."
    assert message["speak"] is False, "the pitwall does not read the driver's words back"
    assert message["priority"] == "info"
    assert message["event_ref"]["key"] == "driver_message"

    # Emitted, not queued: the driver already used the airtime.
    body = agent_client.get("/api/v1/pitwall/radio").json()
    assert body["count"] == 1
    assert body["pending"] == []
    assert body["messages"][0]["message_id"] == message["message_id"]


def test_a_driver_message_never_carries_backend_audio(voiced_client):
    message = voiced_client.post(
        "/api/v1/pitwall/driver-message", json={"text": "Box this lap?"}
    ).json()["message"]
    assert message["audio_url"] is None
    assert len(voiced_client.transport_spy.requests) == 0


def test_an_empty_driver_message_is_refused(agent_client):
    assert agent_client.post("/api/v1/pitwall/driver-message", json={"text": ""}).status_code == 422
    assert (
        agent_client.post("/api/v1/pitwall/driver-message", json={"text": "   "}).status_code
        == 422
    )


def test_driver_messages_need_the_agent_layer(client):
    response = client.post("/api/v1/pitwall/driver-message", json={"text": "hello"})
    assert response.status_code == 503


def test_the_driver_transcript_reaches_the_socket(agent_client):
    with agent_client.websocket_connect("/ws/pitwall") as ws:
        assert ws.receive_json()["type"] == "state"
        agent_client.post("/api/v1/pitwall/driver-message", json={"text": "Tyres are done."})
        for _ in range(20):
            frame = ws.receive_json()
            if frame["type"] == "radio":
                assert frame["message"]["agent"] == "driver"
                assert frame["message"]["speak"] is False
                return
        raise AssertionError("no radio frame for the driver message")


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def test_status_reports_the_voice_engine(voiced_client, agent_client):
    body = voiced_client.get("/api/v1/pitwall/status").json()
    assert body["tts"]["available"] is True and body["tts"]["provider"] == "rest"


def test_status_without_a_voice_says_so_rather_than_omitting_it(agent_client):
    body = agent_client.get("/api/v1/pitwall/status").json()
    assert body["tts"] is None


def test_services_always_build_a_tts_object_even_with_the_pitwall_off(tmp_path, monkeypatch):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_PITWALL", "false")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    with TestClient(create_app()) as c:
        services = c.app.state.services
        assert services.engine is None
        assert isinstance(services.tts.provider, NullProvider)
        # v1 surface is untouched by any of this.
        assert c.get("/api/v1/health").status_code == 200
        assert c.get("/api/v1/pitwall/tts").status_code == 404  # route not mounted
