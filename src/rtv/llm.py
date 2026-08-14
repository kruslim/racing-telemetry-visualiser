"""One place that decides which LLM backend this deployment talks to.

Every LLM layer in this repo -- the Layer-3 coach, the four pitwall agents, the
eval judges -- speaks the **Anthropic Messages wire format**: system blocks with
``cache_control``, ``tool_use`` / ``tool_result`` content blocks, and tool schemas
carrying ``input_schema``. That was never a portability problem waiting to happen,
because Kimi publishes an Anthropic-compatible endpoint. Pointing at it is a
base-URL and credential change, not a rewrite.

Exactly one thing does not port, and it is worth naming precisely rather than
discovering it at 200 km/h:

    ``client.messages.parse(output_format=SomePydanticModel)``

is an Anthropic SDK helper over their native structured-output feature. A
compatibility layer that faithfully implements ``/v1/messages`` and tool calling
need not implement it. So the provider carries a **tool-forced** structured-output
strategy as well (see :mod:`rtv.pitwall.provider`), and this module picks the
right default per backend: native for Anthropic, tool-forced for Kimi. Either can
be forced with ``RTV_LLM_STRUCTURED_OUTPUT``.

Nothing here reads a credential at import time, and no credential is ever logged.
An offline ``pytest`` run never reaches :func:`build_client`.
"""

from __future__ import annotations

import os
from typing import Any

from rtv.config import Settings, get_settings
from rtv.logging import get_logger

log = get_logger("llm")

ANTHROPIC = "anthropic"
KIMI = "kimi"

#: Credential env var per backend, when ``llm_api_key_env`` does not name one.
DEFAULT_KEY_ENV = {ANTHROPIC: "ANTHROPIC_API_KEY", KIMI: "KIMI_API_KEY"}

#: Structured-output strategy per backend. See the module docstring for why these
#: differ; ``RTV_LLM_STRUCTURED_OUTPUT`` overrides.
DEFAULT_STRUCTURED_OUTPUT = {ANTHROPIC: "native", KIMI: "tool"}


class LLMConfigError(RuntimeError):
    """Raised when a backend is selected but cannot be constructed.

    Deliberately distinct from "no key exported", which is a normal offline state
    the callers already degrade gracefully for.
    """


def provider_name(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    name = (s.llm_provider or ANTHROPIC).strip().lower()
    if name not in DEFAULT_KEY_ENV:
        raise LLMConfigError(
            f"RTV_LLM_PROVIDER={name!r} is not one of {sorted(DEFAULT_KEY_ENV)}."
        )
    return name


def key_env_var(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return s.llm_api_key_env.strip() or DEFAULT_KEY_ENV[provider_name(s)]


def api_key(settings: Settings | None = None) -> str | None:
    """The credential, or ``None``. Never logged, never returned in a payload."""
    return os.environ.get(key_env_var(settings)) or None


def base_url(settings: Settings | None = None) -> str | None:
    """Backend base URL, or ``None`` to let the SDK use its own default."""
    s = settings or get_settings()
    if s.llm_base_url.strip():
        return s.llm_base_url.strip()
    if provider_name(s) == KIMI:
        return s.llm_kimi_base_url.strip() or None
    return None


def structured_output_mode(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    mode = (s.llm_structured_output or "").strip().lower()
    if mode in ("native", "tool"):
        return mode
    if mode:
        raise LLMConfigError(
            f"RTV_LLM_STRUCTURED_OUTPUT={mode!r} must be 'native', 'tool' or empty."
        )
    return DEFAULT_STRUCTURED_OUTPUT[provider_name(s)]


def resolve_model(configured: str, *, tier: str, settings: Settings | None = None) -> str:
    """Map a configured model id onto the active backend.

    ``tier`` is ``"fast"`` or ``"reasoning"``. A model id that is already
    non-default and not a Claude id is returned untouched -- an operator who names
    an explicit model means it, whichever backend they named it for. This is what
    lets a Kimi deployment keep the existing per-agent override env vars working
    without a second set of them.
    """
    s = settings or get_settings()
    configured = (configured or "").strip()
    if provider_name(s) != KIMI:
        return configured
    if configured and not configured.startswith("claude-"):
        return configured
    return (
        s.llm_kimi_model_reasoning if tier == "reasoning" else s.llm_kimi_model_fast
    ).strip() or configured


def is_configured(settings: Settings | None = None) -> bool:
    """True when a credential is present for the selected backend."""
    return api_key(settings) is not None


def describe(settings: Settings | None = None) -> dict[str, Any]:
    """Status payload. Reports *whether* a credential exists, never its value."""
    s = settings or get_settings()
    return {
        "provider": provider_name(s),
        "base_url": base_url(s),
        "key_env": key_env_var(s),
        "key_present": is_configured(s),
        "structured_output": structured_output_mode(s),
    }


def build_client(settings: Settings | None = None) -> Any:
    """An ``AsyncAnthropic`` client pointed at the selected backend.

    Both backends are driven through the Anthropic SDK because both speak the
    Anthropic Messages format; only the base URL and credential differ. The import
    stays lazy so the ``ai`` extra remains optional, matching how DuckDB/PyArrow
    and pyirsdk are handled elsewhere.
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:  # pragma: no cover - depends on the ai extra
        raise LLMConfigError(
            "The 'anthropic' package is required for every LLM backend, including "
            "Kimi, because both speak the Anthropic Messages format. "
            "Install the ai extra."
        ) from exc

    s = settings or get_settings()
    key = api_key(s)
    if key is None:
        raise LLMConfigError(
            f"No credential: set {key_env_var(s)} for provider {provider_name(s)!r}."
        )
    kwargs: dict[str, Any] = {"api_key": key}
    url = base_url(s)
    if url:
        kwargs["base_url"] = url
    log.info(
        "LLM backend: provider=%s base_url=%s structured_output=%s",
        provider_name(s),
        url or "<sdk default>",
        structured_output_mode(s),
    )
    return AsyncAnthropic(**kwargs)
