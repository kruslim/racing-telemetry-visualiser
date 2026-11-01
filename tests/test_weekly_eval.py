"""scripts/weekly_eval.py: the dry run, the diff with last week and the 429 stop, offline."""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JOURNALS = Path(__file__).parent / "fixtures" / "journals"
RUN2 = JOURNALS / "spa-run2-excerpt.jsonl"
RUN3 = JOURNALS / "spa-run3-excerpt.jsonl"


def _load():
    path = ROOT / "scripts" / "weekly_eval.py"
    spec = importlib.util.spec_from_file_location("weekly_eval", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["weekly_eval"] = module
    spec.loader.exec_module(module)
    return module


we = _load()


@pytest.fixture
def no_models(monkeypatch):
    """Any model client, judge or server start in a dry run fails the test."""

    def boom(*_a, **_k):
        raise AssertionError("the dry run touched a model or the server")

    import evals.judge_provider

    import rtv.pitwall.provider

    monkeypatch.setattr(rtv.pitwall.provider.KimiProvider, "__init__", boom)
    monkeypatch.setattr(evals.judge_provider, "judge_backend", boom)
    monkeypatch.setattr(we.Server, "start", boom)


def _fingerprint(prompt: str = "a", model: str = "kimi-k3"):
    return lambda: {
        "agents": {"coach": {"model": model, "prompt_sha": prompt, "tools_sha": "t",
                             "output_schema_sha": "o"}},
        "coach_model": "kimi-k3",
    }


def test_dry_run_writes_summary_and_report_without_models(tmp_path, no_models, capsys):
    rc = we.main(["--dry-run", "--out-root", str(tmp_path), "--date", "2026-11-02",
                  "--replay-journal", str(RUN3)])
    assert rc == 0
    folder = tmp_path / "2026-11-02-dry-run"
    summary = json.loads((folder / "summary.json").read_text())
    assert summary["dry_run"] is True and summary["quota"] is None
    assert summary["steps"]["replay"]["status"] == "recorded"
    assert summary["steps"]["golden"]["status"] == "planned"
    assert summary["steps"]["real_offline"]["status"] == "done"
    assert summary["metrics"]["replay.refusals"] == 1
    assert summary["metrics"]["real_offline.cases"] == summary["metrics"]["real.cases"]
    assert set(summary["fingerprint"]["agents"]) == {
        "strategist", "vehicle_engineer", "spotter", "coach"
    }
    report = (folder / "REPORT.md").read_text()
    assert report.index("## Headline changes") < report.index("### Summary")
    assert "First result" in report
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert out.startswith("Weekly eval 2026-11-02 done.") and len(out.split()) < 80


def test_second_run_diffs_against_the_first(tmp_path, no_models, capsys):
    we.main(["--dry-run", "--out-root", str(tmp_path), "--date", "2026-11-02",
             "--replay-journal", str(RUN2)], fingerprint=_fingerprint("a"))
    we.main(["--dry-run", "--out-root", str(tmp_path), "--date", "2026-11-09",
             "--replay-journal", str(RUN3)], fingerprint=_fingerprint("b", "kimi-k4"))
    summary = json.loads((tmp_path / "2026-11-09-dry-run" / "summary.json").read_text())
    assert summary["previous"] == "2026-11-02-dry-run"
    report = (tmp_path / "2026-11-09-dry-run" / "REPORT.md").read_text()
    assert "dry-run placeholder" in report
    assert "DRIFT: coach: model, prompt changed (kimi-k3 -> kimi-k4)" in report
    # Both runs side by side in the compare_runs table.
    assert "| 2026-11-02-dry-run | 2026-11-09 |" in report
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert "drift: coach" in last and len(last.split()) < 80


def test_find_previous_prefers_live_runs_and_ignores_newer(tmp_path):
    for name, dry in (("2026-11-02", False), ("2026-11-05-dry-run", True),
                      ("2026-11-16", False)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "summary.json").write_text(json.dumps({"dry_run": dry}))
    folder, _ = we.find_previous(tmp_path, "2026-11-09")
    assert folder.name == "2026-11-02"
    only_dry = tmp_path / "other"
    (only_dry / "2026-11-01-dry-run").mkdir(parents=True)
    (only_dry / "2026-11-01-dry-run" / "summary.json").write_text('{"dry_run": true}')
    assert we.find_previous(only_dry, "2026-11-09")[0].name == "2026-11-01-dry-run"
    assert we.find_previous(tmp_path / "missing", "2026-11-09") is None


def test_run_folder_never_overwrites(tmp_path):
    (tmp_path / "2026-11-09").mkdir()
    assert we.run_folder(tmp_path, "2026-11-09", False).name == "2026-11-09-2"
    assert we.run_folder(tmp_path, "2026-11-09", True).name == "2026-11-09-dry-run"


def test_headlines_put_regressions_first_and_skip_noise():
    old = {"replay.refusals": 1, "replay.radio_late": 14, "replay.median_call_ms": 9000,
           "coach.judge_gemini.grounding": 4.0, "replay.tokens_out": 15000}
    new = {"replay.refusals": 3, "replay.radio_late": 7, "replay.median_call_ms": 9500,
           "coach.judge_gemini.grounding": 4.6, "replay.tokens_out": 30000}
    lines = we.headline_changes(old, new)
    assert lines[0] == "WORSE: replay refusals 1 -> 3"
    assert "WORSE: replay tokens out 15,000 -> 30,000" in lines
    assert "better: late radio calls 14 -> 7" in lines
    assert "better: coach judge grounding 4 -> 4.6" in lines
    assert not any("median call" in line for line in lines)  # 5.6 %: noise


def test_fingerprint_is_free_and_stable():
    a, b = we.spec_fingerprint(), we.spec_fingerprint()
    assert a == b and we.fingerprint_drift(a, b) == []
    changed = json.loads(json.dumps(a))
    changed["agents"]["spotter"]["tools_sha"] = "x"
    assert we.fingerprint_drift(a, changed) == ["spotter: tools changed"]


def test_quota_watch_catches_a_logged_429():
    watch = we.QuotaWatch()
    log = logging.getLogger("rtv")
    log.addHandler(watch)
    try:
        logging.getLogger("rtv.pitwall.framework").warning("tool slow")
        assert watch.hit is None
        try:
            raise RuntimeError("Error code: 429 - You've reached your 5-hour usage limit")
        except RuntimeError:
            logging.getLogger("rtv.pitwall.framework").exception("Agent coach failed on x")
    finally:
        log.removeHandler(watch)
    assert watch.hit and "429" in watch.hit


def test_journal_quota_hit(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(json.dumps({"kind": "llm_call_end", "data": {"ok": True}}) + "\n")
    assert we.journal_quota_hit(path) is None
    with path.open("a") as fh:
        fh.write(json.dumps({"kind": "llm_call_end",
                             "data": {"ok": False, "error": "RateLimitError"}}) + "\n")
    assert we.journal_quota_hit(path) == "RateLimitError"


class _QuotaProvider:
    calls = 0

    async def complete(self, **_kwargs):
        type(self).calls += 1
        raise RuntimeError("Error code: 429 - usage limit")


def test_golden_step_stops_cleanly_on_a_429(monkeypatch):
    import asyncio

    import rtv.pitwall.provider

    monkeypatch.setattr(rtv.pitwall.provider, "KimiProvider", _QuotaProvider)
    watch = we.QuotaWatch()
    logging.getLogger("rtv").addHandler(watch)
    try:
        result = asyncio.run(we.step_golden_live(watch))
    finally:
        logging.getLogger("rtv").removeHandler(watch)
    assert result["status"] == "quota"
    assert result["metrics"]["planned"] > 20
    assert result["rows"] == []
    # One case's call (plus its single transport retry), then nothing else.
    assert _QuotaProvider.calls <= 2
