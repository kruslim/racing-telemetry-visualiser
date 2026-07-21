# CLAUDE.md — RTV v2 Pitwall build

This repo is being upgraded in staged headless prompts from v1 (post-hoc telemetry
analysis + tiered AI coach) to v2 (live multi-agent AI pitwall: race-state engine,
strategist / vehicle engineer / spotter / coach agents, TTS radio, live pitwall UI).

## Before any work
- Read `docs/PITWALL.md` first — it is the shared state between build stages
  (what exists so far, public interfaces, deviations). If it doesn't exist yet,
  you are stage 1.
- Skim `README.md`, `docs/COACHING.md`, and the packages you will touch under
  `src/rtv/` so new code matches established patterns.

## Hard conventions (match the existing codebase)
- Offline-first: the full `pytest` suite must run with NO iRacing installed and
  NO `ANTHROPIC_API_KEY`. LLM layers use the scripted-model / mocked-provider
  patterns already in `src/rtv/coaching/agent/` and `evals/`.
- Import-guard optional heavy deps (see how DuckDB/PyArrow/pyirsdk are handled).
- Pydantic models for all structured data; wiring goes through `src/rtv/services.py`.
- Grounding discipline: no agent/LLM output may assert a number that isn't
  traceable to deterministic data; prefer grounded refusal over invention
  (follow the Layer-3b coach's refusal + validator patterns).
- Frontend is vanilla JS + canvas/DOM, no build step, no frameworks, no
  localStorage/sessionStorage, served by FastAPI from `frontend/`.
- New env vars must be added to `.env.example`.

## Definition of done for EVERY stage
1. Full `pytest` suite green (offline).
2. Eval harness (`evals/`) green where the stage touches agents.
3. Smoke scripts mentioned in the task pass.
4. `docs/PITWALL.md` updated: what was built, interfaces exposed for the next
   stage, deviations from spec (with one-line rationale each).
5. Existing v1 behaviour untouched (analysis UI, coaching endpoints, MCP server).

## Never
- Never stop to ask the user a question — choose conservatively and log it.
- Never delete or rewrite existing passing tests to make new code pass.
- Never introduce a network/API dependency into the default test path.
