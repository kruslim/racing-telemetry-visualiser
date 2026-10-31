"""The eval judge's backend: Gemini by default, and never reachable from the app.

Every test drives a fake client: no key, no network.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from evals.judge_provider import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_THINKING_HEADROOM,
    GeminiProvider,
    JudgeConfigError,
    JudgeSettings,
    judge_backend,
)
from evals.judges import JUDGE_SYSTEM, JudgeScore, judge
from evals.run import judge_table

ROOT = Path(__file__).resolve().parents[1]

GOOD = {"clarity": 4, "actionability": 3, "grounding": 5, "comment": "Specific to T5."}


class _FakeModels:
    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls: list[dict] = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(
            prompt_token_count=120,
            candidates_token_count=40,
            thoughts_token_count=300,
            cached_content_token_count=None,
        )
        return SimpleNamespace(text=self._texts.pop(0), usage_metadata=usage)


def _fake_client(*texts: str):
    models = _FakeModels(list(texts))
    return SimpleNamespace(aio=SimpleNamespace(models=models)), models


def _settings(**kw) -> JudgeSettings:
    # _env_file=None: a developer's real .env must never reach a test.
    return JudgeSettings(_env_file=None, **kw)


@pytest.mark.asyncio
async def test_gemini_judge_asks_for_the_score_schema_and_parses_it():
    client, models = _fake_client(json.dumps(GOOD))
    out = await judge(
        {"plan": "brake later into T5", "advices": []},
        {"corners": [{"id": 5}]},
        GeminiProvider(client),
        model="gemini-test",
    )
    assert out == JudgeScore(**GOOD)
    sent = models.calls[0]
    assert sent["model"] == "gemini-test"
    config = sent["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_json_schema"] == JudgeScore.model_json_schema()
    assert config["system_instruction"] == JUDGE_SYSTEM
    assert config["max_output_tokens"] == 1024 + GEMINI_THINKING_HEADROOM
    (content,) = sent["contents"]
    assert content["role"] == "user"
    assert "FINDINGS:" in content["parts"][0]["text"]


@pytest.mark.asyncio
async def test_gemini_judge_records_usage_including_thinking():
    from rtv.llm import USAGE

    USAGE.reset()
    client, _ = _fake_client(json.dumps(GOOD))
    await judge({"plan": "x"}, {}, GeminiProvider(client), model="gemini-test")
    row = USAGE.summary()["layers"]["eval_judge"]
    assert row["calls"] == 1
    assert row["input_tokens"] == 120
    assert row["output_tokens"] == 340


@pytest.mark.asyncio
async def test_an_invalid_gemini_answer_is_retried_once_then_none():
    client, models = _fake_client("{not json", json.dumps({"clarity": 9}))
    out = await judge({"plan": "x"}, {}, GeminiProvider(client), model="gemini-test")
    assert out is None
    assert len(models.calls) == 2

    client, models = _fake_client("{not json", json.dumps(GOOD))
    out = await judge({"plan": "x"}, {}, GeminiProvider(client), model="gemini-test")
    assert out == JudgeScore(**GOOD)


@pytest.mark.asyncio
async def test_the_gemini_provider_refuses_a_tool_loop():
    client, _ = _fake_client(json.dumps(GOOD))
    with pytest.raises(ValueError, match="judge-only"):
        await GeminiProvider(client).complete(
            agent="eval_judge",
            model="m",
            system=[],
            messages=[{"role": "user", "content": "x"}],
            tools=[object()],
            output_model=JudgeScore,
            max_tokens=10,
        )


def test_the_judge_defaults_to_gemini():
    assert _settings().provider == "gemini"


def test_gemini_backend_without_a_key_is_a_config_error():
    with pytest.raises(JudgeConfigError, match="GEMINI_API_KEY"):
        judge_backend(settings=_settings())


def test_gemini_backend_uses_the_pinned_default_model():
    pytest.importorskip("google.genai")
    backend = judge_backend(settings=_settings(GEMINI_API_KEY="test-key-not-real"))
    assert backend.name == "gemini"
    assert backend.model == DEFAULT_GEMINI_MODEL
    assert isinstance(backend.provider, GeminiProvider)
    assert judge_backend(
        model="gemini-other", settings=_settings(GEMINI_API_KEY="test-key-not-real")
    ).model == "gemini-other"


def test_anthropic_backend_needs_an_explicit_model_and_key():
    with pytest.raises(JudgeConfigError, match="RTV_JUDGE_MODEL"):
        judge_backend("anthropic", settings=_settings())
    with pytest.raises(JudgeConfigError, match="ANTHROPIC_API_KEY"):
        judge_backend("anthropic", "some-opus-id", settings=_settings())


def test_anthropic_backend_reuses_the_forced_respond_tool_provider():
    pytest.importorskip("anthropic")
    from rtv.pitwall.provider import KimiProvider

    backend = judge_backend(
        "anthropic", "some-opus-id", settings=_settings(ANTHROPIC_API_KEY="test-key-not-real")
    )
    assert backend.name == "anthropic"
    assert isinstance(backend.provider, KimiProvider)
    assert backend.label == "anthropic:some-opus-id"


def test_kimi_backend_is_kept_for_comparison(monkeypatch):
    import rtv.llm
    from rtv.pitwall.provider import KimiProvider

    monkeypatch.setattr(rtv.llm, "build_client", lambda: object())
    backend = judge_backend("kimi", settings=_settings())
    assert isinstance(backend.provider, KimiProvider)
    assert backend.model == rtv.llm.model("reasoning")


def test_an_unknown_provider_is_a_config_error():
    with pytest.raises(JudgeConfigError, match="gemini, anthropic or kimi"):
        judge_backend("openai", settings=_settings())


def test_judge_table_lists_each_case_and_the_mean():
    table = judge_table(
        {
            "gemini:g": [
                {"case": "a", "score": GOOD},
                {"case": "b", "score": {**GOOD, "clarity": 2}},
                {"case": "c", "score": None},
            ]
        }
    )
    assert "| gemini:g | a | 4 | 3 | 5 |" in table
    assert "| gemini:g | c | no score | no score | no score |" in table
    assert "| gemini:g | **mean (2)** | 3.00 | 3.00 | 5.00 |" in table


# --- scope rule: the judge's model family stays inside evals/ ----------------------

_FORBIDDEN = ("evals", "google.genai", "google.generativeai")


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def test_nothing_in_the_app_imports_the_judge_provider_or_gemini():
    offenders = []
    for path in sorted((ROOT / "src" / "rtv").rglob("*.py")):
        for name in _imports(path):
            if any(name == f or name.startswith(f + ".") for f in _FORBIDDEN):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert not offenders, "the app must stay Kimi Code only:\n" + "\n".join(offenders)
