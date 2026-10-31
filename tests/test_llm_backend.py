"""Backend selection and the tool-forced structured-output path.

Everything here is offline. No credential is read except the fake ones these
tests export themselves, and no client is ever constructed against a network.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from rtv import llm
from rtv.config import Settings
from rtv.pitwall.provider import RESPOND_TOOL, KimiProvider, structured_call


class Answer(BaseModel):
    call: str
    laps: int


def _settings(**kw) -> Settings:
    return Settings(**kw)


# --------------------------------------------------------------- configuration
def test_kimi_k3_on_the_coding_subscription_is_the_default():
    """The shipped default is the deployment this repo actually runs."""
    s = _settings()
    assert llm.base_url(s) == "https://api.kimi.com/coding"
    assert llm.key_env_var(s) == "KIMI_CODE_API_KEY"
    assert llm.model("reasoning", settings=s) == "kimi-k3"
    assert llm.model("fast", settings=s) == "kimi-k3"


def test_endpoint_key_env_and_models_are_overridable():
    s = _settings(
        llm_base_url="https://example.invalid/anthropic",
        llm_api_key_env="MY_KIMI_KEY",
        llm_kimi_model_fast="kimi-small",
    )
    assert llm.base_url(s) == "https://example.invalid/anthropic"
    assert llm.key_env_var(s) == "MY_KIMI_KEY"
    assert llm.model("fast", settings=s) == "kimi-small"
    # An explicit per-call override always wins.
    assert llm.model("fast", "kimi-for-coding", settings=s) == "kimi-for-coding"


def test_there_is_no_provider_switch_any_more():
    """Kimi Code is the only backend; an old RTV_LLM_PROVIDER line is ignored."""
    s = _settings(llm_provider="anthropic")
    assert llm.describe(s)["provider"] == "kimi"
    assert llm.base_url(s) == "https://api.kimi.com/coding"


def test_the_kimi_code_key_wins_over_the_legacy_name(monkeypatch):
    monkeypatch.setenv("KIMI_CODE_API_KEY", "code-key")
    monkeypatch.setenv("KIMI_API_KEY", "legacy-key")
    s = _settings()
    assert llm.key_env_var(s) == "KIMI_CODE_API_KEY"
    assert llm.api_key(s) == "code-key"


def test_the_legacy_kimi_key_name_still_works_alone(monkeypatch):
    monkeypatch.delenv("KIMI_CODE_API_KEY", raising=False)
    monkeypatch.setenv("KIMI_API_KEY", "legacy-key")
    s = _settings()
    assert llm.key_env_var(s) == "KIMI_API_KEY"
    assert llm.api_key(s) == "legacy-key"


def test_describe_reports_presence_not_the_credential(monkeypatch):
    monkeypatch.setenv("KIMI_CODE_API_KEY", "sk-secret-value")
    payload = llm.describe(_settings())
    assert payload["key_present"] is True
    assert "sk-secret-value" not in repr(payload)


def test_build_client_without_a_credential_is_a_config_error(monkeypatch):
    monkeypatch.delenv("KIMI_CODE_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    with pytest.raises(llm.LLMConfigError):
        llm.build_client(_settings())


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


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


def _provider(content):
    client = _FakeClient(_Response(content))
    return KimiProvider(client=client), client


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
async def test_never_sends_output_format_and_always_offers_the_contract():
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
async def test_thinking_is_switched_off_explicitly():
    """kimi-k3 thinks by default and then rejects a named tool_choice (HTTP 400)."""
    provider, client = _provider([_Block("text", text="hi")])
    await _complete(provider)
    assert client.messages.calls[0]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_with_thinking_on_the_last_pass_forces_any_tool_not_a_named_one():
    provider = KimiProvider(
        client=_FakeClient(_Response([_Block("text", text="hi")])),
        thinking=True,
    )
    await _complete(provider)
    sent = provider._client.messages.calls[0]
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["tool_choice"] == {"type": "any"}
    assert [t["name"] for t in sent["tools"]] == [RESPOND_TOOL]


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
async def test_structured_call_is_one_tool_free_forced_answer():
    provider, client = _provider(
        [_Block("tool_use", id="t1", name=RESPOND_TOOL, input={"call": "hold", "laps": 2})]
    )
    out = await structured_call(
        provider,
        agent="judge",
        model="kimi-k3",
        system=[{"type": "text", "text": "role"}],
        prompt="score this",
        output_model=Answer,
        max_tokens=100,
    )
    assert out == Answer(call="hold", laps=2)
    sent = client.messages.calls[0]
    assert sent["messages"] == [{"role": "user", "content": "score this"}]
    assert sent["tool_choice"] == {"type": "tool", "name": RESPOND_TOOL}


class _SequenceMessages(_FakeMessages):
    """Hands out one response per call, in order."""

    def __init__(self, responses):
        super().__init__(None)
        self._responses = list(responses)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


async def _structured(provider):
    return await structured_call(
        provider,
        agent="orchestrator.CornerAdvice",
        model="kimi-k3",
        system=[{"type": "text", "text": "role"}],
        prompt="advise",
        output_model=Answer,
        max_tokens=100,
    )


@pytest.mark.asyncio
async def test_structured_call_asks_once_more_after_an_invalid_payload():
    """Seen live on kimi-k3: an empty respond call dropped a whole specialist."""
    client = _FakeClient(None)
    client.messages = _SequenceMessages(
        [
            _Response([_Block("tool_use", id="t1", name=RESPOND_TOOL, input={})]),
            _Response(
                [_Block("tool_use", id="t2", name=RESPOND_TOOL, input={"call": "hold", "laps": 1})]
            ),
        ]
    )
    out = await _structured(KimiProvider(client=client))
    assert out == Answer(call="hold", laps=1)
    assert len(client.messages.calls) == 2


@pytest.mark.asyncio
async def test_structured_call_gives_up_after_the_retry():
    bad = _Response([_Block("tool_use", id="t1", name=RESPOND_TOOL, input={})])
    client = _FakeClient(None)
    client.messages = _SequenceMessages([bad, bad, bad])
    assert await _structured(KimiProvider(client=client)) is None
    assert len(client.messages.calls) == 2
