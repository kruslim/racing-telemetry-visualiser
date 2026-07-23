"""Pitwall agent surface: status, the radio feed, and the kill switches.

Everything here is operational rather than analytical. The interesting question
for an operator mid-race is "what is running, what has it said, and how do I make
it stop", so that is exactly what this exposes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

from rtv.api.deps import get_services
from rtv.pitwall.agents import AGENT_REGISTRY
from rtv.pitwall.tts import TTSError, TTSUnavailable
from rtv.services import AppServices

router = APIRouter(tags=["pitwall-agents"])


def _pitwall(services: AppServices):
    if services.pitwall is None:
        raise HTTPException(
            503,
            "The pitwall agent layer is not running. It needs RTV_PITWALL_AGENTS=true, "
            'the "ai" extra and ANTHROPIC_API_KEY.',
        )
    return services.pitwall


@router.get("/pitwall/status", summary="Agent layer status, per-agent stats and radio counters")
def pitwall_status(services: AppServices = Depends(get_services)) -> dict:
    if services.pitwall is None:
        # Not an error: the deterministic pitwall works without the agent layer,
        # and a UI needs to be able to say *why* the radio is quiet.
        return {
            "available": False,
            "reason": "Agent layer not mounted (RTV_PITWALL_AGENTS, the 'ai' extra "
            "or ANTHROPIC_API_KEY).",
            "known_agents": [spec.name for spec in AGENT_REGISTRY],
        }
    return {"available": True, **services.pitwall.status()}


@router.get("/pitwall/radio", summary="Recent radio messages (bounded ring buffer)")
def pitwall_radio(
    limit: int = Query(50, ge=1, le=500),
    services: AppServices = Depends(get_services),
) -> dict:
    pitwall = _pitwall(services)
    messages = pitwall.feed.history(limit)
    return {
        "count": len(messages),
        "messages": [m.to_api() for m in messages],
        "pending": [m.to_api() for m in pitwall.feed.pending()],
    }


class EnabledRequest(BaseModel):
    enabled: bool = Field(description="False is the kill switch: no agent is invoked.")


@router.post("/pitwall/enabled", summary="Global agent kill switch")
def pitwall_set_enabled(
    body: EnabledRequest, services: AppServices = Depends(get_services)
) -> dict:
    pitwall = _pitwall(services)
    pitwall.set_enabled(body.enabled)
    return {"enabled": pitwall.enabled}


@router.post("/pitwall/agents/{name}/enabled", summary="Enable or disable one agent")
def pitwall_set_agent_enabled(
    name: str, body: EnabledRequest, services: AppServices = Depends(get_services)
) -> dict:
    pitwall = _pitwall(services)
    if not pitwall.set_agent_enabled(name, body.enabled):
        raise HTTPException(404, f"No agent named {name!r}.")
    return {"agent": name, "enabled": pitwall.agent_enabled(name)}


@router.post("/pitwall/reset", summary="Clear agent cooldowns and the radio channel")
def pitwall_reset(services: AppServices = Depends(get_services)) -> dict:
    pitwall = _pitwall(services)
    pitwall.reset()
    return pitwall.status()


# --------------------------------------------------------------------------
# the radio's voice
# --------------------------------------------------------------------------
@router.get("/pitwall/tts", summary="TTS provider, its availability, and the role voices")
def pitwall_tts(services: AppServices = Depends(get_services)) -> dict:
    """Answers whether or not anything else is mounted.

    A browser needs to know two things before it can speak: whether the backend
    will hand it audio, and how each role is supposed to sound if it will not.
    Both are here, so the frontend never has to guess and never has to fail.
    """
    if services.tts is None:  # pragma: no cover - only when RTV_PITWALL is off
        return {
            "available": False,
            "provider": "none",
            "reason": "The pitwall is not mounted (RTV_PITWALL).",
            "voices": {},
            "engine": "webspeech",
        }
    described = services.tts.describe()
    return {
        **described,
        # What the frontend should actually use. "webspeech" is not a fallback in
        # the apologetic sense -- it is the default engine, and the one the
        # audio-discipline tests run against.
        "engine": "backend" if described["available"] else "webspeech",
    }


@router.get(
    "/pitwall/audio/{message_id}",
    summary="Synthesised audio for one radio message",
    response_class=Response,
    responses={
        200: {"content": {"audio/mpeg": {}}, "description": "Audio bytes."},
        404: {"description": "No such message in the radio ring buffer."},
        503: {"description": "No backend TTS provider; use the Web Speech API."},
    },
)
async def pitwall_audio(
    message_id: str = Path(min_length=1, max_length=64),
    services: AppServices = Depends(get_services),
) -> Response:
    """Serve (and cache) the audio for a message the pitwall has already said.

    Deliberately *not* a "speak this text" endpoint: the only thing that can be
    synthesised is something an agent actually put on the radio, looked up by its
    content-derived id. That keeps the grounding discipline intact all the way to
    the speaker -- there is no path from arbitrary text to the driver's ear.
    """
    pitwall = _pitwall(services)
    tts = services.tts
    if tts is None or not tts.available:
        reason = tts.reason if tts is not None else "No TTS service."
        raise HTTPException(503, reason)
    message = pitwall.feed.find(message_id)
    if message is None:
        raise HTTPException(404, f"No radio message {message_id!r} in the ring buffer.")
    try:
        clip = await tts.clip_for(message)
    except TTSUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except TTSError as exc:
        # 502: the vendor failed, not the caller. The frontend treats any non-200
        # as "speak it yourself", so a flaky vendor costs a slightly different
        # voice rather than a missed call.
        raise HTTPException(502, str(exc)) from exc
    return Response(
        content=clip.audio,
        media_type=clip.media_type,
        headers={
            # Content-derived id, so the bytes for one id never change.
            "Cache-Control": "public, max-age=3600, immutable",
            "X-RTV-Voice": clip.voice,
            "X-RTV-TTS-Provider": clip.provider,
        },
    )


class DriverMessage(BaseModel):
    text: str = Field(min_length=1, max_length=500, description="What the driver said.")
    source: str = Field(
        default="voice",
        max_length=32,
        description="How it was captured: 'voice' (speech recognition) or 'text'.",
    )


@router.post("/pitwall/driver-message", summary="Something the driver said, onto the channel")
def pitwall_driver_message(
    body: DriverMessage, services: AppServices = Depends(get_services)
) -> dict:
    """Push-to-talk from the cockpit.

    The transcript joins the radio log immediately (the driver has already used
    the airtime) and is marked ``speak: false`` -- the pitwall does not read the
    driver's own words back to them. Agents see it in their recent-radio context.
    """
    pitwall = _pitwall(services)
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Nothing was said.")
    message = pitwall.driver_message(text, source=body.source)
    return {"message": message.to_api()}
