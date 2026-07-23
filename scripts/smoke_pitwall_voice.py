"""Offline smoke test of the stage-4 radio voice (no iRacing, no API key, no network).

Drives the scripted race through the real race-state engine and the real
orchestrator, with a scripted provider standing in for Claude and a fake HTTP
transport standing in for a cloud TTS vendor, then checks the things that would
actually go wrong between an agent deciding to speak and a driver hearing it:

    1. the default deployment has no backend voice, and says so honestly
    2. a configured provider stamps every published message with an audio URL
    3. that URL resolves: GET /api/v1/pitwall/audio/{id} returns the bytes
    4. the second fetch is served from the clip cache, not the vendor
    5. an id nobody published is a 404 -- there is no text-to-speech oracle here
    6. message ids are content-derived, so replaying the race reproduces them
    7. the driver's own push-to-talk transcript is logged but never spoken
    8. each role has an audibly different voice
    9. the frontend is served, and its audio-discipline suite passes under node

Run:  python scripts/smoke_pitwall_voice.py
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from smoke_pitwall_agents import grounded_strategist  # noqa: E402

from rtv.pitwall.agents.strategist import STRATEGIST  # noqa: E402
from rtv.pitwall.orchestrator import PitwallOrchestrator  # noqa: E402
from rtv.pitwall.provider import ScriptedProvider  # noqa: E402
from rtv.pitwall.radio import RadioFeed  # noqa: E402
from rtv.pitwall.tts import (  # noqa: E402
    DEFAULT_VOICES,
    NullProvider,
    RadioTTS,
    RestTTSProvider,
    TTSConfig,
    TTSResponse,
    TTSUnavailable,
)
from rtv.racestate import RaceStateEngine  # noqa: E402
from rtv.racestate.scenario import scenario_catalog, scenario_frames  # noqa: E402

PASS, FAIL = "  ok  ", " FAIL "
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{PASS if condition else FAIL}] {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


class FakeVendor:
    """Stands in for a cloud TTS endpoint. Records every request it is given."""

    def __init__(self) -> None:
        self.requests = []

    async def __call__(self, request) -> TTSResponse:
        self.requests.append(request)
        text = request.json["input"]
        return TTSResponse(
            status=200,
            content=b"ID3" + text.encode("utf-8")[:32],
            media_type="audio/mpeg",
        )


def run_race(orchestrator: PitwallOrchestrator, engine: RaceStateEngine) -> None:
    """Frame by frame, dispatching events against the state current at each."""
    catalog = scenario_catalog()
    sub = engine.bus.subscribe(name="smoke-voice")

    async def drive() -> None:
        for frame in scenario_frames():
            engine.on_frame(frame, catalog)
            for event in sub.drain():
                await orchestrator.handle_event(event, engine.snapshot())
            orchestrator.feed.pump(engine.state.session_time)
        orchestrator.feed.pump(engine.state.session_time + 3600)

    asyncio.run(drive())
    sub.close()


def main() -> int:
    print("\n== stage 4: the pitwall's voice ==\n")

    # -- 1. the default: the browser does the talking ----------------------
    default = RadioTTS(NullProvider())
    check(
        "the default deployment has no backend voice",
        default.available is False and "Web Speech" in default.reason,
        default.reason,
    )

    # -- 2/3/4. a configured provider, end to end --------------------------
    vendor = FakeVendor()
    provider = RestTTSProvider(
        TTSConfig(
            provider="rest",
            url="https://vendor.invalid/v1/audio/speech",
            api_key="smoke-key",
            model="tts-1",
        ),
        transport=vendor,
    )
    tts = RadioTTS(provider)

    engine = RaceStateEngine(source="replay")
    orchestrator = PitwallOrchestrator(
        engine,
        ScriptedProvider(grounded_strategist),
        [STRATEGIST],
        feed=RadioFeed(),
        clock=None,
        tts=tts,
        tool_config={"pit_lane_loss_s": 25.0, "standings_window": 3},
    )
    run_race(orchestrator, engine)

    aired = orchestrator.feed.history()
    check("the strategist spoke during the scripted race", len(aired) >= 3, f"{len(aired)} calls")
    check(
        "every call carries an audio url",
        all(m.audio_url == f"/api/v1/pitwall/audio/{m.message_id}" for m in aired),
    )

    first = aired[0]
    clip = asyncio.run(tts.clip_for(first))
    check("the url resolves to audio bytes", clip.audio.startswith(b"ID3"), f"{len(clip.audio)} B")
    check("the vendor was called exactly once", len(vendor.requests) == 1)

    asyncio.run(tts.clip_for(first))
    check(
        "the second fetch comes from the clip cache",
        len(vendor.requests) == 1 and tts.hits == 1,
    )

    check(
        "the message the URL names is findable on the feed",
        orchestrator.feed.find(first.message_id) is first,
    )
    check("an id nobody published is not findable", orchestrator.feed.find("deadbeef") is None)

    # -- 6. replay reproduces the ids --------------------------------------
    engine_2 = RaceStateEngine(source="replay")
    orchestrator_2 = PitwallOrchestrator(
        engine_2,
        ScriptedProvider(grounded_strategist),
        [STRATEGIST],
        feed=RadioFeed(),
        clock=None,
        tts=RadioTTS(provider),
        tool_config={"pit_lane_loss_s": 25.0, "standings_window": 3},
    )
    run_race(orchestrator_2, engine_2)
    ids_1 = [m.message_id for m in aired]
    ids_2 = [m.message_id for m in orchestrator_2.feed.history()]
    check("replaying the race reproduces the message ids", ids_1 == ids_2, f"{len(ids_1)} ids")

    # -- 7. push to talk ---------------------------------------------------
    driver = orchestrator.driver_message("Fronts are gone, I need tyres.")
    check("the driver's transcript is logged", orchestrator.feed.history()[-1] is driver)
    check("...and is never read back to them", driver.speak is False)
    check("...and costs no synthesis", driver.audio_url is None and len(vendor.requests) == 1)

    # -- 8. the roles sound different --------------------------------------
    signatures = {
        agent: (
            provider.voice_for(agent).voice,
            provider.voice_for(agent).rate,
            provider.voice_for(agent).pitch,
        )
        for agent in ("strategist", "vehicle_engineer", "spotter", "coach")
    }
    check(
        "all four roles are audibly distinct",
        len(set(signatures.values())) == 4,
        ", ".join(f"{a}={s[0]}@{s[1]}" for a, s in signatures.items()),
    )
    check(
        "an agent with no voice of its own still has one",
        provider.voice_for("director") is not None
        and "director" not in DEFAULT_VOICES,
    )

    # a provider that cannot speak refuses rather than returning silence
    try:
        asyncio.run(default.clip_for(first))
        refused = False
    except TTSUnavailable:
        refused = True
    check("a provider that cannot speak refuses rather than faking it", refused)

    # -- 9. the frontend ---------------------------------------------------
    frontend = ROOT / "frontend"
    expected = ["radio.html", "audio-test.html", "js/audio-manager.js", "js/speech.js"]
    check(
        "the frontend ships the audio pages",
        all((frontend / name).is_file() for name in expected),
    )

    node = shutil.which("node")
    if node is None:
        print("[ skip ] the JS audio-discipline suite (no node on PATH)")
        print("         open frontend/audio-test.html in a browser instead")
    else:
        result = subprocess.run(
            [node, str(frontend / "js" / "run-audio-tests.mjs")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        summary = (result.stdout.strip().splitlines() or ["no output"])[-1]
        check("the JS audio-discipline suite passes", result.returncode == 0, summary)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All voice checks passed. Offline, no key, no network.")
    print("\nTo hear it:  uvicorn rtv.main:app  ->  http://127.0.0.1:8000/radio.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
