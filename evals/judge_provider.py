"""The eval judge's model backend: the one place a non-Kimi model is allowed.

The coach under test runs on Kimi Code. Grading Kimi with Kimi rewards whatever Kimi
already likes, so the judge runs on a different model family by default:

* ``gemini`` (default) -- Google Gemini through ``google-genai``, structured answer from
  its JSON-schema response mode. Key in ``GEMINI_API_KEY``.
* ``anthropic`` -- an Opus model through the Anthropic API, structured answer from the
  same forced respond tool :class:`rtv.pitwall.provider.KimiProvider` uses. Key in
  ``ANTHROPIC_API_KEY``; the model id must be set in ``RTV_JUDGE_MODEL``.
* ``kimi`` -- the old behaviour, Kimi grading Kimi, kept for comparison.

Chosen by ``RTV_JUDGE_PROVIDER`` and ``RTV_JUDGE_MODEL`` (environment or ``.env``).

Scope rule: this module lives in ``evals/`` and nothing under ``src/rtv/`` may import
it or ``google.genai`` (``tests/test_judge_provider.py`` enforces that). The app, the
agents and the coaches stay Kimi Code only. The settings are therefore defined here,
not in :mod:`rtv.config`, so the app's settings never learn the judge exists.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from rtv.logging import get_logger
from rtv.pitwall.provider import ProviderResponse

log = get_logger("evals.judge_provider")

JudgeProviderName = Literal["gemini", "anthropic", "kimi"]

#: Pinned rather than ``gemini-pro-latest`` so a judge score means the same thing from
#: one week to the next. Checked 2026-10-31: the alias served this model.
DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"

#: Gemini counts thinking tokens against ``max_output_tokens``. The judge's own answer
#: is a few hundred tokens, so the caller's cap is raised by this much to leave room to
#: think instead of truncating the JSON mid-object.
GEMINI_THINKING_HEADROOM = 8192


class JudgeConfigError(RuntimeError):
    """The judge backend is not usable: unknown provider, missing key or package."""


class JudgeSettings(BaseSettings):
    """``RTV_JUDGE_*`` settings plus the two non-Kimi keys. Keys are never logged."""

    model_config = SettingsConfigDict(
        env_prefix="RTV_JUDGE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    provider: JudgeProviderName = "gemini"
    #: Empty means the provider's default (Gemini: :data:`DEFAULT_GEMINI_MODEL`, Kimi:
    #: the reasoning tier). The anthropic provider has no default.
    model: str = ""
    gemini_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GEMINI_API_KEY")
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY")
    )


def _text_of(system: list[dict[str, Any]]) -> str:
    return "\n\n".join(b.get("text", "") for b in system if b.get("type", "text") == "text")


def _gemini_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages-format turns as Gemini contents. The judge only ever sends text."""
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m["content"]
        if not isinstance(content, str):
            content = "".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            )
        role = "model" if m["role"] == "assistant" else "user"
        out.append({"role": role, "parts": [{"text": content}]})
    return out


def _gemini_usage(response: Any) -> dict[str, int] | None:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return None
    raw = {
        "input_tokens": getattr(meta, "prompt_token_count", None),
        "output_tokens": (getattr(meta, "candidates_token_count", None) or 0)
        + (getattr(meta, "thoughts_token_count", None) or 0),
        "cache_read_input_tokens": getattr(meta, "cached_content_token_count", None),
    }
    clean = {k: int(v) for k, v in raw.items() if isinstance(v, int)}
    return clean or None


class GeminiProvider:
    """Gemini behind the :class:`rtv.pitwall.provider.LLMProvider` interface, judge only.

    The structured answer comes from the JSON-schema response mode
    (``response_json_schema``), validated against ``output_model``; an invalid answer is
    ``output=None``, which :func:`rtv.pitwall.provider.structured_call` retries once.
    There is no tool loop: the judge asks one question, so tools are refused.
    """

    def __init__(self, client: Any | None = None, *, api_key: str | None = None) -> None:
        if client is None:
            if not api_key:
                raise JudgeConfigError("No Gemini key: set GEMINI_API_KEY.")
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - depends on the judge extra
                raise JudgeConfigError(
                    "The Gemini judge needs google-genai: install the 'judge' extra."
                ) from exc
            client = genai.Client(api_key=api_key)
        self._client = client

    async def complete(
        self,
        *,
        agent: str,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: Sequence[Any],
        output_model: type[BaseModel],
        max_tokens: int,
    ) -> ProviderResponse:
        if tools:
            raise ValueError("GeminiProvider is judge-only and does not run a tool loop")
        config = {
            "system_instruction": _text_of(system),
            "response_mime_type": "application/json",
            "response_json_schema": output_model.model_json_schema(),
            "max_output_tokens": max_tokens + GEMINI_THINKING_HEADROOM,
        }
        response = await self._client.aio.models.generate_content(
            model=model, contents=_gemini_contents(messages), config=config
        )
        from rtv.llm import USAGE

        usage = _gemini_usage(response)
        USAGE.record(agent, model, usage)
        text = getattr(response, "text", None) or ""
        parsed: BaseModel | None = None
        try:
            parsed = output_model.model_validate(json.loads(text))
        except Exception:
            log.exception(
                "%s: Gemini returned an invalid %s payload", agent, output_model.__name__
            )
        return ProviderResponse(output=parsed, text=text, usage=usage)


@dataclass(frozen=True)
class JudgeBackend:
    provider: Any
    model: str
    name: JudgeProviderName

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"


def judge_backend(
    provider: str | None = None,
    model: str | None = None,
    *,
    settings: JudgeSettings | None = None,
) -> JudgeBackend:
    """The judge's provider and model id. Arguments override ``RTV_JUDGE_*``."""
    s = settings or JudgeSettings()
    name = (provider or s.provider).strip().lower()
    chosen = (model if model is not None else s.model).strip()
    if name == "gemini":
        key = s.gemini_api_key.get_secret_value() if s.gemini_api_key else None
        return JudgeBackend(GeminiProvider(api_key=key), chosen or DEFAULT_GEMINI_MODEL, "gemini")
    if name == "anthropic":
        if not chosen:
            raise JudgeConfigError("The anthropic judge needs RTV_JUDGE_MODEL (an Opus model id).")
        key = s.anthropic_api_key.get_secret_value() if s.anthropic_api_key else None
        if not key:
            raise JudgeConfigError("No Anthropic key: set ANTHROPIC_API_KEY.")
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - depends on the ai extra
            raise JudgeConfigError("The anthropic judge needs the 'ai' extra.") from exc
        from rtv.pitwall.provider import KimiProvider

        # The forced respond-tool contract is plain Messages API, so the Kimi provider
        # class works unchanged against Anthropic's own endpoint.
        return JudgeBackend(KimiProvider(AsyncAnthropic(api_key=key)), chosen, "anthropic")
    if name == "kimi":
        from rtv import llm
        from rtv.pitwall.provider import KimiProvider

        return JudgeBackend(KimiProvider(), chosen or llm.model("reasoning"), "kimi")
    raise JudgeConfigError(f"Unknown RTV_JUDGE_PROVIDER {name!r}: use gemini, anthropic or kimi.")
