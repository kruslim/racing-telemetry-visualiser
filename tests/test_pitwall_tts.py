"""The backend TTS layer: the registry, the providers, and the audio contract.

Everything here runs with no network and no key. The REST provider is exercised
through an injected transport, which is the only way to prove the request it
would send is the request it should send without actually sending one.
"""

from __future__ import annotations

import asyncio

import pytest

from rtv.pitwall.framework import EventRef, RadioMessage, RadioPriority
from rtv.pitwall.tts import (
    AUDIO_URL_PREFIX,
    DEFAULT_VOICES,
    FALLBACK_VOICE,
    NullProvider,
    RadioTTS,
    RestTTSProvider,
    TTSConfig,
    TTSError,
    TTSRequest,
    TTSResponse,
    TTSUnavailable,
    VoiceProfile,
    build_radio_tts,
    build_tts_provider,
    get_tts_provider_factory,
    parse_voice_overrides,
    register_tts_provider,
    tts_provider_names,
    unregister_tts_provider,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class FakeTransport:
    """Records what would have gone over the wire, and answers as told."""

    def __init__(self, *, status: int = 200, content: bytes = b"ID3-audio", raises=None):
        self.requests: list[TTSRequest] = []
        self.status = status
        self.content = content
        self.raises = raises

    async def __call__(self, request: TTSRequest) -> TTSResponse:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return TTSResponse(status=self.status, content=self.content, media_type="audio/mpeg")


def message(agent: str = "strategist", text: str = "Box this lap.", **kw) -> RadioMessage:
    event = EventRef(
        event_type="pit_window_open", key="pit_window_open", tick=120, session_time=2.0
    )
    return RadioMessage(
        agent=agent,
        priority=kw.pop("priority", RadioPriority.ADVISORY),
        spoken_text=text,
        detail_text="Because the numbers said so.",
        event_ref=event,
        **kw,
    )


def rest(**kw) -> tuple[RestTTSProvider, FakeTransport]:
    transport = FakeTransport(**{k: kw.pop(k) for k in ("status", "content", "raises") if k in kw})
    config = TTSConfig(
        provider="rest",
        url="https://example.invalid/v1/audio/speech",
        api_key="k-123",
        model="tts-1",
        **kw,
    )
    return RestTTSProvider(config, transport=transport), transport


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
def test_the_builtin_providers_are_registered():
    names = tts_provider_names()
    assert "none" in names and "rest" in names and "openai" in names


def test_an_unknown_provider_degrades_to_null_and_says_which_names_exist():
    provider = build_tts_provider(TTSConfig(provider="wishful"))
    assert isinstance(provider, NullProvider)
    assert provider.available is False
    assert "wishful" in provider.reason and "rest" in provider.reason


@pytest.mark.parametrize("name", ["", "none", "null", "off", "webspeech", "browser"])
def test_every_way_of_saying_off_yields_the_null_provider(name):
    provider = build_tts_provider(TTSConfig(provider=name))
    assert isinstance(provider, NullProvider) and provider.available is False


def test_a_third_party_provider_is_one_registration():
    class Vendor(NullProvider):
        name = "vendor"

        @property
        def available(self) -> bool:
            return True

        async def synthesize(self, text, voice):
            return b"vendor-bytes"

    register_tts_provider("vendor", lambda config: Vendor(config))
    try:
        assert get_tts_provider_factory("VENDOR") is not None  # case-insensitive
        provider = build_tts_provider(TTSConfig(provider="vendor"))
        assert provider.available is True
        assert asyncio.run(provider.synthesize("x", FALLBACK_VOICE)) == b"vendor-bytes"
    finally:
        assert unregister_tts_provider("vendor") is True
    assert get_tts_provider_factory("vendor") is None


def test_registering_over_an_existing_name_needs_saying_so():
    with pytest.raises(ValueError):
        register_tts_provider("rest", lambda config: NullProvider(config))
    register_tts_provider("rest", lambda config: NullProvider(config), override=True)
    try:
        assert build_tts_provider(TTSConfig(provider="rest")).available is False
    finally:  # put the real one back for the rest of the session
        register_tts_provider("rest", RestTTSProvider, override=True)
    assert isinstance(build_tts_provider(TTSConfig(provider="rest")), RestTTSProvider)


def test_a_factory_that_explodes_is_still_not_fatal():
    def broken(config):
        raise RuntimeError("no credentials, no anything")

    register_tts_provider("broken", broken)
    try:
        provider = build_tts_provider(TTSConfig(provider="broken"))
        assert provider.available is False
        assert "no credentials" in provider.reason
    finally:
        unregister_tts_provider("broken")


def test_an_unnamed_provider_is_refused():
    with pytest.raises(ValueError):
        register_tts_provider("   ", lambda config: NullProvider(config))


# --------------------------------------------------------------------------
# voices
# --------------------------------------------------------------------------
def test_every_role_sounds_different():
    """The point of per-agent voices: strategist != spotter, by ear."""
    provider = NullProvider()
    signatures = {
        agent: (
            provider.voice_for(agent).voice,
            provider.voice_for(agent).rate,
            provider.voice_for(agent).pitch,
        )
        for agent in ("strategist", "vehicle_engineer", "spotter", "coach")
    }
    assert len(set(signatures.values())) == 4


def test_an_unknown_agent_still_has_a_voice():
    assert NullProvider().voice_for("director") == FALLBACK_VOICE


def test_voices_can_be_overridden_per_agent_without_losing_the_rest_of_the_profile():
    config = TTSConfig(voices=parse_voice_overrides("spotter=shimmer, coach = echo , junk"))
    provider = NullProvider(config)
    spotter = provider.voice_for("spotter")
    assert spotter.voice == "shimmer"
    assert spotter.rate == DEFAULT_VOICES["spotter"].rate  # rate/pitch are not lost
    assert provider.voice_for("coach").voice == "echo"
    assert provider.voice_for("strategist").voice == DEFAULT_VOICES["strategist"].voice


def test_voice_overrides_parse_defensively():
    assert parse_voice_overrides("") == {}
    assert parse_voice_overrides("nonsense") == {}
    assert parse_voice_overrides("a=1,,b=2") == {"a": "1", "b": "2"}


def test_a_voice_profile_serialises_for_the_frontend():
    payload = DEFAULT_VOICES["spotter"].to_api()
    assert payload["rate"] > 1.0 and payload["pitch"] > 1.0  # the spotter is urgent
    assert isinstance(payload["browser_hints"], list)
    assert payload["label"] == "Spotter"


# --------------------------------------------------------------------------
# the null provider
# --------------------------------------------------------------------------
def test_the_null_provider_explains_itself_rather_than_failing_silently():
    provider = NullProvider()
    assert provider.available is False
    assert "Web Speech" in provider.reason
    with pytest.raises(TTSUnavailable):
        asyncio.run(provider.synthesize("anything", FALLBACK_VOICE))


def test_the_null_provider_describes_the_voices_the_browser_should_use():
    described = NullProvider().describe()
    assert described["available"] is False
    assert set(described["voices"]) == set(DEFAULT_VOICES)


# --------------------------------------------------------------------------
# the REST reference provider
# --------------------------------------------------------------------------
def test_the_rest_provider_sends_the_body_it_should():
    provider, transport = rest()
    audio = asyncio.run(provider.synthesize("Box now.", provider.voice_for("spotter")))
    assert audio == b"ID3-audio"
    request = transport.requests[0]
    assert request.url.endswith("/audio/speech")
    assert request.headers["Authorization"] == "Bearer k-123"
    assert request.json["input"] == "Box now."
    assert request.json["voice"] == DEFAULT_VOICES["spotter"].voice
    assert request.json["model"] == "tts-1"
    assert request.json["response_format"] == "mp3"


def test_the_rest_provider_is_unavailable_without_a_url_or_a_key():
    no_url = RestTTSProvider(TTSConfig(provider="rest", api_key="k"), transport=FakeTransport())
    assert no_url.available is False and "URL" in no_url.reason

    no_key = RestTTSProvider(
        TTSConfig(provider="rest", url="https://x.invalid"), transport=FakeTransport()
    )
    assert no_key.available is False and "API_KEY" in no_key.reason
    with pytest.raises(TTSUnavailable):
        asyncio.run(no_key.synthesize("hello", FALLBACK_VOICE))


def test_a_vendor_error_is_a_tts_error_not_a_crash():
    provider, _ = rest(status=429)
    with pytest.raises(TTSError) as excinfo:
        asyncio.run(provider.synthesize("Box now.", FALLBACK_VOICE))
    assert "429" in str(excinfo.value)


def test_an_empty_vendor_response_is_an_error_rather_than_silent_audio():
    provider, _ = rest(content=b"")
    with pytest.raises(TTSError):
        asyncio.run(provider.synthesize("Box now.", FALLBACK_VOICE))


def test_a_transport_that_raises_becomes_one_story():
    provider, _ = rest(raises=OSError("dns is having a day"))
    with pytest.raises(TTSError) as excinfo:
        asyncio.run(provider.synthesize("Box now.", FALLBACK_VOICE))
    assert "dns is having a day" in str(excinfo.value)


def test_nothing_to_say_is_refused_before_the_wire():
    provider, transport = rest()
    with pytest.raises(TTSError):
        asyncio.run(provider.synthesize("   ", FALLBACK_VOICE))
    assert transport.requests == []


# --------------------------------------------------------------------------
# message ids and the service
# --------------------------------------------------------------------------
def test_a_message_id_is_derived_from_content_so_a_replay_reproduces_it():
    first, second = message(), message()
    assert first.message_id == second.message_id
    assert first.message_id != message(text="Stay out.").message_id
    assert first.message_id != message(agent="spotter").message_id
    assert len(first.message_id) == 16
    assert first.to_api()["message_id"] == first.message_id


def test_url_for_is_none_when_nothing_can_speak_which_is_the_fallback_signal():
    service = RadioTTS(NullProvider())
    assert service.available is False
    assert service.url_for(message()) is None


def test_url_for_points_at_the_endpoint_that_serves_it():
    provider, _ = rest()
    service = RadioTTS(provider)
    url = service.url_for(message())
    assert url == f"{AUDIO_URL_PREFIX}/{message().message_id}"


def test_a_clip_is_synthesised_once_and_then_cached():
    provider, transport = rest()
    service = RadioTTS(provider)
    target = message()
    first = asyncio.run(service.clip_for(target))
    second = asyncio.run(service.clip_for(target))
    assert first.audio == second.audio == b"ID3-audio"
    assert len(transport.requests) == 1, "the second fetch came from the cache"
    assert (service.hits, service.misses) == (1, 1)
    assert first.voice == DEFAULT_VOICES["strategist"].voice
    assert first.provider == "rest"


def test_the_clip_cache_is_bounded():
    provider, transport = rest()
    service = RadioTTS(provider, cache_size=2)
    for i in range(4):
        asyncio.run(service.clip_for(message(text=f"Call number {i}.")))
    assert len(service._cache) == 2
    assert len(transport.requests) == 4


def test_clip_for_refuses_rather_than_returning_silence_when_unavailable():
    service = RadioTTS(NullProvider())
    with pytest.raises(TTSUnavailable):
        asyncio.run(service.clip_for(message()))


def test_a_failed_synthesis_is_counted_and_not_cached():
    provider, transport = rest(status=500)
    service = RadioTTS(provider)
    with pytest.raises(TTSError):
        asyncio.run(service.clip_for(message()))
    with pytest.raises(TTSError):
        asyncio.run(service.clip_for(message()))
    assert service.failures == 2
    assert len(transport.requests) == 2


def test_describe_tells_an_operator_what_is_wrong_and_what_the_voices_are():
    described = RadioTTS(NullProvider()).describe()
    assert described["available"] is False
    assert described["url_prefix"] == AUDIO_URL_PREFIX
    assert described["cache"]["capacity"] == 64
    assert "strategist" in described["voices"]


# --------------------------------------------------------------------------
# config plumbing
# --------------------------------------------------------------------------
def test_settings_default_to_the_browser_doing_the_talking():
    from rtv.config import Settings

    settings = Settings(_env_file=None)
    config = TTSConfig.from_settings(settings)
    assert config.provider == "none"
    service = build_radio_tts(settings)
    assert service.available is False
    assert service.url_for(message()) is None


def test_settings_reach_the_provider(monkeypatch):
    from rtv.config import Settings

    settings = Settings(
        _env_file=None,
        pitwall_tts="rest",
        pitwall_tts_url="https://example.invalid/speech",
        pitwall_tts_api_key="secret",
        pitwall_tts_model="tts-1-hd",
        pitwall_tts_voices="spotter=shimmer",
        pitwall_tts_cache=8,
    )
    config = TTSConfig.from_settings(settings)
    assert config.provider == "rest" and config.voices == {"spotter": "shimmer"}
    service = build_radio_tts(settings)
    assert service.available is True
    assert service.provider.voice_for("spotter").voice == "shimmer"
    assert service.url_for(message()).startswith(AUDIO_URL_PREFIX)


def test_a_voice_profile_is_immutable_so_one_agent_cannot_retune_another():
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        VoiceProfile(voice="a").rate = 2.0  # type: ignore[misc]
