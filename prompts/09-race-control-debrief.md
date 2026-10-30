HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md under "Deviations".
- Before finishing: run the FULL `pytest` suite and iterate until it is completely green (offline - no iRacing, no ANTHROPIC_API_KEY needed). Do not finish with failing or skipped-because-broken tests.
- Also run any smoke script mentioned below and make it pass.
- Update docs/PITWALL.md with what you built, the public interfaces the next stage will consume, and any deviations from this spec.
- Do not refactor or break existing v1 functionality (analysis UI, coaching API, MCP server, existing tests).

TASK:

Read first: whole repo state after prompts 1-8, especially docs/PITWALL.md.

GOAL
Two new agents, and they are deliberately the only two. One lives during the race
and owns the decisions nobody currently owns; one lives after it and closes the
loop that makes this system keep the driver updated without being asked.

The stage-2/3 claim is that adding an agent is configuration rather than framework
work. Hold that line: if either agent needs a framework change, that is a finding
worth recording in Deviations, not a licence to widen every agent's fact set.

1. RACE CONTROL AGENT (src/rtv/pitwall/agents/race_control.py):
  Owns compliance and consequence - the sentences the spotter and strategist
  currently cannot say because they are not shown the data.
  - New DETERMINISTIC detectors first, in the engine, before any agent exists
    (the established pattern - aggregate in the blackbox, wake the agent once):
    incident-count thresholds relative to the session limit, repeated track-limit
    offs, a penalty state change, blue-flag non-compliance over N corners.
    Do NOT invent a channel. Where iRacing exposes no penalty channel in the
    runtime catalog, the capability flag goes False and the agent is simply never
    woken by it - a stated blind spot beats a guessed one.
  - Triggers, predicates and cooldowns in the existing style. This agent should be
    the QUIETEST on the channel: it speaks when there is a consequence, not when
    there is an incident.
  - Output contract in the established style, with the incident/limit numbers
    exact-matched against the fact set. A race control agent that misreports your
    incident count is worse than one that says nothing.
  - State slice: flags, incidents, penalties, player, the relevant standings.
    NOT fuel, NOT tyres, NOT the race plan.

2. DEBRIEF ANALYST (post-session, not on the radio):
  Runs ONCE, off the stage-07 session_end hook, over the stage-07 race journal.
  Entirely different economics from the live agents and the design must reflect it:
  - No latency budget. Use extended thinking (the Layer-3b coach's adaptive
    thinking pattern is the reference, not the live agents' no-thinking default).
  - Full-race context: the journal, the final state, every radio call, the plan
    revision history, and the EXISTING Layer-1 coaching features over the recorded
    laps. Reuse rtv.coaching.features - do not re-implement analysis that the
    post-hoc coach already does correctly.
  - Output a structured, validated debrief: race summary, what the plan was and
    where it changed, where time was actually lost (from telemetry, not vibes),
    the recurring issues by corner, tyre and fuel performance against projection,
    and a short prioritised list of what to work on. Every figure grounded, same
    validator, same refusal discipline.
  - It must produce SOMETHING useful with no API key: fall back to the stage-07
    deterministic summary and say plainly that the analysis half is unavailable.
    The offline test path exercises exactly this.
  - Delivered through the stage-07 notifier, written to the journal, and exposed at
    GET /api/v1/sessions/{id}/debrief.

3. UI:
  - A debrief view: pick a session, read the debrief, click through to the existing
    v1 analysis worksheet for any lap it references. This is where v1 and v2 finally
    become one product rather than two - the debrief should deep-link into the
    telemetry UI that already exists.
  - Follow the existing view-model + n/a discipline.

4. EVALS:
  - Add both agents to evals/pitwall_cases.py using their own AgentRuntime.match(),
    per the existing generation approach - no hand-maintained trigger list.
  - Race control checks: consequence_ok (it only speaks when there is one),
    count_ok (the incident figure matches the event exactly), refusal_ok (no
    penalty claim without a penalty channel).
  - Debrief checks: every claimed figure traces to the journal or the coaching
    features; no lap referenced that was not recorded; the priority list is
    supported by the findings above it.

5. SMOKE:
  - scripts/smoke_pitwall_debrief.py: run the scripted race under autonomy, let it
    end, assert a debrief is produced with the scripted provider and that the
    no-key path still yields the deterministic summary.

DEFINITION OF DONE: pytest + evals green offline and with no API key; smoke green;
six agents documented in docs/PITWALL.md with the same table the four already have;
an explicit note recording whether the "a new agent is configuration" claim survived.
