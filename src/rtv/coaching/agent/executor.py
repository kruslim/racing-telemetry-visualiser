"""In-process tool execution for the coach's ``tools`` node.

Executes the same registry the graph advertises against a ``FindingsProvider`` held by
closure. Argument-validation failures and below-provider raises (``KeyError`` for an unknown
session/lap or channel, ``FileNotFoundError`` for missing telemetry) both come back as
structured error *payloads* the model can read and recover from — never as exceptions that
crash the loop. The dict-with-``error`` return is the tool-error signal for every caller.
"""

from __future__ import annotations

from pydantic import ValidationError

from rtv.coaching.agent.provider import FindingsProvider
from rtv.coaching.agent.registry import TOOLS, TOOLS_BY_NAME
from rtv.coaching.agent.tool_schema import inline_schema_defs


def _invalid_arguments_payload(name: str, exc: ValidationError) -> dict:
    return {
        "error": "invalid_arguments",
        "tool": name,
        "message": f"Arguments did not validate against the schema for {name!r}.",
        "details": [
            {"field": ".".join(str(p) for p in err["loc"]), "problem": err["msg"]}
            for err in exc.errors()
        ],
        "hint": "Re-read the tool's input schema and supply arguments matching it exactly.",
    }


class CoachToolExecutor:
    """Bind the tool registry to one provider for the life of a coach run."""

    def __init__(self, provider: FindingsProvider) -> None:
        self._provider = provider

    def available_channels(self, session_id: str) -> list[str]:
        """Ground truth for the refusal path, straight from the provider. Returns an empty
        list if the session cannot be resolved (a refusal should still render, not crash)."""
        try:
            return self._provider.available_channels(session_id)
        except (KeyError, FileNotFoundError):
            return []

    def definitions(self) -> list[dict]:
        """Tool definitions in provider format, schemas verbatim from the Pydantic models so
        the ``Field(description=...)`` text reaches the model unchanged."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": inline_schema_defs(spec.input_model.model_json_schema()),
            }
            for spec in TOOLS
        ]

    def execute(self, name: str, arguments: dict) -> dict:
        """Run one tool call and return a JSON-serializable payload (success or error)."""
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            return {
                "error": "unknown_tool",
                "requested": name,
                "message": f"Unknown tool: {name!r}.",
                "hint": f"Valid tools: {', '.join(s.name for s in TOOLS)}.",
            }

        try:
            inp = spec.input_model.model_validate(arguments or {})
        except ValidationError as exc:
            return _invalid_arguments_payload(name, exc)

        try:
            return spec.invoke(self._provider, inp)
        except KeyError as exc:
            # Unknown session/lap or channel — the recovery info tells the model to refuse.
            return {
                "error": "not_found",
                "tool": name,
                "message": str(exc).strip("'\""),
                "hint": (
                    "This session, lap or channel is not available. Call list_sessions / "
                    "list_laps / list_available_channels to see what exists, then refuse if "
                    "the question cannot be answered."
                ),
            }
        except FileNotFoundError as exc:
            return {
                "error": "no_telemetry",
                "tool": name,
                "message": str(exc),
                "hint": (
                    "The requested laps have no usable telemetry. Choose different laps or "
                    "refuse if the data cannot support the question."
                ),
            }
