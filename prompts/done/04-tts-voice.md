HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md under "Deviations".
- Before finishing: run the FULL `pytest` suite and iterate until it is completely green (offline - no iRacing, no ANTHROPIC_API_KEY needed). Do not finish with failing or skipped-because-broken tests.
- Also run any smoke script mentioned below and make it pass.
- Update docs/PITWALL.md with what you built, the public interfaces the next stage will consume, and any deviations from this spec.
- Do not refactor or break existing v1 functionality (analysis UI, coaching API, MCP server, existing tests).

TASK:

Read first: docs/PITWALL.md, src/rtv/pitwall/ (RadioMessage, orchestrator, priorities).

GOAL
Give the pitwall a voice. RadioMessages' spoken_text becomes actual audio in the browser, with real radio discipline: critical calls interrupt, advisories queue, nothing talks over anything, and the driver can mute roles.

DESIGN
1. Default engine: browser-native Web Speech API (speechSynthesis) - zero cost, zero setup, works offline. Implement in the frontend (frontend/), driven by {type:"radio"} frames on /ws/pitwall.
2. Pluggable premium path: a backend TTS provider interface (src/rtv/pitwall/tts.py) with a provider registry, one reference implementation stubbed for a cloud TTS (e.g. OpenAI/ElevenLabs-style REST - implement the interface + config plumbing + a NullProvider; wire actual vendor calls behind env config, degrade silently to Web Speech if unset). Backend synthesises to audio bytes served at GET /api/v1/pitwall/audio/{message_id}; the frontend prefers backend audio when the message carries an audio URL.
3. AUDIO DISCIPLINE (frontend audio manager, pure JS to match the no-build-step frontend):
  - single-speaker rule: one utterance at a time
  - priority queue: critical interrupts current playback (cancel + play); advisory queues; info speaks only if idle and is dropped if it waits >15s (stale)
  - supersede: if a queued message is superseded (same subject, per orchestrator flag), replace it
  - per-agent voice differentiation where the engine allows (different voice/rate/pitch per role so strategist != spotter by ear)
  - per-agent mute toggles + master volume, persisted in URL params or in-memory only (NO localStorage - if you consider it, don't)
  - a subtle radio-click cue before critical messages (short synthesized beep via WebAudio, no asset files)
4. Driver voice input (stretch, keep small): a push-to-talk button using the Web Speech recognition API where the browser supports it  ->  transcribed text  ->  existing POST /api/v1/pitwall/driver-message. Feature-detect; hide the button when unsupported.

TESTS
- backend: tts provider registry unit tests; audio endpoint contract test with NullProvider
- frontend: the audio manager must be written as a small pure module with its queue/priority/supersede logic separated from speechSynthesis calls so it can be tested; add JS tests if the repo has a JS test setup, otherwise add a deterministic self-test page (frontend/audio-test.html) that simulates a message sequence and asserts queue behaviour in-page, documented in docs/PITWALL.md
- pytest stays green offline; nothing here may require an API key or network

DEFINITION OF DONE
- Replay a session with the UI open: you HEAR the pitwall - strategist and spotter in different voices, a critical spotter call interrupting an advisory mid-sentence in the synthetic scenario
- Mute/volume controls work; no console errors when Web Speech is unavailable
