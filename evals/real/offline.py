"""Re-score recorded agent answers with today's deterministic checks. No model, no key.

For each case the recorded answer is put back through what judged it live and what
the evals judge it with now:

* **validator**: :func:`rtv.pitwall.validator.validate_output` against a fact set
  rebuilt exactly as the runtime builds it (state slice, event payload, then every
  tool result in call order), plus the agent's policy on the full state;
* **checks**: the role's ground-truth scorer from :mod:`evals.pitwall_checks`;
* **corners**: every corner the answer names, against the session's corner map
  (labels and mapped names, :func:`rtv.coaching.agent.validation.split_corner`);
* **validity**: the B1 air-time rule re-stamped from the run's snapshot, and the
  verdict it gives at the recorded air time.

The result of a whole set is a plain dict per case. ``baseline.json`` beside the set
holds the last accepted result, and :func:`diff` prints what changed, so a validator
or check change shows up in a PR as a readable diff rather than a silent shift.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from evals.pitwall_checks import check_for_agent
from evals.real.cases import BASELINE_FILE, load_case_set, set_dir
from rtv.coaching.agent.validation import name_key, split_corner
from rtv.pitwall import validity
from rtv.pitwall.agents import AGENT_REGISTRY
from rtv.pitwall.framework import EventRef, RadioMessage
from rtv.pitwall.validator import MAX_SPOKEN_WORDS, FactSet, validate_output
from rtv.racestate.models import RaceEvent, RaceState

SPECS = {spec.name: spec for spec in AGENT_REGISTRY}

#: Corner labels written in prose: "T7", "Turn 7".
_PROSE_LABEL_RE = re.compile(r"\b(?:T|Turn\s+)(\d{1,2})\b")

#: Wheel positions, which the vehicle engineer also calls corners.
_WHEELS = frozenset({"LF", "RF", "LR", "RR"})

#: Base fields of every agent answer; the rest is the role's structured payload.
_BASE = ("spoken_text", "detail_text", "priority")


def event_of(case: dict[str, Any]) -> RaceEvent:
    data = {k: v for k, v in case["input"]["event"].items() if k != "key"}
    return RaceEvent.model_validate(data)


def state_of(case: dict[str, Any]) -> RaceState:
    return RaceState.model_validate(case["state_full"])


def facts_of(case: dict[str, Any]) -> FactSet:
    """The fact set the runtime built for this run, from the recorded data."""
    data = case["input"]
    facts = FactSet()
    facts.add("race_state", data["state_slice"])
    facts.add("event", data["event"].get("payload") or {})
    from rtv.pitwall.agent_input import previous_facts

    facts.add("previous", previous_facts(data.get("previous") or []))
    for heard in data.get("radio") or []:
        if heard.get("agent") != data.get("agent"):
            facts.hear(heard["agent"], heard.get("spoken_text") or "")
    for call in case["tool_calls"]:
        if call["ok"]:
            facts.add(call["tool"], call["result"])
    return facts


def payload_of(output: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in output.items() if k not in _BASE}


def _check_kwargs(case: dict[str, Any]) -> dict[str, Any]:
    agent = case["agent"]
    calls = [c for c in case["tool_calls"] if c["ok"]]
    if agent == "vehicle_engineer":
        setups = [c["result"] for c in calls if c["tool"] == "get_setup_snapshot"]
        return {
            "setup_available": any(
                isinstance(r, dict) and r.get("available", True) is not False for r in setups
            )
        }
    if agent == "coach":
        details = [c["result"] for c in calls if c["tool"] == "get_corner_detail"]
        return {
            "telemetry_available": any(
                isinstance(r, dict)
                and isinstance(r.get("telemetry"), dict)
                and r["telemetry"].get("available", True) is not False
                for r in details
            )
        }
    return {}


def corner_issues(output: dict[str, Any], state: RaceState) -> list[str]:
    """Corners the answer names that the session's corner map does not have."""
    corners = {c.label: c for c in state.track.corners}
    issues: list[str] = []
    cited = output.get("corner")
    if isinstance(cited, str) and cited.strip().upper() in _WHEELS:
        cited = None  # the engineer's "corner" can be a wheel (RF), not a turn
    if cited:
        label, name = split_corner(cited)
        if label is not None and re.fullmatch(r"C\d+", label.upper()):
            if not state.track.provisional and corners:
                issues.append(f"corner {cited!r} is a provisional bucket but the map is built")
        elif label not in corners:
            issues.append(f"corner {cited!r} is not in the session's corner map")
        elif name is not None and name_key(name) != name_key(corners[label].name):
            truth = corners[label].name
            issues.append(
                f"corner {cited!r} names it {name!r}, but "
                + (f"{label} is {truth}" if truth else f"{label} has no mapped name")
            )
    text = f"{output.get('spoken_text') or ''} {output.get('detail_text') or ''}"
    for match in _PROSE_LABEL_RE.finditer(text):
        label = f"T{int(match.group(1))}"
        if label not in corners:
            issues.append(f"prose names {match.group(0)!r}, not in the session's corner map")
    return sorted(set(issues))


def _validator(case: dict[str, Any], answer: dict[str, Any], facts: FactSet,
               state: RaceState, event: RaceEvent) -> dict[str, Any]:
    output = answer["output"] or {}
    spec = SPECS.get(case["agent"])
    report = validate_output(
        output.get("spoken_text") or "",
        output.get("detail_text") or "",
        facts,
        payload=payload_of(output),
        prose_check=spec is not None and spec.prose_check,
        word_limit=spec.spoken_word_limit if spec is not None else MAX_SPOKEN_WORDS,
    )
    policy = None
    if spec is not None and spec.policy is not None:
        try:
            policy = spec.policy(spec.output_model.model_validate(output), state, event)
        except Exception as exc:  # a recorded answer the contract no longer accepts
            policy = f"contract: {type(exc).__name__}"
    ok = report.ok and policy is None
    recorded = answer["recorded"]
    return {
        "repaired": answer["repaired"],
        "ok": ok,
        "unsupported": list(report.unsupported),
        "contradictions": list(report.contradictions),
        "over_word_limit": report.over_word_limit,
        "policy": policy,
        "same_as_recorded": (
            ok == recorded["ok"]
            and list(report.unsupported) == recorded["unsupported"]
            and policy == recorded["policy"]
        ),
    }


def _message(case: dict[str, Any], output: dict[str, Any], event: RaceEvent) -> RadioMessage:
    return RadioMessage(
        agent=case["agent"],
        priority=output.get("priority") or "info",
        spoken_text=output.get("spoken_text") or "",
        detail_text=output.get("detail_text") or "",
        data=payload_of(output),
        event_ref=EventRef.of(event),
        session_time=event.session_time,
    )


_RULE_FIELDS = ("max_age_s",) + validity.RULE_FIELDS


def _validity(case: dict[str, Any], output: dict[str, Any], state: RaceState,
              event: RaceEvent) -> dict[str, Any] | None:
    radio = case.get("radio")
    air = case.get("air")
    spec = SPECS.get(case["agent"])
    if radio is None or spec is None or radio.get("refused"):
        return None
    message = _message(case, output, event)
    validity.stamp(
        message,
        spec,
        state,
        event,
        max_age_s=radio.get("max_age_s"),
        pit_entry_pct=_pit_entry_of(radio),
    )
    rule = {k: getattr(message, k) for k in _RULE_FIELDS if getattr(message, k) is not None}
    recorded_rule = {k: radio.get(k) for k in _RULE_FIELDS if radio.get(k) not in (None, False)}
    rule_cmp = {k: v for k, v in rule.items() if v is not False}
    out: dict[str, Any] = {"rule": rule_cmp, "rule_same_as_recorded": rule_cmp == recorded_rule}
    if air is not None and air.get("session_time") is not None:
        air_state = RaceState.model_validate(air["state"]) if air.get("state") else None
        verdict, reason, _ = validity.check(message, air_state, float(air["session_time"]))
        recorded = {"radio_aired": validity.FRESH, "radio_late": validity.LATE,
                    "radio_expired": validity.EXPIRED}[air["kind"]]
        out.update(verdict=verdict, reason=reason, recorded_verdict=recorded)
    return out


def _pit_entry_of(radio: dict[str, Any]) -> float | None:
    # The engine's pit entry is not on the message; a box call carries it as
    # ``invalid_if_lap_dist_past`` (validity.box_call_rules), the only rule that reads it.
    value = radio.get("invalid_if_lap_dist_past")
    return float(value) if value is not None else None


def score_case(case: dict[str, Any]) -> dict[str, Any]:
    """Today's deterministic verdicts on one recorded case."""
    state = state_of(case)
    event = event_of(case)
    facts = facts_of(case)
    answers = [_validator(case, a, facts, state, event) for a in case["answers"]]
    final = case["answers"][-1]["output"] or {}
    result = {
        "spoken_text": final.get("spoken_text") or "",
        "detail_text": final.get("detail_text") or "",
        "data": payload_of(final),
    }
    checks = check_for_agent(
        case["agent"], result, state, event, facts=facts, **_check_kwargs(case)
    )
    scored = {k: v for k, v in checks.items() if k.endswith("_ok")}
    return {
        "case_id": case["case_id"],
        "agent": case["agent"],
        "event_key": case["event_key"],
        "answers": answers,
        "checks": {**scored, "score": round(checks["score"], 3)},
        "check_issues": list(checks.get("issues") or []),
        "corners": corner_issues(final, state),
        "validity": _validity(case, final, state, event),
    }


def score_set(name: str | Path) -> list[dict[str, Any]]:
    return [score_case(case) for case in load_case_set(name)]


def summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline counts over a scored set."""
    answers = [a for r in results for a in r["answers"]]
    with_validity = [r["validity"] for r in results if r["validity"]]
    verdicts = [v for v in with_validity if "verdict" in v]
    return {
        "cases": len(results),
        "answers": len(answers),
        "validator_failures": sum(1 for a in answers if not a["ok"]),
        "validator_changed_vs_recorded": sum(1 for a in answers if not a["same_as_recorded"]),
        "final_answers_failing_checks": sum(1 for r in results if r["checks"]["score"] < 1),
        "mean_check_score": round(
            sum(r["checks"]["score"] for r in results) / len(results), 3
        ) if results else None,
        "corner_issues": sum(len(r["corners"]) for r in results),
        "validity_rules_changed": sum(
            1 for v in with_validity if not v["rule_same_as_recorded"]
        ),
        "validity_verdicts_changed": sum(
            1 for v in verdicts if v["verdict"] != v["recorded_verdict"]
        ),
    }


def baseline_path(name: str | Path) -> Path:
    return set_dir(name) / BASELINE_FILE


def to_baseline(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"summary": summary(results), "cases": results}


def write_baseline(name: str | Path, results: list[dict[str, Any]] | None = None) -> Path:
    results = score_set(name) if results is None else results
    path = baseline_path(name)
    path.write_text(json.dumps(to_baseline(results), indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def read_baseline(name: str | Path) -> dict[str, Any]:
    return json.loads(baseline_path(name).read_text(encoding="utf-8"))


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Readable lines for every case field that differs between two baselines."""
    lines: list[str] = []
    before = {c["case_id"]: c for c in old.get("cases", [])}
    after = {c["case_id"]: c for c in new.get("cases", [])}
    for case_id in sorted(set(before) | set(after)):
        a, b = before.get(case_id), after.get(case_id)
        if a is None or b is None:
            lines.append(f"{case_id}: {'added' if a is None else 'removed'}")
            continue
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                lines.append(
                    f"{case_id}.{key}: {json.dumps(a.get(key), sort_keys=True)} -> "
                    f"{json.dumps(b.get(key), sort_keys=True)}"
                )
    for key in sorted(set(old.get("summary", {})) | set(new.get("summary", {}))):
        x, y = old.get("summary", {}).get(key), new.get("summary", {}).get(key)
        if x != y:
            lines.append(f"summary.{key}: {x} -> {y}")
    return lines
