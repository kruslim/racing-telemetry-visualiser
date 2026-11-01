"""Stage 08 end to end: the race plan across a whole race, through the real app.

The synthetic race (a race segment) is replayed through the app's own engine, journal
and agent layer with a scripted strategist. At the pit window the strategist first
proposes a stop past the run-dry lap (rejected), then a stop a lap early (accepted),
then answers. Asserted from the journal, as the review reads it: a baseline plan
appears, survives the yellow, is revised at the pit window, every revision traces to
a grounded proposal, and the rejected proposal left the plan unchanged.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from evals.synthetic_race import SCENARIO_SESSION_ID, ScenarioSpec, scenario_source  # noqa: E402

from rtv.pitwall.agents.strategist import STRATEGIST, PitCall  # noqa: E402
from rtv.pitwall.framework import RadioPriority  # noqa: E402
from rtv.pitwall.orchestrator import PitwallOrchestrator  # noqa: E402
from rtv.pitwall.provider import ScriptedProvider, tool_use_turn  # noqa: E402
from rtv.pitwall.radio import RadioFeed  # noqa: E402
from rtv.racestate.models import RaceEvent  # noqa: E402

RACE = ScenarioSpec(session_num=0, sessions=("Race",), laps=9)
BOGUS = {"next_stop_lap": 9, "rationale": "Stretch the stint to lap 9."}
EARLY = {"next_stop_lap": 4, "rationale": "Take the open window a lap early."}


def _strategist(*, messages, turn, **_):
    first = messages[0]["content"]
    text = first if isinstance(first, str) else json.dumps(first)
    if "pit_window_open" in text:
        if turn == 0:
            return tool_use_turn([("propose_plan_revision", BOGUS)])
        if turn == 1:
            return tool_use_turn([("propose_plan_revision", EARLY)])
        return PitCall(
            priority=RadioPriority.ADVISORY,
            spoken_text="Plan revised, box at the end of lap 4.",
            detail_text="The window opened on lap 4; the plan now stops on lap 4.",
            recommendation="plan_stop",
            target_lap=4,
            confidence="medium",
            rationale="The window is open.",
        )
    return PitCall(
        priority=RadioPriority.INFO,
        spoken_text="Holding, nothing to change.",
        detail_text="No figures asserted.",
        recommendation="hold",
        confidence="low",
        rationale="No change.",
    )


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
        yield c
    get_settings.cache_clear()


def test_the_race_plan_across_a_race(client):
    services = client.app.state.services
    engine = services.engine
    orch = PitwallOrchestrator(
        engine, ScriptedProvider(_strategist), [STRATEGIST], feed=RadioFeed(), clock=None
    )
    orch.attach_activity(services.activity)
    orch.feed.subscribe_callback(services.recorder.on_radio)
    services.pitwall = orch
    services.segments.pitwall = orch

    at_flags: list[tuple[str, int | None]] = []

    def run(ev: RaceEvent) -> None:
        if ev.event_type.value == "plan_revised":
            # Published from inside the strategist's tool call; no agent takes it.
            return
        if ev.event_type.value == "flag_change":
            plan = engine.state.plan
            at_flags.append((ev.payload.get("to"), plan.version if plan else None))
        asyncio.run(orch.handle_event(ev))
        orch.feed.flush()

    engine.bus.subscribe_callback(run)
    services.replay.start(scenario_source(RACE))
    services.replay.join(timeout=60)
    assert services.on_session_end.wait()

    [run_row] = client.get(f"/api/v1/sessions/{SCENARIO_SESSION_ID}/pitwall-runs").json()["runs"]
    records = client.get(
        f"/api/v1/sessions/{SCENARIO_SESSION_ID}/pitwall-log", params={"run": run_row["run_id"]}
    ).json()["records"]
    revised = [
        r["data"] for r in records
        if r["kind"] == "race_event" and r["data"]["key"] == "plan_revised"
    ]
    rejected = [r for r in records if r["kind"] == "plan_proposal_rejected"]
    proposals = [
        r
        for r in records
        if r["kind"] == "tool_call" and r["data"]["tool"] == "propose_plan_revision"
    ]

    # A baseline plan appears as soon as the fuel model does (lap 2), derived.
    baseline = revised[0]
    assert baseline["lap"] == 2 and baseline["payload"]["old_version"] is None
    assert baseline["payload"]["provenance"] == "derived"
    assert baseline["payload"]["changes"]["next_stop_lap"]["to"] == 5

    # It survives the full-course yellow on lap 3: there, unchanged, before and after.
    yellow = [v for to, v in at_flags if to == "yellow"]
    back = [v for to, v in at_flags if to == "green"]
    assert yellow and back and yellow[0] == back[-1] == baseline["payload"]["new_version"]

    # The bogus proposal was rejected with its reason and changed nothing.
    [rej] = rejected
    assert rej["agent"] == "strategist" and "run-dry lap 6" in rej["data"]["reason"]
    assert rej["data"]["proposal"]["next_stop_lap"] == 9
    assert rej["data"]["plan_version"] == baseline["payload"]["new_version"]

    # Revised at the pit window: the accepted proposal is the very next version.
    agent = [r for r in revised if r["payload"]["provenance"] == "agent_proposed"]
    [accepted] = agent
    assert accepted["lap"] == 4
    assert accepted["payload"]["old_version"] == rej["data"]["plan_version"]
    assert accepted["payload"]["changes"]["next_stop_lap"] == {"from": 5, "to": 4}

    # Every agent revision traces to a grounded proposal: a propose tool call in a
    # strategist run whose result says accepted, at that plan version.
    results = [p["data"]["result_full"] for p in proposals]
    assert [r["accepted"] for r in results] == [False, True]
    assert results[1]["plan_version"] == accepted["payload"]["new_version"]
    assert {p["agent"] for p in proposals} == {"strategist"}

    # The call that followed is grounded in the revised plan and aired.
    aired = [r["data"] for r in records if r["kind"] == "radio"]
    call = next(m for m in aired if m["spoken_text"].startswith("Plan revised"))
    assert call["grounded"] and not call["refused"] and call["data"]["target_lap"] == 4

    # The stop happened on lap 5 (the scripted pit entry), so the plan consumed it
    # and, with fuel to the flag, plans no further stop.
    final = client.get(
        f"/api/v1/sessions/{SCENARIO_SESSION_ID}/pitwall-log/state",
        params={"t": 1e6, "run": run_row["run_id"]},
    ).json()["record"]["data"]["plan"]
    assert final["stops_done"] == 1 and final["stop_count"]["value"] == 0
    # The journal's final snapshot carries the revision log, the rejection included.
    log = final["revisions"]
    assert [r["accepted"] for r in log if r["provenance"] == "agent_proposed"] == [False, True]
    versions = [r["payload"]["new_version"] for r in revised]
    assert versions == sorted(versions) and len(set(versions)) == len(versions)
