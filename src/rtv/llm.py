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


def key_env_var(settings: Settings | None = None) -> str:
    """The env var the credential is read from.

    An explicit ``llm_api_key_env`` always wins. Otherwise ``KIMI_CODE_API_KEY``,
    unless only a fallback name is set.
    """
    s = settings or get_settings()
    if s.llm_api_key_env.strip():
        return s.llm_api_key_env.strip()
    for candidate in (DEFAULT_KEY_ENV, *FALLBACK_KEY_ENVS):
        if os.environ.get(candidate):
            return candidate
    return DEFAULT_KEY_ENV


def api_key(settings: Settings | None = None) -> str | None:
    """The credential, or ``None``. Never logged, never returned in a payload."""
    return os.environ.get(key_env_var(settings)) or None


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
