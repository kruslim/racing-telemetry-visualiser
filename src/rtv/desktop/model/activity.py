"""The agent layer as data: activity records in, per-agent state and runs out.

A port of ``frontend/js/shared/activity-model.js``. The live view feeds it records
as ``/ws/pitwall`` pushes them, the review feeds it a recorded journal up to the
cursor, and both get the same answer. A record is the backend envelope from
``rtv.pitwall.activity``: ``{seq, wall_ms, session_time, tick, kind, agent, run_id,
event_key, data}``. Kinds this module does not know are ignored by ``apply``.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .format import to_fixed

_LONG_DECIMAL_RE = re.compile(r"(?<![\w.:])(-?\d+)\.(\d+)(?![\d.])")


def round_figures(text: Any, digits: int = 2) -> str:
    """Port of ``roundFigures``: every long decimal in ``text`` rounded for reading."""
    if not isinstance(text, str):
        return "" if text is None else str(text)

    def _sub(m: re.Match[str]) -> str:
        if len(m.group(2)) <= digits:
            return m.group(0)
        fixed = to_fixed(float(m.group(0)), digits)
        # String(Number(x.toFixed(d))): trailing zeros and a bare point go, "-0" is "0".
        out = fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
        return "0" if out in ("-0", "") else out

    return _LONG_DECIMAL_RE.sub(_sub, text)


AGENTS = ("strategist", "vehicle_engineer", "spotter", "coach")

NODES = (
    "telemetry",
    "engine",
    "detectors",
    "bus",
    "orchestrator",
    *AGENTS,
    "llm",
    "tools",
    "validator",
    "radio",
    "driver",
)

RUN_LIMIT = 200


@dataclass
class AgentState:
    name: str
    status: str = "idle"  # idle | queued | thinking | tool | validating
    run_id: str | None = None
    since: float | None = None
    detail: str = ""
    runs: int = 0
    calls: int = 0
    tools: int = 0
    refused: int = 0
    errors: int = 0
    last_outcome: str | None = None
    last_latency_ms: float | None = None
    skipped: str | None = None


@dataclass
class Run:
    run_id: str
    agent: str | None
    event_key: str | None
    session_time: float | None
    tick: int | None
    start_wall: float | None
    end_wall: float | None = None
    trigger: str | None = None
    model: str | None = None
    severity: str | None = None
    context_radio: list = field(default_factory=list)
    context_events: Any = None
    spans: list = field(default_factory=list)
    validations: list = field(default_factory=list)
    outcome: str | None = None
    message_id: str | None = None
    latency_ms: float | None = None
    turns: int = 0
    tools_used: list = field(default_factory=list)
    seqs: list = field(default_factory=list)


@dataclass
class Model:
    last_seq: int = 0
    agents: dict = field(default_factory=lambda: {a: AgentState(a) for a in AGENTS})
    runs: OrderedDict = field(default_factory=OrderedDict)
    radio: dict = field(default_factory=lambda: {"queued": 0, "aired": 0, "superseded": 0})
    dispatch: dict = field(
        default_factory=lambda: {
            "events": 0,
            "routed": 0,
            "blocked": 0,
            "cooldown": 0,
            "superseded": 0,
        }
    )
    llm: dict = field(
        default_factory=lambda: {"calls": 0, "inflight": 0, "errors": 0, "latency_total": 0.0}
    )
    tools: dict = field(default_factory=lambda: {"calls": 0, "errors": 0, "by_name": {}})
    validator: dict = field(default_factory=lambda: {"checks": 0, "failed": 0, "repaired": 0})
    last_blocked: str | None = None


def create_model() -> Model:
    return Model()


def _agent_of(model: Model, name: str | None) -> AgentState | None:
    if not name:
        return None
    if name not in model.agents:
        model.agents[name] = AgentState(name)
    return model.agents[name]


def _run_of(model: Model, record: dict) -> Run | None:
    run_id = record.get("run_id")
    if not run_id:
        return None
    run = model.runs.get(run_id)
    if run is None:
        run = Run(
            run_id=run_id,
            agent=record.get("agent"),
            event_key=record.get("event_key"),
            session_time=record.get("session_time"),
            tick=record.get("tick"),
            start_wall=record.get("wall_ms"),
        )
        model.runs[run_id] = run
        while len(model.runs) > RUN_LIMIT:
            model.runs.popitem(last=False)
    return run


def _set_status(agent: AgentState, status: str, t: Any, detail: Any) -> None:
    if agent.status != status:
        agent.since = t
    agent.status = status
    agent.detail = str(detail or "")


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def apply(model: Model, record: dict | None) -> Model:  # noqa: C901 - one switch
    """Fold one record into the model (mutated in place)."""
    if not isinstance(record, dict):
        return model
    seq = record.get("seq")
    if isinstance(seq, int):
        if seq <= model.last_seq:
            return model  # replayed
        model.last_seq = seq
    data = record.get("data") or {}
    agent = _agent_of(model, record.get("agent"))
    t = record.get("session_time")
    kind = record.get("kind")

    if kind == "dispatch":
        model.dispatch["events"] += 1
        if data.get("blocked"):
            model.dispatch["blocked"] += 1
            model.last_blocked = data["blocked"]
            return model
        for name in data.get("accepted") or []:
            model.dispatch["routed"] += 1
            a = _agent_of(model, name)
            a.skipped = None
            if a.status == "idle":
                _set_status(a, "queued", t, record.get("event_key"))
        for name, reason in (data.get("skipped") or {}).items():
            if reason == "cooldown":
                model.dispatch["cooldown"] += 1
            _agent_of(model, name).skipped = reason
    elif kind == "inbox_superseded":
        model.dispatch["superseded"] += 1
    elif kind == "run_start":
        run = _run_of(model, record)
        run.trigger = data.get("trigger")
        run.model = data.get("model")
        run.severity = data.get("severity")
        run.context_radio = data.get("context_radio") or []
        run.context_events = data.get("context_events")
        run.seqs.append(seq)
        if agent:
            agent.runs += 1
            agent.run_id = record.get("run_id")
            _set_status(agent, "thinking", t, record.get("event_key"))
    elif kind == "llm_call_start":
        run = _run_of(model, record)
        model.llm["inflight"] += 1
        if run:
            run.spans.append(
                {
                    "kind": "llm",
                    "call": data.get("call"),
                    "name": data.get("model") or "model",
                    "start_wall": record.get("wall_ms"),
                    "end_wall": None,
                    "ok": None,
                }
            )
            run.seqs.append(seq)
        if agent:
            _set_status(agent, "thinking", t, data.get("model") or "")
    elif kind == "llm_call_end":
        run = _run_of(model, record)
        model.llm["inflight"] = max(0, model.llm["inflight"] - 1)
        model.llm["calls"] += 1
        model.llm["latency_total"] += _num(data.get("latency_ms"))
        if not data.get("ok"):
            model.llm["errors"] += 1
        if run:
            span = next(
                (
                    s
                    for s in reversed(run.spans)
                    if s["kind"] == "llm" and s.get("call") == data.get("call")
                ),
                None,
            )
            if span:
                span.update(
                    end_wall=record.get("wall_ms"),
                    latency_ms=data.get("latency_ms"),
                    ok=bool(data.get("ok")),
                    error=data.get("error"),
                    tool_calls=data.get("tool_calls") or [],
                )
            run.seqs.append(seq)
        if agent:
            agent.calls += 1
            agent.last_latency_ms = data.get("latency_ms")
    elif kind == "tool_call":
        run = _run_of(model, record)
        model.tools["calls"] += 1
        if not data.get("ok"):
            model.tools["errors"] += 1
        tool = data.get("tool")
        if tool:
            model.tools["by_name"][tool] = model.tools["by_name"].get(tool, 0) + 1
        if run:
            latency = _num(data.get("latency_ms"))
            wall = record.get("wall_ms")
            run.spans.append(
                {
                    "kind": "tool",
                    "name": tool,
                    "start_wall": None if wall is None else wall - latency,
                    "end_wall": wall,
                    "latency_ms": data.get("latency_ms"),
                    "ok": bool(data.get("ok")),
                    "args": data.get("args") or {},
                    "result": data.get("result"),
                }
            )
            run.seqs.append(seq)
        if agent:
            agent.tools += 1
            _set_status(agent, "tool", t, tool or "")
    elif kind == "validate":
        run = _run_of(model, record)
        model.validator["checks"] += 1
        if not data.get("ok"):
            model.validator["failed"] += 1
        if data.get("repaired"):
            model.validator["repaired"] += 1
        if run:
            run.validations.append(
                {
                    "ok": bool(data.get("ok")),
                    "unsupported": [round_figures(u) for u in data.get("unsupported") or []],
                    # Why each figure failed, long decimals rounded for reading (task A3).
                    "reasons": [round_figures(r) for r in data.get("unsupported_detail") or []],
                    "repaired": bool(data.get("repaired")),
                    "word_count": data.get("word_count"),
                }
            )
            run.seqs.append(seq)
        if agent:
            _set_status(
                agent, "validating", t, "grounded" if data.get("ok") else "unsupported figures"
            )
    elif kind == "run_end":
        run = _run_of(model, record)
        if run:
            run.end_wall = record.get("wall_ms")
            run.outcome = data.get("outcome")
            run.message_id = data.get("message_id")
            run.latency_ms = data.get("latency_ms")
            run.turns = data.get("turns") or 0
            run.tools_used = data.get("tools_used") or []
            run.seqs.append(seq)
        if agent:
            agent.last_outcome = data.get("outcome")
            if data.get("outcome") == "refused":
                agent.refused += 1
            if data.get("outcome") == "error":
                agent.errors += 1
            agent.run_id = None
            _set_status(agent, "idle", t, "")
    elif kind == "radio_queued":
        model.radio["queued"] += 1
    elif kind == "radio_superseded":
        model.radio["superseded"] += 1
    elif kind == "radio_aired":
        model.radio["aired"] += 1
    return model


def _seq_key(record: dict) -> int:
    seq = record.get("seq")
    return seq if isinstance(seq, int) else 0


def reduce(records: list[dict] | None, up_to: float | None = None) -> Model:
    """Rebuild from scratch, optionally only records at or before ``up_to``."""
    model = create_model()
    for record in sorted(records or [], key=_seq_key):
        t = record.get("session_time")
        if up_to is not None and t is not None and t > up_to:
            continue
        apply(model, record)
    return model


def recent_runs(model: Model, limit: int = 30) -> list[Run]:
    """Runs newest first, for the waterfall."""
    return list(reversed(model.runs.values()))[:limit]


def mean_latency(model: Model) -> float | None:
    calls = model.llm["calls"]
    return model.llm["latency_total"] / calls if calls else None


def edges_for(record: dict | None) -> list[dict]:
    """Which graph edges a record travelled. Every edge is a backend code path."""
    if not record:
        return []
    data = record.get("data") or {}
    agent = record.get("agent")
    kind = record.get("kind")

    def e(src: str | None, dst: str | None, **extra: Any) -> dict:
        return {"from": src, "to": dst, "kind": kind, "agent": agent, **extra}

    if kind == "frames":
        return [e("telemetry", "engine")]
    if kind == "race_event":
        return [e("detectors", "bus")]
    if kind == "dispatch":
        if data.get("blocked"):
            return [e("bus", "orchestrator", blocked=True)]
        return [e("bus", "orchestrator")] + [
            e("orchestrator", name, agent=name) for name in data.get("accepted") or []
        ]
    if kind == "run_start":
        return [
            e("radio", agent, source=m.get("agent"), context=True)
            for m in data.get("context_radio") or []
        ]
    if kind == "llm_call_start":
        return [e(agent, "llm")]
    if kind == "llm_call_end":
        return [e("llm", agent, ok=bool(data.get("ok")))]
    if kind == "tool_call":
        ok = bool(data.get("ok"))
        return [e(agent, "tools"), e("tools", agent, ok=ok, delay=True)]
    if kind == "validate":
        return [e(agent, "validator", ok=bool(data.get("ok")))]
    if kind == "radio_queued":
        return [e("validator" if agent in AGENTS else "orchestrator", "radio")]
    if kind == "radio_aired":
        return [e("driver", "radio") if agent == "driver" else e("radio", "driver")]
    return []
