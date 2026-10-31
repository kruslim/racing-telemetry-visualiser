"""Run statistics and the run comparison, on two trimmed real Spa journal excerpts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from rtv.store.run_stats import compare_markdown, load_run_stats, percentile, run_stats

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "journals"
RUN2 = FIXTURES / "spa-run2-excerpt.jsonl"
RUN3 = FIXTURES / "spa-run3-excerpt.jsonl"


def test_counts_runs_calls_and_outcomes():
    s = load_run_stats(RUN3)
    assert s["run"] == "spa-run3-excerpt"
    assert s["agents_enabled"] is True
    assert s["model_calls"] == 7
    assert s["agent_runs"] == 4
    assert (s["radio_runs"], s["refusals"], s["errored_runs"]) == (2, 1, 1)
    assert s["calls_without_answer"] == 2
    # The engineer's unanswered call asked for a tool; only the spotter's was malformed.
    assert s["malformed_respond"] == 1
    assert s["agents"]["vehicle_engineer"]["refusals"] == 1
    assert s["agents"]["spotter"]["errored_runs"] == 1


def test_validator_failures_split_first_pass_and_after_repair():
    s = load_run_stats(RUN3)
    assert (s["validator_failures"], s["validator_checks"]) == (3, 5)
    assert (s["first_pass_failures"], s["first_pass_checks"]) == (2, 3)
    assert (s["failures_after_repair"], s["repairs"]) == (1, 2)
    assert s["over_word_limit"] == 1
    assert s["untraceable_numbers"] == 3  # 29, 206, then 42 after repair


def test_tokens_latency_and_triggers():
    s = load_run_stats(RUN2)
    assert s["tokens"] == {"input": 9263, "cache_read": 9472, "output": 2728}
    calls = s["agents"]["vehicle_engineer"]["call_latency"]
    assert calls["median_ms"] is not None and calls["p90_ms"] >= calls["median_ms"]
    assert s["runs_by_trigger"] == {
        "car_health_warning": 1,
        "flag_change": 1,
        "stint_lap_milestone": 1,
        "tyre_out_of_band": 1,
    }
    assert sum(s["calls_by_trigger"].values()) == s["model_calls"]


def test_missing_fields_are_unknown_not_zero():
    # An old journal: no token counts, no answered flag, no repair flag, no late radio.
    s = run_stats(
        [
            {"kind": "run_start", "agent": "coach", "run_id": "coach-1", "data": {"trigger": "x"}},
            {
                "kind": "llm_call_end",
                "agent": "coach",
                "run_id": "coach-1",
                "data": {"ok": True, "latency_ms": 1000.0},
            },
            {"kind": "validate", "agent": "coach", "run_id": "coach-1", "data": {"ok": True}},
            {
                "kind": "run_end",
                "agent": "coach",
                "run_id": "coach-1",
                "data": {"outcome": "radio", "latency_ms": 1200.0},
            },
        ]
    )
    assert s["tokens"] == {"input": None, "cache_read": None, "output": None}
    assert s["calls_without_answer"] is None
    assert s["first_pass_failures"] is None
    assert s["radio_late"] is None and s["radio_expired"] is None
    md = compare_markdown([s], ["old"])
    assert "| tokens in | n/a |" in md
    assert "| radio late | n/a |" in md


def test_late_and_expired_radio_are_counted_when_recorded():
    s = run_stats(
        [
            {"kind": "radio_late", "agent": "strategist", "data": {}},
            {"kind": "radio_late", "agent": "strategist", "data": {}},
            {"kind": "radio_expired", "agent": "spotter", "data": {}},
        ]
    )
    assert (s["radio_late"], s["radio_expired"]) == (2, 1)


def test_torn_and_unknown_lines_are_ignored(tmp_path):
    path = tmp_path / "torn.jsonl"
    lines = RUN3.read_text(encoding="utf-8").splitlines()
    lines.insert(3, json.dumps({"kind": "something_new", "data": {}}))
    path.write_text("\n".join(lines) + '\n{"kind": "run_e', encoding="utf-8")
    assert load_run_stats(path)["model_calls"] == 7


def test_percentile():
    assert percentile([], 0.9) is None
    assert percentile([5.0], 0.9) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert abs(percentile([10.0, 20.0], 0.9) - 19.0) < 1e-9


def test_markdown_puts_runs_side_by_side():
    md = compare_markdown([load_run_stats(RUN2), load_run_stats(RUN3)], ["run 2", "run 3"])
    assert "|  | run 2 | run 3 |" in md
    assert "| model calls | 6 | 7 |" in md
    assert "| validator failures / checks | 2 / 4 | 3 / 5 |" in md
    assert "| spotter · refusals | 1 | 0 |" in md
    assert "| offtrack | 0 / 0 | 1 / 2 |" in md


def test_script_prints_markdown_and_json(tmp_path):
    script = ROOT / "scripts" / "compare_runs.py"
    out = tmp_path / "table.md"
    md = subprocess.run(
        [sys.executable, str(script), str(RUN2), str(RUN3), "--out", str(out)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "## Per agent" in md and out.read_text(encoding="utf-8") == md
    raw = subprocess.run(
        [sys.executable, str(script), str(RUN2), str(RUN3), "--json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    data = json.loads(raw)
    assert [d["model_calls"] for d in data] == [6, 7]
