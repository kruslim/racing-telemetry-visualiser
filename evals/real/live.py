"""Re-execute recorded agent runs against today's prompt and model, and score them.

Each case's recorded state, event, radio and context events are handed to a fresh
:class:`~rtv.pitwall.framework.AgentRuntime` built from today's spec. Tools answer
from the recording when the model asks the same question the recorded run asked
(same tool, same arguments), so a re-run sees the same corner telemetry and setup
sheet the replay had even though no store or engine is attached; a question the
recording cannot answer goes to the real tool on the recorded state, and is counted
(``unrecorded_tool_calls``), because those tools then run without the store.

The new answer is scored by exactly the same code as the offline mode
(:func:`evals.real.offline.score_case`), so recorded and re-executed results line up
field by field. ``spec_drift`` per case says what changed since the recording
(prompt, tools, output contract, model). An optional LLM judge (Gemini by default,
:mod:`evals.judge_provider`) adds an advisory 1-5 score; it never gates anything.

This calls the pitwall model for every case (Kimi quota) and the judge if asked.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, Field

from evals.real.offline import SPECS, event_of, score_case, state_of
from rtv.pitwall import agent_input
from rtv.pitwall.framework import AgentRuntime, EventRef, RadioMessage, ToolSpec, agent_model
from rtv.racestate.models import RaceEvent


def _key(name: str, args: Any) -> str:
    return name + json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)


def recorded_tools(tools: tuple[ToolSpec, ...], case: dict[str, Any],
                   misses: list[str]) -> tuple[ToolSpec, ...]:
    """``tools`` answering from the case's recorded results where the call matches."""
    recorded = {
        _key(c["tool"], c["args"]): c["result"] for c in case["tool_calls"] if c["ok"]
    }

    def wrap(tool: ToolSpec) -> ToolSpec:
        def fn(ctx, args, _tool=tool):
            key = _key(_tool.name, args)
            if key in recorded:
                return recorded[key]
            misses.append(key)
            return _tool.fn(ctx, args)

        return replace(tool, fn=fn)

    return tuple(wrap(t) for t in tools)


def radio_history(case: dict[str, Any], event: RaceEvent) -> list[RadioMessage]:
    """The radio the run was shown, as messages (only agent, priority and words matter)."""
    ref = EventRef.of(event)
    return [
        RadioMessage(
            agent=m["agent"],
            priority=m["priority"],
            spoken_text=m["spoken_text"],
            detail_text="",
            event_ref=ref,
        )
        for m in case["input"].get("radio") or []
    ]


class _Collector:
    """A tracer that keeps the run's records, to rebuild a case from the re-run."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, kind: str, **fields: Any) -> None:
        self.records.append((kind, fields))


def rerun_case(case: dict[str, Any], collected: _Collector) -> dict[str, Any]:
    """A case with the re-run's input, tool calls and answers in place of the recording's."""
    new = {**case, "tool_calls": [], "answers": [], "radio": None, "air": None}
    for kind, data in collected.records:
        if kind == "run_start" and isinstance(data.get("input"), dict):
            new["input"] = data["input"]
        elif kind == "tool_call":
            new["tool_calls"].append(
                {
                    "tool": data.get("tool"),
                    "args": data.get("args") or {},
                    "ok": bool(data.get("ok")),
                    "prefetched": bool(data.get("prefetched")),
                    "result": data.get("result_full"),
                }
            )
        elif kind == "validate":
            new["answers"].append(
                {
                    "repaired": bool(data.get("repaired")),
                    "output": data.get("output_full"),
                    "recorded": {
                        "ok": data.get("ok"),
                        "policy": data.get("policy"),
                        "unsupported": list(data.get("unsupported") or []),
                        "over_word_limit": bool(data.get("over_word_limit")),
                    },
                }
            )
    return new


async def run_case(
    case: dict[str, Any],
    provider: Any,
    *,
    model: str = "",
    tool_config: dict[str, Any] | None = None,
    judges: Sequence[Any] = (),
) -> dict[str, Any]:
    """Re-execute one case; recorded and new scores side by side."""
    base = SPECS[case["agent"]]
    misses: list[str] = []
    spec = replace(
        base,
        model=agent_model(base, override=model),
        tools=recorded_tools(base.tools, case, misses),
    )
    collected = _Collector()
    runtime = AgentRuntime(spec, provider, tool_config=tool_config or {}, tracer=collected)
    drift = agent_input.spec_drift(case["input"], spec, runtime.system_prompt)
    event = event_of(case)
    state = state_of(case)
    message = await runtime.invoke(
        event,
        state,
        events=[RaceEvent.model_validate(_event_data(e)) for e in case["context_events"]],
        radio_history=radio_history(case, event),
    )
    recorded = score_case(case)
    out: dict[str, Any] = {
        "case_id": case["case_id"],
        "agent": case["agent"],
        "model": spec.model,
        "spec_drift": drift,
        "recorded_spoken": (case["answers"][-1]["output"] or {}).get("spoken_text"),
        "recorded_score": recorded["checks"]["score"],
        "recorded_validator_ok": recorded["answers"][-1]["ok"],
        "unrecorded_tool_calls": misses,
        "stats": runtime.stats(),
    }
    new_case = rerun_case(case, collected)
    if message is None or not new_case["answers"]:
        out.update(new=None, error="the agent produced no structured answer")
        return out
    new = score_case(new_case)
    out.update(
        new=new,
        new_spoken=message.spoken_text,
        new_score=new["checks"]["score"],
        new_validator_ok=new["answers"][-1]["ok"],
        refused=message.refused,
    )
    if judges:
        out["judges"] = [await judge_answer(new_case, backend) for backend in judges]
    return out


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in event.items() if k != "key"}


# ---- advisory judge -------------------------------------------------------
JUDGE_SYSTEM = """\
You are a strict evaluator of a sim-racing pit wall. You are given what one pit-wall
agent was shown (its role, the race state slice, the triggering event and the tool
results it read) and the radio call it made. Rate the call. Numbers are checked
elsewhere by deterministic code: do not re-verify arithmetic. Score whether the call is
the right thing to say now, whether the driver can act on it, and whether it stays
inside what the data shows. Generic or padded calls score low.
"""


class PitwallJudgeScore(BaseModel):
    usefulness: int = Field(ge=1, le=5, description="Is this the right call to make now?")
    actionability: int = Field(ge=1, le=5, description="Can the driver act on it at once?")
    grounding: int = Field(ge=1, le=5, description="Does it stay inside what the data shows?")
    comment: str = Field(description="One-sentence justification")


async def judge_answer(case: dict[str, Any], backend: Any) -> dict[str, Any] | None:
    """An advisory judge score for a case's final answer, or ``None`` on no answer."""
    from rtv.pitwall.provider import structured_call

    data = case["input"]
    output = case["answers"][-1]["output"] or {}
    prompt = (
        f"AGENT: {case['agent']}\n\n"
        f"STATE SLICE:\n{agent_input.as_text(data['state_slice'])}\n\n"
        f"EVENT:\n{agent_input.as_text(data['event'])}\n\n"
        "TOOL RESULTS:\n"
        + "\n".join(
            f"- {c['tool']}: {agent_input.as_text(c['result'])}" for c in case["tool_calls"]
        )
        + "\n\nCALL:\n"
        + json.dumps(output, separators=(",", ":"))
    )
    score = await structured_call(
        backend.provider,
        agent="eval_judge",
        model=backend.model,
        system=[{"type": "text", "text": JUDGE_SYSTEM}],
        prompt=prompt,
        output_model=PitwallJudgeScore,
        max_tokens=1024,
    )
    if score is None:
        return None
    return {"judge": backend.label, **score.model_dump()}


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline numbers for a live run."""
    answered = [r for r in rows if r.get("new")]
    judged = [j for r in answered for j in r.get("judges") or [] if j]
    out: dict[str, Any] = {
        "cases": len(rows),
        "answered": len(answered),
        "drifted": sum(1 for r in rows if r["spec_drift"]),
        "recorded_mean_score": _mean(r["recorded_score"] for r in rows),
        "new_mean_score": _mean(r["new_score"] for r in answered),
        "recorded_validator_failures": sum(1 for r in rows if not r["recorded_validator_ok"]),
        "new_validator_failures": sum(1 for r in answered if not r["new_validator_ok"]),
        "new_refusals": sum(1 for r in answered if r.get("refused")),
        "unrecorded_tool_calls": sum(len(r["unrecorded_tool_calls"]) for r in rows),
    }
    for label in sorted({j["judge"] for j in judged}):
        mine = [j for j in judged if j["judge"] == label]
        out[f"judge {label}"] = {
            key: _mean(j[key] for j in mine)
            for key in ("usefulness", "actionability", "grounding")
        } | {"scored": len(mine)}
    return out


def _mean(values) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 3) if values else None
