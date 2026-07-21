HEADLESS MODE — no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md under "Deviations".
- Before finishing: run the FULL `pytest` suite and iterate until it is completely green (offline — no iRacing, no ANTHROPIC_API_KEY needed). Do not finish with failing or skipped-because-broken tests.
- Also run any smoke script mentioned below and make it pass.
- Update docs/PITWALL.md with what you built, the public interfaces the next stage will consume, and any deviations from this spec.
- Do not refactor or break existing v1 functionality (analysis UI, coaching API, MCP server, existing tests).

TASK:

Read first: docs/PITWALL.md, src/rtv/pitwall/ (framework, orchestrator, strategist), src/rtv/racestate/, src/rtv/coaching/ (Layer-1 features and the LangGraph coach).

GOAL
Add the remaining pitwall roles using the existing framework — this should be mostly AgentSpecs, prompts, tools, and evals, NOT framework changes. If you find the framework can't express something, fix the framework minimally and note why in docs/PITWALL.md.

1. VEHICLE / PERFORMANCE ENGINEER (agents/vehicle_engineer.py):
   - Triggers: repeated lockup/wheelspin events at the same corner (deterministic aggregation first — add a corner-recurrence aggregator to racestate/detectors.py that emits recurring_issue events; the agent fires on those, not on every single lockup), tyre temp/pressure drifting out of band, car_health thresholds (oil/water temp), damage events, stint_end (post-stint tyre/brake review)
   - Tools: get_tyre_trend, get_recent_detector_events, get_corner_detail (reuse coaching Layer-1), get_car_health, get_setup_snapshot (from session-info YAML where available)
   - Voice: concise engineering advisories ("Fronts are up at 105 — two clicks off the brake bias, and give them a lap of air in T1-T3.")
   - Setup advice must be hedged as suggestion and grounded in observed data only.

2. SPOTTER / RACE CONTROL (agents/spotter.py):
   - This role is 90% deterministic. Rule-based callouts (NO LLM) generated directly from race events in a spotter.py rules table: flag changes, cars entering close proximity behind/ahead (from standings gaps), blue flags, pit entry/exit confirmations, lap-time reports at line, "last lap" — these become RadioMessages with agent="spotter" and near-zero latency.
   - LLM involvement only for: incident summaries after a caution (what happened, who's involved, expected impact) and end-of-race summary, triggered by flag events. Model: claude-haiku-4-5-20251001.
   - Critical spotter messages (proximity, yellow ahead) are priority=critical and must bypass any queued advisory audio.

3. COACH INTEGRATION:
   - The existing LangGraph coach becomes the pitwall's fourth role, unchanged in its core, wrapped in an AgentSpec with post-hoc triggers only: stint_end, session_end, and driver_report messages about driving technique. It runs its normal lap-findings comparison (last stint's best vs session/personal best) and emits a prioritised top-3 as detail_text with a one-line spoken summary.
   - Do not run the coach mid-stint uninvited — real teams don't coach drivers mid-flight-lap.

4. Driver-message routing upgrade: replace keyword routing with a single cheap LLM classification call (haiku) → {target_agent, intent}; fall back to keyword rules if no API key. Add tests for both paths.

5. EVALS — extend the seed set:
   - vehicle engineer: recurring-lockup scenario → advisory referencing the right corner with numbers matching detector ground truth; tyre-drift scenario; refusal when tyre channels absent
   - spotter rules: table-driven test — every event type → expected callout text, deterministic, no model at all
   - coach wrapper: stint_end on replay → findings match Layer-1 ground truth (reuse existing checks)
   - full-grid test: replay the synthetic race with ALL agents enabled + scripted model → assert the merged radio log against a golden baseline (ordering, priorities, no duplicate/superseded messages leaking through)

DEFINITION OF DONE
- pytest + evals green offline
- smoke script now prints a full four-agent radio log for the synthetic race
- docs/PITWALL.md updated with all four role specs and the deterministic-vs-LLM split per role
