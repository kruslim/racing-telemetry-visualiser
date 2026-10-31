HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md as a deviation of this stage.
- Read CLAUDE.md first and follow its commands and conventions (repo virtualenv, `RTV_` settings with a line in .env.example, lazy optional imports, offline tests).
- Before finishing: run the FULL pytest suite, `ruff check .` and `scripts/smoke_offline.py`, and iterate until all three are green (offline - no iRacing, no KIMI_CODE_API_KEY, no network). Do not finish with failing tests or with tests skipped because they are broken. Never delete or rewrite a passing test to make new code pass.
- Update docs/PITWALL.md: the "Current architecture" page at the top for every area this stage changes (including the interfaces the next stage consumes), and a short history entry for this stage (what was built, deviations) in the stage-history appendix. Do not append another long stage section at the end of the file.
- Do not break v1 or earlier stages (post-race review, coaching endpoints, MCP server, the four agents, the journal). No fake data in the app: generated sessions live in evals/ and are used by tests and evals only.

TASK: Stage 07 - session lifecycle and autonomous arm/disarm (plan task D3).

Run order and dependencies: stages run 07, 08, 10, 09. This stage depends on
nothing pending. If the deterministic spotter (plan task B2) has merged, its
template calls are covered below; if not, ignore those lines.

Read first: CLAUDE.md, the "Current architecture" page of docs/PITWALL.md and its
"Race journal" section, src/rtv/services.py (build_services, on_state,
_begin_journal), src/rtv/store/journal.py, src/rtv/racestate/summary.py,
src/rtv/ingest/live.py (_open_session), src/rtv/racestate/engine.py (reset,
bind_catalog, flag phase) and src/rtv/pitwall/orchestrator.py (reset, suspend,
resume, set_enabled, the call budget).

GOAL
Make the pitwall run a race weekend by itself. Today an operator starts the
server, presses Start, and has to reset by hand between sessions. After this
stage the system notices each session (practice, qualifying, race) begin and
end, gives each one a clean engine, agent layer, radio, call budget and journal
run, arms the agents for the green flag and stands them down at the end, with no
human in the loop. Everything here is deterministic supervision: no new agents
and no new LLM calls.

ALREADY BUILT - REUSE, DO NOT REBUILD
- The race journal: rtv.store.journal.SessionRecorder writes
  data/sessions/<session_id>/runs/<run_id>.jsonl (race events, aired radio,
  agent activity, periodic state snapshots, lifecycle start/end records), opened
  by build_services when a replay starts or the live poller reports IN_SESSION
  for a new session id. Read endpoints: GET /api/v1/sessions/{id}/pitwall-runs,
  /pitwall-log and /pitwall-log/state. Do NOT add a second journal, a DuckDB
  journal table or a /journal endpoint; extend this one.
- The deterministic post-race summary: rtv.racestate.summary.race_summary and
  GET /api/v1/sessions/{id}/summary?run=. It is the session-end summary; do not
  write another.
- PitwallOrchestrator.reset() (cooldowns, breakers, radio, director, a fresh
  per-session call budget), suspend()/resume() (telemetry gap, separate from the
  operator kill switch), set_enabled() (the operator kill switch) and
  max_calls_per_session.

GAPS IN TODAY'S CODE (the reason for this stage; confirm each before fixing)
- EventType.SESSION_STATE_CHANGE is declared but nothing emits it. The engine
  decodes SessionState into state.session.state and stops there.
- The live poller opens a new RTV session only when the subsession id changes.
  Practice, qualifying and race of one event share a subsession, so they share
  one session id, one journal run and one engine state: the race inherits
  practice's fuel model, cooldowns, radio and spent call budget. Nothing reads
  SessionNum.
- On a new live session id, on_state opens a journal run and names the session
  on the engine, but resets neither the engine's accumulators (bind_catalog only
  runs when the schema hash changes) nor the agent layer.
- POST /api/v1/replay/start calls engine.reset() but not pitwall.reset(), so a
  second replay in one server run inherits the first one's cooldowns, radio and
  spent budget.

1. SESSION LIFECYCLE (src/rtv/racestate/lifecycle.py):
  - A SessionLifecycle state machine fed by the engine on every frame from
    channels the engine already reads plus SessionNum, and the session type from
    the session-info YAML (Sessions[SessionNum].SessionType). States at minimum:
    idle -> practice / qualify / race -> pre_green -> green -> checkered ->
    cool_down -> ended. Green must use the engine's existing flag-phase logic,
    which treats start_hidden after the formation lap as green (the real Spa
    .ibt never shows a plain green bit after the start). Do not invent a channel:
    a transition the catalog cannot back leaves the state "unknown", and a
    `lifecycle` capability flag (with `missing` entries) says why.
  - Emit the existing session_state_change for every SessionState transition,
    and add session_start, session_green, session_checkered and session_end as
    ordinary RaceEvents on engine.bus (payload: session_num, session_type, from,
    to). The journal, /ws/pitwall, the orchestrator and both UIs get them with
    no new wiring; no consumer branches on their origin. Add them to the event
    catalog in docs/PITWALL.md. None of the four agents is woken by them.

2. ONE CLEAN SEGMENT PER SESSION (always on, not gated by autonomy - it is a
   correctness fix):
  - A new segment starts on a SessionNum change, a new RTV session id, or the
    session clock going backwards. On a new segment: reset the engine's
    accumulators while keeping the catalog binding and session info (add a
    small engine method rather than rebinding), call pitwall.reset(), end the
    current journal run with reason "session_changed" and begin a new run whose
    lifecycle record carries session_num and session_type.
  - list_runs / pitwall-runs report session_num and session_type per run so the
    review can label "Practice / Qualifying / Race" runs. default_run keeps its
    rule (newest run the agents spoke in, else newest).
  - The lap table covers the whole RTV session. The summary for a run must count
    only that run's laps (filter by the run's session-time bounds, or by
    session number if the lap rows carry it), so practice laps never appear in
    the race summary.
  - POST /api/v1/replay/start also calls pitwall.reset() when agents are mounted.

3. AUTONOMOUS ARM / DISARM (gated by RTV_PITWALL_AUTONOMOUS, default false):
  - Autonomy adds a "standby" state to the orchestrator next to "suspended":
    dispatch is blocked with reason "standby" in the activity record, and status
    reports it. Autonomy never turns on agents the operator switched off with
    the kill switch or per-agent flags; the operator always wins.
  - Arm on session_green when the session type is listed in
    RTV_PITWALL_AUTONOMOUS_SESSIONS (default "race"). Practice and qualifying
    get a reduced roster from RTV_PITWALL_ROSTER_PRACTICE and
    RTV_PITWALL_ROSTER_QUALIFY (comma-separated agent names; default empty, so
    no strategist bills for a practice session). Deterministic callers (the B2
    spotter templates, if merged) cost nothing and are not held in standby.
  - Disarm at session_end, or at session_checkered once the player has taken
    the flag. On disarm, log the segment's model calls used against the budget
    and write them into the journal run's end record.
  - With RTV_PITWALL_AUTONOMOUS=false the agents are invoked exactly as today:
    the same dispatch decisions on the synthetic race, with lifecycle events on
    the bus as the only addition (and they wake nobody). Prove it with a test.

4. SESSION-END HOOK (what stage 09's debrief consumes):
  - AppServices.on_session_end: a list of callbacks receiving one SessionEnded
    record (session_id, run_id, journal path, session_num, session_type,
    reason, calls_used). Fired once per segment when its journal run closes,
    from a worker so it never blocks the frame path; a callback that raises is
    logged and ignored.
  - One built-in subscriber: compute race_summary for the run and store it next
    to the run as <run_id>.summary.json. GET /sessions/{id}/summary?run= serves
    the stored file when present and computes it otherwise (same shape).
  - A notifier protocol with a no-op default and a file/console implementation
    that emits the summary on session end. Any webhook or email notifier must be
    import-guarded and off by default; nothing in the default test path touches
    the network.
  - Document the hook signature and the journal fields stage 09 reads.

5. UI (desktop is the primary UI; the web pages are frozen to fixes only):
  - The desktop live view shows the lifecycle state, session type and whether
    the agents are armed, in standby (and why) or off. View-model logic goes in
    src/rtv/desktop/model/ with tests; keep PySide6 imports inside rtv.desktop.
  - The desktop review labels each run with its session type.

6. TESTS (offline):
  - A synthetic practice -> qualify -> race stream in one session (extend the
    test-only generators in evals/, never src/): three segments, three journal
    runs, and the race segment does not inherit the practice fuel model,
    cooldowns, radio or spent budget.
  - Two consecutive replays in one process produce two independent journal runs
    with no state bleed (cooldowns, radio, budget, director).
  - Autonomy on: standby before green, armed at green, disarmed at the end, the
    practice roster honoured, the operator kill switch never overridden.
  - Autonomy off: dispatch behaviour unchanged (see 3).
  - The session-end hook fires once per segment, and a raising callback is
    contained.
  - On the real Spa fixture (tests/test_real_ibt.py style, skipped without it):
    one race segment, session_green on lap 1, session_checkered at the flag, and
    the summary counts only that segment's laps.

DEFINITION OF DONE: pytest, ruff and smoke_offline green offline; every new
setting in config.py and .env.example; docs/PITWALL.md documents the lifecycle
states and events, the segment rule, standby versus the kill switch, the
session_num/session_type journal fields and the on_session_end signature.

AFTER THE HEADLESS RUN (for the operator, not part of this session): replay the
Spa .ibt twice in one server run with RTV_PITWALL_AUTONOMOUS=true and agents on,
check two clean journal runs, and compare the second with the latest baseline
run using scripts/compare_runs.py.
