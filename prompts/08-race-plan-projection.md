HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md as a deviation of this stage.
- Read CLAUDE.md first and follow its commands and conventions (repo virtualenv, `RTV_` settings with a line in .env.example, lazy optional imports, offline tests).
- Before finishing: run the FULL pytest suite, `ruff check .` and `scripts/smoke_offline.py`, and iterate until all three are green (offline - no iRacing, no KIMI_CODE_API_KEY, no network). Do not finish with failing tests or with tests skipped because they are broken. Never delete or rewrite a passing test to make new code pass.
- Update docs/PITWALL.md: the "Current architecture" page at the top for every area this stage changes (including the interfaces the next stage consumes), and a short history entry for this stage (what was built, deviations) in the stage-history appendix. Do not append another long stage section at the end of the file.
- Do not break v1 or earlier stages (post-race review, coaching endpoints, MCP server, the four agents, the journal). No fake data in the app: generated sessions live in evals/ and are used by tests and evals only.

TASK: Stage 08 - the race plan and the projection layer (plan task D4).

Run order and dependencies: stages run 07, 08, 10, 09. This stage needs, merged
on main before it starts:
- Stage 07 (session lifecycle): a plan belongs to one race segment and is reset
  with it.
- Plan task A1, the typed FactSet: facts are (value, unit_family, path), e.g.
  fuel.laps_remaining = 6.2 laps, and the validator requires a match on value
  AND unit family. The plan's numbers become facts of that kind.
- Plan task B3, cross-agent decision groups in RadioFeed (which needs B1,
  air-time validity): subjects that belong to one decision (pit_stop,
  damage_stop, ...) share a supersede group across agents.
If any of these is missing, stop and record that in docs/PITWALL.md rather than
building a substitute.

Read first: CLAUDE.md, the "Current architecture" page of docs/PITWALL.md,
src/rtv/racestate/strategy_math.py (stop_plan, StopPlan, fuel_projection,
simulate_pit_outcome, tyre_trend), src/rtv/pitwall/validator.py and the shared
units module A1 added, src/rtv/pitwall/radio.py (decision groups, validity),
src/rtv/pitwall/tools.py and the four agent specs.

GOAL
Give the pitwall a memory and a future. Today each agent is woken by an event,
reads a slice, speaks once and forgets, so it can answer questions but cannot
engineer a race, and two agents can contradict each other on the same stop. This
stage adds a persistent, versioned RacePlan the strategist revises and the others
read, and a deterministic projection layer so agents can talk about what happens
next, not only what is true now.

1. PROJECTION LAYER (src/rtv/racestate/projection.py) - pure, deterministic, no LLM:
  Extend strategy_math.py's pattern: every function returns a grounded result
  or a stated reason it cannot, with units on every number.
  - Lap-time degradation: fit lap time against stint lap over the current
    stint's green, non-pit, non-outlier laps. Return slope (s/lap), confidence,
    sample count and a projected lap time N laps out. Below a configurable
    minimum of clean laps it is ungrounded with a reason. (The existing
    tyre_trend is a temperature trend, not lap-time degradation; keep it.)
  - Fuel save: litres per lap needed to stretch the stint by N laps, from the
    existing consumption model. Ungrounded without per-lap consumption.
  - Traffic: when the player catches the car ahead or is caught by the car
    behind, from standings, gaps and relative pace. None without gap_basis, as
    gaps already behave. A replayed .ibt has no CarIdx channels, so this is
    ungrounded there by design; live telemetry has them.
  - Undercut / overcut: reuse simulate_pit_outcome over both branches and
    return the net, with assumptions[] in the existing style.
  Unit-test it standalone, with no model in the loop, as the strategy-math tests
  do, and on the real Spa fixture (skipped without it): degradation fits the
  real laps with a sample count, traffic is ungrounded with its reason.

2. THE RACE PLAN (src/rtv/racestate/plan.py):
  - RacePlan (Pydantic, versioned): stop count and target laps, fuel per stint,
    pace target, tyre posture (push / neutral / conserve). Every element carries
    provenance (derived, agent_proposed, operator_set), a rationale and the
    plan version it was set in. It lives in RaceState (so /ws/pitwall, the
    journal's state snapshots and the final snapshot carry it) and exists only
    in race segments; stage 07's segment reset clears it.
  - A baseline plan is derived from the existing stop_plan plus the projection
    layer as soon as the data supports it, and degrades like everything else:
    no fuel model means no fuel plan, not a default one.
  - A revision is a PROPOSAL, not a write. The strategist proposes through a
    tool; it lands only if (a) every number in it passes the typed FactSet
    check against the strategist's facts and (b) a deterministic sanity check
    passes (target lap inside the fuel window, stop count reachable, and so
    on). A rejected proposal is recorded with its reason and changes nothing.
  - Plan numbers enter the FactSet as typed facts with a plan.* path, e.g.
    plan.next_stop_lap = 13 lap (provenance: agent_proposed, version 4). A
    proposal must be backed by deterministic facts (fuel, projection, the
    regulation payload), never by an earlier agent-proposed plan value, so the
    plan cannot launder a model's guess into a citable fact. Tests: a bogus
    proposal does not change the plan AND is not quotable by the next agent; a
    plan figure cited with the wrong unit family is refused (an A1-style
    swapped-fact case).
  - An accepted revision emits a plan_revised RaceEvent (what changed, why, old
    and new version); a rejected one emits an activity record
    (plan_proposal_rejected) so the review audit shows it. Both reach the
    journal through the existing wiring.

3. AGENTS CONSUME THE PLAN:
  - The plan (or a scoped view) goes into the state slice of the agents that
    should see it. The strategist owns revisions. The vehicle engineer and the
    coach read the tyre posture and pace target ("that posture needs you off
    the kerbs in T4") but cannot revise it. Enforce with slices and tools, not
    prompt text.
  - New tools, scoped per agent: get_race_plan, get_lap_time_degradation,
    get_fuel_save_options, get_traffic_projection, propose_plan_revision
    (strategist only).
  - Radio: a call about the stop uses the pit_stop decision group from B3, so a
    revised box lap supersedes the strategist's earlier queued pit call, and a
    critical engineer damage_stop call supersedes a queued strategist pit_stop
    advisory. A plan-derived box call carries B1 validity: valid until the
    target lap, invalid once the car is past pit entry on that lap.
  - If plan task D1 (the agent sees its own previous call) has merged, include
    the plan version in that context. No new agent in this stage.

4. EVALS (evals/, hermetic, generated from the synthetic race as today):
  - evals/pitwall_checks.py gains plan_ok (an accepted revision lies inside the
    deterministic feasible band) and projection_ok (no agent states a projected
    figure the projection layer marked ungrounded).
  - Degraded cases: degradation unfittable, gap_basis absent. A set where
    everything is knowable cannot tell a grounded strategist from a confident
    one.

5. UI (desktop is the primary UI; the web pages are frozen to fixes only):
  - Desktop live view: current plan, next stop lap, tyre posture and the
    revision history with rationale. View-model logic in
    src/rtv/desktop/model/ with tests, following the n/a rule: an element with
    no grounding renders n/a, never zero.
  - Desktop review: plan revisions (accepted and rejected) on the timeline and
    in the agent audit.

6. END-TO-END TEST (pytest, on the synthetic race with ScriptedModel; replaces
   the old smoke script, which the cleanup removed): a baseline plan appears,
   survives the yellow, is revised at the pit window, every revision traces to
   a grounded proposal, and a rejected proposal leaves the plan unchanged.

DEFINITION OF DONE: pytest, ruff and smoke_offline green offline; the projection
layer fully tested with no model in the loop; docs/PITWALL.md documents the
RacePlan schema, the plan.* fact paths, the proposal and validation flow, why a
proposal is not a write, and how plan calls use the decision groups.

AFTER THE HEADLESS RUN (for the operator, not part of this session): a Kimi Spa
replay with agents on, compared with the latest baseline run using
scripts/compare_runs.py; check the plan is revised across the race and that the
other agents cite it.
