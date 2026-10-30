HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md under "Deviations".
- Before finishing: run the FULL `pytest` suite and iterate until it is completely green (offline - no iRacing, no ANTHROPIC_API_KEY needed). Do not finish with failing or skipped-because-broken tests.
- Also run any smoke script mentioned below and make it pass.
- Update docs/PITWALL.md with what you built, the public interfaces the next stage will consume, and any deviations from this spec.
- Do not refactor or break existing v1 functionality (analysis UI, coaching API, MCP server, existing tests).

TASK:

Read first: whole repo state after prompts 1-4, especially docs/PITWALL.md.

GOAL
Close out the backend: stub the future race-director layer, harden the live path, and bring docs/README to portfolio quality.

1. RACE-DIRECTOR STUB (src/rtv/director/): interfaces only, no implementation logic.
  - ScenarioScript model: a timed/conditional list of injected events (weather change, FCY, forced pit regulation) - Pydantic, documented
  - DirectorEngine protocol with a NoopDirector default; orchestrator accepts an optional director whose injected events flow through the same bus, indistinguishable from detector events
  - One example scenario JSON in docs/ + a test that a scripted FCY injection produces the same downstream behaviour as a real flag event (proves the seam works)
  - README/docs mark this clearly as "planned: scripted race weekends / WEC-style energy regulations"

2. LIVE-PATH HARDENING:
  - graceful behaviour when iRacing disconnects mid-session (engine pauses, state flagged stale, agents suspended, clean resume)
  - API-failure resilience: LLM call errors  ->  retry once with backoff, then emit an info RadioMessage ("Pitwall AI degraded") and keep deterministic layers running; never crash the engine loop
  - cost guard: hard cap on LLM calls per session (env-configurable, default generous), counter exposed at GET /api/v1/pitwall/status along with per-agent call counts and last latencies
  - config audit: every new env var documented in .env.example

3. DOCS + README:
  - README: update the architecture diagram and feature tour for the pitwall (v1 post-hoc analysis  ->  v2 live multi-agent pitwall narrative), quick-start for "race with the pitwall" (live) and "replay a race" (offline demo), honest cost estimate per race hour
  - docs/PITWALL.md finalised: full event catalog, all four agent specs, radio priority rules, TTS design, eval methodology
  - a 60-second demo path documented: one command  ->  replay synthetic race  ->  browser opens pitwall  ->  voice + UI live (this is the recruiter/demo flow; make it frictionless)

4. FULL-SYSTEM TEST: one integration test that boots the app via TestClient, starts a replay, connects to /ws/pitwall, and asserts state+event+radio frames arrive in contract-valid form with the scripted model.

DEFINITION OF DONE: pytest + evals green offline; smoke script green; README accurate to what exists (no vaporware claims); demo path works from a clean clone.
