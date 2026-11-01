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

    {"v": 1,
     "agent": "strategist", "model": "kimi-k3", "max_tokens": 1500,
     "max_tool_iterations": 4, "provider": {"name": "kimi", "thinking": false},
     "system_prompt": "<role prompt + grounding rules>", "prompt_sha": "1a2b...",
     "state_slice": {...}, "event": {<RaceEvent.to_api()>},
     "radio": [{"message_id", "agent", "priority", "spoken_text"}, ...],
     "previous": [{"message_id", "subject", "lap", "session_time", "spoken_text",
                   "status", "late_by_s", "expired_reason"}, ...],   # only when any
     "prefetched": [{"tool": "fuel_plan", "args": {...}, "result": {...}}, ...],
     "tools": ["fuel_plan", ...], "tools_sha": "...",
     "output_contract": "StrategistOutput", "output_schema_sha": "..."}

``radio`` is only the calls the model was shown (the last five); the ``run_start``
record's ``context_radio`` still lists every id the run was given. ``previous`` is
the agent's own last call on each of its last :data:`PREVIOUS_SUBJECTS` subjects, with
what the radio did with it (aired, aired late, dropped, still queued), so the agent
revises its earlier call instead of restating it (task D1). It is left out when the
agent has no earlier call, so such a run's record is unchanged. Tool results the
model asks for *during* the run are on its ``tool_call`` records (``result_full``).

This module imports nothing from the runtime so the evals and the review can use it
without the agent layer.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

#: Version of the ``run_start`` ``input`` record. Bump on any change to its shape
#: or to how :func:`model_input` reads it. Optional keys that are left out when
#: empty (``superseded``, ``previous``) do not bump it: older records rebuild unchanged.
INPUT_VERSION = 1

#: How many of an agent's own subjects its first message recalls (one line each).
PREVIOUS_SUBJECTS = 2

#: ``status`` of a recalled call: what the radio did with it.
AIRED, LATE, EXPIRED, SUPERSEDED, QUEUED = "aired", "late", "expired", "superseded", "queued"

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


#: Decimals an agent is shown. Raw telemetry floats print with 15-17 digits
#: (55.096466064453125 C); four decimals keep every derived figure the engine already
#: rounds (17.409 L, 0.4801 of a lap) exact and cut those digits from the prompt.
SHOWN_DECIMALS = 4


def compact(value: Any, decimals: int = SHOWN_DECIMALS) -> Any:
    """``value`` as JSON data with every float rounded to ``decimals``.

    Applied to what an agent is shown (its state slice, the event payload, each tool
    result) *before* the fact set reads it, so the figures the validator holds are
    exactly the figures the model saw.
    """
    data = jsonable(value)

    def walk(item: Any) -> Any:
        if isinstance(item, float):
            rounded = round(item, decimals)
            return rounded if math.isfinite(rounded) else item
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        return item

    return walk(data)


def sha(text: str) -> str:
    """Short content hash: enough to tell two prompt versions apart."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


#: Shown (after the race state) only when the slice carries the director's
#: ``injected`` overlay, i.e. only in an eval rehearsal (``rtv.director``).
INJECTED_RULE = """\
SCRIPTED CONDITIONS: RACE STATE.injected lists conditions a race director has \
scripted for this rehearsal (kind, payload, director_id, since). Treat each as in \
force even where the telemetry fields say otherwise; the telemetry is not \
rewritten. An injected full-course yellow is a caution. An injected regulation \
binds: a mandatory_pit_stop requires a stop between its applies_from_lap and \
applies_to_lap even when the fuel reaches the flag. An injected weather step is \
the only basis for saying rain is coming: quote its figures from \
race_state.injected, and never turn a temperature trend into a forecast."""


def system_blocks(system_prompt: str, state_slice: Any) -> list[dict[str, Any]]:
    """Stable role prompt first (cached), volatile state after it.

    Ordering matters: prompt caching is a prefix match, so the part that never
    changes has to come first or nothing caches (see the Layer-3b coach). A slice
    with an ``injected`` overlay (eval rehearsals only) adds :data:`INJECTED_RULE`.
    """
    blocks = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "RACE STATE:\n" + as_text(state_slice)},
    ]
    if isinstance(state_slice, Mapping) and state_slice.get("injected"):
        blocks.append({"type": "text", "text": INJECTED_RULE})
    return blocks


def instruction(
    event: Mapping[str, Any],
    radio: Sequence[Mapping[str, Any]] = (),
    prefetched: Sequence[Mapping[str, Any]] = (),
    superseded: Sequence[Mapping[str, Any]] = (),
    previous: Sequence[Mapping[str, Any]] = (),
    *,
    word_limit: int = SPOKEN_WORD_LIMIT,
    tools_offered: bool = True,
) -> str:
    """The run's first user message, from the event, the agent's own earlier calls,
    the radio and prefetched results.

    ``superseded`` (at most one entry) is this agent's last call that another
    agent's call kept off the air: one line, so the agent does not repeat it.
    ``previous`` are its own last calls per subject (:func:`previous_line`).
    """
    lap = event.get("lap")
    parts = [
        f"TRIGGER: {event['key']} (severity {event['severity']}) at "
        f"session time {float(event['session_time']):.1f}s"
        + (f", lap {lap}" if lap is not None else "")
        + f".\nEvent payload: {as_text(event.get('payload'))}"
    ]
    payload = event.get("payload") or {}
    if event.get("event_type") == "driver_question" and isinstance(payload, Mapping):
        # The driver asked; the run exists to answer them, not to make a call.
        parts.append(
            f'THE DRIVER ASKED: "{payload.get("text", "")}"\n'
            "Answer that question in spoken_text, in your own role, as you would on the "
            "radio. Use only the race state and tool results; if they cannot answer it, "
            "say so plainly and never guess a figure."
        )
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
    for note in list(superseded)[-1:]:
        parts.append(superseded_line(note))
    if not tools_offered:
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
            "Answer now in the required structure, from the race state and these "
            f"results; there are no more tools. Keep spoken_text under {word_limit} words."
        )
    elif prefetched:
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
            f"Keep spoken_text under {word_limit} words."
        )
    else:
        parts.append(
            "Call the tools you need, then answer in the required structure. "
            "Keep spoken_text under "
            f"{word_limit} words."
        )
    return "\n\n".join(parts)


def superseded_line(note: Mapping[str, Any]) -> str:
    """The one line an agent is shown about its call that did not air."""
    return (
        f'NOT AIRED: your call "{note.get("spoken_text")}" was dropped, {note.get("reason")}: '
        f'"{note.get("by_spoken_text")}". Do not repeat it unless the situation has changed.'
    )


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
    elif status == SUPERSEDED:
        reason = call.get("expired_reason")
        note = "never aired, replaced" + (f": {reason}" if reason else "")
    elif status == QUEUED:
        note = "not aired yet, still queued"
    else:
        note = "aired"
    return f'You said {when}: "{call["spoken_text"]}" ({note})'


def previous_facts(previous: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The figures :func:`previous_line` shows about the agent's own earlier calls.

    The lap it said it on and how late it aired are radio facts the agent may repeat
    ("my lap-4 call aired 41 s late"); the words it said are not, so they are left out.
    """
    out: list[dict[str, Any]] = []
    for call in list(previous)[-PREVIOUS_SUBJECTS:] if PREVIOUS_SUBJECTS else []:
        row = {"lap": call.get("lap")}
        if call.get("status") == LATE and call.get("late_by_s") is not None:
            row["late_by_s"] = round(float(call["late_by_s"]))
        out.append(row)
    return out


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
    elif getattr(message, "superseded", False):
        status = SUPERSEDED
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
        # Why it never aired: the radio's expiry reason, or why another call replaced it.
        "expired_reason": (
            message.expired_reason
            if status == EXPIRED
            else getattr(message, "superseded_reason", None)
            if status == SUPERSEDED
            else None
        ),
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
    superseded: Sequence[Mapping[str, Any]] = (),
    previous: Sequence[Mapping[str, Any]] = (),
    max_tool_iterations: int | None = None,
) -> dict[str, Any]:
    """The ``run_start`` input record of one run of ``spec`` (an ``AgentSpec``).

    ``superseded`` and ``previous`` (:func:`previous_call` entries, oldest first, the
    last :data:`PREVIOUS_SUBJECTS` kept) are recorded only when there are any, so a run
    without them keeps the record (and the model input) it always had.
    """
    kept = list(previous)[-PREVIOUS_SUBJECTS:] if PREVIOUS_SUBJECTS else []
    # The agent's own call that YOUR EARLIER CALLS already quotes is not shown twice.
    recalled = {p.get("message_id") for p in kept}
    shown = [
        m
        for m in (list(radio_history)[-RADIO_SHOWN:] if RADIO_SHOWN else [])
        if m.message_id not in recalled
    ]
    record = {
        "v": INPUT_VERSION,
        "agent": spec.name,
        "model": spec.model,
        "max_tokens": spec.max_tokens,
        "max_tool_iterations": (
            spec.max_tool_iterations if max_tool_iterations is None else max_tool_iterations
        ),
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
        "prefetched": [
            {"tool": name, "args": jsonable(dict(args)), "result": jsonable(payload)}
            for name, args, payload in prefetched
        ],
        "tools": [t.name for t in spec.tools],
        "tools_sha": tools_sha(spec.tools),
        "output_contract": spec.output_model.__name__,
        "output_schema_sha": output_schema_sha(spec.output_model),
    }
    limit = getattr(spec, "spoken_word_limit", SPOKEN_WORD_LIMIT)
    if limit != SPOKEN_WORD_LIMIT:
        # Recorded only when it is not the default, so other runs keep their record.
        record["spoken_word_limit"] = limit
    if superseded:
        record["superseded"] = [jsonable(dict(n)) for n in list(superseded)[-1:]]
    if kept:
        record["previous"] = [jsonable(dict(p)) for p in kept]
    return record


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
    if version != INPUT_VERSION:
        raise ValueError(f"Unsupported run input version {version!r}.")
    tools_offered = data.get("max_tool_iterations", 1) != 0
    return {
        "agent": data["agent"],
        "model": data["model"],
        "max_tokens": data["max_tokens"],
        "tools": list(data["tools"]) if tools_offered else [],
        "output_contract": data["output_contract"],
        "system": system_blocks(data["system_prompt"], data["state_slice"]),
        "messages": [
            {
                "role": "user",
                "content": instruction(
                    data["event"],
                    data["radio"],
                    data["prefetched"],
                    data.get("superseded") or (),
                    data.get("previous") or (),
                    word_limit=data.get("spoken_word_limit", SPOKEN_WORD_LIMIT),
                    tools_offered=tools_offered,
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
