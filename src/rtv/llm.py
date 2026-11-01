"""The one place that knows how this deployment reaches its LLM: Kimi Code.

Every LLM layer in this repo -- the Layer-3 coach, the orchestrator, the four
pitwall agents, the eval judge -- talks to Kimi's Anthropic-compatible endpoint
(``https://api.kimi.com/coding`` on the Kimi Code subscription). That endpoint
speaks the Anthropic **Messages wire format**, so the ``anthropic`` SDK (and
``langchain-anthropic`` for the LangGraph coach) are the transport; nothing here
calls Anthropic itself.

A structured answer is always obtained by forcing a single "respond" tool and
validating its input (see :mod:`rtv.pitwall.provider`): the compatibility layer
implements tool calling but not the SDK's native ``messages.parse(output_format=)``.

Nothing here reads a credential at import time, and no credential is ever logged.
An offline ``pytest`` run never reaches :func:`build_client`.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any

from rtv.config import Settings, get_settings
from rtv.logging import get_logger

log = get_logger("llm")

PROVIDER = "kimi"

#: The Kimi Code (coding subscription) key, which is what the default
#: ``llm_kimi_base_url`` accepts; a Moonshot developer-API key is not valid there.
DEFAULT_KEY_ENV = "KIMI_CODE_API_KEY"

#: Older credential names still honoured, after the default, so an existing .env
#: that predates the rename keeps working.
FALLBACK_KEY_ENVS = ("KIMI_API_KEY",)


class LLMConfigError(RuntimeError):
    """Raised when the client cannot be constructed.

    Deliberately distinct from "no key exported", which is a normal offline state
    the callers already degrade gracefully for.
    """


def _from_env_file(name: str) -> str | None:
    """``name`` from the settings' ``.env`` file, when the process env lacks it.

    pydantic-settings loads only the ``RTV_*`` fields from ``.env``; the key has a
    fixed, unprefixed name, so a key kept in ``.env`` was silently ignored. The
    file is read only for this one name, and the value is never logged.
    """
    env_file = Settings.model_config.get("env_file")
    if not env_file:
        return None
    try:
        from dotenv import dotenv_values  # a pydantic-settings dependency
    except ImportError:  # pragma: no cover
        return None
    for path in (env_file,) if isinstance(env_file, str | os.PathLike) else env_file:
        if os.path.isfile(path):
            value = dotenv_values(path).get(name)
            if value:
                return value
    return None


#: Kept so the offline suite, which stubs ``_from_env_file``, can still test it.
_from_env_file_real = _from_env_file


def _lookup(name: str) -> str | None:
    """The process environment first (it wins, as for every setting), then ``.env``."""
    return os.environ.get(name) or _from_env_file(name)


def key_env_var(settings: Settings | None = None) -> str:
    """The env var the credential is read from.

    An explicit ``llm_api_key_env`` always wins. Otherwise ``KIMI_CODE_API_KEY``,
    unless only a fallback name is set.
    """
    s = settings or get_settings()
    if s.llm_api_key_env.strip():
        return s.llm_api_key_env.strip()
    for candidate in (DEFAULT_KEY_ENV, *FALLBACK_KEY_ENVS):
        if _lookup(candidate):
            return candidate
    return DEFAULT_KEY_ENV


def api_key(settings: Settings | None = None) -> str | None:
    """The credential, or ``None``. Never logged, never returned in a payload."""
    return _lookup(key_env_var(settings)) or None


def base_url(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return s.llm_base_url.strip() or s.llm_kimi_base_url.strip()


def model(tier: str, override: str = "", settings: Settings | None = None) -> str:
    """The model id for a ``"fast"`` or ``"reasoning"`` call; ``override`` wins."""
    if override.strip():
        return override.strip()
    s = settings or get_settings()
    return (s.llm_kimi_model_reasoning if tier == "reasoning" else s.llm_kimi_model_fast).strip()


def is_configured(settings: Settings | None = None) -> bool:
    """True when the credential is present."""
    return api_key(settings) is not None


def describe(settings: Settings | None = None) -> dict[str, Any]:
    """Status payload. Reports *whether* a credential exists, never its value."""
    s = settings or get_settings()
    return {
        "provider": PROVIDER,
        "base_url": base_url(s),
        "key_env": key_env_var(s),
        "key_present": is_configured(s),
        "model_reasoning": model("reasoning", settings=s),
        "model_fast": model("fast", settings=s),
    }


def build_client(settings: Settings | None = None) -> Any:
    """An ``AsyncAnthropic`` SDK client pointed at the Kimi Code endpoint.

    The import stays lazy so the ``ai`` extra remains optional, matching how
    DuckDB/PyArrow and pyirsdk are handled elsewhere.
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:  # pragma: no cover - depends on the ai extra
        raise LLMConfigError(
            "The 'anthropic' SDK package is the transport for Kimi's "
            "Anthropic-compatible endpoint. Install the ai extra."
        ) from exc

    s = settings or get_settings()
    key = api_key(s)
    if key is None:
        raise LLMConfigError(f"No credential: set {key_env_var(s)} to your Kimi Code key.")
    url = base_url(s)
    log.info("LLM backend: kimi at %s", url)
    return AsyncAnthropic(api_key=key, base_url=url)


def image_block(data: bytes, media_type: str = "image/jpeg") -> dict[str, Any]:
    """An Anthropic-format base64 image content block, as the Kimi Code endpoint takes it.

    Verified on kimi-k3 (2026-10-30): base64 works; image URLs are untested. Used by
    :mod:`rtv.vision` so vision calls go through the same client as every other layer.
    """
    import base64

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


# --------------------------------------------------------------------------
# token usage
# --------------------------------------------------------------------------
#: The usage fields the Messages format reports, in the order they are logged.
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def usage_of(response: Any) -> dict[str, int] | None:
    """The token counts on a Messages response (or its ``usage``), else ``None``.

    Tolerant of an SDK object, a plain dict and a LangChain ``usage_metadata``
    dict, because each LLM layer reaches Kimi through a different client.
    """
    usage = getattr(response, "usage", response)
    if usage is None:
        return None
    if not isinstance(usage, dict):
        usage = {f: getattr(usage, f, None) for f in USAGE_FIELDS}
    if "input_tokens" not in usage and "prompt_tokens" in usage:  # pragma: no cover
        usage = {"input_tokens": usage.get("prompt_tokens"),
                 "output_tokens": usage.get("completion_tokens")}
    details = usage.get("input_token_details") or {}
    out = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", details.get("cache_read")),
        "cache_creation_input_tokens": usage.get(
            "cache_creation_input_tokens", details.get("cache_creation")
        ),
    }
    clean = {k: int(v) for k, v in out.items() if isinstance(v, int | float)}
    return clean or None


class UsageLedger:
    """Process-wide token totals per LLM layer, so the Kimi quota is visible.

    Every call path records here: the pitwall agents and the post-session layers
    through :class:`rtv.pitwall.provider.KimiProvider`, the coach chat through
    :func:`langchain_usage_callback`. Nothing here sees prompt text or a key.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._layers: dict[str, dict[str, int]] = {}

    def record(self, layer: str, model: str, usage: dict[str, int] | None) -> None:
        with self._lock:
            row = self._layers.setdefault(layer, {"calls": 0, **dict.fromkeys(USAGE_FIELDS, 0)})
            row["calls"] += 1
            for key, value in (usage or {}).items():
                row[key] = row.get(key, 0) + value
        if usage:
            log.info(
                "LLM usage %s on %s: %d in, %d out, %d cache read",
                layer, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                usage.get("cache_read_input_tokens", 0),
            )
        else:
            log.info("LLM usage %s on %s: the response carried no usage", layer, model)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            layers = {name: dict(row) for name, row in sorted(self._layers.items())}
        total = {"calls": 0, **dict.fromkeys(USAGE_FIELDS, 0)}
        for row in layers.values():
            for key, value in row.items():
                total[key] = total.get(key, 0) + value
        return {"since": "process start", "total": total, "layers": layers}

    def reset(self) -> None:
        with self._lock:
            self._layers.clear()


#: Kimi's chat-template control tokens, as they appear when the backend fails to parse
#: a tool call and hands its raw markup back inside an argument: ``<|close|>``,
#: ``<|sep|>``, ``<|oopen|>argument key=...``, ``<|diagnosis<|string|>``, ``<|/call>``.
#: Seen in the orchestrator's ``corner`` field on the A2 coach eval (2 of 5 cases).
KIMI_MARKUP_RE = re.compile(r"<\|/?[A-Za-z_][A-Za-z0-9_]*")


def find_markup(value: Any, path: str = "") -> str | None:
    """Where Kimi tool-call markup sits in ``value`` (a tool input or text), else ``None``.

    Walks dicts and lists, so the answer names the field (``corner``, ``cues[1]``) plus
    a short excerpt of the token. A payload that carries any is a malformed call: its
    fields hold fragments of other fields, never content to show a driver.
    """
    if isinstance(value, str):
        m = KIMI_MARKUP_RE.search(value)
        if m is None:
            return None
        return f"{path or 'text'}: {value[m.start():m.start() + 40]!r}"
    if isinstance(value, dict):
        for key, item in value.items():
            found = find_markup(item, f"{path}.{key}" if path else str(key))
            if found:
                return found
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            found = find_markup(item, f"{path}[{i}]")
            if found:
                return found
    return None


#: The one ledger this process keeps.
USAGE = UsageLedger()


def record_usage(layer: str, model: str, response: Any) -> dict[str, int] | None:
    """Record one call's tokens against ``layer`` and return the counts."""
    usage = usage_of(response)
    USAGE.record(layer, model, usage)
    return usage


def langchain_usage_callback(layer: str) -> Any:
    """A LangChain callback that records each chat-model call in :data:`USAGE`."""
    from langchain_core.callbacks import BaseCallbackHandler  # lazy: optional extra

    class _UsageCallback(BaseCallbackHandler):
        def on_llm_end(self, response: Any, **kwargs: Any) -> None:
            for generations in getattr(response, "generations", None) or ():
                for gen in generations:
                    message = getattr(gen, "message", None)
                    meta = getattr(message, "usage_metadata", None)
                    model_name = (getattr(message, "response_metadata", None) or {}).get(
                        "model", ""
                    )
                    record_usage(layer, model_name, meta)

    return _UsageCallback()
