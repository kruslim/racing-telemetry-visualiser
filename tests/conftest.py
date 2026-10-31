"""Shared test guarantees and scaffolding.

The suite is required to run with no iRacing install, no network and no API key.
The first two are structural; the third is not, because a developer who has a
``KIMI_CODE_API_KEY`` exported for the coach would otherwise have the pitwall
agent layer wire itself into every app fixture. So every LLM credential is removed
for the whole session: an offline suite that only passes on a machine without a key
is not an offline suite.

It also provides a scripted chat model so the coach-graph suite is hermetic. The
graph coach, its eval runner, and (later) the review gate all take a chat model.
Driving them with a scripted model — no network, no key, no cost — is what lets the
regression suite run in CI deterministically. ``ScriptedModel`` satisfies the only
interface the graph needs: ``bind_tools(defs).invoke(messages) -> AIMessage``.
"""

from __future__ import annotations

from itertools import count

import pytest

#: Every env var a credential could be read from: ``rtv.llm``'s Kimi names, plus
#: the two the anthropic SDK (the transport for Kimi's endpoint) reads on its own.
_LLM_KEY_ENVS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "KIMI_CODE_API_KEY",
    "KIMI_API_KEY",
)


@pytest.fixture(autouse=True, scope="session")
def _no_api_key() -> None:
    monkeypatch = pytest.MonkeyPatch()
    for name in _LLM_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    # rtv.llm also reads the key from .env; a developer's real .env must never
    # reach a test.
    import rtv.llm

    monkeypatch.setattr(rtv.llm, "_from_env_file", lambda name: None)
    yield
    monkeypatch.undo()


_ids = count(1)


def ai_call(tool: str, args: dict):
    """An assistant turn requesting exactly one tool call."""
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="", tool_calls=[{"name": tool, "args": args, "id": f"call_{next(_ids)}"}]
    )


class _Bound:
    def __init__(self, parent: ScriptedModel, tool_names: list[str]) -> None:
        self.parent = parent
        self.tool_names = tool_names

    def invoke(self, messages: list):
        self.parent.invocations.append({"tools": self.tool_names, "messages": list(messages)})
        assert self.parent.responses, "script exhausted: graph asked for more turns than scripted"
        return self.parent.responses.pop(0)


class ScriptedModel:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.invocations: list[dict] = []

    def bind_tools(self, tools: list[dict]) -> _Bound:
        return _Bound(self, [t["name"] for t in tools])


@pytest.fixture
def ai():
    """The single-tool-call assistant-turn factory."""
    pytest.importorskip("langchain_core")
    return ai_call


@pytest.fixture
def make_model():
    """Factory building a ScriptedModel from a list of scripted AIMessage responses."""
    return ScriptedModel
