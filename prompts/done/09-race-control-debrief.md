HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md as a deviation of this stage.
- Read CLAUDE.md first and follow its commands and conventions (repo virtualenv, `RTV_` settings with a line in .env.example, lazy optional imports, offline tests).
- Before finishing: run the FULL pytest suite, `ruff check .` and `scripts/smoke_offline.py`, and iterate until all three are green (offline - no iRacing, no KIMI_CODE_API_KEY, no network). Do not finish with failing tests or with tests skipped because they are broken. Never delete or rewrite a passing test to make new code pass.
- Update docs/PITWALL.md: the "Current architecture" page at the top for every area this stage changes (including the interfaces the next stage consumes), and a short history entry for this stage (what was built, deviations) in the stage-history appendix. Do not append another long stage section at the end of the file.
- Do not break v1 or earlier stages (post-race review, coaching endpoints, MCP server, the four agents, the journal). No fake data in the app: generated sessions live in evals/ and are used by tests and evals only.

TASK: Stage 09 - the race-control agent (part A, plan task D6a) and the debrief
analyst (part B, plan task D6b).

The two parts share little. Each has its own definition of done and can be built
and merged as its own PR: run part A, then part B, or only the part you were
asked for. If you were asked for one part, do not start the other.

Run order and dependencies: stages run 07, 08, 10, 09. Both parts need stage 07
(lifecycle, segments, on_session_end) and stage 08 (the race plan) merged, plus
plan tasks A1 (typed FactSet: (value, unit_family, path) facts, match on value
and unit family) and B2 (the deterministic caller path in the radio, used by the
spotter templates). Part B also uses plan task A2 (one check_prose for every
coach). Stage 10 is not required; if it has merged, part A adds one director
scenario to the eval path (see RC4). If a required dependency is missing, stop
and record that in docs/PITWALL.md.

Read first: CLAUDE.md, the "Current architecture" page of docs/PITWALL.md (agent
table, radio rules, journal schema, on_session_end), src/rtv/pitwall/agents/
and framework.py (AgentSpec, AgentRuntime.match), src/rtv/pitwall/radio.py,
src/rtv/racestate/detectors.py (the incident detector), rtv.catalog.enums
(SessionFlags bits), src/rtv/racestate/summary.py, src/rtv/coaching/features.py,
src/rtv/pitwall/provider.py (thinking=True opt-in), evals/pitwall_cases.py and
evals/pitwall_checks.py.

The stage-2/3 claim is that adding an agent is configuration, not framework
work. Hold that line. If either agent needs a framework change, make the
smallest one and record it as a finding, rather than widening every agent's
fact set.

===========================================================================
PART A (D6a) - THE RACE-CONTROL AGENT
===========================================================================

Owns compliance and consequence: the sentences nobody says today because no
agent is shown the data.

RC1. DETERMINISTIC DETECTORS FIRST (in the engine, before the agent exists):
  - Incident count against the session's limit (the incident limit from the
    session-info YAML, if present), on top of the existing incident detector.
  - Repeated track-limit offs within a window, from the existing offtrack
    detector.
  - A penalty state change for the player from the SessionFlags bits the
    catalog decodes (black, furled, repair, disqualify), where present.
  - Blue-flag non-compliance over N corners. It needs CarIdx standings, which a
    replayed .ibt does not have; on such a session the capability flag is
    False and the agent is never woken by it.
  Do not invent a channel: where the catalog does not back a detector, its
  capability flag goes False with `missing` entries. A stated blind spot beats
  a guessed one. Aggregate in the engine and wake the agent once.

RC2. TWO PATHS, LIKE THE SPOTTER AFTER B2:
  - A call that is only a read-out ("incidents 14 of 17", "black flag shown")
    goes through the B2 deterministic caller as a template: exact numbers, no
    model call.
  - The LLM agent (src/rtv/pitwall/agents/race_control.py) is woken only when
    there is a consequence to reason about (one more incident ends the race; a
    drive-through has to be served; track limits are one off from a warning).
    It is the quietest agent on the channel: triggers, predicates and cooldowns
    in the existing style.
  - Its incident and limit numbers must match typed facts exactly (count
    family). A race-control agent that misreports the incident count is worse
    than one that says nothing.
  - A penalty that must be served in the pit lane joins the pit_stop decision
    group (B3), so it and the strategist's stop call cannot contradict each
    other on the radio.
  - State slice: flags, incidents and limit, penalty state, player, the
    relevant standings. Not fuel, not tyres, not the race plan.
  - Model from settings like the other agents (RTV_PITWALL_MODEL_RACE_CONTROL,
    falling back to the rtv.llm default).

RC3. UI (desktop primary; web frozen): the agent appears in the desktop live
  view's agent lanes and the review audit. Add it to the agent list in
  src/rtv/desktop/model/activity.py (and the shared JS AGENTS list only if a
  fix is needed for the frozen web pages to tolerate a fifth agent).

RC4. EVALS AND TESTS:
  - Add the agent to evals/pitwall_cases.py via its own AgentRuntime.match(),
    like the others; no hand-kept trigger list.
  - Checks: consequence_ok (it only speaks when there is a consequence),
    count_ok (the incident figure matches the event exactly), refusal_ok (no
    penalty claim without a penalty channel).
  - Detector tests on the synthetic race, and on the real Spa fixture (skipped
    without it): the incident counts the .ibt records produce the right events,
    and blue-flag is reported as a blind spot.
  - If stage 10 has merged: one scenario in the eval path that injects an
    incident-limit approach and checks the template and the agent call.

DEFINITION OF DONE (part A): pytest, ruff and smoke_offline green offline; five
live agents in the agent table of docs/PITWALL.md; an explicit note on whether
"a new agent is configuration" survived.

===========================================================================
PART B (D6b) - THE DEBRIEF ANALYST (post-session, never on the radio)
===========================================================================

Runs once per race segment, off stage 07's on_session_end hook, over that
segment's journal run. Different economics from the live agents, and the design
should show it.

DB1. INPUTS (no raw telemetry ever reaches the model):
  - From the journal run: race events, aired radio (with the late/expired tags of plan task
    B1 if present), agent run outcomes, plan revisions (accepted and rejected)
    and the final state snapshot. Leave the periodic state snapshots out; a
    Spa journal is about 6.5 MB, mostly snapshots.
  - The stored deterministic summary (<run_id>.summary.json from stage 07).
  - Layer-1 findings for the recorded laps from rtv.coaching.features via
    CoachingService (/coaching/lap-findings). Reuse; do not re-implement
    analysis the post-race coach already does.
  - Corner labels as the review shows them (plan task E1, if merged).

DB2. THE MODEL CALL:
  - No latency budget: use KimiProvider with thinking=True (the provider's
    opt-in), a structured answer through the forced respond tool (never
    messages.parse), and a model from RTV_DEBRIEF_MODEL falling back to the
    rtv.llm default.
  - Output, validated: race summary; what the plan was and where it changed;
    where time was lost (from the findings and lap times, not vibes); recurring
    issues by corner; tyre and fuel against the projection; a short prioritised
    list of what to work on. Every figure is checked with the typed FactSet
    over the inputs, and every corner claim with A2's check_prose. A section
    that fails is repaired once, then refused with a reason, as the coaches do.
  - With no key, or when the call fails, it still produces a debrief: the
    deterministic summary plus a plain note that the analysis half is
    unavailable. The offline tests exercise exactly this path.
  - RTV_DEBRIEF_AUTO (default true when agents are mounted) controls the
    automatic run at session end; RTV_DEBRIEF_SESSIONS (default "race") the
    session types it runs for.

DB3. STORAGE AND API:
  - Stored next to the run as <run_id>.debrief.json, and a debrief record in
    the run's journal.
  - GET /api/v1/sessions/{id}/debrief?run= returns it (404 when none).
    POST /api/v1/sessions/{id}/debrief?run= regenerates it from the recorded
    journal; it never re-runs the live agents.
  - Delivered through stage 07's notifier.

DB4. UI (desktop primary; web frozen):
  - A debrief tab in the desktop review: pick a run, read the debrief, and
    click any lap or corner it references to open that lap in the review's lap
    analysis (and the corner's window). Its "Regenerate" button says that it
    calls Kimi before it does. View-model logic in src/rtv/desktop/model/ with
    tests, n/a rule as everywhere.

DB5. EVALS AND TESTS:
  - Hermetic debrief cases driven by ScriptedModel over a journal produced by
    replaying the synthetic race: every claimed figure traces to the journal,
    the summary or the findings; no lap referenced that was not recorded; the
    priority list is supported by the findings above it; a swapped-fact case
    (right number, wrong fact) is refused.
  - The session-end path end to end: a synthetic race with autonomy on ends,
    the debrief is produced with the scripted provider, and with no key the
    deterministic fallback is produced.
  - On the real Spa fixture (skipped without it): the no-key debrief of an
    agents-off replay references only recorded laps.

DEFINITION OF DONE (part B): pytest, ruff and smoke_offline green offline and
with no key; docs/PITWALL.md documents the debrief inputs, schema, endpoints and
settings, and that the debrief is not a radio agent.

AFTER THE HEADLESS RUN (for the operator, not part of this session): part A, a
Kimi Spa replay compared with the latest baseline using scripts/compare_runs.py
(race control should be near silent on a clean race); part B, a Kimi debrief of
the recorded Spa run, read by the user.
