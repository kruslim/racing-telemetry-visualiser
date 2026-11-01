"""The post-race debrief (pitwall stage 09 part B): inputs, checks, storage, API, hook.

All offline: the analysis half is driven by a ScriptedProvider (the pitwall's scripted
model), and the no-key path is exercised exactly as it runs without a Kimi key.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("fastapi")

from evals.debrief_cases import (  # noqa: E402
    debrief_cases,
    faithful_answer,
    inputs_of,
    scripted_handler,
)
from evals.synthetic_debrief import (  # noqa: E402
    DEBRIEF_SESSION_ID,
    debrief_source,
    seed_debrief_session,
)

from rtv.debrief.checks import check_section, fact_set, known_corners  # noqa: E402
from rtv.debrief.contract import SECTIONS, DebriefAnswer  # noqa: E402
from rtv.grounding.units import read_figures  # noqa: E402
from rtv.pitwall.provider import ProviderResponse, ScriptedProvider  # noqa: E402
from rtv.store import journal  # noqa: E402

SID = DEBRIEF_SESSION_ID


def _app(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_PITWALL", "true")
    # No key, whatever the developer's environment holds.
    for name in ("KIMI_CODE_API_KEY", "KIMI_API_KEY"):
        monkeypatch.setenv(name, "")
    import rtv.llm

    monkeypatch.setattr(rtv.llm, "_from_env_file", lambda _name: None)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    return TestClient(create_app())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _app(tmp_path, monkeypatch, RTV_DEBRIEF_AUTO="true") as c:
        seed_debrief_session(c.app.state.services.writer)
        yield c
    from rtv.config import get_settings

    get_settings.cache_clear()


def _replay(services) -> dict:
    services.replay.start(debrief_source())
    services.replay.join(timeout=60)
    assert services.on_session_end.wait(timeout=60)
    runs = journal.list_runs(services.settings.journal_dir, SID)
    assert len(runs) == 1
    return runs[0]


def _inputs(services, run_id: str) -> dict:
    from rtv.debrief.inputs import build_inputs

    return build_inputs(
        session_id=SID,
        path=journal.run_path(services.settings.journal_dir, SID, run_id),
        repo=services.repo,
        coaching=services.coaching,
    )


# ---- the no-key path (DB2, DB5) -------------------------------------------------


def test_with_no_key_the_session_end_writes_the_deterministic_debrief(client):
    services = client.app.state.services
    assert not services.debrief.can_analyse()
    run = _replay(services)
    assert run["session_type"] == "race"

    doc = client.get(f"/api/v1/sessions/{SID}/debrief", params={"run": run["run_id"]}).json()
    assert doc["trigger"] == "session_end"
    assert doc["analysis"] == {
        "available": False,
        "reason": "no Kimi key is configured (KIMI_CODE_API_KEY)",
    }
    assert "analysis half is unavailable" in doc["note"]
    assert [s["status"] for s in doc["sections"]] == ["unavailable"] * len(SECTIONS)
    assert doc["model_calls"] == 0 and doc["can_analyse"] is False
    det = doc["deterministic"]
    summary = client.get(f"/api/v1/sessions/{SID}/summary", params={"run": run["run_id"]}).json()
    assert det["result"]["best_time"] == summary["best_time"]
    assert det["result"]["laps"] == summary["laps"]
    # Every lap the deterministic half names was recorded.
    recorded = set(doc["references"]["laps"])
    named = {r["lap"] for r in det["time_lost"]} | {
        lap for r in det["recurring"] for lap in r["laps"]
    }
    assert named and named <= recorded
    assert det["recurring"], "the scripted race repeats its losses at a corner"

    # Stored beside the run, and recorded at the end of the run's journal.
    path = journal.run_path(services.settings.journal_dir, SID, run["run_id"])
    assert journal.debrief_path(path).is_file()
    records = list(journal.iter_records(path))
    assert records[-1]["kind"] == "debrief"
    assert records[-1]["seq"] == records[-2]["seq"] + 1
    assert records[-1]["data"]["analysis"]["available"] is False
    assert journal.summarize_run(path)["counts"]["debrief"] == 1


def test_get_is_404_without_a_debrief_and_post_regenerates_from_the_journal(
    tmp_path, monkeypatch
):
    with _app(tmp_path, monkeypatch, RTV_DEBRIEF_AUTO="false") as c:
        services = c.app.state.services
        seed_debrief_session(services.writer)
        run = _replay(services)
        url = f"/api/v1/sessions/{SID}/debrief"
        assert c.get(url, params={"run": run["run_id"]}).status_code == 404
        assert c.get(url, params={"run": "nope"}).status_code == 404
        first = c.post(url, params={"run": run["run_id"]}).json()
        assert first["trigger"] == "manual"
        again = c.post(url).json()  # default run
        assert again["run_id"] == run["run_id"]
        assert again["deterministic"] == first["deterministic"]
        stored = c.get(url, params={"run": run["run_id"]}).json()
        assert stored["generated_at"] == again["generated_at"]
        path = journal.run_path(services.settings.journal_dir, SID, run["run_id"])
        kinds = [r["kind"] for r in journal.iter_records(path)]
        assert kinds.count("debrief") == 2 and kinds[-2:] == ["debrief", "debrief"]
        # Regenerating never re-ran an agent: nothing but the debrief was added.
        assert "run_start" not in kinds
    from rtv.config import get_settings

    get_settings.cache_clear()


def test_the_auto_debrief_follows_its_settings(tmp_path, monkeypatch):
    # Practice only: the race segment gets no automatic debrief.
    with _app(tmp_path, monkeypatch, RTV_DEBRIEF_AUTO="true", RTV_DEBRIEF_SESSIONS="practice") as c:
        services = c.app.state.services
        seed_debrief_session(services.writer)
        run = _replay(services)
        assert services.debrief.generated == 0
        got = c.get(f"/api/v1/sessions/{SID}/debrief", params={"run": run["run_id"]})
        assert got.status_code == 404
    from rtv.config import get_settings

    get_settings.cache_clear()


def test_auto_defaults_to_the_agent_switch():
    from rtv.config import Settings
    from rtv.debrief.service import DebriefService

    def svc(**kw):
        return DebriefService(Settings(**kw), None, None)

    assert svc(pitwall_agents=True).auto() is True
    assert svc(pitwall_agents=False).auto() is False
    assert svc(pitwall_agents=False, debrief_auto="true").auto() is True
    assert svc(pitwall_agents=True, debrief_auto="false").auto() is False
    assert svc(debrief_sessions="Race, qualify").sessions() == {"race", "qualify"}


def test_the_file_notifier_delivers_the_debrief(tmp_path, monkeypatch):
    with _app(
        tmp_path,
        monkeypatch,
        RTV_DEBRIEF_AUTO="true",
        RTV_PITWALL_SESSION_END_NOTIFIER="file",
    ) as c:
        services = c.app.state.services
        seed_debrief_session(services.writer)
        run = _replay(services)
        notice = services.settings.journal_dir / SID / "runs" / f"{run['run_id']}.notice.txt"
        lines = notice.read_text().splitlines()
        assert len(lines) == 2
        assert lines[1].startswith(f"Debrief of run {run['run_id']} ready: deterministic")
    from rtv.config import get_settings

    get_settings.cache_clear()


# ---- inputs (DB1) ---------------------------------------------------------------


def test_the_inputs_leave_out_snapshots_and_rejected_numbers(client):
    services = client.app.state.services
    run = _replay(services)
    inputs = _inputs(services, run["run_id"])
    text = json.dumps(inputs)
    assert '"kind": "state"' not in text and "_records" not in inputs
    assert inputs["recorded_laps"] == [lap["lap"] for lap in inputs["laps"] if lap["lap_time_s"]]
    assert inputs["findings"]["reference_lap"] in inputs["recorded_laps"]
    assert inputs["fuel_by_lap"] and inputs["final_state"]["fuel"]
    assert all(e["type"] != "plan_revised" for e in inputs["events"])
    # A rejected proposal's reason never becomes a fact.
    inputs["plan"]["rejected"] = [
        {"lap": 2, "agent": "strategist", "changed": ["fuel_to_add_l"], "reason": "41.7 L"}
    ]
    facts = fact_set(inputs)
    assert not facts.supports_figure(read_figures("41.7 L")[0])


# ---- the analysis half (DB2, DB5) -----------------------------------------------


@pytest.fixture()
def recorded(client):
    services = client.app.state.services
    run = _replay(services)
    return services, run["run_id"], _inputs(services, run["run_id"])


def test_every_figure_of_the_faithful_answer_traces_to_the_inputs(recorded):
    services, _run, inputs = recorded
    answer = faithful_answer(inputs)
    facts, corners = fact_set(inputs), known_corners(inputs)
    for name in SECTIONS[:-1]:
        rep = check_section(name, answer, facts=facts, inputs=inputs, corners=corners)
        assert rep.ok, (name, rep.issues)
    assert answer.time_lost and answer.recurring and answer.priorities


def _cases(inputs):
    return {case.name: case for case in debrief_cases(inputs)}


CASE_NAMES = [
    "faithful",
    "invented_figure",
    "invented_figure_repaired",
    "unrecorded_lap",
    "swapped_fact",
    "hallucinated_corner",
    "wrong_corner_name",
    "unsupported_priority",
    "priority_on_a_refused_section",
]


@pytest.mark.parametrize("name", CASE_NAMES)
def test_hermetic_debrief_cases(recorded, name):
    services, run_id, inputs = recorded
    case = _cases(inputs)[name]
    provider = ScriptedProvider(scripted_handler(case))
    services.debrief.provider = provider
    doc = services.debrief.generate(SID, run_id)
    status = {s["name"]: s["status"] for s in doc["sections"]}
    for section, want in case.expect.items():
        assert status[section] == want, (section, status, doc["sections"])
    assert doc["analysis"]["available"] is True
    refused = [s for s in doc["sections"] if s["status"] == "refused"]
    for s in refused:
        assert s["reason"].startswith("Refused:") and s["issues"]
        assert "items" not in s and "text" not in s  # a refused section shows no claims
    # One call, plus the one repair only when a section failed.
    assert doc["model_calls"] == len(provider.calls) == (2 if refused or name.endswith(
        "repaired") else 1)
    first = provider.calls[0]
    assert first["agent"] == "debrief" and first["output_model"] == "DebriefAnswer"
    assert first["tools"] == []  # no tool loop: everything is in the inputs
    assert inputs_of(first["messages"][0]["content"]) == json.loads(json.dumps(inputs))


def test_a_failed_call_still_produces_the_deterministic_debrief(recorded):
    services, run_id, _inputs_ = recorded

    def boom(**_kw):
        raise RuntimeError("HTTP 429")

    services.debrief.provider = ScriptedProvider(boom)
    doc = services.debrief.generate(SID, run_id)
    assert doc["analysis"]["available"] is False
    assert "HTTP 429" in doc["analysis"]["reason"]
    assert doc["deterministic"]["result"]["laps"]
    assert {s["status"] for s in doc["sections"]} == {"unavailable"}


def test_an_answer_that_never_validates_is_unavailable_not_invented(recorded):
    services, run_id, _ = recorded
    services.debrief.provider = ScriptedProvider(lambda **_kw: ProviderResponse(output=None))
    doc = services.debrief.generate(SID, run_id)
    assert doc["analysis"]["available"] is False
    assert "no valid answer" in doc["analysis"]["reason"]


def test_the_session_end_runs_the_scripted_analysis_with_autonomy_on(tmp_path, monkeypatch):
    """DB5: a synthetic race with autonomy on ends, and the debrief is produced."""
    with _app(
        tmp_path,
        monkeypatch,
        RTV_DEBRIEF_AUTO="true",
        RTV_PITWALL_AUTONOMOUS="true",
    ) as c:
        services = c.app.state.services
        seed_debrief_session(services.writer)
        from rtv.pitwall.orchestrator import PitwallOrchestrator
        from rtv.pitwall.radio import RadioFeed

        orch = PitwallOrchestrator(
            services.engine,
            None,
            [],  # no live agents needed: the debrief is what is under test
            feed=RadioFeed(),
            autonomous=True,
            callers=[],
        )
        orch.attach_activity(services.activity)
        services.pitwall = orch
        services.segments.pitwall = orch
        import asyncio

        services.engine.bus.subscribe_callback(lambda ev: asyncio.run(orch.handle_event(ev)))

        def handler(*, messages, **_kw):
            return faithful_answer(inputs_of(messages[0]["content"]))

        services.debrief.provider = ScriptedProvider(handler)
        run = _replay(services)
        autonomy = [
            r for r in journal.iter_records(
                journal.run_path(services.settings.journal_dir, SID, run["run_id"])
            )
            if r["kind"] == "autonomy"
        ]
        armed = [a["data"]["armed"] for a in autonomy]
        assert True in armed and armed[-1] is False  # armed at green, stood down at the end
        doc = c.get(f"/api/v1/sessions/{SID}/debrief").json()
        assert doc["trigger"] == "session_end" and doc["analysis"]["available"]
        assert {s["status"] for s in doc["sections"]} == {"ok"}
        assert doc["kept_sections"] == len(SECTIONS)
    from rtv.config import get_settings

    get_settings.cache_clear()


# ---- the Kimi call itself (DB2), with a fake client -----------------------------


class _Block(SimpleNamespace):
    pass


class _FakeMessages:
    def __init__(self, payload):
        self.payload = payload
        self.kwargs: list[dict] = []

    async def create(self, **kwargs):
        self.kwargs.append(kwargs)
        block = _Block(type="tool_use", id="t1", name="rtv_final_answer", input=self.payload)
        return SimpleNamespace(content=[block], usage=None)

    def parse(self, **_kw):  # pragma: no cover - must never be used
        raise AssertionError("messages.parse is not allowed")


def test_the_kimi_call_thinks_and_answers_through_the_forced_respond_tool(
    recorded, monkeypatch
):
    services, run_id, inputs = recorded
    fake = _FakeMessages(faithful_answer(inputs).model_dump())
    import rtv.llm

    monkeypatch.setattr(rtv.llm, "is_configured", lambda _s=None: True)
    monkeypatch.setattr(rtv.llm, "build_client", lambda *_a, **_k: SimpleNamespace(messages=fake))
    monkeypatch.setattr(services.settings, "debrief_model", "kimi-debrief-test")
    services.debrief.provider = None
    services.debrief._kimi = None
    doc = services.debrief.generate(SID, run_id)
    assert doc["analysis"]["available"] and doc["model"] == "kimi-debrief-test"
    call = fake.kwargs[0]
    assert call["model"] == "kimi-debrief-test"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["tool_choice"] == {"type": "any"}
    assert [t["name"] for t in call["tools"]] == ["rtv_final_answer"]
    assert set(DebriefAnswer.model_fields) <= set(call["tools"][0]["input_schema"]["properties"])


def test_the_model_falls_back_to_the_reasoning_default():
    from rtv.config import Settings
    from rtv.debrief.service import DebriefService

    s = Settings(llm_kimi_model_reasoning="kimi-reasoning-x")
    assert DebriefService(s, None, None).model() == "kimi-reasoning-x"
    s = Settings(llm_kimi_model_reasoning="kimi-reasoning-x", debrief_model="kimi-y")
    assert DebriefService(s, None, None).model() == "kimi-y"


def test_the_digest_counts_layer1_labels_and_skips_off_map_corners():
    """Layer 1 labels every pair from the session map (R3); the digest no longer relabels."""
    from types import SimpleNamespace

    from rtv.debrief.inputs import findings_digest

    def corner(label, name, dt, *, off_map=False):
        return {"label": label, "name": name, "distance": 100.0, "net_dt": dt, "diags": [],
                "off_map": off_map}

    by_lap = {
        2: [corner("T1", "La Source", 0.2), corner("T2", "Les Combes", 0.3)],
        3: [corner("T1", "La Source", 0.25), corner("T9", "Raidillon", 0.4, off_map=True),
            corner("T2", "Les Combes", 0.1)],
    }

    class Coaching:
        def lap_findings(self, _sid, main, _ref):
            return SimpleNamespace(to_dict=lambda: {
                "corners": by_lap[main], "chief": {}, "notes": [], "main_time": 141.0,
                "corner_map_lap": 3, "lap_length_m": 1000.0,
            })

    laps = [{"lap": n, "lap_time": t, "is_valid": True} for n, t in ((1, 140.0), (2, 141.0),
                                                                    (3, 141.5))]
    live = [{"label": "T1", "name": "La Source", "lap_dist_pct": 0.1},
            {"label": "T2", "name": "Kemmel", "lap_dist_pct": 0.3}]
    d = findings_digest(Coaching(), "s", laps, live)
    rows = {r["corner"]: r for r in d["recurring"]}
    assert set(rows) == {"T1", "T2"} and rows["T1"]["laps"] == [2, 3]
    assert "Corner labels follow the session corner map (built on lap 3)." in d["notes"]
    assert any("1 lap-pair corner(s)" in n for n in d["notes"])
    assert any("calls T2 Kemmel" in n and "Les Combes" in n for n in d["notes"])
