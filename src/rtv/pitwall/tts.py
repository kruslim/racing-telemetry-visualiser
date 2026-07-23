"""Text-to-speech for the radio channel: an interface, a registry, and a null.

The pitwall's default voice is the **browser's** ``speechSynthesis`` (see
``frontend/js/speech.js``): zero cost, zero setup, works with the network
unplugged. This module is the *optional* premium path — a deployment that wants a
particular voice points ``RTV_PITWALL_TTS`` at a provider and the same
:class:`~rtv.pitwall.framework.RadioMessage` starts carrying an ``audio_url``.

Three things are deliberate:

* **The registry is the extension point.** Vendors disagree about request bodies
  in ways no single config schema survives, so a new vendor is a factory
  registered under a name, not a branch in here. :class:`RestTTSProvider` is the
  reference implementation (OpenAI-compatible ``/audio/speech`` body) and doubles
  as the worked example.
* **Unavailable is a first-class state, not an error.** No provider configured,
  no API key, no ``httpx`` — every one of those yields a :class:`NullProvider`
  that says *why*. The frontend sees no ``audio_url`` and falls back to Web
  Speech, which is exactly the "degrade silently" the spec asks for.
* **Nothing is synthesised until it is asked for.** ``RadioMessage`` carries a URL
  the instant it is published; bytes are produced (and cached) only when a browser
  fetches ``GET /api/v1/pitwall/audio/{message_id}``. A message that is superseded
  before it airs therefore costs nothing.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from rtv.logging import get_logger

log = get_logger("pitwall.tts")

#: Where the audio for a message is served from. One place, so the URL the
#: message carries and the route that answers it cannot drift apart.
AUDIO_URL_PREFIX = "/api/v1/pitwall/audio"


class TTSUnavailable(RuntimeError):
    """No provider can speak this. Carries the reason, for the operator."""


class TTSError(RuntimeError):
    """A configured provider tried and failed."""


# --------------------------------------------------------------------------
# voices
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class VoiceProfile:
    """How one role sounds.

    ``voice`` is the vendor's voice id (backend path). ``browser_hints`` are
    substrings matched against ``speechSynthesis.getVoices()`` (frontend path);
    ``rate``/``pitch`` apply to both. A role that ends up on the same underlying
    voice as another is still distinguishable by rate and pitch, which is why
    both are set per role rather than left at 1.0.
    """

    voice: str
    lang: str = "en-GB"
    rate: float = 1.0
    pitch: float = 1.0
    browser_hints: tuple[str, ...] = ()
    label: str = ""

    def to_api(self) -> dict[str, Any]:
        return {
            "voice": self.voice,
            "lang": self.lang,
            "rate": self.rate,
            "pitch": self.pitch,
            "browser_hints": list(self.browser_hints),
            "label": self.label or self.voice,
        }


#: One voice per role, chosen so the pitwall is legible by ear alone: the
#: strategist is slow and low (he is thinking), the spotter is fast and high (he
#: is reacting), the engineer sits between them, the coach is deliberate. The
#: driver's own transcript is never spoken -- it is here so the UI can label it.
DEFAULT_VOICES: dict[str, VoiceProfile] = {
    "strategist": VoiceProfile(
        voice="onyx", lang="en-GB", rate=0.98, pitch=0.85,
        browser_hints=("George", "Daniel", "Google UK English Male", "en-GB"),
        label="Strategist",
    ),
    "vehicle_engineer": VoiceProfile(
        voice="echo", lang="en-GB", rate=1.02, pitch=1.0,
        browser_hints=("Ryan", "Arthur", "Google UK English Male", "en-GB"),
        label="Engineer",
    ),
    "spotter": VoiceProfile(
        voice="fable", lang="en-US", rate=1.22, pitch=1.18,
        browser_hints=("Guy", "Christopher", "Google US English", "en-US"),
        label="Spotter",
    ),
    "coach": VoiceProfile(
        voice="nova", lang="en-GB", rate=0.95, pitch=1.1,
        browser_hints=("Sonia", "Libby", "Google UK English Female", "en-GB"),
        label="Coach",
    ),
    "driver": VoiceProfile(
        voice="alloy", lang="en-GB", rate=1.0, pitch=1.0, label="Driver",
    ),
}

#: Anything not in the table above. A new agent is audible before anyone edits
#: this file; it simply does not yet have a voice of its own.
FALLBACK_VOICE = VoiceProfile(
    voice="alloy", lang="en-GB", rate=1.0, pitch=1.0,
    browser_hints=("en-GB", "en-US"), label="Pitwall",
)


def parse_voice_overrides(raw: str) -> dict[str, str]:
    """``"strategist=onyx, spotter=nova"`` -> ``{"strategist": "onyx", ...}``."""
    out: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        if "=" not in chunk:
            continue
        agent, _, voice = chunk.partition("=")
        agent, voice = agent.strip(), voice.strip()
        if agent and voice:
            out[agent] = voice
    return out


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TTSConfig:
    """Everything a provider factory is given. Plain data, no Settings import."""

    provider: str = "none"
    url: str = ""
    api_key: str = ""
    model: str = ""
    audio_format: str = "mp3"
    media_type: str = "audio/mpeg"
    timeout_s: float = 8.0
    #: agent -> vendor voice id, overriding :data:`DEFAULT_VOICES`.
    voices: Mapping[str, str] = field(default_factory=dict)
    cache_size: int = 64

    @classmethod
    def from_settings(cls, settings: Any) -> TTSConfig:
        return cls(
            provider=(getattr(settings, "pitwall_tts", "") or "none").strip().lower(),
            url=getattr(settings, "pitwall_tts_url", "") or "",
            api_key=getattr(settings, "pitwall_tts_api_key", "") or "",
            model=getattr(settings, "pitwall_tts_model", "") or "",
            audio_format=getattr(settings, "pitwall_tts_format", "mp3") or "mp3",
            media_type=getattr(settings, "pitwall_tts_media_type", "audio/mpeg")
            or "audio/mpeg",
            timeout_s=float(getattr(settings, "pitwall_tts_timeout_s", 8.0)),
            voices=parse_voice_overrides(getattr(settings, "pitwall_tts_voices", "")),
            cache_size=int(getattr(settings, "pitwall_tts_cache", 64)),
        )


# --------------------------------------------------------------------------
# the transport seam
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TTSRequest:
    url: str
    headers: dict[str, str]
    json: dict[str, Any]
    timeout_s: float


@dataclass(frozen=True)
class TTSResponse:
    status: int
    content: bytes
    media_type: str = ""


#: Injected so the whole REST path is exercised offline. The default one is the
#: only code in this module that can touch a network, and it is never reached
#: unless an operator configured a URL and a key.
Transport = Callable[[TTSRequest], Awaitable[TTSResponse]]


async def httpx_transport(request: TTSRequest) -> TTSResponse:  # pragma: no cover - network
    """The real transport. ``httpx`` is imported lazily, like every heavy dep."""
    import httpx

    async with httpx.AsyncClient(timeout=request.timeout_s) as client:
        response = await client.post(
            request.url, headers=request.headers, json=request.json
        )
    return TTSResponse(
        status=response.status_code,
        content=response.content,
        media_type=response.headers.get("content-type", ""),
    )


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------
class TTSProvider(Protocol):  # pragma: no cover - structural type
    name: str
    available: bool
    reason: str
    media_type: str

    def voice_for(self, agent: str) -> VoiceProfile: ...

    async def synthesize(self, text: str, voice: VoiceProfile) -> bytes: ...


class BaseTTSProvider:
    """Voice resolution, shared. Subclasses only implement :meth:`synthesize`."""

    name = "base"

    def __init__(self, config: TTSConfig | None = None) -> None:
        self.config = config or TTSConfig()
        self.media_type = self.config.media_type
        self.reason = ""

    @property
    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    def voice_for(self, agent: str) -> VoiceProfile:
        profile = DEFAULT_VOICES.get(agent, FALLBACK_VOICE)
        override = self.config.voices.get(agent)
        if override:
            profile = VoiceProfile(
                voice=override,
                lang=profile.lang,
                rate=profile.rate,
                pitch=profile.pitch,
                browser_hints=profile.browser_hints,
                label=profile.label,
            )
        return profile

    def voices(self) -> dict[str, VoiceProfile]:
        """Every known role's voice, with overrides applied."""
        return {agent: self.voice_for(agent) for agent in DEFAULT_VOICES}

    async def synthesize(self, text: str, voice: VoiceProfile) -> bytes:
        raise NotImplementedError  # pragma: no cover

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": self.available,
            "reason": self.reason,
            "media_type": self.media_type,
            "voices": {a: v.to_api() for a, v in self.voices().items()},
        }


class NullProvider(BaseTTSProvider):
    """Speaks nothing, and says why. The default, and every failure's landing pad.

    This is not a degraded mode so much as *the* mode: with no provider
    configured the browser does the talking, which is what the spec asks for.
    """

    name = "none"

    def __init__(self, config: TTSConfig | None = None, *, reason: str = "") -> None:
        super().__init__(config)
        self.reason = reason or (
            "No backend TTS provider configured (RTV_PITWALL_TTS). "
            "The pitwall speaks through the browser's Web Speech API."
        )

    @property
    def available(self) -> bool:
        return False

    async def synthesize(self, text: str, voice: VoiceProfile) -> bytes:
        raise TTSUnavailable(self.reason)


class RestTTSProvider(BaseTTSProvider):
    """A cloud TTS over a JSON REST call, OpenAI ``/audio/speech``-compatible.

    The body is ``{model, input, voice, response_format, speed}`` with a bearer
    token — the shape OpenAI and several compatible gateways accept. A vendor
    that wants a different body (ElevenLabs puts the voice in the path) registers
    its own factory rather than growing a flag in here; see
    :func:`register_tts_provider`.

    Constructed even when unconfigured, so ``/api/v1/pitwall/tts`` can report
    *which* piece is missing instead of a generic "off".
    """

    name = "rest"

    def __init__(self, config: TTSConfig, *, transport: Transport | None = None) -> None:
        super().__init__(config)
        self._transport = transport or httpx_transport
        self.reason = self._why_not()

    def _why_not(self) -> str:
        if not self.config.url:
            return "RTV_PITWALL_TTS_URL is not set."
        if not self.config.api_key:
            return "RTV_PITWALL_TTS_API_KEY is not set."
        return ""

    @property
    def available(self) -> bool:
        return not self.reason

    def build_request(self, text: str, voice: VoiceProfile) -> TTSRequest:
        return TTSRequest(
            url=self.config.url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.model or "tts-1",
                "input": text,
                "voice": voice.voice,
                "response_format": self.config.audio_format,
                "speed": round(voice.rate, 2),
            },
            timeout_s=self.config.timeout_s,
        )

    async def synthesize(self, text: str, voice: VoiceProfile) -> bytes:
        if not self.available:
            raise TTSUnavailable(self.reason)
        if not text.strip():
            raise TTSError("Nothing to say.")
        try:
            response = await self._transport(self.build_request(text, voice))
        except TTSError:
            raise
        except Exception as exc:  # network, timeout, DNS -- all one story to a caller
            raise TTSError(f"{self.name} transport failed: {exc}") from exc
        if response.status != 200:
            raise TTSError(
                f"{self.name} returned HTTP {response.status} "
                f"({len(response.content)} bytes)."
            )
        if not response.content:
            raise TTSError(f"{self.name} returned no audio.")
        return response.content


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
ProviderFactory = Callable[[TTSConfig], "TTSProvider"]

_PROVIDERS: dict[str, ProviderFactory] = {}


def register_tts_provider(
    name: str, factory: ProviderFactory, *, override: bool = False
) -> ProviderFactory:
    """Register a provider factory under ``name``.

    Refuses to shadow an existing name unless asked: silently replacing the
    provider a deployment thought it configured is the sort of thing that is only
    noticed when the radio goes quiet mid-race.
    """
    key = name.strip().lower()
    if not key:
        raise ValueError("A TTS provider needs a name.")
    if key in _PROVIDERS and not override:
        raise ValueError(f"A TTS provider named {key!r} is already registered.")
    _PROVIDERS[key] = factory
    return factory


def unregister_tts_provider(name: str) -> bool:
    return _PROVIDERS.pop(name.strip().lower(), None) is not None


def tts_provider_names() -> list[str]:
    return sorted(_PROVIDERS)


def get_tts_provider_factory(name: str) -> ProviderFactory | None:
    return _PROVIDERS.get((name or "").strip().lower())


def build_tts_provider(config: TTSConfig) -> TTSProvider:
    """Build the configured provider. Never raises; falls back to :class:`NullProvider`.

    A misconfigured voice must not be able to take the pitwall down, so every way
    this can go wrong ends in a null provider carrying an explanation.
    """
    name = (config.provider or "none").strip().lower()
    if name in ("", "none", "null", "off", "false", "webspeech", "browser"):
        return NullProvider(config)
    factory = get_tts_provider_factory(name)
    if factory is None:
        return NullProvider(
            config,
            reason=f"Unknown TTS provider {name!r}. Known: {', '.join(tts_provider_names())}.",
        )
    try:
        return factory(config)
    except Exception as exc:  # pragma: no cover - a bad factory is still not fatal
        log.exception("TTS provider %s failed to build", name)
        return NullProvider(config, reason=f"Provider {name!r} failed to build: {exc}")


register_tts_provider("none", NullProvider)
register_tts_provider("rest", RestTTSProvider)
# OpenAI's /v1/audio/speech is exactly the body RestTTSProvider sends, so the
# alias is honest rather than aspirational.
register_tts_provider("openai", RestTTSProvider)


# --------------------------------------------------------------------------
# the service the app holds
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SpeechClip:
    audio: bytes
    media_type: str
    voice: str
    provider: str


class RadioTTS:
    """A provider plus an LRU of what it has already said.

    A radio message is spoken once, but a browser may re-request it (a reconnect,
    a replay of the log, two tabs). Synthesis is the expensive half, so the bytes
    are kept; the cache is keyed by ``message_id``, which is content-derived, so
    the same call re-published never pays twice.
    """

    def __init__(self, provider: TTSProvider, *, cache_size: int = 64) -> None:
        self.provider = provider
        self._cache: OrderedDict[str, SpeechClip] = OrderedDict()
        self._cache_size = max(0, cache_size)
        self._locks: dict[str, asyncio.Lock] = {}
        self.hits = 0
        self.misses = 0
        self.failures = 0

    @property
    def available(self) -> bool:
        return bool(getattr(self.provider, "available", False))

    @property
    def reason(self) -> str:
        return getattr(self.provider, "reason", "")

    @property
    def media_type(self) -> str:
        return getattr(self.provider, "media_type", "audio/mpeg")

    def url_for(self, message: Any) -> str | None:
        """The URL this message's audio will be served from, or ``None``.

        ``None`` is the signal the frontend acts on: no backend audio, use Web
        Speech. It is returned whenever the provider cannot speak, so an
        unreachable vendor degrades to the browser rather than to silence.
        """
        if not self.available:
            return None
        message_id = getattr(message, "message_id", None) or str(message)
        return f"{AUDIO_URL_PREFIX}/{message_id}"

    def cached(self, message_id: str) -> SpeechClip | None:
        clip = self._cache.get(message_id)
        if clip is not None:
            self._cache.move_to_end(message_id)
        return clip

    async def clip_for(self, message: Any) -> SpeechClip:
        """Synthesise (or serve from cache) the audio for one radio message."""
        message_id = str(getattr(message, "message_id", "") or "")
        text = str(getattr(message, "spoken_text", "") or "")
        agent = str(getattr(message, "agent", "") or "")
        if not self.available:
            raise TTSUnavailable(self.reason or "No backend TTS provider configured.")

        cached = self.cached(message_id)
        if cached is not None:
            self.hits += 1
            return cached

        lock = self._locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            cached = self.cached(message_id)
            if cached is not None:  # another request synthesised it while we waited
                self.hits += 1
                return cached
            voice = self.provider.voice_for(agent)
            try:
                audio = await self.provider.synthesize(text, voice)
            except Exception:
                self.failures += 1
                raise
            finally:
                self._locks.pop(message_id, None)
            clip = SpeechClip(
                audio=audio,
                media_type=self.media_type,
                voice=voice.voice,
                provider=getattr(self.provider, "name", "unknown"),
            )
            self.misses += 1
            self._store(message_id, clip)
            return clip

    def _store(self, message_id: str, clip: SpeechClip) -> None:
        if self._cache_size <= 0 or not message_id:
            return
        self._cache[message_id] = clip
        self._cache.move_to_end(message_id)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        self._cache.clear()

    def describe(self) -> dict[str, Any]:
        describe = getattr(self.provider, "describe", None)
        base = describe() if callable(describe) else {
            "provider": getattr(self.provider, "name", "unknown"),
            "available": self.available,
            "reason": self.reason,
            "media_type": self.media_type,
            "voices": {},
        }
        return {
            **base,
            "url_prefix": AUDIO_URL_PREFIX,
            "cache": {
                "size": len(self._cache),
                "capacity": self._cache_size,
                "hits": self.hits,
                "misses": self.misses,
                "failures": self.failures,
            },
        }


def build_radio_tts(settings: Any) -> RadioTTS:
    """The one call ``services.build_services`` makes. Always returns something."""
    config = TTSConfig.from_settings(settings)
    provider = build_tts_provider(config)
    if provider.available:
        log.info("Pitwall TTS: %s (%s)", provider.name, provider.media_type)
    else:
        log.info("Pitwall TTS: browser Web Speech only (%s)", provider.reason)
    return RadioTTS(provider, cache_size=config.cache_size)
