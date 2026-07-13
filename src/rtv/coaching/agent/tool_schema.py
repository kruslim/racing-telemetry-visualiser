"""Make a Pydantic JSON schema safe to hand to a provider's tool-binding layer.

Pydantic v2 factors nested models into a top-level ``$defs`` block and references them
with ``$ref``. Some function-calling adapters do not follow that indirection and warn (or
silently flatten) on every call. ``inline_schema_defs`` produces an equivalent,
self-contained schema: every ``$ref`` is replaced by the definition it points at and
``$defs`` is dropped. Pure transform over a schema dict — no telemetry knowledge here.

Ported verbatim from the reference project's ``agent/tool_schema.py``; the coaching
contracts nest ``CoachClaim``/``CoachCitation`` so the graph needs this to bind them.
"""

from __future__ import annotations

from typing import Any


def inline_schema_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` with every ``$ref`` inlined and ``$defs`` removed.

    Sibling keys alongside a ``$ref`` (``description``/``default``) are preserved and win
    over the referenced definition. A ref already being expanded resolves to an empty
    schema rather than recursing forever (the coaching contracts are acyclic, so the guard
    is only ever a safety net).
    """
    defs: dict[str, Any] = schema.get("$defs", {})

    def resolve(node: Any, active: frozenset[str]) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = str(node["$ref"]).split("/")[-1]
                siblings = {k: resolve(v, active) for k, v in node.items() if k != "$ref"}
                target = defs.get(name)
                if target is None or name in active:
                    return siblings  # unknown or cyclic ref: keep what we can, drop the ref
                resolved = resolve(target, active | {name})
                return {**resolved, **siblings}
            return {k: resolve(v, active) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item, active) for item in node]
        return node

    return resolve({k: v for k, v in schema.items() if k != "$defs"}, frozenset())
