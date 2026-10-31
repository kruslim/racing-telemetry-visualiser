"""Real-journal eval cases (F3): recorded Spa runs re-scored offline, and the live mode.

Offline throughout. The committed set ``evals/real/spa_run`` was cut from a Kimi Spa
replay; here its recorded answers go back through today's validator and checks and must
match ``baseline.json``. A change to the validator or the checks that moves a verdict
fails this test with a per-case diff; if the change is intended, rebuild the baseline
(``scripts/build_real_evals.py --baseline spa_run``) and commit the diff with it.
The live mode is driven by ``ScriptedProvider``: no key, no network.
"""

from __future__ import annotations

import pytest
from evals.real import offline
from evals.real.cases import extract_cases, load_case_set, write_case_set
from evals.real.live import aggregate, run_case
from evals.synthetic_race import ScenarioSpec, replay_scenario

from rtv.pitwall import agent_input
from rtv.pitwall.activity import ActivityLog, public
from rtv.pitwall.agents import AGENT_REGISTRY
from rtv.pitwall.agents.strategist import STRATEGIST, PitCall
from rtv.pitwall.framework import AgentRuntime, RadioPriority
from rtv.pitwall.provider import ProviderResponse, ScriptedProvider
from rtv.racestate import RaceStateEngine
from rtv.racestate.models import EventType, RaceEvent, Severity
from rtv.store import journal
from rtv.store.journal import SessionRecorder

SPA = "spa_run"
SPECS = {spec.name: spec for spec in AGENT_REGISTRY}


# ---- the committed Spa set -------------------------------------------------
@pytest.fixture(scope="module")
def spa_cases() -> list[dict]:
    return load_case_set(SPA)


def test_spa_offline_results_match_the_baseline():
    now = offline.to_baseline(offline.score_set(SPA))
    lines = offline.diff(offline.read_baseline(SPA), now)
    assert not lines, (
        "Offline results of evals/real/spa_run changed. If intended, run "
        "`python scripts/build_real_evals.py --baseline spa_run` and commit the diff:\n"
        + "\n".join(lines[:60])
    )


def test_spa_set_covers_every_agent_and_rebuilds_each_input(spa_cases):
    assert {c["agent"] for c in spa_cases} == set(SPECS)
    for case in spa_cases:
        first = agent_input.model_input(case["input"])
        assert first["agent"] == case["agent"]
        assert first["messages"][0]["content"].startswith("TRIGGER: ")
        assert case["answers"] and case["answers"][-1]["output"]["spoken_text"]
        drift = agent_input.spec_drift(
            case["input"], SPECS[case["agent"]], AgentRuntime(
                SPECS[case["agent"]], ScriptedProvider(lambda **_: ProviderResponse())
            ).system_prompt,
        )
        assert set(drift) <= {"prompt", "tools", "output_contract", "model"}


def test_spa_validator_replay_agrees_with_the_recording_where_the_baseline_says(spa_cases):
    # Recorded verdicts are what the live validator said at the time. Each answer the
    # baseline marks as agreeing must reproduce exactly; that is what makes a later
    # disagreement a real change rather than noise in how the fact set is rebuilt.
    baseline = {c["case_id"]: c for c in offline.read_baseline(SPA)["cases"]}
    for case in spa_cases:
        scored = offline.score_case(case)
        for now, then in zip(scored["answers"], baseline[case["case_id"]]["answers"], strict=True):
            assert now["same_as_recorded"] == then["same_as_recorded"]


def test_a_validator_change_shows_up_as_a_diff(spa_cases):
    case = next(c for c in spa_cases if c["agent"] == "strategist")
    before = offline.score_case(case)
    tampered = {**case, "answers": [dict(a) for a in case["answers"]]}
    out = dict(tampered["answers"][-1]["output"])
    out["spoken_text"] = "Box now, we are 9137 laps short."
    tampered["answers"][-1] = {**tampered["answers"][-1], "output": out}
    after = offline.score_case(tampered)
    lines = offline.diff(offline.to_baseline([before]), offline.to_baseline([after]))
    assert any("answers" in line and "9137" in line for line in lines)
    assert after["answers"][-1]["ok"] is False


# ---- journal -> case -> scores, on a scripted run --------------------------
@pytest.fixture(scope="module")
def engine() -> RaceStateEngine:
    engine = RaceStateEngine(source="replay")
    replay_scenario(engine, ScenarioSpec())
    return engine


def _event() -> RaceEvent:
    return RaceEvent(
        event_type=EventType("pit_window_open"),
        tick=842 * 60,
        session_time=842.5,
        lap=7,
        severity=Severity.ADVISORY,
        payload={"lap": 7, "laps_remaining_fuel": 4.25},
    )


def _answer(text: str = "Stay out, no stop needed.") -> PitCall:
    return PitCall(
        priority=RadioPriority.ADVISORY,
        spoken_text=text,
        detail_text="Stay out.",
        recommendation="stay_out",
        confidence="high",
        rationale="No figures asserted.",
    )


async def _journal(tmp_path, engine, handler):
    log = ActivityLog(wall_clock=None)
    recorder = SessionRecorder(tmp_path, wall_clock=lambda: 1_790_000_000.0)
    log.subscribe_callback(recorder.on_activity)
    run_id = recorder.begin("s1", "replay")
    runtime = AgentRuntime(
        STRATEGIST, ScriptedProvider(handler), tracer=log, retry_attempts=0
    )
    await runtime.invoke(_event(), engine.snapshot())
    recorder.end()
    return journal.run_path(tmp_path, "s1", run_id), log


async def test_journal_keeps_full_state_and_answer_but_the_feed_does_not(tmp_path, engine):
    path, log = await _journal(tmp_path, engine, lambda **_: ProviderResponse(output=_answer()))
    records = list(journal.iter_records(path))
    start = next(r for r in records if r["kind"] == "run_start")
    check = next(r for r in records if r["kind"] == "validate")
    assert start["data"]["state_full"]["session"] == engine.snapshot().to_api()["session"]
    assert check["data"]["output_full"]["spoken_text"] == "Stay out, no stop needed."
    assert check["data"]["output_full"]["recommendation"] == "stay_out"
    for record in log.history():
        slim = public(record)["data"]
        assert "state_full" not in slim and "output_full" not in slim


async def test_cases_round_trip_and_score_like_the_live_validator(tmp_path, engine):
    path, _ = await _journal(
        tmp_path, engine, lambda **_: ProviderResponse(output=_answer("Box now, 47.3 laps."))
    )
    [case] = extract_cases(path)
    assert case["agent"] == "strategist" and case["case_id"].startswith("strategist-1-")
    assert len(case["answers"]) == 2  # first pass refused for 47.3, then the repair turn
    write_case_set([case], tmp_path / "set", source={"journal": "test"})
    [loaded] = load_case_set(tmp_path / "set")
    assert loaded["input"]["system_prompt"] == case["input"]["system_prompt"]
    scored = offline.score_case(loaded)
    assert all(a["same_as_recorded"] for a in scored["answers"])
    assert scored["answers"][0]["ok"] is False and "47.3" in scored["answers"][0]["unsupported"]

    offline.write_baseline(tmp_path / "set")
    assert offline.diff(
        offline.read_baseline(tmp_path / "set"),
        offline.to_baseline(offline.score_set(tmp_path / "set")),
    ) == []


async def test_live_mode_reexecutes_the_recorded_input(tmp_path, engine):
    path, _ = await _journal(tmp_path, engine, lambda **_: ProviderResponse(output=_answer()))
    [case] = extract_cases(path)

    provider = ScriptedProvider(lambda **_: ProviderResponse(output=_answer("Stay out.")))
    row = await run_case(case, provider)
    assert row["spec_drift"] == []
    assert row["new_spoken"] == "Stay out." and row["new_score"] == row["recorded_score"]
    assert row["unrecorded_tool_calls"] == []
    # The re-run was sent exactly the recorded first call.
    sent = provider.calls[0]
    assert sent["system"] == agent_input.model_input(case["input"])["system"]
    assert sent["messages"][0] == agent_input.model_input(case["input"])["messages"][0]

    changed = {**case, "input": {**case["input"], "prompt_sha": "old"}}
    drifted = await run_case(changed, ScriptedProvider(lambda **_: ProviderResponse()))
    assert drifted["spec_drift"] == ["prompt"] and drifted["new"] is None
    summary = aggregate([row, drifted])
    assert (summary["cases"], summary["answered"], summary["drifted"]) == (2, 1, 1)


async def test_live_mode_runs_an_unrecorded_tool_on_the_recorded_state(tmp_path, engine):
    path, _ = await _journal(tmp_path, engine, lambda **_: ProviderResponse(output=_answer()))
    [case] = extract_cases(path)
    case = {**case, "tool_calls": []}
    assert STRATEGIST.prefetch  # it prefetches, so the recording is now missing those calls
    row = await run_case(case, ScriptedProvider(lambda **_: ProviderResponse(output=_answer())))
    assert row["unrecorded_tool_calls"] and row["new"] is not None
