HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md under "Deviations".
- Before finishing: run the FULL `pytest` suite and iterate until it is completely green (offline - no iRacing, no ANTHROPIC_API_KEY needed). Do not finish with failing or skipped-because-broken tests.
- Also run any smoke script mentioned below and make it pass.
- Update docs/PITWALL.md with what you built, the public interfaces the next stage will consume, and any deviations from this spec.
- Do not refactor or break existing v1 functionality (analysis UI, coaching API, MCP server, existing tests).

TASK:

Read first: README.md, docs/COACHING.md, docs/PITWALL.md, src/rtv/racestate/ (built in the previous step), src/rtv/coaching/orchestrator.py and coaching/agent/ (the existing LangGraph coach, refusal + validator patterns), and evals/.

GOAL
Build the agentic layer of the pitwall: a generic event-driven agent framework, an orchestrator that routes race events to agents and merges their output into one prioritised "radio" feed, and the first full agent - the STRATEGIST - as the reference implementation, with its own eval set. Design so the next prompt can add two more agents as pure configuration + prompts, no framework changes.

ARCHITECTURE PRINCIPLES (hold firm):
- Hybrid cost model: the LLM is called ONLY when an agent's trigger fires on a race event, never on a timer or per tick. All continuous math already lives in racestate/.
- Every agent = (role system prompt) + (scoped tool subset) + (trigger predicate over RaceEvents) + (output contract). One shared runtime.
- Grounding discipline carries over from the Layer-3b coach: every numeric claim in an agent's output must be traceable to RaceState/tool data; reuse/adapt the in-loop citation validator; refuse rather than invent.
- Anthropic API via the existing provider abstraction. Default models: claude-haiku-4-5-20251001 for high-frequency/low-stakes calls, claude-sonnet-4-6 for strategy reasoning. Model per agent configurable via env. Structured outputs via Pydantic like the existing orchestrator.

NEW PACKAGE: src/rtv/pitwall/

1. framework.py:
  - AgentSpec: name, role_prompt, model, tools (callables over RaceState + the store), triggers (list of event_type + optional predicate), cooldown (min seconds between invocations per trigger type), max_tokens, output contract
  - AgentRuntime: on trigger  ->  assemble a compact context (current RaceState slice relevant to the agent, the triggering event, recent radio history for continuity)  ->  call the model with tool use  ->  validate output  ->  emit RadioMessage
  - RadioMessage (Pydantic): {agent, priority: critical|advisory|info, spoken_text (short, radio-style, <25 words), detail_text (longer, for UI panel), data (structured payload for UI widgets), event_ref, timestamp}

2. orchestrator.py (pitwall version):
  - subscribes to the racestate event bus, routes events to matching AgentSpecs
  - concurrency: agents run async; per-agent serialisation (an agent never overlaps itself); global cap on in-flight LLM calls
  - radio discipline: single merged output queue ordered by priority then time; critical pre-empts; drop/supersede stale advisory messages about the same subject (e.g. two pit-window updates  ->  keep newest)
  - publishes RadioMessages onto the bus  ->  /ws/pitwall gains {type:"radio"} frames
  - kill switch + per-agent enable flags via env/API

3. THE STRATEGIST (agents/strategist.py):
  - Triggers: pit_window_open, pit_window_closing, flag_transition (esp. yellow/FCY - evaluate opportunistic stop), stint_lap_milestones (every N green laps, from a deterministic event, not a timer), fuel_margin_below_threshold, rival_pitted (from standings pit-status change)
  - Tools (deterministic, from racestate + store): get_race_state_slice, get_fuel_projection, get_tyre_trend, get_standings_around_player, get_stint_history, simulate_pit_outcome (deterministic function: given stop on lap L  ->  rejoin position estimate from gaps + pit-lane time; implement this in racestate/strategy_math.py, tested independently)
  - Output style: real race-engineer radio ("Box this lap, box box. Rejoin P6, three seconds clear behind."), with the reasoning in detail_text
  - The agent recommends; it never asserts certainty it doesn't have. If fuel channels are missing  ->  grounded refusal path: says strategy is limited and why.

4. Driver input channel: POST /api/v1/pitwall/driver-message {text}  ->  routed as a driver_report event; the orchestrator sends it to the agent whose domain matches (keyword/intent routing is fine for now; strategist handles strategy questions, others come next prompt; unmatched  ->  strategist as default responder). Response comes back as a normal RadioMessage.

5. EVALS (extend evals/ using its existing patterns - scripted model, baseline diffing, hermetic):
  - Replay the synthetic race scenario through racestate + orchestrator with a scripted model provider
  - Assert: strategist called to box within the correct lap window; recommended stop under the yellow when the math favoured it; every number in its output matches ground-truth strategy_math results (reuse evals/checks.py style); refusal case when fuel channels stripped
  - Latency budget test: trigger -> RadioMessage under a configurable budget with the scripted model
  - Baseline diff = merge blocker, same as the coach evals

TESTS: framework unit tests (trigger matching, cooldowns, per-agent serialisation, priority merging/supersede logic), strategy_math unit tests with hand-computed cases, orchestrator integration test on replay - all offline.

DEFINITION OF DONE
- pytest green offline; evals runner green against new seed cases
- scripts/smoke_pitwall.py extended: replay with scripted model  ->  prints the radio feed; with ANTHROPIC_API_KEY set and RTV_LIVE_LLM=true, same script runs the real API end-to-end
- docs/PITWALL.md: agent framework design, strategist spec, cost model (calls per race estimate)
