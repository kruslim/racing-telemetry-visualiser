"""Agent statistics of one race-journal run, and a side-by-side comparison of several.

Every Kimi replay of the pitwall leaves a journal (:mod:`rtv.store.journal`) that
holds each agent run, model call, validator check and radio call. This module turns
one journal into the numbers that say whether a change made the agents better or
worse: calls, runs, refusals, validator failures before and after repair, latency
per agent, tokens, calls by trigger and late or expired radio calls.

It reads only the activity records, never the 60 Hz telemetry, and is pure Python so
``scripts/compare_runs.py``, the evals and a scheduled routine can share it.

A value is ``None`` when the journal does not record the field at all (an older
journal without token counts, or one written before the radio tagged late calls),
so "n/a" is never confused with a real zero.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from rtv.store.journal import iter_records, schema_version_of

#: Agent order in tables; any other agent follows alphabetically.
AGENT_ORDER = ("strategist", "vehicle_engineer", "spotter", "coach")


def percentile(values: Sequence[float], q: float) -> float | None:
    """The ``q`` quantile (0-1) by linear interpolation; ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _latency(values: list[float]) -> dict[str, float | None]:
    return {
        "median_ms": statistics.median(values) if values else None,
        "p90_ms": percentile(values, 0.9),
    }


def _sum_present(values: Iterable[Any]) -> int | None:
    """Sum of the values that are numbers; ``None`` when none is."""
    present = [v for v in values if isinstance(v, int | float)]
    return sum(present) if present else None


def _agent_key(name: str) -> tuple[int, str]:
    return (AGENT_ORDER.index(name) if name in AGENT_ORDER else len(AGENT_ORDER), name)


def run_stats(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Statistics of one journal run, as plain JSON-ready data."""
    kinds: Counter[str] = Counter()
    run_trigger: dict[str, str] = {}
    run_agent: dict[str, str] = {}
    calls: list[dict[str, Any]] = []
    run_ends: list[dict[str, Any]] = []
    validates: list[dict[str, Any]] = []
    lifecycle: dict[str, Any] = {}
    radio_by_agent: Counter[str] = Counter()
    runs_with_input = 0
    # B3: supersedes that name their rule (journals before it do not).
    superseded_rules: Counter[str] = Counter()
    superseded_cross = 0

    for rec in records:
        kind = rec.get("kind")
        if not isinstance(kind, str):
            continue
        kinds[kind] += 1
        data = rec.get("data") or {}
        agent = rec.get("agent") or "unknown"
        run_id = rec.get("run_id")
        if kind == "lifecycle" and data.get("phase") == "start":
            lifecycle = data
        elif kind == "run_start" and run_id:
            run_trigger[run_id] = data.get("trigger") or rec.get("event_key") or "unknown"
            run_agent[run_id] = agent
            if isinstance(data.get("input"), dict):
                runs_with_input += 1
        elif kind == "llm_call_end":
            calls.append({"agent": agent, "run_id": run_id, **data})
        elif kind == "run_end":
            run_ends.append({"agent": agent, "run_id": run_id, **data})
        elif kind == "validate":
            validates.append({"agent": agent, "run_id": run_id, **data})
        elif kind == "radio_aired":
            radio_by_agent[agent] += 1
        elif kind == "radio_superseded" and data.get("rule"):
            superseded_rules[data["rule"]] += 1
            if data.get("by_agent") and data.get("by_agent") != agent:
                superseded_cross += 1

    def trigger_of(item: dict[str, Any]) -> str:
        return run_trigger.get(item.get("run_id") or "", "unknown")

    agents = sorted(
        {c["agent"] for c in calls} | {r["agent"] for r in run_ends} | set(run_agent.values()),
        key=_agent_key,
    )

    def summary(sel_calls: list[dict], sel_runs: list[dict], sel_val: list[dict]) -> dict:
        outcomes = Counter(r.get("outcome") or "unknown" for r in sel_runs)
        has_answered = any("answered" in c for c in sel_calls)
        has_repaired = any("repaired" in v for v in sel_val)
        first = [v for v in sel_val if not v.get("repaired")]
        repaired = [v for v in sel_val if v.get("repaired")]
        return {
            "model_calls": len(sel_calls),
            "agent_runs": len(sel_runs),
            "radio_runs": outcomes.get("radio", 0),
            "refusals": outcomes.get("refused", 0),
            "errored_runs": outcomes.get("error", 0),
            "call_errors": sum(1 for c in sel_calls if c.get("ok") is False),
            # A call that neither answered nor asked for a tool: a respond payload
            # that failed the contract, or plain text. The journal does not say which.
            "calls_without_answer": (
                sum(1 for c in sel_calls if c.get("answered") is False) if has_answered else None
            ),
            # A turn that asked for a tool (propose_plan_revision, a lookup): the
            # loop working, not a miss. Counted in calls_without_answer too.
            "tool_turns": (
                sum(1 for c in sel_calls if c.get("answered") is False and c.get("tool_calls"))
                if has_answered
                else None
            ),
            "malformed_respond": (
                sum(
                    1
                    for c in sel_calls
                    if c.get("answered") is False
                    and c.get("ok") is not False
                    and not c.get("tool_calls")
                )
                if has_answered
                else None
            ),
            "validator_checks": len(sel_val),
            "validator_failures": sum(1 for v in sel_val if v.get("ok") is False),
            "first_pass_checks": len(first) if has_repaired else None,
            "first_pass_failures": (
                sum(1 for v in first if v.get("ok") is False) if has_repaired else None
            ),
            "repairs": len(repaired) if has_repaired else None,
            "failures_after_repair": (
                sum(1 for v in repaired if v.get("ok") is False) if has_repaired else None
            ),
            "over_word_limit": sum(1 for v in sel_val if v.get("over_word_limit")),
            "untraceable_numbers": sum(len(v.get("unsupported") or []) for v in sel_val),
            "call_latency": _latency(
                [c["latency_ms"] for c in sel_calls if isinstance(c.get("latency_ms"), int | float)]
            ),
            "run_latency": _latency(
                [r["latency_ms"] for r in sel_runs if isinstance(r.get("latency_ms"), int | float)]
            ),
            "tokens": {
                "input": _sum_present(c.get("input_tokens") for c in sel_calls),
                "cache_read": _sum_present(c.get("cache_read_input_tokens") for c in sel_calls),
                "output": _sum_present(c.get("output_tokens") for c in sel_calls),
            },
        }

    overall = summary(calls, run_ends, validates)
    by_agent = {
        name: {
            **summary(
                [c for c in calls if c["agent"] == name],
                [r for r in run_ends if r["agent"] == name],
                [v for v in validates if v["agent"] == name],
            ),
            "radio_aired": radio_by_agent.get(name, 0),
        }
        for name in agents
    }
    calls_by_trigger = Counter(trigger_of(c) for c in calls)
    runs_by_trigger = Counter(trigger_of(r) for r in run_ends)

    # Late and expired radio calls only exist once the radio tags them; before
    # that the kinds are absent and the counts are unknown, not zero. (A journal
    # from a radio that tags them but had nothing late also reads n/a; the
    # difference does not matter for a comparison.)
    def optional(kind: str) -> int | None:
        return kinds[kind] or None

    return {
        "agents_enabled": lifecycle.get("agents_enabled"),
        "session_id": lifecycle.get("session_id"),
        "source": lifecycle.get("source"),
        "schema_version": schema_version_of(lifecycle),
        # Agent runs whose whole model input is on run_start (schema 2 on).
        "runs_with_input": runs_with_input,
        **overall,
        "dispatches": kinds.get("dispatch", 0),
        "inbox_superseded": kinds.get("inbox_superseded", 0),
        "radio_queued": kinds.get("radio_queued", 0),
        "radio_aired": kinds.get("radio_aired", 0),
        "radio_superseded": kinds.get("radio_superseded", 0),
        "radio_late": optional("radio_late"),
        "radio_expired": optional("radio_expired"),
        # Cross-agent decision groups (B3): n/a for journals recorded before them.
        "radio_superseded_cross_agent": superseded_cross if superseded_rules else None,
        "radio_conflicts": superseded_rules.get("conflict", 0) if superseded_rules else None,
        "radio_superseded_by_rule": dict(sorted(superseded_rules.items())),
        "agents": by_agent,
        "calls_by_trigger": dict(sorted(calls_by_trigger.items())),
        "runs_by_trigger": dict(sorted(runs_by_trigger.items())),
    }


def load_run_stats(path: str | Path) -> dict[str, Any]:
    """:func:`run_stats` of a journal file, plus where it came from."""
    path = Path(path)
    return {"path": str(path), "run": path.stem, **run_stats(iter_records(path))}


# ---- rendering ------------------------------------------------------------


def fmt(value: Any, unit: str = "") -> str:
    """One table cell: "n/a" for unknown, seconds for latency, k for big counts."""
    if value is None:
        return "n/a"
    if unit == "ms":
        return f"{value / 1000:.1f} s"
    if unit == "tok":
        return f"{value / 1000:.1f}k" if value >= 1000 else str(value)
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _ratio(num: Any, den: Any) -> str:
    if num is None or den is None:
        return "n/a"
    return f"{num} / {den}"


#: (label, getter, unit) rows of the summary table.
_SUMMARY_ROWS: list[tuple[str, Any, str]] = [
    ("journal schema", lambda s: s.get("schema_version"), ""),
    ("runs with recorded input", lambda s: s.get("runs_with_input"), ""),
    ("model calls", lambda s: s["model_calls"], ""),
    ("agent runs", lambda s: s["agent_runs"], ""),
    ("runs that aired radio", lambda s: s["radio_runs"], ""),
    ("refusals", lambda s: s["refusals"], ""),
    ("errored runs", lambda s: s["errored_runs"], ""),
    ("calls without a structured answer", lambda s: s["calls_without_answer"], ""),
    ("of which tool-use turns", lambda s: s["tool_turns"], ""),
    (
        "no answer and no tool call (malformed respond or text)",
        lambda s: s["malformed_respond"],
        "",
    ),
    ("transport errors", lambda s: s["call_errors"], ""),
    (
        "validator failures / checks",
        lambda s: _ratio(s["validator_failures"], s["validator_checks"]),
        "",
    ),
    (
        "first pass failures / checks",
        lambda s: _ratio(s["first_pass_failures"], s["first_pass_checks"]),
        "",
    ),
    (
        "after repair failures / repairs",
        lambda s: _ratio(s["failures_after_repair"], s["repairs"]),
        "",
    ),
    ("over word limit", lambda s: s["over_word_limit"], ""),
    ("untraceable numbers", lambda s: s["untraceable_numbers"], ""),
    ("median call", lambda s: s["call_latency"]["median_ms"], "ms"),
    ("p90 call", lambda s: s["call_latency"]["p90_ms"], "ms"),
    ("median run", lambda s: s["run_latency"]["median_ms"], "ms"),
    ("p90 run", lambda s: s["run_latency"]["p90_ms"], "ms"),
    ("tokens in", lambda s: s["tokens"]["input"], "tok"),
    ("tokens cache read", lambda s: s["tokens"]["cache_read"], "tok"),
    ("tokens out", lambda s: s["tokens"]["output"], "tok"),
    ("dispatches", lambda s: s["dispatches"], ""),
    ("inbox superseded", lambda s: s["inbox_superseded"], ""),
    ("radio aired", lambda s: s["radio_aired"], ""),
    ("radio superseded", lambda s: s["radio_superseded"], ""),
    ("radio late", lambda s: s["radio_late"], ""),
    ("radio expired", lambda s: s["radio_expired"], ""),
    ("radio superseded across agents", lambda s: s.get("radio_superseded_cross_agent"), ""),
    ("radio conflicts dropped", lambda s: s.get("radio_conflicts"), ""),
]

_AGENT_ROWS: list[tuple[str, Any, str]] = [
    ("runs", lambda s: s["agent_runs"], ""),
    ("calls", lambda s: s["model_calls"], ""),
    ("refusals", lambda s: s["refusals"], ""),
    ("errored runs", lambda s: s["errored_runs"], ""),
    ("no structured answer", lambda s: s["calls_without_answer"], ""),
    (
        "validator fail / checks",
        lambda s: _ratio(s["validator_failures"], s["validator_checks"]),
        "",
    ),
    ("median call", lambda s: s["call_latency"]["median_ms"], "ms"),
    ("p90 call", lambda s: s["call_latency"]["p90_ms"], "ms"),
    ("median run", lambda s: s["run_latency"]["median_ms"], "ms"),
    ("p90 run", lambda s: s["run_latency"]["p90_ms"], "ms"),
    ("tokens out", lambda s: s["tokens"]["output"], "tok"),
]


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return out


def compare_markdown(stats: Sequence[dict[str, Any]], labels: Sequence[str] | None = None) -> str:
    """Side-by-side Markdown tables of several runs (usually two: before, after)."""
    labels = list(labels or [s.get("run", f"run {i + 1}") for i, s in enumerate(stats)])
    lines = ["## Summary", ""]
    lines += _table(
        ["", *labels],
        [[label, *(fmt(get(s), unit) for s in stats)] for label, get, unit in _SUMMARY_ROWS],
    )

    agents = sorted({a for s in stats for a in s["agents"]}, key=_agent_key)
    lines += ["", "## Per agent", ""]
    rows = []
    for agent in agents:
        for label, get, unit in _AGENT_ROWS:
            cells = []
            for s in stats:
                a = s["agents"].get(agent)
                cells.append("n/a" if a is None else fmt(get(a), unit))
            rows.append([f"{agent} · {label}", *cells])
    lines += _table(["", *labels], rows)

    triggers = sorted(
        {t for s in stats for t in s["calls_by_trigger"]}
        | {t for s in stats for t in s["runs_by_trigger"]}
    )
    lines += ["", "## By trigger (runs / calls)", ""]
    lines += _table(
        ["trigger", *labels],
        [
            [
                t,
                *(
                    f"{s['runs_by_trigger'].get(t, 0)} / {s['calls_by_trigger'].get(t, 0)}"
                    for s in stats
                ),
            ]
            for t in triggers
        ],
    )
    return "\n".join(lines) + "\n"
