HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md under "Deviations".
- Before finishing: run the FULL `pytest` suite and iterate until it is completely green (offline - no iRacing, no ANTHROPIC_API_KEY needed). Do not finish with failing or skipped-because-broken tests.
- Also run any smoke script mentioned below and make it pass.
- Update docs/PITWALL.md with what you built, the public interfaces the next stage will consume, and any deviations from this spec.
- Do not refactor or break existing v1 functionality (analysis UI, coaching API, MCP server, existing tests).

TASK:

Read first: whole repo state after prompts 1-9, especially the stage-5 section of
docs/PITWALL.md, src/rtv/director/, docs/director_scenario.example.json and
tests/test_director.py.

GOAL
Implement the race director. Stage 5 built and tested the seam and left the walker
unwritten on purpose; everything downstream already treats an injected event as a
first-class citizen, and two tests pin that. This stage writes the walker and turns
the pitwall into something you can rehearse against.

Do not redesign the seam. DirectorEngine, ScenarioScript, ScriptedInjection,
InjectionTrigger and injected_event() are the contract - implement against them.

1. THE SCHEDULER (src/rtv/director/scheduler.py):
  - ScriptedDirector implementing the existing DirectorEngine protocol. poll(state)
    walks the bound script, evaluates each pending injection's conjunctive trigger
    against the current state, and returns the events that should fire now.
  - Honour every InjectionTrigger condition already modelled: at_session_time,
    at_lap, at_lap_dist_pct, after + delay_s, while_flag, once. Delays are SESSION
    seconds so a 4x replay rehearses the same race - this is already the stated
    contract and a test must pin it.
  - poll() is on the pump's hot path: cheap, synchronous, non-blocking, and it must
    never raise into the pump. Match the resilience discipline the engine uses.
  - Firing must be idempotent per entry (once), and edge-triggered - a condition
    that stays true does not re-fire.
  - reset() forgets what fired, for a second replay in one process. describe()
    reports loaded / fired / pending, replacing NoopDirector's "planned" wording.
    NoopDirector stays as the default and is NOT deleted.

2. WIRING:
  - RTV_PITWALL_DIRECTOR selects the implementation (noop default, scripted opt-in);
    the existing RTV_PITWALL_DIRECTOR_SCRIPT keeps loading and validating the script.
    A script bound to the noop director must still lint, exactly as today.
  - POST /api/v1/director/load, /start, /stop, /reset and GET /api/v1/director/status
    so a scenario can be driven from the UI. Follow the existing replay-control
    endpoints for shape and error handling.

3. THE PROOF (extend, do not replace, tests/test_director.py):
  - The stage-5 equivalence tests must still pass UNCHANGED against the real
    scheduler - that is the whole point of having written them first.
  - The five-entry docs/director_scenario.example.json runs end to end: FCY ->
    restart -> rain -> mandatory stop -> hazard, each landing at the scripted
    condition, each carrying its provenance keys, each waking the agents the
    equivalent real event would wake.
  - A determinism test: the same script over the same replay produces a
    byte-identical event log twice.
  - A test that a chained injection (after + delay_s) fires the correct session
    interval later at both 1x and 4x replay speed.

4. WEATHER AND REGULATION FINALLY MEAN SOMETHING:
  - weather_change and regulation_change are the two catalog members no detector
    emits (stage 1). Now that a director can produce them, make the consumers real:
    the strategist must react to a scripted regulation change (a stop becoming
    mandatory), and the vehicle engineer / strategist to a scripted rain arrival.
  - Add the triggers and predicates in the existing style. Add eval cases for both,
    generated the same way as everything else.
  - Grounding still applies: an agent may say rain is coming ONLY because the
    injected event said so, and must not extrapolate a temperature drift into a
    forecast. Test the refusal.

5. UI:
  - Director controls on the pit stand: pick a scenario, start it, see pending and
    fired injections. Injected events already badge as DIRECTOR on the ticker via
    eventLine() - verify that still holds and do not duplicate the mechanism.

6. SMOKE:
  - Extend scripts/smoke_pitwall_director.py from a seam check into a full scenario
    run, asserting the ordered arc of the example script.
  - A scripted "demo race" one-liner: replay + director + agents + voice, no iRacing
    and no key, that shows a safety car and a rain call inside 60 seconds. This is
    the demo path stage 5 asked for, finally able to show something happening.

DEFINITION OF DONE: pytest + evals green offline; smoke green; the stage-5
equivalence tests pass unmodified; docs/PITWALL.md's "deliberately unimplemented"
section rewritten to describe what now exists, with the seam argument preserved as
the reason it was cheap to build.
