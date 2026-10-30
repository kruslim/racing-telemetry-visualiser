HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md under "Deviations".
- Before finishing: run the FULL `pytest` suite and iterate until it is completely green (offline - no iRacing, no ANTHROPIC_API_KEY needed). Do not finish with failing or skipped-because-broken tests.
- Also run any smoke script mentioned below and make it pass.
- Update docs/PITWALL.md with what you built, the public interfaces the next stage will consume, and any deviations from this spec.
- Do not refactor or break existing v1 functionality (analysis UI, coaching API, MCP server, existing tests).

TASK:

Read first: whole repo state after prompts 1-6, especially docs/PITWALL.md.

GOAL
Make the pitwall run itself. Today an operator starts the server, opens the UI and
presses Start. After this stage the system arms itself when a session begins,
disarms itself when the session ends, and never needs a human in the loop for a
normal race weekend.

This is the autonomy stage. Everything here is deterministic supervision - no new
agents, no new LLM calls except the one already-specified debrief hand-off.

1. SESSION LIFECYCLE (src/rtv/racestate/ or a new src/rtv/session/):
  - A SessionLifecycle state machine over the EXISTING frame stream and the
    SessionState / SessionFlags channels the engine already reads. States at
    minimum: idle -> practice/qual/race detected -> green -> running -> checkered
    -> ended. Do not invent a channel; if a transition is not backed by the
    catalog, the state stays unknown and a capability flag records why (the
    established grounding discipline, applied to lifecycle).
  - Emit lifecycle transitions as ordinary RaceEvents on the existing bus
    (session_start, session_green, session_checkered, session_end) so every
    existing consumer - ring buffer, /ws/pitwall, orchestrator, UI ticker - gets
    them for free. Follow the stage-5 rule: no consumer branches on their origin.
  - Detect a NEW session (car/track/session-id change, or a session-time reset)
    and call engine.reset() plus orchestrator reset, so a second race in one
    server run does not inherit the first race's cooldowns, fuel model or radio.

2. AUTONOMOUS ARM/DISARM:
  - The agent layer arms on session_green and disarms on session_end, driving the
    EXISTING kill switch rather than a parallel mechanism. Practice and qualifying
    default to a reduced roster (env-configurable) - a strategist has nothing to
    say in a practice session and should not bill for it.
  - Respect the existing per-session LLM cost cap from stage 5; reset the counter
    on a new session, and log the previous session's spend at disarm.
  - RTV_PITWALL_AUTONOMOUS (default false) gates the whole behaviour. When false
    everything behaves exactly as it does today - this must be provable by test.

3. RACE JOURNAL (the durable record):
  - Persist per session: every RaceEvent, every RadioMessage, the lifecycle
    transitions, and the final RaceState snapshot. Reuse the existing DuckDB /
    Parquet store patterns; do not add a new storage dependency.
  - GET /api/v1/sessions/{id}/journal returns it. A journal must be replayable
    into the existing UI without the sim present.
  - This is what stage 09's debrief analyst reads. Design the interface for that
    consumer now and document it.

4. HAND-OFF HOOK:
  - On session_end, fire a single documented extension point
    (e.g. services.on_session_end callbacks) carrying the session id and journal
    handle. Ship it with a no-op default and ONE built-in subscriber that writes a
    deterministic, LLM-free session summary (laps, best lap, stints, incidents,
    fuel used, radio call count) to disk and to the API.
  - The LLM debrief is stage 09 and must NOT be built here. This stage proves the
    seam the same way stage 5 proved the director seam.

5. NOTIFICATION (keep-me-updated, deterministic only):
  - A pluggable notifier protocol with a no-op default and a file/console
    implementation. On session_end it emits the deterministic summary.
  - Do not add a network dependency to the default test path. Any webhook/email
    implementation must be import-guarded and off by default.

6. SMOKE + TESTS:
  - scripts/smoke_pitwall_autonomy.py: drive the scripted scenario end to end with
    RTV_PITWALL_AUTONOMOUS=true and assert the full arc - arm on green, radio
    during the race, disarm at the flag, journal written, summary produced - with
    no human input and no API key.
  - A test proving two consecutive replays in ONE process produce two independent
    journals with no state bleed between them.
  - A test proving RTV_PITWALL_AUTONOMOUS=false leaves stage-1-to-6 behaviour
    byte-identical.

DEFINITION OF DONE: pytest + evals green offline; both smoke scripts green; every
new env var in .env.example; docs/PITWALL.md updated with the lifecycle event
catalog, the journal schema and the session-end hook signature stage 09 consumes.
