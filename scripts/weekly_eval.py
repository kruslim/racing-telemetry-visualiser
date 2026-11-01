"""Weekly live eval: run every live eval on the current checkout and diff it with last week.

Four steps, in this order:

1. ``replay``  one Spa replay at 2x with the pitwall agents on (``RTV_PITWALL_AGENTS=true``),
   in a server of its own on a throwaway data dir; the journal is summarised with
   :func:`rtv.store.run_stats.run_stats` (what ``scripts/compare_runs.py`` prints).
2. ``real``    the real-journal cases (``evals/real/spa_run``) re-executed live with the
   Gemini judge, as ``evals/run_pitwall.py --real spa_run --judge-provider gemini`` does,
   plus the free offline re-score against ``baseline.json``.
3. ``golden``  the generated pitwall golden sets, as ``evals/run_pitwall.py`` does.
4. ``coach``   the coach eval (``evals/run.py``) on five Spa laps with the Gemini judge.

Every result lands in ``<out-root>/<date>/`` (``summary.json``, ``REPORT.md``, the per-step
JSON and the replay journal). ``REPORT.md`` diffs the run with the newest earlier result
in ``<out-root>``: headline changes first (refusals, validator failures, late calls,
latency, tokens, judge scores, model-id or prompt drift), then the replay tables from
``compare_markdown`` and every metric side by side.

Quota: a Kimi 429 ("5-hour usage limit") stops the run cleanly. The step it hit keeps
what it finished, later model steps are skipped, and the report says so.

    python scripts/weekly_eval.py --dry-run          # no model calls: plan + free checks
    python scripts/weekly_eval.py                    # the live run (Kimi + Gemini quota)
    python scripts/weekly_eval.py --steps real,golden --out-root /tmp/weekly

``--dry-run`` calls no model and starts no server. It still records the free parts (the
offline real-case scores, the agents' model ids and prompt hashes, the golden-set size and,
with ``--replay-journal``, an existing journal's statistics) so a dry run can stand in as
the baseline for the first live run. Docs: ``docs/WEEKLY_EVAL.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from rtv.store.run_stats import compare_markdown, run_stats  # noqa: E402

STEPS = ("replay", "real", "golden", "coach")
DEFAULT_OUT_ROOT = Path("/mnt/project-files/weekly-evals")
IBT_NAME = "audir8lmsevo2gt3_spa_race.ibt"
IBT_CANDIDATES = (
    ROOT / "tests" / "fixtures" / "real" / IBT_NAME,
    Path("/mnt/project-files/fixtures/gt3-audi-r8-spa") / IBT_NAME,
)
#: The F6 coach cases (project-review/f6-judge-run/spa_cases.json); session ids change on
#: every import, so the laps are kept and the id is filled in after the import.
COACH_LAPS = ((2, 6), (3, 6), (8, 6), (14, 6), (16, 17))
REAL_SET = "spa_run"
JUDGE = "gemini"

QUOTA_RE = re.compile(r"\b429\b|RateLimitError|rate_limit_error|usage limit", re.IGNORECASE)


# --------------------------------------------------------------------------
# quota
# --------------------------------------------------------------------------
class QuotaWatch(logging.Handler):
    """Notices the first Kimi 429 in the ``rtv`` log (agent errors are logged, not raised)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.hit: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            text += f" {type(exc).__name__}: {exc}"
        self.note(text)

    def note(self, text: str) -> bool:
        """Record ``text`` as the quota hit if it looks like one; True when it does."""
        if self.hit is None and QUOTA_RE.search(text or ""):
            self.hit = " ".join(text.split())[:300]
        return self.hit is not None

    def check_exception(self, exc: BaseException) -> bool:
        return self.note(f"{type(exc).__name__}: {exc}")


def journal_quota_hit(path: Path) -> str | None:
    """The first 429-looking error in a journal, or ``None``."""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if QUOTA_RE.search(line):
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        return line.strip()[:300]
                    data = rec.get("data") or {}
                    return str(data.get("error") or rec.get("kind") or "429")[:300]
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------
# what the agents are today: model ids and prompt hashes
# --------------------------------------------------------------------------
def spec_fingerprint() -> dict[str, Any]:
    """Model id and prompt/tools/contract hashes per pitwall agent, plus the coach model.

    Free: builds each runtime without calling it. Two weeks with different values mean
    the prompt, tools, output contract or model id changed (``spec_drift`` per agent).
    """
    from rtv import llm
    from rtv.pitwall import agent_input
    from rtv.pitwall.agents import AGENT_REGISTRY
    from rtv.pitwall.framework import AgentRuntime, agent_model

    agents: dict[str, Any] = {}
    for spec in AGENT_REGISTRY:
        spec = replace(spec, model=agent_model(spec))
        runtime = AgentRuntime(spec, provider=None)
        agents[spec.name] = {
            "model": spec.model,
            "prompt_sha": agent_input.sha(runtime.system_prompt),
            "tools_sha": agent_input.tools_sha(spec.tools),
            "output_schema_sha": agent_input.output_schema_sha(spec.output_model),
        }
    return {"agents": agents, "coach_model": llm.model("reasoning")}


def fingerprint_drift(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[str]:
    """Readable lines for every agent whose model or prompt changed between two runs."""
    if not old or not new:
        return []
    labels = {
        "model": "model",
        "prompt_sha": "prompt",
        "tools_sha": "tools",
        "output_schema_sha": "output_contract",
    }
    lines = []
    before, after = old.get("agents") or {}, new.get("agents") or {}
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name), after.get(name)
        if a is None or b is None:
            lines.append(f"{name}: agent {'added' if a is None else 'removed'}")
            continue
        changed = [label for key, label in labels.items() if a.get(key) != b.get(key)]
        if changed:
            extra = f" ({a.get('model')} -> {b.get('model')})" if "model" in changed else ""
            lines.append(f"{name}: {', '.join(changed)} changed{extra}")
    if old.get("coach_model") != new.get("coach_model"):
        lines.append(f"coach model: {old.get('coach_model')} -> {new.get('coach_model')}")
    return lines


def git_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
            text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


# --------------------------------------------------------------------------
# metrics: one flat dict per run, so two weeks diff key by key
# --------------------------------------------------------------------------
def replay_metrics(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_calls": stats.get("model_calls"),
        "agent_runs": stats.get("agent_runs"),
        "refusals": stats.get("refusals"),
        "errored_runs": stats.get("errored_runs"),
        "validator_failures": stats.get("validator_failures"),
        "validator_checks": stats.get("validator_checks"),
        "radio_aired": stats.get("radio_aired"),
        "radio_late": stats.get("radio_late"),
        "radio_expired": stats.get("radio_expired"),
        "median_call_ms": (stats.get("call_latency") or {}).get("median_ms"),
        "p90_call_ms": (stats.get("call_latency") or {}).get("p90_ms"),
        "median_run_ms": (stats.get("run_latency") or {}).get("median_ms"),
        "tokens_in": (stats.get("tokens") or {}).get("input"),
        "tokens_out": (stats.get("tokens") or {}).get("output"),
    }


def flat_metrics(steps: dict[str, Any]) -> dict[str, Any]:
    """``section.key -> value`` for every number a step reported."""
    out: dict[str, Any] = {}
    for section, result in steps.items():
        for key, value in (result.get("metrics") or {}).items():
            if isinstance(value, dict):
                for sub, v in value.items():
                    out[f"{section}.{key}.{sub}"] = v
            else:
                out[f"{section}.{key}"] = value
    return out


#: (metric key, label, which way is better). Shown first in the report when they move.
HEADLINES: tuple[tuple[str, str, str], ...] = (
    ("replay.refusals", "replay refusals", "lower"),
    ("real.new_refusals", "real-case refusals", "lower"),
    ("golden.refusals", "golden-set refusals", "lower"),
    ("replay.validator_failures", "replay validator failures", "lower"),
    ("real.new_validator_failures", "real-case validator failures (live)", "lower"),
    ("real_offline.validator_failures", "real-case validator failures (recorded)", "lower"),
    ("replay.radio_late", "late radio calls", "lower"),
    ("replay.radio_expired", "expired radio calls", "lower"),
    ("replay.errored_runs", "replay errored runs", "lower"),
    ("golden.errors", "golden-set errored runs", "lower"),
    ("replay.median_call_ms", "replay median call (ms)", "lower"),
    ("replay.p90_call_ms", "replay p90 call (ms)", "lower"),
    ("golden.avg_latency_ms", "golden-set mean call (ms)", "lower"),
    ("replay.tokens_in", "replay tokens in", "lower"),
    ("replay.tokens_out", "replay tokens out", "lower"),
    ("golden.tokens_in", "golden-set tokens in", "lower"),
    ("golden.tokens_out", "golden-set tokens out", "lower"),
    ("golden.mean_score", "golden-set mean score", "higher"),
    ("real.new_mean_score", "real-case mean score (live)", "higher"),
    ("real_offline.mean_check_score", "real-case mean score (recorded)", "higher"),
    ("real.judge_gemini.usefulness", "real-case judge usefulness", "higher"),
    ("real.judge_gemini.grounding", "real-case judge grounding", "higher"),
    ("coach.outcome_accuracy", "coach outcome accuracy", "higher"),
    ("coach.mean_claim_factuality", "coach claim factuality", "higher"),
    ("coach.total_hallucinations", "coach hallucinations", "lower"),
    ("coach.judge_gemini.clarity", "coach judge clarity", "higher"),
    ("coach.judge_gemini.actionability", "coach judge actionability", "higher"),
    ("coach.judge_gemini.grounding", "coach judge grounding", "higher"),
)

#: Relative change below which a latency or token move is noise, not a headline.
NOISE = {"_ms": 0.15, "tokens_": 0.10}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _noise(key: str, old: float, new: float) -> bool:
    for marker, share in NOISE.items():
        if marker in key and old:
            return abs(new - old) / abs(old) < share
    return False


def fmt_value(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3g}" if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


def headline_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """One line per headline metric that moved, worse ones first."""
    worse, better = [], []
    for key, label, good in HEADLINES:
        a, b = _num(old.get(key)), _num(new.get(key))
        if a is None or b is None or a == b or _noise(key, a, b):
            continue
        improved = (b < a) == (good == "lower")
        line = f"{'better' if improved else 'WORSE'}: {label} {fmt_value(a)} -> {fmt_value(b)}"
        (better if improved else worse).append(line)
    return worse + better


# --------------------------------------------------------------------------
# finding last week's result
# --------------------------------------------------------------------------
def find_previous(out_root: Path, before: str) -> tuple[Path, dict[str, Any]] | None:
    """The newest result in ``out_root`` older than ``before``; live runs beat dry runs.

    A dry run only stands in when no live run exists yet (the first scheduled run diffs
    against the dry-run placeholder).
    """
    found: list[tuple[str, bool, Path, dict[str, Any]]] = []
    if not out_root.is_dir():
        return None
    for folder in out_root.iterdir():
        summary = folder / "summary.json"
        if not folder.is_dir() or folder.name >= before or not summary.is_file():
            continue
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except ValueError:
            continue
        found.append((folder.name, not data.get("dry_run"), folder, data))
    if not found:
        return None
    live = [f for f in found if f[1]]
    name, _, folder, data = max(live or found, key=lambda f: f[0])
    return folder, data


def run_folder(out_root: Path, date: str, dry_run: bool) -> Path:
    base = f"{date}-dry-run" if dry_run else date
    folder, n = out_root / base, 2
    while folder.exists():
        folder, n = out_root / f"{base}-{n}", n + 1
    return folder


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------
def _usage_delta() -> dict[str, Any]:
    from rtv import llm

    total = llm.USAGE.summary()["total"]
    llm.USAGE.reset()
    return total


def step_real_offline() -> dict[str, Any]:
    """Free: re-score the recorded real cases with today's checks, diff with baseline.json."""
    from evals.real import offline

    results = offline.score_set(REAL_SET)
    new = offline.to_baseline(results)
    changes = offline.diff(offline.read_baseline(REAL_SET), new)
    return {
        "status": "done",
        "mode": "offline",
        "metrics": new["summary"],
        "baseline_changes": changes[:50],
        "baseline_changed": len(changes),
        "note": f"recorded answers re-scored with today's checks; {len(changes)} field(s) "
        "differ from baseline.json",
    }


def step_real_dry() -> dict[str, Any]:
    from evals.real.cases import load_case_set

    cases = load_case_set(REAL_SET)
    agents = sorted({c["agent"] for c in cases})
    return {
        "status": "planned",
        "note": f"{len(cases)} cases ({', '.join(agents)}) re-executed live with the "
        f"{JUDGE} judge",
        "metrics": {"cases": len(cases)},
    }


async def step_real_live(watch: QuotaWatch) -> dict[str, Any]:
    from evals.judge_provider import judge_backend
    from evals.real.cases import load_case_set
    from evals.real.live import aggregate, run_case

    from rtv.pitwall.provider import KimiProvider
    from rtv.racestate.strategy_math import DEFAULT_PIT_LANE_LOSS_S

    _usage_delta()
    judges = [judge_backend(JUDGE)]
    provider = KimiProvider()
    tool_config = {"pit_lane_loss_s": DEFAULT_PIT_LANE_LOSS_S, "standings_window": 3}
    cases = load_case_set(REAL_SET)
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    for case in cases:
        if watch.hit:
            break
        try:
            rows.append(await run_case(case, provider, tool_config=tool_config, judges=judges))
        except Exception as exc:  # a judge or transport failure on one case
            if watch.check_exception(exc):
                break
            rows.append(_failed_real_row(case, exc))
    summary = aggregate(rows) if rows else {"cases": 0}
    metrics = {k.replace("judge ", "judge_"): v for k, v in summary.items()}
    drift: dict[str, int] = {}
    for row in rows:
        for item in row.get("spec_drift") or []:
            drift[item] = drift.get(item, 0) + 1
    return {
        "status": "quota" if watch.hit else "done",
        "mode": "live",
        "cases_total": len(cases),
        "metrics": metrics,
        "spec_drift_vs_recording": drift,
        "seconds": round(time.monotonic() - started, 1),
        "usage": _usage_delta(),
        "rows": rows,
    }


def _failed_real_row(case: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "case_id": case["case_id"], "agent": case["agent"], "spec_drift": [],
        "recorded_score": 0.0, "recorded_validator_ok": True, "unrecorded_tool_calls": [],
        "new": None, "error": f"{type(exc).__name__}: {exc}"[:300],
    }


def step_golden_dry() -> dict[str, Any]:
    from evals.pitwall_cases import build_all_cases

    sets = build_all_cases()
    return {
        "status": "planned",
        "note": "generated golden sets, run live: "
        + ", ".join(f"{a} {len(c)}" for a, c in sets.items()),
        "metrics": {"cases": sum(len(c) for c in sets.values())},
    }


async def step_golden_live(watch: QuotaWatch) -> dict[str, Any]:
    from evals.pitwall_cases import build_all_cases
    from evals.pitwall_checks import aggregate, check_for_agent
    from evals.run_pitwall import SPECS, _kwargs_for

    from rtv.pitwall.framework import AgentRuntime, agent_model
    from rtv.pitwall.provider import KimiProvider
    from rtv.racestate.strategy_math import DEFAULT_PIT_LANE_LOSS_S

    _usage_delta()
    provider = KimiProvider()
    rows: list[dict[str, Any]] = []
    per_agent: dict[str, Any] = {}
    totals = {"refusals": 0, "errors": 0, "llm_calls": 0, "tokens_in": 0, "tokens_out": 0}
    latency_sum = 0.0
    started = time.monotonic()
    planned = 0
    for agent, cases in build_all_cases().items():
        planned += len(cases)
        if watch.hit:
            continue
        spec = replace(SPECS[agent], model=agent_model(SPECS[agent]))
        runtime = AgentRuntime(
            spec, provider,
            tool_config={"pit_lane_loss_s": DEFAULT_PIT_LANE_LOSS_S, "standings_window": 3},
        )
        mine: list[dict[str, Any]] = []
        for case in cases:
            if watch.hit:
                break
            message = await runtime.invoke(
                case.event, case.state, events=case.events, injected=case.injected
            )
            if message is None:
                continue
            score = check_for_agent(
                agent, message.to_api(), case.state, case.event,
                **_kwargs_for(agent, case, DEFAULT_PIT_LANE_LOSS_S),
                **({"injected": case.injected, "events": case.events}
                   if agent == "strategist" else {}),
            )
            mine.append({**score, "agent": agent, "case": case.name,
                         "spoken_text": message.spoken_text, "refused": message.refused})
        stats = runtime.stats()
        per_agent[agent] = {**aggregate(mine), "model": spec.model,
                            **{k: v for k, v in stats.items() if k != "cooldowns"}}
        rows.extend(mine)
        totals["refusals"] += stats["refusals"]
        totals["errors"] += stats["errors"]
        totals["llm_calls"] += stats["llm_calls"]
        totals["tokens_in"] += stats["input_tokens"]
        totals["tokens_out"] += stats["output_tokens"]
        latency_sum += stats["avg_latency_ms"] * stats["llm_calls"]
    overall = aggregate(rows)
    metrics: dict[str, Any] = {
        **overall,
        "planned": planned,
        **totals,
        "avg_latency_ms": round(latency_sum / totals["llm_calls"], 1)
        if totals["llm_calls"] else None,
    }
    for agent, agg in per_agent.items():
        metrics[f"{agent}_mean_score"] = agg.get("mean_score")
    return {
        "status": "quota" if watch.hit else "done",
        "mode": "live",
        "metrics": metrics,
        "agents": per_agent,
        "seconds": round(time.monotonic() - started, 1),
        "usage": _usage_delta(),
        "rows": rows,
    }


def coach_cases(session_id: str) -> list[dict[str, Any]]:
    return [
        {"name": f"spa-lap{m}-vs-lap{r}", "session_id": session_id, "main_lap": m, "ref_lap": r}
        for m, r in COACH_LAPS
    ]


def step_coach_dry() -> dict[str, Any]:
    return {
        "status": "planned",
        "note": "coach eval on Spa laps "
        + ", ".join(f"{m} vs {r}" for m, r in COACH_LAPS) + f" with the {JUDGE} judge",
        "metrics": {"cases": len(COACH_LAPS)},
    }


async def step_coach_live(watch: QuotaWatch, findings: dict[str, dict]) -> dict[str, Any]:
    from evals.checks import aggregate, cross_check
    from evals.judge_provider import judge_backend
    from evals.judges import judge

    from rtv import llm
    from rtv.coaching.orchestrator import CoachOrchestrator
    from rtv.pitwall.provider import KimiProvider

    if not findings:
        return {"status": "skipped", "note": "no lap findings (the Spa import failed)"}
    _usage_delta()
    model = llm.model("reasoning")
    orch = CoachOrchestrator(KimiProvider(), model=model)
    backend = judge_backend(JUDGE)
    rows, scores, cases = [], [], []
    started = time.monotonic()
    for name, f in findings.items():
        if watch.hit:
            break
        try:
            result = (await orch.coach(f)).model_dump()
        except Exception as exc:
            if watch.check_exception(exc):
                break
            cases.append({"case": name, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        score = cross_check(result, f)
        rows.append(score)
        j = None
        try:
            j = await judge(result, f, backend.provider, model=backend.model)
        except Exception as exc:  # judge errors never fail the coach case
            cases.append({"case": name, "judge_error": f"{type(exc).__name__}: {exc}"[:300]})
        if j is not None:
            scores.append(j.model_dump())
        cases.append({"case": name, "score": score, "judge": j.model_dump() if j else None})
    metrics: dict[str, Any] = dict(aggregate(rows))
    if scores:
        metrics[f"judge_{JUDGE}"] = {
            k: round(sum(s[k] for s in scores) / len(scores), 3)
            for k in ("clarity", "actionability", "grounding")
        } | {"scored": len(scores)}
    return {
        "status": "quota" if watch.hit else "done",
        "mode": "live",
        "model": model,
        "judge": backend.label,
        "metrics": metrics,
        "seconds": round(time.monotonic() - started, 1),
        "usage": _usage_delta(),
        "cases": cases,
    }


def step_replay_from_journal(journal: Path) -> dict[str, Any]:
    stats = run_stats(_records(journal))
    stats["run"] = journal.stem
    return {
        "status": "recorded",
        "note": f"statistics of an existing journal ({journal.name}); no replay ran",
        "journal": str(journal),
        "metrics": replay_metrics(stats),
        "stats": stats,
    }


def _records(path: Path) -> list[dict[str, Any]]:
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


# --------------------------------------------------------------------------
# the server for the import and the replay
# --------------------------------------------------------------------------
class Server:
    """A local RTV server on its own data dir, with the pitwall agents on."""

    def __init__(self, data_dir: Path, log_path: Path) -> None:
        self.data_dir = data_dir
        self.log_path = log_path
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}/api/v1"
        self.proc: subprocess.Popen | None = None

    def start(self, timeout_s: float = 120) -> None:
        env = dict(os.environ)
        env.update(RTV_DATA_DIR=str(self.data_dir), RTV_PITWALL_AGENTS="true")
        self._log = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "rtv.main:app", "--app-dir", str(ROOT / "src"),
             "--host", "127.0.0.1", "--port", str(self.port), "--workers", "1"],
            cwd=ROOT, env=env, stdout=self._log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early; see {self.log_path}")
            try:
                self.get("/health")
                return
            except OSError:
                time.sleep(1)
        raise RuntimeError("server did not come up")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "_log", None):
            self._log.close()

    def get(self, path: str) -> Any:
        with urllib.request.urlopen(self.base + path, timeout=60) as r:
            return json.loads(r.read())

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body or {}).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def import_ibt(self, ibt: Path, timeout_s: float = 1800) -> str:
        job = self.post("/import", {"path": str(ibt), "label": "weekly eval"})["job_id"]
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self.get(f"/import/{job}")
            if status.get("status") == "complete":
                return status["session_id"]
            if status.get("status") in ("failed", "error"):
                raise RuntimeError(f"import failed: {status.get('error')}")
            time.sleep(3)
        raise RuntimeError("import timed out")

    def findings(self, session_id: str) -> dict[str, dict]:
        out = {}
        for case in coach_cases(session_id):
            q = f"session_id={session_id}&main_lap={case['main_lap']}&ref_lap={case['ref_lap']}"
            out[case["name"]] = self.get(f"/coaching/lap-findings?{q}")
        return out


def run_replay(server: Server, session_id: str, watch: QuotaWatch, out_dir: Path, *,
               speed: float, timeout_s: float, poll_s: float = 15) -> dict[str, Any]:
    started = time.monotonic()
    server.post("/replay/start", {"session_id": session_id, "speed": speed})
    status: dict[str, Any] = {}
    stopped_for = None
    while time.monotonic() - started < timeout_s:
        time.sleep(poll_s)
        status = server.get("/replay/status")
        if watch.note(_quota_lines(server.log_text())):
            stopped_for = "quota"
            server.post("/replay/stop")
            break
        if not status.get("running"):
            break
    else:
        stopped_for = "timeout"
        server.post("/replay/stop")
    time.sleep(5)  # let the journal run close
    runs = sorted((server.data_dir / "sessions" / session_id / "runs").glob("*.jsonl"),
                  key=lambda p: p.stat().st_mtime)
    if not runs:
        return {"status": "failed", "note": "the replay wrote no journal", "replay": status}
    journal = out_dir / runs[-1].name
    shutil.copy2(runs[-1], journal)
    if stopped_for is None and (hit := journal_quota_hit(journal)):
        watch.note(hit)
        stopped_for = "quota" if watch.hit else None
    stats = run_stats(_records(journal))
    stats["run"] = journal.stem
    return {
        "status": stopped_for or ("done" if status.get("finished") else "partial"),
        "mode": "live",
        "speed": speed,
        "frames": f"{status.get('frames')}/{status.get('total')}",
        "journal": journal.name,
        "seconds": round(time.monotonic() - started, 1),
        "metrics": replay_metrics(stats),
        "stats": stats,
    }


def _quota_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if QUOTA_RE.search(line))[:2000]


def find_ibt(explicit: str) -> Path | None:
    if explicit:
        return Path(explicit)
    for path in IBT_CANDIDATES:
        if path.is_file():
            return path
    subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_fixtures.py")], cwd=ROOT,
                   check=False)
    return IBT_CANDIDATES[0] if IBT_CANDIDATES[0].is_file() else None


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def render_report(current: dict[str, Any], previous: dict[str, Any] | None,
                  previous_name: str | None) -> str:
    cur_m = current["metrics"]
    prev_m = (previous or {}).get("metrics") or {}
    kind = "dry run (no model calls)" if current.get("dry_run") else "live run"
    lines = [f"# Weekly eval {current['date']} ({kind})", ""]
    lines.append(f"Commit `{current.get('commit') or 'unknown'}`. Compared with "
                 + (f"`{previous_name}`" + (" (a dry-run placeholder)"
                                            if previous and previous.get("dry_run") else "")
                    if previous else "nothing (first result)") + ".")
    lines.append("")
    if current.get("quota"):
        lines += [f"**Stopped on a Kimi 429:** {current['quota']}", "",
                  "Results below are partial; skipped steps say so.", ""]

    lines += ["## Headline changes", ""]
    heads = headline_changes(prev_m, cur_m) if previous else []
    drift = fingerprint_drift((previous or {}).get("fingerprint"), current.get("fingerprint"))
    for line in drift:
        heads.insert(0, f"DRIFT: {line}")
    lines += [f"- {h}" for h in heads] or (
        ["- No headline metric moved."] if previous else ["- First result; nothing to diff."]
    )
    real_off = current["steps"].get("real_offline") or {}
    if real_off.get("baseline_changed"):
        lines.append(f"- real cases: {real_off['baseline_changed']} field(s) differ from "
                     "`evals/real/spa_run/baseline.json` (see the list at the end)")
    lines.append("")

    lines += ["## Steps", "", "| step | status | notes |", "|---|---|---|"]
    for name, step in current["steps"].items():
        note = step.get("note") or ""
        if step.get("seconds") is not None:
            note = (note + " " if note else "") + f"{step['seconds']:.0f} s"
        usage = step.get("usage") or {}
        if usage.get("calls"):
            note += f", {usage['calls']} Kimi calls, {usage.get('input_tokens', 0)} in / " \
                    f"{usage.get('output_tokens', 0)} out tokens"
        lines.append(f"| {name} | {step.get('status')} | {note.strip()} |")
    lines.append("")

    cur_stats = (current["steps"].get("replay") or {}).get("stats")
    prev_stats = ((previous or {}).get("steps") or {}).get("replay", {}).get("stats")
    if cur_stats:
        lines += ["## Spa replay (compare_runs)", ""]
        tables = [prev_stats, cur_stats] if prev_stats else [cur_stats]
        labels = [previous_name or "previous", current["date"]] if prev_stats \
            else [current["date"]]
        # compare_markdown's own "## " sections sit one level below this one.
        table = compare_markdown(tables, labels).rstrip().replace("## ", "### ")
        lines += [table, ""]

    lines += ["## All metrics", "", f"| metric | {previous_name or 'previous'} | "
              f"{current['date']} |", "|---|---|---|"]
    for key in sorted(set(cur_m) | set(prev_m)):
        a, b = prev_m.get(key), cur_m.get(key)
        mark = " *" if previous and a != b else ""
        lines.append(f"| {key}{mark} | {fmt_value(a)} | {fmt_value(b)} |")
    lines.append("")
    if real_off.get("baseline_changes"):
        lines += ["## Real cases vs baseline.json", ""]
        lines += [f"- `{c}`" for c in real_off["baseline_changes"]] + [""]
    fp = current.get("fingerprint") or {}
    if fp.get("agents"):
        lines += ["## Agent models and prompt hashes", "", "| agent | model | prompt |",
                  "|---|---|---|"]
        for name, row in fp["agents"].items():
            lines.append(f"| {name} | {row['model']} | {row['prompt_sha'][:12]} |")
        lines += [f"| coach (post-race) | {fp.get('coach_model')} | |", ""]
    return "\n".join(lines)


def short_summary(current: dict[str, Any], previous_name: str | None,
                  report_path: Path) -> str:
    """Under 80 words: what the routine posts to the project chat."""
    heads = headline_changes(
        (current.get("previous_metrics") or {}), current["metrics"]
    ) if previous_name else []
    drift = fingerprint_drift(current.get("previous_fingerprint"), current.get("fingerprint"))
    parts = [f"Weekly eval {current['date']}"
             + (" stopped on a Kimi 429 (partial)." if current.get("quota") else " done.")]
    items = [f"drift: {d}" for d in drift[:2]] + heads[:4]
    if items:
        parts.append(" ".join(f"{i}." for i in items))
    elif previous_name:
        parts.append(f"No headline change vs {previous_name}.")
    parts.append(f"Report: {report_path}")
    words = " ".join(parts).split()
    return " ".join(words[:78])


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
async def run(args: argparse.Namespace, out_dir: Path, watch: QuotaWatch) -> dict[str, Any]:
    steps: dict[str, Any] = {}
    wanted = [s for s in STEPS if s in args.steps]
    findings: dict[str, dict] = {}

    if args.dry_run:
        if "replay" in wanted:
            steps["replay"] = (step_replay_from_journal(Path(args.replay_journal))
                               if args.replay_journal else
                               {"status": "planned",
                                "note": f"one Spa replay at {args.speed:g}x, agents on"})
        if "real" in wanted:
            steps["real"] = step_real_dry()
        if "golden" in wanted:
            steps["golden"] = step_golden_dry()
        if "coach" in wanted:
            steps["coach"] = step_coach_dry()
        steps["real_offline"] = step_real_offline()
        return steps

    from rtv import llm

    if not llm.is_configured():
        raise SystemExit(f"{llm.key_env_var()} is not set; use --dry-run.")

    server = None
    if "replay" in wanted or "coach" in wanted:
        ibt = find_ibt(args.ibt)
        if ibt is None or not ibt.is_file():
            note = "no Spa .ibt (scripts/fetch_fixtures.py failed)"
            for name in ("replay", "coach"):
                if name in wanted:
                    steps[name] = {"status": "skipped", "note": note}
        else:
            work = Path(tempfile.mkdtemp(prefix="rtv-weekly-"))
            server = Server(work / "data", out_dir / "server.log")
            try:
                server.start()
                session_id = server.import_ibt(ibt)
                if "coach" in wanted:
                    findings = server.findings(session_id)
                if "replay" in wanted:
                    steps["replay"] = run_replay(
                        server, session_id, watch, out_dir,
                        speed=args.speed, timeout_s=args.replay_timeout,
                    )
            except Exception as exc:
                steps.setdefault("replay", {"status": "failed",
                                            "note": f"{type(exc).__name__}: {exc}"[:300]})
            finally:
                server.stop()
                shutil.rmtree(work, ignore_errors=True)

    for name, fn in (("real", step_real_live), ("golden", step_golden_live)):
        if name not in wanted:
            continue
        steps[name] = ({"status": "skipped", "note": "Kimi quota hit earlier"}
                       if watch.hit else await fn(watch))
    if "coach" in wanted and "coach" not in steps:
        steps["coach"] = ({"status": "skipped", "note": "Kimi quota hit earlier"}
                          if watch.hit else await step_coach_live(watch, findings))
    steps["real_offline"] = step_real_offline()
    return steps


def main(argv: Sequence[str] | None = None,
         fingerprint: Callable[[], dict[str, Any]] = spec_fingerprint) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="No model calls, no server.")
    p.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT,
                   help=f"Where weekly results live (default {DEFAULT_OUT_ROOT}).")
    p.add_argument("--date", default=dt.datetime.now(dt.UTC).strftime("%Y-%m-%d"))
    p.add_argument("--steps", default=",".join(STEPS),
                   help=f"Comma-separated subset of {', '.join(STEPS)}.")
    p.add_argument("--ibt", default="", help="The Spa .ibt (default: fixtures, else fetch).")
    p.add_argument("--speed", type=float, default=2.0, help="Replay speed (default 2x).")
    p.add_argument("--replay-timeout", type=float, default=5400, help="Seconds.")
    p.add_argument("--replay-journal", default="",
                   help="--dry-run: record this journal's statistics as the replay result.")
    args = p.parse_args(argv)
    args.steps = {s.strip() for s in args.steps.split(",") if s.strip()}
    unknown = args.steps - set(STEPS)
    if unknown:
        p.error(f"unknown step(s): {', '.join(sorted(unknown))}")

    out_dir = run_folder(args.out_root, args.date, args.dry_run)
    out_dir.mkdir(parents=True)
    watch = QuotaWatch()
    rtv_log = logging.getLogger("rtv")
    rtv_log.addHandler(watch)
    try:
        steps = asyncio.run(run(args, out_dir, watch))
    finally:
        rtv_log.removeHandler(watch)

    previous = find_previous(args.out_root, out_dir.name)
    prev_name, prev_data = (previous[0].name, previous[1]) if previous else (None, None)
    for name, step in steps.items():
        rows = step.pop("rows", None)
        if rows is not None:
            (out_dir / f"{name}.json").write_text(
                json.dumps(rows, indent=1, default=str) + "\n", encoding="utf-8")
    current = {
        "date": args.date,
        "dry_run": args.dry_run,
        "commit": git_head(),
        "quota": watch.hit,
        "fingerprint": fingerprint(),
        "steps": steps,
    }
    current["metrics"] = flat_metrics(steps)
    report = out_dir / "REPORT.md"
    report.write_text(render_report(current, prev_data, prev_name) + "\n", encoding="utf-8")
    current["previous"] = prev_name
    (out_dir / "summary.json").write_text(
        json.dumps(current, indent=1, default=str) + "\n", encoding="utf-8")
    current["previous_metrics"] = (prev_data or {}).get("metrics")
    current["previous_fingerprint"] = (prev_data or {}).get("fingerprint")
    print(short_summary(current, prev_name, report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
