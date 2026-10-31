HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md as a deviation of this stage.
- Read CLAUDE.md first and follow its commands and conventions (repo virtualenv, `RTV_` settings with a line in .env.example, lazy optional imports, offline tests).
- Before finishing: run the FULL pytest suite, `ruff check .` and `scripts/smoke_offline.py`, and iterate until all three are green (offline - no iRacing, no KIMI_CODE_API_KEY, no network). Do not finish with failing tests or with tests skipped because they are broken. Never delete or rewrite a passing test to make new code pass.
- Update docs/PITWALL.md: the "Current architecture" page at the top for every area this stage changes (including the interfaces the next stage consumes), and a short history entry for this stage (what was built, deviations) in the stage-history appendix. Do not append another long stage section at the end of the file.
- Do not break v1 or earlier stages (post-race review, coaching endpoints, MCP server, the four agents, the journal). No fake data in the app: generated sessions live in evals/ and are used by tests and evals only.

TASK: Stage 10 - the race director scheduler, as a rehearsal tool in the eval
path (plan task D5).

Run order and dependencies: stages run 07, 08, 10, 09. This stage needs stage 07
(session lifecycle) and stage 08 (the race plan) merged, and plan task B2 (the
deterministic spotter: detectors fire spotter templates such as car_left,
car_right, clear, blue_flag and closing directly, with no model call; the LLM
spotter only gives slow summaries on a long cooldown). If one is missing, stop
and record that in docs/PITWALL.md.

Read first: CLAUDE.md, the "Current architecture" page and the director-seam
history of docs/PITWALL.md, src/rtv/director/ (models.py, engine.py),
docs/director_scenario.example.json, tests/test_director.py,
PitwallOrchestrator.poll_director, services.build_director, evals/run_pitwall.py,
evals/synthetic_race.py, and rtv.racestate.replay (store_source, ReplayDriver).

GOAL
Write the walker stage 5 left unwritten, so scripted cautions, rain and rule
changes can be rehearsed against the agents. That is the cheapest way to test
strategy. A director makes things happen that did not happen, so it runs ONLY
through the eval path, over the synthetic race or a recorded session read from
the store, and its output never appears as real data: it is not served by the
app, not written to data/sessions, and not shown in the live view or the review
as if it happened.

Do not redesign the seam. DirectorEngine, ScenarioScript, ScriptedInjection,
InjectionTrigger, injected_event() and is_injected() are the contract.

WHAT CHANGES FROM THE ORIGINAL STAGE-10 SPEC
- No /api/v1/director/* endpoints, no director controls on the pit stand or in
  the desktop app, no "demo race" one-liner and no RTV_PITWALL_DIRECTOR switch
  in the app. The app keeps NoopDirector; RTV_PITWALL_DIRECTOR_SCRIPT keeps
  loading and validating (linting) a script exactly as today.
- The old smoke scripts were removed in the cleanup; the end-to-end proof is a
  pytest test plus the eval runner below.
- Spotter car calls are deterministic templates (B2). An injected traffic or
  hazard event must reach the driver through that template path, exactly as a
  real detector event would, not through an LLM spotter call.

1. THE SCHEDULER (src/rtv/director/scheduler.py):
  - ScriptedDirector implements DirectorEngine. poll(state) walks the bound
    script, evaluates each pending injection's conjunctive trigger against the
    current state and returns the events to fire now.
  - Honour every InjectionTrigger condition: at_session_time, at_lap,
    at_lap_dist_pct, after + delay_s, while_flag, once. Delays are SESSION
    seconds, so a 4x replay rehearses the same race; a test pins it.
  - poll() is on the pump's hot path: cheap, synchronous, non-blocking, never
    raises into the pump. Firing is idempotent per entry and edge-triggered: a
    condition that stays true does not re-fire.
  - reset() forgets what fired (stage 07 calls pitwall.reset() at every new
    segment, which resets the director). describe() reports loaded, fired and
    pending. NoopDirector stays the app default and is not changed.

2. THE STATE AN AGENT SEES DURING AN INJECTION:
  - The telemetry is real and says green while an injected yellow is active.
    Never rewrite telemetry fields. Add an `injected` overlay to the state
    slice listing the active injected conditions (kind, payload, director_id,
    since), so an agent reasoning about a scripted yellow sees why, and the
    validator can cite the injected payload's numbers with their provenance.
    Document the rule.

3. THE EVAL RUNNER (evals/run_director.py, or a --scenario mode of
   evals/run_pitwall.py; pick one and document it):
  - Builds an engine and orchestrator with ScriptedDirector over (a) the
    synthetic race or (b) a recorded session from the store via store_source,
    read-only. Offline by default with the scripted provider; --live uses Kimi.
  - Writes its own journal with SessionRecorder to an eval output directory
    (gitignored, given on the command line), with source "eval" and the
    scenario name in the lifecycle record. Never under data/sessions.
  - Reports, per injection: when it fired, which agents and which
    deterministic callers it woke, what aired, and the eval checks below.
  - A replayed .ibt has no CarIdx channels: traffic injections have no
    standings there. Say so in the report rather than faking a field.

4. WEATHER AND REGULATION FINALLY MEAN SOMETHING:
  - weather_change and regulation_change are director-only event types. Make
    their consumers real: a scripted regulation change (a stop becomes
    mandatory) wakes the strategist, which proposes a plan revision through
    stage 08's propose_plan_revision, validated against the regulation
    payload's facts; a scripted rain arrival wakes the vehicle engineer and the
    strategist. Triggers and predicates in the existing style.
  - Grounding still applies: an agent may say rain is coming only because the
    injected event said so, and must not turn a temperature drift into a
    forecast. Test the refusal.
  - In the live app nothing emits these events, so live behaviour is unchanged.

5. THE PROOF (extend, do not replace, tests/test_director.py):
  - The stage-5 equivalence tests
    (test_a_scripted_yellow_is_shaped_exactly_like_a_real_one,
    test_a_scripted_fcy_produces_the_same_downstream_behaviour,
    test_the_agents_see_the_injection_through_the_running_pump) pass UNCHANGED
    against ScriptedDirector.
  - The five-entry docs/director_scenario.example.json runs end to end on the
    synthetic race: FCY -> restart -> rain -> mandatory stop -> hazard, each at
    its scripted condition, each with its provenance keys, each waking what the
    equivalent real event wakes (the hazard: the B2 template, not the LLM
    spotter).
  - Determinism: the same script over the same replay gives a byte-identical
    event log twice. A chained injection (after + delay_s) fires the right
    session interval later at 1x and 4x.
  - No fake data: running the app with RTV_PITWALL_DIRECTOR_SCRIPT set injects
    nothing and writes no injected event to data/sessions.
  - Eval cases for weather and regulation, generated like the others.

DEFINITION OF DONE: pytest, ruff and smoke_offline green offline; the stage-5
equivalence tests pass unmodified; docs/PITWALL.md's "deliberately
unimplemented" director text is rewritten to describe the scheduler, the eval
runner and the injected overlay, keeping the seam argument as the reason it was
cheap to build, and stating that the director never runs in the app.

AFTER THE HEADLESS RUN (for the operator, not part of this session): one Kimi run
of the example scenario over the synthetic race with --live, and one over the
recorded Spa session; check the strategist revises the plan for the mandatory
stop and the radio reacts to the yellow.
