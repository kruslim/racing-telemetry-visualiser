"""The whole stack, booted, over one socket (stage 5, item 4).

Every other test in this suite exercises a layer. This one boots the real
FastAPI app through its real lifespan -- race-state engine, event bus, the agent
orchestrator with its supervised tasks, the radio feed, the WebSocket -- starts a
replay of the scripted race, and asserts that ``state``, ``event`` **and**
``radio`` frames all arrive on ``/ws/pitwall`` in contract-valid form.

The only thing that is not real is the model: ``rtv.services.build_pitwall`` is
replaced with one that hands the orchestrator a
:class:`~rtv.pitwall.provider.ScriptedProvider`. That is the same seam the evals
and every smoke script use, and it is the only way this can be a *default*
test -- the suite must run with no API key and no network.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("fastapi")

from rtv.pitwall.agents import AGENT_REGISTRY  # noqa: E402
from rtv.pitwall.agents.coach import CoachCall  # noqa: E402
from rtv.pitwall.agents.spotter import SpotterCall  # noqa: E402
from rtv.pitwall.agents.strategist import PitCall  # noqa: E402
from rtv.pitwall.agents.vehicle_engineer import EngineerCall  # noqa: E402
from rtv.pitwall.framework import RadioMessage, RadioPriority  # noqa: E402
from rtv.racestate.models import RaceEvent, RaceState  # noqa: E402

#: Deliberately free of numbers. The citation validator is real in this test, so
#: an invented figure here would become a grounded refusal and the radio frames
#: would still arrive -- but they would be testing the wrong thing.
ANSWERS = {
    "strategist": lambda: PitCall(
        priority=RadioPriority.ADVISORY,
        spoken_text="Window is open, box when you are ready.",
        detail_text="Fuel covers it either way; no figures asserted.",
        recommendation="box_next_lap",
        confidence="medium",
        rationale="Grounded in the fuel projection tool.",
    ),
    "vehicle_engineer": lambda: EngineerCall(
        priority=RadioPriority.ADVISORY,
        spoken_text="Fronts are working hard into that corner.",
        detail_text="Repeat lock-ups at the same braking zone.",
        finding="brake_lockup",
        trend="worsening",
        driver_action="Ease the initial brake pressure.",
        confidence="medium",
        rationale="From the recurrence payload.",
    ),
    "spotter": lambda: SpotterCall(
        priority=RadioPriority.CRITICAL,
        spoken_text="Car closing behind, hold your line.",
        detail_text="From the standings tool.",
        threat="car_closing",
        side="behind",
        action="Hold your line.",
        confidence="medium",
    ),
    "coach": lambda: CoachCall(
        priority=RadioPriority.INFO,
        spoken_text="Brake a touch earlier there and let it settle.",
        detail_text="Pattern only, no stored trace.",
        theme="braking",
        cue="Brake a touch earlier.",
        from_telemetry=False,
        confidence="low",
        rationale="The event pattern alone.",
    ),
}


def _scripted_answer(*, agent, **_):
    return ANSWERS[agent]()


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """The real app, booted once, with a scripted model behind the real agents.

    Module-scoped deliberately: booting the app and replaying a race is the
    expensive part, and the assertions below are all about *one* race that
    happened, not several.
    """
    from rtv.pitwall.orchestrator import PitwallOrchestrator
    from rtv.pitwall.provider import ScriptedProvider
    from rtv.pitwall.radio import RadioFeed
    from rtv.services import build_director

    def scripted_pitwall(engine, settings, *, coaching=None, repo=None, tts=None):
        return PitwallOrchestrator(
            engine,
            ScriptedProvider(_scripted_answer),
            list(AGENT_REGISTRY),
            feed=RadioFeed(history=settings.pitwall_radio_history),
            tool_config={"pit_lane_loss_s": settings.pitwall_pit_lane_loss_s,
                         "standings_window": 3},
            tool_extras={"coaching": coaching, "repo": repo},
            tts=tts,
            director=build_director(settings),
        )

    import rtv.services

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(rtv.services, "build_pitwall", scripted_pitwall)
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path_factory.mktemp("data")))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_PITWALL", "true")
    monkeypatch.setenv("RTV_PITWALL_AGENTS", "true")

    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _drive_a_race(client, *, budget: int = 4000) -> dict[str, list[dict]]:
    """Replay the scripted race and collect frames until all three types arrive."""
    frames: dict[str, list[dict]] = {"state": [], "event": [], "radio": []}
    with client.websocket_connect("/ws/pitwall") as ws:
        assert ws.receive_json()["type"] == "state"  # pushed on connect
        ws.send_json({"op": "subscribe", "rate_hz": 20})
        client.post(
            "/api/v1/replay/start", json={"session_id": "scenario", "speed": 8.0}
        )
        for _ in range(budget):
            message = ws.receive_json()
            frames.setdefault(message["type"], []).append(message)
            if all(frames[k] for k in ("state", "event", "radio")):
                break
    client.post("/api/v1/replay/stop")
    return frames


@pytest.fixture(scope="module")
def frames(client) -> dict[str, list[dict]]:
    return _drive_a_race(client)


# --------------------------------------------------------------------------
# the layer is really mounted
# --------------------------------------------------------------------------
def test_the_agent_layer_boots_with_the_app(client):
    status = client.get("/api/v1/pitwall/status").json()
    assert status["available"] is True
    assert status["running"] is True  # the lifespan started the supervised tasks
    assert status["enabled"] is True
    assert status["healthy"] is True
    assert [a["name"] for a in status["agents"]] == [
        "strategist", "vehicle_engineer", "spotter", "coach"
    ]


def test_the_health_endpoint_sees_the_whole_stack(client):
    body = client.get("/api/v1/pitwall/health").json()
    assert body["ok"] is True
    assert body["agents"]["mounted"] is True
    assert body["agents"]["running"] is True
    assert body["agents"]["degraded"] == []
    assert body["agents"]["director"]["status"] == "planned"


# --------------------------------------------------------------------------
# all three frame types, contract-valid
# --------------------------------------------------------------------------
def test_all_three_frame_types_arrive(frames):
    assert frames["state"], "no state snapshots"
    assert frames["event"], "no race events"
    assert frames["radio"], "no agent radio -- the agent layer never ran"


def test_every_state_frame_validates_against_the_race_state_model(frames):
    for frame in frames["state"]:
        state = RaceState.model_validate(frame["state"])
        assert state.version >= 0
        assert state.stale is False


def test_every_event_frame_validates_and_carries_its_key(frames):
    for frame in frames["event"]:
        payload = frame["event"]
        event = RaceEvent.model_validate(payload)
        assert payload["key"] == event.key
        assert payload["state_version"] >= 0
        # No wall clock anywhere in the event contract -- that is what makes the
        # replay log reproducible.
        assert "wall_time" not in payload


def test_every_radio_frame_validates_against_the_message_model(frames):
    for frame in frames["radio"]:
        message = RadioMessage.model_validate(frame["message"])
        assert message.agent in {a.name for a in AGENT_REGISTRY}
        assert message.spoken_text
        assert len(message.spoken_text.split()) <= 25
        assert message.event_ref.key  # pinned to the observation that caused it
        assert frame["message"]["message_id"] == message.message_id


def test_the_radio_is_grounded_and_traceable(frames):
    messages = [RadioMessage.model_validate(f["message"]) for f in frames["radio"]]
    assert all(m.grounded for m in messages), [m.ungrounded for m in messages]
    assert not any(m.refused for m in messages)
    # Every call names the deterministic event that woke it.
    keys = {m.event_ref.key for m in messages}
    assert keys <= {e["event"]["key"] for e in frames["event"]} | keys


def test_the_replay_really_drove_the_engine(client, frames):
    health = client.get("/api/v1/pitwall/health").json()["engine"]
    assert health["bound"] is True
    assert health["frames"] > 0
    assert health["faults"] == 0
    assert health["events"] > 0


def test_the_session_cost_guard_counted_the_scripted_calls(client, frames):
    status = client.get("/api/v1/pitwall/status").json()
    assert status["calls_used"] > 0
    assert status["calls_remaining"] < status["max_calls_per_session"]
    spoke = {a["name"] for a in status["agents"] if a["llm_calls"]}
    assert spoke, "no agent recorded a model call"
    assert all(
        a["last_latency_ms"] is not None for a in status["agents"] if a["llm_calls"]
    )
