"""Cut agent runs out of a race journal into a committed eval case set.

One case is one agent run (one ``run_start`` .. ``run_end`` of one ``run_id``):

* ``input``: the ``run_start`` input record (:mod:`rtv.pitwall.agent_input`), with
  its ``system_prompt`` moved to the set's ``prompts.json`` (keyed by
  ``prompt_sha``), because every run of an agent repeats the same prompt;
* ``state_full``: the whole :class:`~rtv.racestate.models.RaceState` the run was
  given (journal schema 3);
* ``context_events``: the race events the run's tools could read (the bus history
  it was handed, rebuilt from the journal's ``race_event`` records);
* ``tool_calls``: every tool the run called, prefetched or asked for, with the
  whole result;
* ``answers``: one per ``validate`` record (first pass, then the repair if there
  was one): the model's structured answer and the verdict recorded at the time;
* ``run_end``, ``radio`` (the message as it was published) and ``air`` (what the
  radio decided when the channel reached it, plus the race state at that moment
  for the B1 validity check).

Only journals of schema 3 or later carry ``state_full`` and ``output_full``; older
runs are skipped (a schema-2 run has an input but no recorded answer to replay).

The set on disk is ``cases.jsonl`` (one case per line, in journal order) plus
``prompts.json`` and a ``source.json`` naming the journal it was cut from.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rtv.store import journal

#: Where the committed case sets live.
REAL_DIR = Path(__file__).resolve().parent

CASES_FILE = "cases.jsonl"
PROMPTS_FILE = "prompts.json"
SOURCE_FILE = "source.json"
BASELINE_FILE = "baseline.json"

#: Record kinds that end how a call went on the radio.
AIR_KINDS = ("radio_aired", "radio_late", "radio_expired")


def set_dir(name: str | Path) -> Path:
    """``spa_run`` -> ``evals/real/spa_run``; a path is used as given."""
    path = Path(name)
    if path.is_dir() or len(path.parts) > 1:
        return path
    return REAL_DIR / str(name)


def _slim_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """The race state at air time, reduced to what :func:`validity.check` reads."""
    if not state:
        return None
    standings = state.get("standings") or {}
    return {
        "session_time": state.get("session_time"),
        "stale": state.get("stale", False),
        "player": state.get("player") or {},
        "capabilities": state.get("capabilities") or {},
        "standings": {
            "cars": [
                {"idx": c.get("idx"), "on_pit_road": c.get("on_pit_road")}
                for c in standings.get("cars") or []
            ]
        },
    }


def extract_cases(path: Path | str) -> list[dict[str, Any]]:
    """Every agent run of a schema-3 journal as an eval case, in journal order."""
    path = Path(path)
    records = list(journal.iter_records(path))
    session_id = None
    for rec in records:
        if rec.get("kind") == "lifecycle" and (rec.get("data") or {}).get("phase") == "start":
            session_id = rec["data"].get("session_id")
            break

    states: list[tuple[float, dict[str, Any]]] = []
    race_events: list[dict[str, Any]] = []
    runs: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    by_message: dict[str, str] = {}

    for rec in records:
        kind = rec.get("kind")
        data = rec.get("data") or {}
        run_id = rec.get("run_id")
        if kind == "state" and rec.get("session_time") is not None:
            states.append((float(rec["session_time"]), data))
        elif kind == "race_event":
            race_events.append(data)
        elif kind == "run_start" and run_id:
            if "state_full" not in data or not isinstance(data.get("input"), dict):
                continue
            seen = len(race_events)
            n = int(data.get("context_events") or 0)
            runs[run_id] = {
                "run_id": run_id,
                "agent": rec.get("agent"),
                "event_key": rec.get("event_key"),
                "session_time": rec.get("session_time"),
                "input": data["input"],
                "state_full": data["state_full"],
                "context_events": race_events[max(0, seen - n) : seen] if n else [],
                "tool_calls": [],
                "answers": [],
                "run_end": None,
                "radio": None,
                "air": None,
            }
            order.append(run_id)
        elif run_id in runs and kind == "tool_call":
            runs[run_id]["tool_calls"].append(
                {
                    "tool": data.get("tool"),
                    "args": data.get("args") or {},
                    "ok": bool(data.get("ok")),
                    "prefetched": bool(data.get("prefetched")),
                    "result": data.get("result_full"),
                }
            )
        elif run_id in runs and kind == "validate":
            runs[run_id]["answers"].append(
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
        elif run_id in runs and kind == "run_end":
            runs[run_id]["run_end"] = {
                k: data.get(k)
                for k in ("outcome", "message_id", "priority", "tools_used", "turns", "latency_ms")
            }
            if data.get("message_id"):
                by_message[data["message_id"]] = run_id
        elif kind == "radio":
            owner = by_message.get(_message_id_of(rec, data))
            if owner and runs[owner]["radio"] is None:
                runs[owner]["radio"] = {
                    k: v for k, v in data.items() if k not in ("activity_seq", "audio_url")
                }
        elif kind in AIR_KINDS:
            owner = by_message.get(data.get("message_id"))
            if owner and runs[owner]["air"] is None:
                at = rec.get("session_time")
                runs[owner]["air"] = {
                    "kind": kind,
                    "session_time": at,
                    "late_by_s": data.get("late_by_s"),
                    "reason": data.get("reason"),
                    "state": _slim_state(_state_before(states, at)),
                }

    cases = []
    for run_id in order:
        case = runs[run_id]
        if not case["answers"]:
            continue  # errored before any answer: nothing to replay
        case["case_id"] = f"{run_id}-{case['event_key']}".replace(":", "-")
        case["session_id"] = session_id
        cases.append(case)
    return cases


def _message_id_of(rec: dict[str, Any], data: dict[str, Any]) -> str | None:
    return data.get("message_id") or rec.get("message_id")


def _state_before(states: list[tuple[float, dict[str, Any]]], t: Any) -> dict | None:
    if t is None:
        return None
    best = None
    for at, state in states:
        if at <= float(t) + 1e-6:
            best = state
        else:
            break
    return best


def write_case_set(
    cases: Iterable[dict[str, Any]], out: Path | str, *, source: dict[str, Any]
) -> Path:
    """Write ``cases`` as a case set in ``out`` (prompts deduplicated)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    prompts: dict[str, str] = {}
    lines = []
    for case in cases:
        case = json.loads(json.dumps(case))
        data = case["input"]
        prompts[data["prompt_sha"]] = data.pop("system_prompt")
        lines.append(json.dumps(case, sort_keys=True, separators=(",", ":")))
    (out / CASES_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / PROMPTS_FILE).write_text(
        json.dumps(prompts, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / SOURCE_FILE).write_text(
        json.dumps(source, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return out


def load_case_set(name: str | Path) -> list[dict[str, Any]]:
    """The cases of a set, each with its ``input.system_prompt`` restored."""
    root = set_dir(name)
    prompts = json.loads((root / PROMPTS_FILE).read_text(encoding="utf-8"))
    cases = []
    for line in (root / CASES_FILE).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        case["input"]["system_prompt"] = prompts[case["input"]["prompt_sha"]]
        cases.append(case)
    return cases
