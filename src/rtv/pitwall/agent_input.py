"""An agent run's full model input, as data: recorded on ``run_start``, rebuilt from it.

The race journal (:mod:`rtv.store.journal`) keeps every agent run. For a run to be
re-executed later -- as an eval case against a new prompt or model, or to see what
the model was actually shown -- the journal must hold everything the first model
call was built from, not a summary of it. :func:`build_input` turns one run's
context into that record (the ``input`` field of the ``run_start`` activity
record); :func:`model_input` turns the record back into the exact ``system`` and
``messages`` the runtime sent.

Both directions go through the same two pure functions, :func:`system_blocks` and
:func:`instruction`, and the live runtime builds its first call *from the record*,
so a rebuilt input is byte-identical to the one sent by construction, not by a
copy kept in step by hand.

The record, version :data:`INPUT_VERSION`::

    {"v": 2,
     "agent": "strategist", "model": "kimi-k3", "max_tokens": 1500,
     "max_tool_iterations": 4, "provider": {"name": "kimi", "thinking": false},
     "system_prompt": "<role prompt + grounding rules>", "prompt_sha": "1a2b...",
     "state_slice": {...}, "event": {<RaceEvent.to_api()>},
     "radio": [{"message_id", "agent", "priority", "spoken_text"}, ...],
     "previous": [{"message_id", "subject", "lap", "session_time", "spoken_text",
                   "status", "late_by_s", "expired_reason"}, ...],
     "prefetched": [{"tool": "fuel_plan", "args": {...}, "result": {...}}, ...],
     "tools": ["fuel_plan", ...], "tools_sha": "...",
     "output_contract": "StrategistOutput", "output_schema_sha": "..."}

``radio`` is only the calls the model was shown (the last five); the ``run_start``
record's ``context_radio`` still lists every id the run was given. ``previous`` is
the agent's own last call on each of its last :data:`PREVIOUS_SUBJECTS` subjects, with
what the radio did with it (aired, aired late, dropped, still queued), so the agent
revises its earlier call instead of restating it (task D1). Version 1 records have no
``previous`` and rebuild exactly as before. Tool results the
model asks for *during* the run are on its ``tool_call`` records (``result_full``).

This module imports nothing from the runtime so the evals and the review can use it
without the agent layer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

#: Version of the ``run_start`` ``input`` record. Bump on any change to its shape
#: or to how :func:`model_input` reads it. 2 added ``previous``.
INPUT_VERSION = 2

#: Versions :func:`model_input` can rebuild.
READABLE_VERSIONS = (1, 2)

#: How many of an agent's own subjects its first message recalls (one line each).
PREVIOUS_SUBJECTS = 2

#: ``status`` of a recalled call: what the radio did with it.
AIRED, LATE, EXPIRED, QUEUED = "aired", "late", "expired", "queued"

#: Radio is a scarce channel; a message longer than this stops being radio.
SPOKEN_WORD_LIMIT = 25

#: Prefix the radio puts on a call that aired late (``rtv.pitwall.framework``
#: re-exports it; defined here so this module imports nothing from the runtime).
LATE_PREFIX = "Late call: "

#: How many recent radio calls an agent's first message shows.
RADIO_SHOWN = 5


def as_text(value: Any) -> str:
    """Compact JSON for the prompt (strings pass through unchanged)."""
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        return json.dumps(value, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


def jsonable(value: Any) -> Any:
    """``value`` as plain JSON data whose :func:`as_text` is unchanged.

    The record is written to a JSON-lines file and read back; normalising first
    means what the model is shown and what the journal holds are the same data.
    """
    if isinstance(value, str):
        return value
    return json.loads(as_text(value))


def sha(text: str) -> str:
    """Short content hash: enough to tell two prompt versions apart."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def system_blocks(system_prompt: str, state_slice: Any) -> list[dict[str, Any]]:
    """Stable role prompt first (cached), volatile state after it.

    Ordering matters: prompt caching is a prefix match, so the part that never
    changes has to come first or nothing caches (see the Layer-3b coach).
    """
    return [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "RACE STATE:\n" + as_text(state_slice)},
    ]


def instruction(
    event: Mapping[str, Any],
    radio: Sequence[Mapping[str, Any]] = (),
    prefetched: Sequence[Mapping[str, Any]] = (),
    previous: Sequence[Mapping[str, Any]] = (),
) -> str:
    """The run's first user message, from the event, the agent's own earlier calls,
    the radio and prefetched results."""
    lap = event.get("lap")
    parts = [
        f"TRIGGER: {event['key']} (severity {event['severity']}) at "
        f"session time {float(event['session_time']):.1f}s"
        + (f", lap {lap}" if lap is not None else "")
        + f".\nEvent payload: {as_text(event.get('payload'))}"
    ]
    if previous:
        parts.append(
            "YOUR EARLIER CALLS (revise or confirm them; do not restate one that still "
            "stands word for word, and say what changed if you change it):\n"
            + "\n".join("- " + previous_line(p) for p in previous)
        )
    if radio:
        recent = "\n".join(
            f"- [{m['agent']}/{m['priority']}] {m['spoken_text']}" for m in radio
        )
        parts.append("RECENT RADIO (do not repeat a call that still stands):\n" + recent)
    if prefetched:
        parts.append(
            "TOOL RESULTS (already called for you, as if you had called them):\n"
            + "\n".join(
                f"- {p['tool']}({as_text(p['args']) if p['args'] else ''}): "
                f"{as_text(p['result'])}"
                for p in prefetched
            )
        )
        parts.append(
            "If these and the race state settle it, answer now in the required "
            "structure; call another tool only for a figure that is not here. "
            f"Keep spoken_text under {SPOKEN_WORD_LIMIT} words."
        )
    else:
        parts.append(
            "Call the tools you need, then answer in the required structure. "
            "Keep spoken_text under "
            f"{SPOKEN_WORD_LIMIT} words."
        )
    return "\n\n".join(parts)


def previous_line(call: Mapping[str, Any]) -> str:
    """One recalled call as the agent is shown it.

    ``You said on lap 4: "Plan to box lap 13." (aired late by 9 s)``
    """
    lap = call.get("lap")
    when = f"on lap {lap}" if lap is not None else f"at {float(call['session_time']):.0f}s"
    status = call.get("status")
    if status == LATE:
        late_by = call.get("late_by_s")
        note = f"aired late by {late_by:.0f} s" if late_by is not None else "aired late"
    elif status == EXPIRED:
        reason = call.get("expired_reason")
        note = "never aired, dropped" + (f": {reason}" if reason else "")
    elif status == QUEUED:
        note = "not aired yet, still queued"
    else:
        note = "aired"
    return f'You said {when}: "{call["spoken_text"]}" ({note})'


def previous_call(message: Any, *, queued: bool = False) -> dict[str, Any]:
    """The ``previous`` entry for one of the agent's own ``RadioMessage`` s.

    ``queued`` = the message is still waiting for the channel. Otherwise the air-time
    fields the radio set (task B1) say whether it aired, aired late or was dropped.
    The spoken text is the agent's own words, without the late tag the radio added.
    """
    spoken = message.spoken_text
    if spoken.startswith(LATE_PREFIX):
        spoken = spoken[len(LATE_PREFIX) :]
    if message.expired:
        status = EXPIRED
    elif queued:
        status = QUEUED
    elif message.late:
        status = LATE
    else:
        status = AIRED
    return {
        "message_id": message.message_id,
        "subject": message.subject,
        "lap": message.event_ref.lap,
        "session_time": message.session_time,
        "spoken_text": spoken,
        "status": status,
        "late_by_s": message.late_by_s if status == LATE else None,
        "expired_reason": message.expired_reason if status == EXPIRED else None,
    }


def tools_sha(tools: Sequence[Any]) -> str:
    """Hash of the tool definitions offered (name, description, schema)."""
    defs = [t.to_api() if hasattr(t, "to_api") else t for t in tools]
    return sha(json.dumps(defs, sort_keys=True, separators=(",", ":"), default=str))


def output_schema_sha(output_model: type[BaseModel]) -> str:
    schema = output_model.model_json_schema()
    return sha(json.dumps(schema, sort_keys=True, separators=(",", ":"), default=str))


def build_input(
    spec: Any,
    *,
    system_prompt: str,
    state_slice: Any,
    event: Mapping[str, Any],
    radio_history: Sequence[Any] = (),
    prefetched: Sequence[tuple[str, Mapping[str, Any], Any]] = (),
    provider: Mapping[str, Any] | None = None,
    previous: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The ``run_start`` input record of one run of ``spec`` (an ``AgentSpec``).

    ``previous`` are :func:`previous_call` entries, oldest first; only the last
    :data:`PREVIOUS_SUBJECTS` are kept.
    """
    shown = list(radio_history)[-RADIO_SHOWN:] if RADIO_SHOWN else []
    return {
        "v": INPUT_VERSION,
        "agent": spec.name,
        "model": spec.model,
        "max_tokens": spec.max_tokens,
        "max_tool_iterations": spec.max_tool_iterations,
        "provider": dict(provider or {}),
        "system_prompt": system_prompt,
        "prompt_sha": sha(system_prompt),
        "state_slice": jsonable(state_slice),
        "event": jsonable(dict(event)),
        "radio": [
            {
                "message_id": m.message_id,
                "agent": m.agent,
                "priority": getattr(m.priority, "value", m.priority),
                "spoken_text": m.spoken_text,
            }
            for m in shown
        ],
        "previous": [
            jsonable(dict(p)) for p in list(previous)[-PREVIOUS_SUBJECTS:]
        ]
        if PREVIOUS_SUBJECTS
        else [],
        "prefetched": [
            {"tool": name, "args": jsonable(dict(args)), "result": jsonable(payload)}
            for name, args, payload in prefetched
        ],
        "tools": [t.name for t in spec.tools],
        "tools_sha": tools_sha(spec.tools),
        "output_contract": spec.output_model.__name__,
        "output_schema_sha": output_schema_sha(spec.output_model),
    }


def input_of(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The input record of a ``run_start`` journal record (or the input itself).

    ``None`` for a run recorded before the journal kept inputs (schema version 1).
    """
    if "system_prompt" in record and "event" in record:
        return dict(record)
    data = record.get("data") if "data" in record else record
    found = (data or {}).get("input")
    return dict(found) if isinstance(found, Mapping) else None


def model_input(record: Mapping[str, Any]) -> dict[str, Any]:
    """The first model call of a run, rebuilt from its ``run_start`` record.

    Returns ``system`` and ``messages`` exactly as the runtime passed them to the
    provider, plus the call settings (``model``, ``max_tokens``, ``tools`` by name,
    ``output_contract``). Raises ``ValueError`` for a run without a recorded input.
    """
    data = input_of(record)
    if data is None:
        raise ValueError("This run_start record has no input (journal schema version 1).")
    version = data.get("v")
    if version not in READABLE_VERSIONS:
        raise ValueError(f"Unsupported run input version {version!r}.")
    return {
        "agent": data["agent"],
        "model": data["model"],
        "max_tokens": data["max_tokens"],
        "tools": list(data["tools"]),
        "output_contract": data["output_contract"],
        "system": system_blocks(data["system_prompt"], data["state_slice"]),
        "messages": [
            {
                "role": "user",
                "content": instruction(
                    data["event"],
                    data["radio"],
                    data["prefetched"],
                    data.get("previous", ()),
                ),
            }
        ],
    }


def spec_drift(record: Mapping[str, Any], spec: Any, system_prompt: str) -> list[str]:
    """What differs between a recorded run and ``spec`` as it is now.

    ``system_prompt`` is the spec's current full system prompt. Empty list = the
    recorded run would be re-executed against the same prompt, tools and contract.
    """
    data = input_of(record) or {}
    drift = []
    if data.get("prompt_sha") != sha(system_prompt):
        drift.append("prompt")
    if data.get("tools_sha") != tools_sha(spec.tools):
        drift.append("tools")
    if data.get("output_schema_sha") != output_schema_sha(spec.output_model):
        drift.append("output_contract")
    if data.get("model") != spec.model:
        drift.append("model")
    return drift
