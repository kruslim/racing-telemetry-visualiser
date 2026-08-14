HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md under "Deviations".
- Before finishing: run the FULL `pytest` suite and iterate until it is completely green (offline - no iRacing, no ANTHROPIC_API_KEY needed). Do not finish with failing or skipped-because-broken tests.
- Also run any smoke script mentioned below and make it pass.
- Update docs/PITWALL.md with what you built, the public interfaces the next stage will consume, and any deviations from this spec.
- Do not refactor or break existing v1 functionality (analysis UI, coaching API, MCP server, existing tests).

TASK:

Read first: whole repo state after prompts 1-7, especially docs/PITWALL.md.

GOAL
Give the pitwall a memory and a future. Today every agent is woken by an event,
reads a slice, speaks once and forgets - so it can answer questions but cannot
engineer a race. This stage adds the two things that close that gap: a persistent
RacePlan the agents revise, and a deterministic projection layer that lets them
talk about what happens NEXT instead of only what is true now.

This is the architectural stage. Get the grounding discipline right and the rest
is mechanical.

1. PROJECTION LAYER (src/rtv/racestate/projection.py) - PURE, DETERMINISTIC, NO LLM:
  Extend the existing strategy_math.py pattern. Every function returns a grounded
  result or a stated reason it cannot; nothing is ever estimated into existence.
  - Tyre degradation: fit lap-time-vs-stint-lap over the CURRENT stint's green,
    non-pit, non-outlier laps. Return slope (s/lap), confidence, sample count, and
    a projected lap time N laps out. With fewer than a configurable minimum of
    clean laps, return ungrounded with a reason - a two-point deg curve is a guess.
  - Fuel-save: litres/lap needed to extend the stint by N laps, and the lap-time
    cost implied by the existing consumption model. Ungrounded without per_lap.
  - Traffic projection: given standings, gaps and relative pace, when the player
    catches the car ahead / is caught by the car behind. Requires gap_basis; None
    without it, exactly as gaps already behave.
  - Undercut/overcut delta: reuse simulate_pit_outcome, project both branches over
    the out-lap and in-lap, return the net. State assumptions in the existing
    assumptions[] style.
  All of it unit-tested standalone against the scripted scenario with NO model in
  the loop, the way test_strategy_math.py already does.

2. THE RACE PLAN (src/rtv/racestate/plan.py) - the fifth citizen of the blackbox:
  - RacePlan (Pydantic, versioned like RaceState): stop count and target laps,
    fuel plan per stint, a pace target, a tyre-management posture
    (push / neutral / conserve), and for each element the PROVENANCE - derived,
    agent-proposed, or operator-set - plus the rationale.
  - A deterministic baseline plan is computed from fuel, race length and the
    projection layer as soon as the data supports it. It must degrade the same way
    everything else does: no fuel model means no fuel plan, not a default one.
  - Plan revision is a PROPOSAL, not a write. An agent proposes a revision; it
    lands only if it passes the existing validator against that agent's FactSet
    and a deterministic sanity check (a target lap must be inside the fuel window,
    a stop count must be reachable, etc). A rejected proposal is recorded with its
    reason and does NOT mutate the plan.
    This preserves the load-bearing rule that no LLM output silently becomes a
    citable fact. Assert it with a test: a bogus proposal must not change the plan
    AND must not become quotable by the next agent.
  - Plan changes emit an ordinary RaceEvent (plan_revised) on the existing bus,
    carrying what changed and why, so the UI ticker and journal get it for free.

3. AGENTS CONSUME THE PLAN:
  - Add the current plan (or a scoped view of it) to the state slice of the agents
    that should see it. The strategist owns revisions. The vehicle engineer and
    coach READ the tyre-management posture and the pace target - so the engineer
    can say "that posture needs you off the kerbs in T4" - but must not revise it.
    Enforce with slices and tools, not prompt text, per the established pattern.
  - New tools, scoped per agent: get_race_plan, get_tyre_degradation,
    get_fuel_save_options, get_traffic_projection, propose_plan_revision
    (strategist only).
  - Do not add a new agent in this stage.

4. EVALS:
  - Extend evals/pitwall_checks.py with plan_ok (a revision is inside the
    deterministic feasible band) and projection_ok (no agent asserts a projected
    figure the projection layer marked ungrounded).
  - Add degraded cases where deg is unfittable and where gap_basis is absent - a
    set where everything is knowable cannot tell a grounded strategist from a
    confident one, which is why the existing harness already does this.

5. UI:
  - Surface the plan on the pit stand: current plan, next stop lap, tyre posture,
    and a revision history with rationale. Follow the existing view-model + n/a
    discipline exactly; a plan element with no grounding renders n/a, never a zero.

6. SMOKE:
  - scripts/smoke_pitwall_plan.py: replay the scripted race and assert a baseline
    plan appears, survives the yellow, is revised at the pit window, and that every
    revision traces to a grounded proposal.

DEFINITION OF DONE: pytest + evals green offline; smoke green; the projection layer
fully tested with no model in the loop; docs/PITWALL.md documents the RacePlan
schema, the proposal/validation flow, and why a proposal is not a write.
