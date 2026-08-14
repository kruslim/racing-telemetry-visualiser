"""Backend selection and the tool-forced structured-output path.

Everything here is offline. No credential is read except the fake ones these
tests export themselves, and no client is ever constructed against a network.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from rtv import llm
from rtv.config import Settings
from rtv.pitwall.provider import RESPOND_TOOL, AnthropicProvider


class Answer(BaseModel):
    call: str
    laps: int


def _settings(**kw) -> Settings:
    return Settings(**kw)


# --------------------------------------------------------------- selection
def test_kimi_k3_on_the_coding_subscription_is_the_default():
    """The shipped default is the deployment this repo actually runs."""
    s = _settings()
    assert llm.provider_name(s) == "kimi"
    assert llm.base_url(s) == "https://api.kimi.com/coding"
    assert llm.key_env_var(s) == "KIMI_API_KEY"
    # Not 'native': Kimi's compatibility layer need not implement output_format.
    assert llm.structured_output_mode(s) == "tool"
    assert llm.resolve_model("", tier="reasoning", settings=s) == "kimi-k3"
    assert llm.resolve_model("", tier="fast", settings=s) == "kimi-k3"


def test_anthropic_remains_available_as_a_fallback():
    s = _settings(llm_provider="anthropic")
    assert llm.base_url(s) is None  # the SDK's own default
    assert llm.key_env_var(s) == "ANTHROPIC_API_KEY"
    assert llm.structured_output_mode(s) == "native"


def test_every_default_is_individually_overridable():
    s = _settings(
        llm_provider="kimi",
        llm_base_url="https://api.moonshot.ai/anthropic",
        llm_api_key_env="MOONSHOT_API_KEY",
        llm_structured_output="native",
    )
    assert llm.base_url(s) == "https://api.moonshot.ai/anthropic"
    assert llm.key_env_var(s) == "MOONSHOT_API_KEY"
    assert llm.structured_output_mode(s) == "native"


def test_an_unknown_provider_fails_at_configuration_time():
    with pytest.raises(llm.LLMConfigError):
        llm.provider_name(_settings(llm_provider="gpt"))
    with pytest.raises(llm.LLMConfigError):
        llm.structured_output_mode(_settings(llm_structured_output="magic"))


def test_describe_reports_presence_not_the_credential(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-secret-value")
    payload = llm.describe(_settings(llm_provider="kimi"))
    assert payload["key_present"] is True
    assert "sk-secret-value" not in repr(payload)


def test_build_client_without_a_credential_is_a_config_error(monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    with pytest.raises(llm.LLMConfigError):
        llm.build_client(_settings(llm_provider="kimi"))


# ------------------------------------------------------------ model mapping
def test_claude_ids_map_onto_kimi_but_explicit_ids_are_left_alone():
    s = _settings(llm_provider="kimi")
    assert llm.resolve_model("claude-sonnet-4-6", tier="reasoning", settings=s) == "kimi-k3"
    assert llm.resolve_model("claude-haiku-4-5-20251001", tier="fast", settings=s) == "kimi-k3"
    # An operator who names a model means it, whichever backend they named it for.
    assert llm.resolve_model("kimi-for-coding", tier="fast", settings=s) == "kimi-for-coding"


def test_model_mapping_is_a_no_op_on_anthropic():
    s = _settings(llm_provider="anthropic")
    got = llm.resolve_model("claude-sonnet-4-6", tier="reasoning", settings=s)
    assert got == "claude-sonnet-4-6"


# ------------------------------------------------- the tool-forced provider
class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, content):
        self.content = content


class _FakeMessages:
    """Records kwargs so the wire shape can be asserted without a network."""

    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response

    async def parse(self, **kwargs):  # pragma: no cover - native path guard
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


def _provider(content):
    client = _FakeClient(_Response(content))
    return AnthropicProvider(client=client, structured_output="tool"), client


async def _complete(provider, *, tools=()):
    return await provider.complete(
        agent="strategist",
        model="kimi-k3",
        system=[{"type": "text", "text": "role"}],
        messages=[{"role": "user", "content": "go"}],
        tools=tools,
        output_model=Answer,
        max_tokens=800,
    )


@pytest.mark.asyncio
async def test_tool_mode_never_sends_output_format_and_always_offers_the_contract():
    provider, client = _provider(
        [_Block("tool_use", id="t1", name=RESPOND_TOOL, input={"call": "box_now", "laps": 5})]
    )
    await _complete(provider)
    sent = client.messages.calls[0]
    assert "output_format" not in sent  # would 400 against a compatibility layer
    assert [t["name"] for t in sent["tools"]] == [RESPOND_TOOL]


@pytest.mark.asyncio
async def test_the_contract_is_forced_only_once_the_real_tools_are_withdrawn():
    class _Tool:
        name = "get_fuel_projection"

        def to_api(self):
            return {"name": self.name, "description": "", "input_schema": {}}

    provider, client = _provider([_Block("text", text="thinking")])
    await _complete(provider, tools=[_Tool()])
    assert client.messages.calls[0]["tool_choice"] == {"type": "auto"}

    provider2, client2 = _provider([_Block("text", text="thinking")])
    await _complete(provider2)  # last pass: the runtime withdraws real tools
    assert client2.messages.calls[0]["tool_choice"] == {"type": "tool", "name": RESPOND_TOOL}


@pytest.mark.asyncio
async def test_a_respond_call_becomes_the_output_and_is_not_a_tool_call():
    provider, _ = _provider(
        [_Block("tool_use", id="t1", name=RESPOND_TOOL, input={"call": "box_now", "laps": 5})]
    )
    result = await _complete(provider)
    assert isinstance(result.output, Answer)
    assert result.output.call == "box_now"
    # The runtime must not try to service it as a real tool.
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_real_tool_calls_still_flow_through_untouched():
    provider, _ = _provider(
        [_Block("tool_use", id="t1", name="get_fuel_projection", input={"laps": 3})]
    )
    result = await _complete(provider)
    assert result.output is None
    assert [c.name for c in result.tool_calls] == ["get_fuel_projection"]
    assert result.tool_calls[0].input == {"laps": 3}


@pytest.mark.asyncio
async def test_answering_alongside_tool_calls_drops_the_calls():
    """A tool_use with no matching tool_result is rejected by every Messages
    implementation, so an answer has to win outright."""
    provider, _ = _provider(
        [
            _Block("tool_use", id="t1", name="get_fuel_projection", input={}),
            _Block("tool_use", id="t2", name=RESPOND_TOOL, input={"call": "hold", "laps": 0}),
        ]
    )
    result = await _complete(provider)
    assert result.output is not None
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_an_invalid_payload_yields_no_output_rather_than_raising():
    """The runtime's grounded-refusal path is the right answer here, and it is
    already tested -- this must not become an exception on the race loop."""
    provider, _ = _provider(
        [_Block("tool_use", id="t1", name=RESPOND_TOOL, input={"call": "box_now"})]
    )
    result = await _complete(provider)
    assert result.output is None


@pytest.mark.asyncio
async def test_native_mode_is_unchanged():
    provider = AnthropicProvider(
        client=_FakeClient(_Response([_Block("text", text="hi")])), structured_output="native"
    )
    client = provider._client
    await _complete(provider)
    assert client.messages.calls[0]["output_format"] is Answer
    assert "tool_choice" not in client.messages.calls[0]


def test_an_invalid_structured_output_mode_is_rejected_at_construction():
    with pytest.raises(ValueError):
        AnthropicProvider(client=_FakeClient(_Response([])), structured_output="magic")
