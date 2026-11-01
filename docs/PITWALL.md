# RTV v2 — Pitwall

The live pitwall: a deterministic race-state engine, five event-driven agents on one
prioritised radio feed, a race journal and a live VLM worker. This file has two parts.
**Current architecture** below is the one page to read before changing anything. The
**[Appendix: stage history](#appendix-stage-history)** holds the per-stage build notes,
measurements and "Deviations" sections, unchanged, for the reasoning behind a choice.

> **How to update this file.** A PR that changes the pitwall updates the
> current-architecture subsection for its area (and the interface list if it adds or
> changes one) so this page stays true, then adds its own notes, measurements and
> deviations as a new section at the end of the appendix. Do not append another stage
> log above the appendix, and do not rewrite older appendix sections: they record what
> was true when they were written, and the page above wins where they differ.

## Current architecture

```
frames (live poller 60 Hz | ReplayDriver)
  -> RaceStateEngine.on_frame           racestate/engine.py   no LLM, ever
       SessionLifecycle (segments)      racestate/lifecycle.py
       detectors -> RaceEvent -> engine.bus (EventBus)
  -> PitwallOrchestrator.dispatch       pitwall/orchestrator.py
       DeterministicCaller.call -> RadioFeed   template calls, no model (pitwall/callers.py)
       trigger match, cooldown, inbox, inflight cap, budget
  -> AgentRuntime.invoke                pitwall/framework.py
       prefetch tools, model + scoped tools, validator, repair or refusal
  -> RadioFeed                          pitwall/radio.py      priority, supersede, airtime
  -> /ws/pitwall, TTS, live view, desktop app
engine.bus -> SegmentJournal (services.py): one journal run per session segment
engine.bus + activity + radio -> SessionRecorder journal (data/sessions/<id>/runs/*.jsonl)
closed run -> AppServices.on_session_end (worker thread): <run_id>.summary.json, notifier,
       then DebriefService (race runs): <run_id>.debrief.json + a debrief journal record
engine.bus -> LiveVisionWorker -> visual_observation events back onto engine.bus

eval path only (evals/run_director.py, never the app):
ScenarioScript -> ScriptedDirector.poll(state) -> injected RaceEvents onto engine.bus
               -> ScriptedDirector.overlay() -> agent slice "injected"
               -> its own journal under the eval --out dir (source "eval")
```

**Engine** (`src/rtv/racestate/`, appendix stage 1 and phase 1). `engine.on_frame(frame,
catalog)` has the live poller's callback signature, and the replay driver calls the same
method, so the engine cannot tell a replay from live telemetry. It keeps one `RaceState`
(session, flags, player, fuel, tyres, car health, standings, conditions, capabilities).
A number with no channel behind it stays `None` and a capability flag says why. A
replayed `.ibt` has no `CarIdx*` channels, so it has no standings and no traffic events.
`engine.health()`, `mark_stale()` / `mark_live()` cover a feed that stops.

**Detectors and events** (`detectors.py`, `models.py`). Pure functions over a frame
window. The engine latches them into `RaceEvent {event_type, tick, session_time, lap,
severity, payload, state_version}`, with no wall-clock field, so a replay gives the same
event log twice. Types cover flags, laps and bests, lock-ups / wheelspin / off-tracks /
incidents, pit entry and exit, stints, pit window and fuel margin, rivals, blue flags,
recurring issues, tyre bands, car health, traffic, `car_alongside` (iRacing's
`CarLeftRight` proximity sensor, live only: an `.ibt` does not record it), weather and
`visual_observation` (published by the vision seam only; the engine never reads it),
and race control's aggregates (`incident_limit`, `track_limits`, `penalty`,
`blue_flag_ignored`; below).
Every event that happened somewhere on the lap (lock-up, wheelspin, off-track, incident,
recurring issue) carries `payload.place = {label, name, lap_dist_pct, provisional}` in the
post-race review's corner labels (`T1 La Source`), from the session corner map in
`RaceState.track` (`rtv.tracks.live`); before two clean laps agree it is the lap-distance
bucket (`C08`) with `provisional: true`.

**Session lifecycle** (`lifecycle.py`, appendix stage 07). `SessionLifecycle` is fed by
the engine every frame from `SessionNum`, `SessionState`, the flag phase and `Lap`, and
takes the session type from the session-info YAML (`Sessions[SessionNum].SessionType`,
normalised to `practice` / `qualify` / `race`; "Warmup" and "Offline Testing" count as
practice). States: `idle -> practice | qualify | race | unknown -> pre_green -> green ->
checkered -> cool_down -> ended`. Green is SessionState `racing` plus the engine's own
flag phase green (which treats `start_hidden` after the formation lap as green); on the
real Spa `.ibt` that is t = 79.4 s at the line that starts lap 1. `checkered` follows
SessionState (the leader's flag); `session_checkered` fires when the *player* takes the
flag, the first lap crossing after that (lap 18 at Spa), or at cool_down if the player
never crosses. A transition the catalog cannot back stays `unknown`: the `lifecycle`
capability needs SessionNum, SessionState and SessionFlags, and `RaceState.lifecycle.notes`
says what is missing (no SessionNum means no session type). `RaceState.lifecycle` carries
`state, segment, session_num, session_type, session_type_raw, session_state, green_at,
checkered_at, player_finished, ended_reason, notes`.

| event | when | payload (all `info`) |
|---|---|---|
| `session_start` | first frame of a segment | `session_num, session_type, segment, from, to, reason` (`start`, `session_num_changed`, `clock_reset`) |
| `session_state_change` | every SessionState transition | `session_num, session_type, segment, from, to` (iRacing labels) |
| `session_green` | racing + flag phase green, once per segment | `session_num, session_type, segment, from, to` |
| `session_checkered` | the player takes the flag | as above plus `player_finished`, `checkered_at` |
| `session_end` | segment closed | as above plus `reason`, `player_finished`; `cause` on a boundary |

No agent trigger matches them; they reach the journal, `/ws/pitwall`, both UIs and the
orchestrator as ordinary `RaceEvent`s.

**Segments: one clean engine, agent layer and journal run per session.** Practice,
qualifying and race of one event share one live RTV session id (the poller keys on the
subsession), so the engine cuts *segments*: a SessionNum change, the session clock going
back more than 0.5 s, or a new RTV session id (`engine.set_session_id` with a different
id) ends the open segment (`session_end`, reason `session_changed`) and starts the next.
At the boundary `engine.reset_segment()` clears every accumulator (fuel model, laps,
latches, stints, tyres, standings) but keeps the catalog binding, the session-info
document, the session name and source, the lifecycle counter and the frame metrics.
`engine.end_segment(reason)` closes a segment from outside (replay finished, shutdown).
`services.SegmentJournal` sits between `engine.bus` and the recorder: on `session_start`
it calls `pitwall.reset()` (cooldowns, breakers, radio, director, a fresh budget) and
opens or names the journal run *before* the event is written; on `session_end` it writes
the event and then closes the run with the segment's `calls_used`. `POST /replay/start`
resets the agent layer too, so two replays in one server run share nothing. A live
disconnect still closes the run without ending the segment (the race state keeps its
fuel model across a gap); the reconnect opens a new run of the same segment.

**Race plan and projections** (`projection.py`, `plan.py`, appendix stage 08). The engine
keeps a lap log (`RaceState.laps`: one `LapRecord {lap, lap_time, stint, stint_lap, green,
pit, session_time}` per completed lap, capped at 400) and, in a race segment only
(`lifecycle.session_type == "race"`), a versioned `RaceState.plan: RacePlan`. The projection
layer is pure functions over a `RaceState` with no model in it, each with a `grounded` flag,
a `reason` when it is not, and a sample count:

| function | what it gives | ungrounded when |
|---|---|---|
| `lap_time_degradation(state)` | `loss_per_lap_s` (least squares over the stint's green, non-pit laps within 3 % of the median), `loss_stderr_s`, current, fresh and projected (`horizon_laps`) lap time, `laps_used`, `outlier_laps`, `confidence` | fewer than 4 clean laps in the stint |
| `fuel_save_options(state)` | per extra lap: `target_per_lap_l`, `save_per_lap_l`, `save_pct` (a fraction), `reach_lap`; `to_flag` when a stop is otherwise needed | no fuel channels or no learned consumption |
| `traffic_projection(state)` | ahead and behind: `gap_s`, `closing_per_lap_s`, `catch_in_laps`, `catch_lap` | no `standings.gap_basis` (always on an `.ibt`, which has no other cars) |
| `undercut_overcut(state, early_lap, late_lap)` | both `simulate_pit_outcome` branches, `tyre_gain_s` from the degradation fit, `extra_fuel_l`, `position_delta`, `net_s`, `favours` | no stop plan; the tyre gain alone is "not priced" without a fit |

`RacePlan {version, segment, stop_count, next_stop_lap, fuel_to_add_l,
fuel_per_lap_target_l, pace_target_s, tyre_posture, stops_done, missing, revisions}`. Each
element is a `PlanElement {value, unit, provenance, rationale, version, set_by,
set_at_lap}` with `provenance` one of `derived`, `agent_proposed`, `operator_set`; an
element with nothing behind it is `None` and `missing[name]` says why (the UIs print n/a,
never 0). `refresh_plan` runs at every lap boundary and pit exit: the derived values come
from `stop_plan`, the fuel model and the degradation fit; a derived change inside
`REFRESH_TOLERANCE` (1.0 L, 0.02 L/lap, 0.3 s) is not a revision, nothing is refreshed on
pit road, and pit exit consumes the stop (`stops_done += 1`, the stop and tyre elements
re-derived). A standing agent value survives a refresh while `sanity_check` still passes
and is reverted to derived (with the reason) once it does not. Every version change is a
`PlanRevision` in `plan.revisions` (capped at 40) and a `plan_revised` event (`old_version,
new_version, provenance, agent, changes {name: {from, to}}, rationale, lap`); no agent
trigger matches it and `get_recent_detector_events` leaves it out.

**A revision is a proposal, not a write.** An agent calls `propose_plan_revision`, which
goes to `engine.propose_plan_revision(proposal, agent=)` under the engine lock. The engine
builds a typed FactSet (`plan.proposal_facts`: session, flags, player, fuel, tyres,
standings, `stop_plan`, `fuel_projection`, `simulate_pit_outcome` for the proposed lap and
every projection, but deliberately *not* the plan, so an earlier plan value cannot launder
itself), then: every proposed number must match a deterministic figure of the same unit
family near-exactly (`_backed`), and `sanity_check` must pass (stop before the run-dry
lap, stop not in the past, no fewer stops than the fuel needs, the per-lap target reaches
the flag and saves at most 15 %, pace within 3 % of the reference lap, posture one of
push / neutral / conserve). A pass bumps the version with `provenance: agent_proposed`
(and re-prices `fuel_to_add_l` when `next_stop_lap` moved); a failure appends an
`accepted: false` revision with the reason, leaves the version and every element alone,
and returns a tool result with the reason only, never the rejected numbers, so the agent
cannot cite them. The tool also writes a `plan_proposal_rejected` activity record.

**Plan in the agents.** The strategist's slice has `plan` (`plan_view(state, "full")`:
values, version, stops done, provenance, missing; no rationales), the engineer's and the
coach's have `plan_view(state, "posture")` (posture and pace target). The engineer's slice
also has `stint` (`plan.stint_horizon`, task R5): `laps_to_stint_end` whole laps to the
plan's next stop, or to the flag when the plan has no stop left (`basis`), never the stop
lap itself; unavailable with a reason when there is no plan. Its prompt quotes tyre trends
as rates and never multiplies them out: on the Spa replay the lap-3 slope (4.2 kPa/lap)
projected +42 kPa by the stop, and the RF actually levelled off about 10 kPa up. Plan numbers are
citable as typed facts at `race_state.plan.*` (slice) and `get_race_plan.*` (tool); units
follow the field suffixes as everywhere else. The strategist policy targets the plan's
`next_stop_lap`, and a `plan_stop` with no `target_lap` is stamped with it for the B1
box-call rules. Plan calls are ordinary `strategist/pit_stop` calls, so the B3 `pit_stop`
group arbitrates them against the engineer's `damage_stop` / `tyre_stop`. Tools added:
`get_race_plan`, `get_lap_time_degradation`, `get_fuel_save_options`,
`get_traffic_projection`, `get_undercut_overcut`, `propose_plan_revision` (strategist
only; engineer and coach read the plan and degradation; the spotter reads traffic).

**Event bus** (`bus.py`). Thread-safe producer with bounded drop-oldest subscriptions
(sync `drain()` and asyncio `next_events()`), sync callbacks on the producer thread, and
`history(limit)` for late joiners. `ActivityLog` (`pitwall/activity.py`) is the same bus
carrying agent activity records.

**Orchestrator** (`pitwall/orchestrator.py`). Subscribes to the bus and routes each event
to the agents whose `Trigger`s match. Per agent: one worker and a one-slot latest-wins
inbox (a queued critical event is never displaced by a lesser one) and a cooldown. Global:
`RTV_PITWALL_MAX_INFLIGHT` model calls at once. `RTV_PITWALL_AGENTS` is the kill switch,
`suspend()` / `resume()` follow the connection, *standby* follows the session (below),
and the race director seam
(`rtv.director`) injects scripted events onto the same bus. The app's director is
always `NoopDirector`; a scripted one exists only in eval rehearsals (below).

**Deterministic callers** (`pitwall/callers.py`, appendix B2). A call whose words are
fully determined by its event is a template, not a model run. `dispatch` runs every
`DeterministicCaller` first and publishes what it returns straight onto the feed, so it
is on the channel in the same tick (well under 100 ms; a model call is 12-17 s). The
spotter's per-event calls are all templates: `car_alongside` ("Car left.", "Three wide,
cars both sides.", "Clear."), `traffic_close` ("P5 behind, 0.6, closing."), `blue_flag`
("Blue flag, P1 lapping you, 1.2 behind."), a yellow or red `flag_change` and an
`incident`. Each figure is copied from the event payload, so the call is grounded by
construction (the tests run every template through the validator with the payload as
the only fact source). A template message is an ordinary `RadioMessage` with
`agent="spotter"`, `source="template"`, `model="template"`, stamped for the air-time
check: `computed_at_session_time` is the event's time, `max_age_s` is 3 s
(`RTV_PITWALL_SPOTTER_MAX_AGE_S`), a traffic or blue-flag call carries
`subject_car_idx` and expires 8 s after its event, a side call expires after 1.5 s and is
never aired late, and flag and incident calls have a 10 s per-subject cooldown. All side
states share the subject `traffic:alongside`, so "Clear." supersedes a queued "Car
left.". The kill switch, `suspend()`, a stale state and the spotter's own enable flag
silence template calls too; the model-call budget does not, since they cost nothing.
`dispatch` traces each one as a `template_call` activity record and lists the caller in
the dispatch record's `templated`; `status()` has `template_calls` and `callers`.
With `RTV_PITWALL_AGENTS=true` and no Kimi key (or no `ai` extra), `build_pitwall`
mounts a template-only orchestrator (`services.build_template_pitwall`: no agents, no
provider, `status()["agents_mounted"]` false), so template calls and driver-question
lookups run with no key (appendix C1).

**Race control** (`racestate/race_control.py`, `pitwall/agents/race_control.py`, appendix
stage 09 part A). The engine aggregates what a steward would see about the player into
`RaceState.race_control` (`incidents, incident_limit, incidents_remaining, limit_source,
incident_stage, offs_in_window, offs_window_laps, offs, penalty, penalty_since,
serve_in_pit, missing`) and four events, each latched so the agent is woken once:

| event | fires when | backed by | capability |
|---|---|---|---|
| `incident_limit` | the count crosses a stage against the limit: `approaching` (8 or fewer left, two 4x contacts), `final` (4 or fewer, one 4x), `reached` (0); only the worst stage crossed, never back down | `PlayerCarMyIncidentCount` + `WeekendInfo.WeekendOptions.IncidentLimit` | `incident_limit` |
| `track_limits` | 3 off-tracks inside the last 3 laps (`track_limit_offs`, `track_limit_window_laps`); re-armed when the window drops below 3 | the offtrack detector | `track_limits` |
| `penalty` | a driver flag on the player is `shown` or `cleared` (`black`, `furled`, `repair`, `disqualify`); `serve_in_pit` for black and repair | `SessionFlags` | `penalties` |
| `blue_flag_ignored` | the same car lapping us stayed within 1.0 s behind for 3 corners (lap buckets) *and* 5 s | CarIdx standings | `blue_flag_compliance` |

A detector the catalog or the session info cannot back has its capability False and a
`race_control.missing[name]` entry saying why (an unlimited race: `incident_limit:
["WeekendInfo.WeekendOptions.IncidentLimit: unlimited"]`; an `.ibt`:
`blue_flag_compliance: ["CarIdxLapDistPct"]`). iRacing publishes no track-limit warning
count and no penalty timer, so neither is modelled. With a numeric limit every
`incident` event also carries `incident_limit`, `incidents_remaining`, `incident_stage`.

Two paths, like the spotter. Read-outs are race-control templates
(`callers.RACE_CONTROL_TEMPLATES`, 5 s max age): "Incident, 14 of 17, 3 to the limit." on
an `incident` with a known limit (the spotter's "Incident, that's 4x." stays quiet then),
"Black flag. Serve it in the pit lane.", "Meatball flag, pit for repairs.", "Black flag
cleared.", "Track limits, 3 offs in 3 laps.", "Blue flag, P1 held up 4 corners, let them
by.". The LLM agent is woken only by a consequence: `incident_limit` at stage `final`, a
`penalty` shown that is served in the pit lane, `track_limits` with 8 or fewer incidents
left, `blue_flag_ignored` (only when `blue_flag_compliance`, so never on an `.ibt`), and
its own driver questions. Its slice is flags, `race_control`, player, session laps and
time left, the cars around us and the race-control capabilities: no fuel, tyres or plan.
Its `policy` withholds a call whose structured `incidents` or `incident_limit` differs
from the state's, that names a penalty no flag shows, or asks to box with nothing to
serve. `pit_request: box_now` gives the subject `penalty_stop`, a member of the B3
`pit_stop` group (action `box_now`), so the strategist's call and race control's cannot
contradict each other on the radio. On the real Spa race (`IncidentLimit: unlimited`, 4x,
no driver flag) race control makes two track-limits read-outs and is never woken.

**Driver questions** (`pitwall/questions.py`, appendix C1). `orch.ask(text, source=
"text"|"voice")` (and `driver_message`, which calls it) puts the transcript on the channel
(`agent="driver"`, `speak=False`, `data.question` = the route) and publishes a
`driver_question` RaceEvent on the race bus (`payload`: `text`, `source`, `question_id` =
the transcript's `message_id`, `owner`, `intent`, `matched`, `answered_by`), so the journal
records it. `route_question(text)` is a keyword table, no model: fuel, pit and strategy go
to the strategist, tyres, temperatures and damage to the vehicle engineer, traffic and gaps
to the spotter, driving and corners to the coach, incidents, penalties and track limits
to race control, anything else to the strategist with `matched=False`. A plain lookup (`fuel_laps`, `gap_ahead`, `gap_behind`, `last_lap`,
`best_lap`, `position`, `laps_to_go`, `tyre_temps`, `incidents`) is answered by `QuestionCaller` (a
`DeterministicCaller`, in the owner's name) from the engine state, or with an honest "No
... data" answer when the state lacks the value. Anything else wakes the owner's
`Trigger("driver_question")` (cooldown 0, its own questions only); the run's first message
starts `THE DRIVER ASKED: "..."` and the answer goes through the validator as usual. Every
answer has `in_reply_to` = the question id and subject `answer:<id>`. A question nobody can
answer (no such agent, agent disabled, budget spent, no live telemetry, the run failed)
gets a `pitwall` reply saying so, also `in_reply_to`; with the kill switch off that reply
is shown, not spoken. A queued question is never displaced from an agent's inbox by a
detector event short of critical.

**Autonomy and standby** (`RTV_PITWALL_AUTONOMOUS`, default false). A third switch next
to the kill switch and `suspended`. With autonomy on, the orchestrator starts in standby;
`observe_lifecycle` (called at the top of `dispatch` for every event, before the kill
switch is consulted) puts it in standby at `session_start`, arms it at `session_green`
when the session type is in `RTV_PITWALL_AUTONOMOUS_SESSIONS` (default `race`; a practice
or qualifying session arms only `RTV_PITWALL_ROSTER_PRACTICE` / `_QUALIFY`, empty by
default, and an unknown type never arms), and stands it down at the player's
`session_checkered` or at `session_end`, logging the calls used against the budget.
While in standby `dispatch` still runs the deterministic template callers (they cost
nothing) and records `blocked: "standby"` for the agents; agents off the armed roster are
`skipped: "roster"`. Autonomy only writes the standby flag and the roster: the kill switch
(`blocked: "disabled"`, checked first) and per-agent switches always win. Each decision is
an `autonomy` activity record (`armed, reason, event, session_type, roster, calls_used,
max_calls_per_session`) and `status()["last_autonomy"]`; `status()` also has `mode`
(`off | suspended | standby | armed | on`), `autonomous`, `standby`, `standby_reason`,
`armed_roster` and `session_type`. With autonomy off the dispatch decisions are exactly
those before stage 07 (a test proves it on the synthetic race).

**Agents** (`pitwall/agents/`). Five declarative `AgentSpec`s, all live:

| agent | owns | woken by (model calls) | template read-outs | tier | max age |
|---|---|---|---|---|---|
| strategist | fuel, stops, the race plan, rivals | pit window, fuel margin and critical, flag changes, rivals pitting, stint milestones, questions | | reasoning | 30 s |
| vehicle_engineer | tyres, car health, recurring issues | recurring issues, tyre bands, car health, pit entry, questions | | fast | 20 s |
| spotter | what is around the car | a lap-line summary with a car within 5 s (300 s cooldown), camera notes, questions | alongside, closing, blue flag, yellow/red, incident | fast | never late |
| coach | how the car is driven | repeated mistakes, off-tracks, stint milestones, camera notes, questions | | fast | 30 s |
| race_control | compliance and consequence | one 4x from the incident limit, a penalty to serve, offs near the limit, a held-up blue flag, questions | incident vs limit, driver flags, track limits, blue flag held up | fast | 15 s |

A spec is a role prompt, an output
model, scoped tools, triggers, a state slice, a radio subject, an optional policy and a
prefetch list, and optionally the text shown for a policy-withheld call
(`withheld_text`); adding an agent is one module plus one `AGENT_REGISTRY` entry. Agents never
call each other; each run is shown the recent radio and its own last call on each of its
last two subjects, with what the radio did with it (D1). Model per agent: the spec names a
tier (strategist `reasoning`, the rest `fast`) and `framework.agent_model()` resolves it
from `RTV_PITWALL_<NAME>_MODEL`, else `RTV_LLM_KIMI_MODEL_REASONING` / `_FAST` (all
`kimi-k3` today). All calls go through `KimiProvider` with a forced respond tool. Both
tiers stay on `kimi-k3`: no model on the Kimi Code key is faster at forced tool calls
with thinking off (B4 trial, see the appendix section "B4 — fast Kimi tier
trial").

**Validator** (`pitwall/validator.py`). Every number in an answer must be in the
`FactSet` built from what the run was shown: state slice, triggering event, prefetch and
tool results. Facts are typed: each carries its unit family and path
(`race_state.fuel.laps_remaining = 6.2 laps`), and a spoken figure with a unit ("6 laps",
"P4", "28 psi", "three tenths") must match a fact of that family, converted into the unit
spoken (`rtv.grounding.units`, shared with the Layer-3b prose check). A bare figure keeps
the any-fact match, and 0-3 are free only when bare. `RTV_PITWALL_TYPED_GROUNDING=false`
restores the old any-number check. Spoken text is tolerant (±0.05 or 2 %, rounding, clock
notation, a fraction as a percentage); structured payload fields are exact. Also the 25-word limit and the
spec's deterministic `policy`. The coach (`AgentSpec.prose_check`) is also held to the
Layer-1 prose check whenever a `get_corner_detail` result held a corner (A2). On failure: one
repair turn naming the figures, then a grounded refusal at `info` priority with the blocked
text kept in `detail_text`.

**Radio** (`pitwall/radio.py`). One channel, ordered by `(priority, sequence)`. A newer
non-critical message with the same `(agent, subject)` supersedes a queued one; a critical
message is never superseded. Subjects of different agents that answer one decision share a
group (`pitwall/decisions.py`, appendix B3: `pit_stop` = the strategist's calls plus the
engineer's `damage_stop`/`tyre_stop` "box now" and race control's `penalty_stop`). In a group the newer call replaces a queued
one of the same or lower priority from any agent, and a call that contradicts a
higher-priority one still queued or aired within 60 s is dropped; the loser goes to
consumers marked `superseded` (`radio_superseded` with `by_message_id`, `rule`, `reason`),
and its agent sees one "NOT AIRED" line on its next run. Airtime is about 0.4 s per word, and
`pump(session_time)` is the deterministic, clock-injected twin of `run()`. Every agent
message is checked again when it reaches the front (`pitwall/validity.py`, appendix B1):
older than its agent's `max_age_s` but still true, it airs tagged late ("Late call:
...", `radio_late`); no longer true (its stop lap gone, the car past the pit entry or
in the pit lane, the car it was about gone, an expiry passed), it is dropped, critical or
not, and handed to consumers marked `expired` (`radio_expired`). A call
triggered by a visual observation always airs at `info`. Voice: browser TTS by default,
optional server engine via `RTV_PITWALL_TTS` (`pitwall/tts.py`).

**Journal** (`rtv.store.journal.SessionRecorder`). One JSON-lines file per run of a
session (live or replay) holding lifecycle, race events, aired radio, every activity
record and state snapshots every `RTV_JOURNAL_STATE_INTERVAL_S`. A run records one
session segment: its start record carries `session_num`, `session_type` and `segment`
when they are known, or a `lifecycle` record with `phase: "segment"` names them once the
first frame does; the end record carries `reason`, `calls_used`, `calls_by_layer` and
`max_calls_per_session`. `list_runs` / `/pitwall-runs` report `session_num`,
`session_type`, `segment`, `end_reason` and `calls_used` per run (`default_run` is
unchanged). The post-race review and the desktop review read only the journal and never
re-run the agents (`/sessions/{id}/pitwall-runs`, `/pitwall-log`, `/pitwall-log/state`,
`/summary`). `/summary?run=` serves the stored `<run_id>.summary.json` when present and
computes the same shape otherwise, from `journal.laps_for_run` (the lap rows the run's
snapshots saw the player on, inside its session-time bounds), so practice laps never
appear in the race summary. Schema version 2 (F1): each `run_start` holds the run's whole model input (`data.input`)
and each `tool_call` its whole result (`data.result_full`); `rtv.pitwall.agent_input.
model_input(record)` rebuilds the first model call. Schema version 3 (F3): `run_start`
also holds `data.state_full` (the whole `RaceState` the run was given) and each `validate`
record `data.output_full` (the model's structured answer before a refusal or truncation
rewrote it). All four fields are journal-only (`activity.JOURNAL_ONLY_FIELDS`). Older
journals read unchanged.

**Real-journal evals** (`evals/real/`, F3). A schema-3 journal is an eval source:
`scripts/build_real_evals.py --journal <run>.jsonl --out evals/real/<set>` cuts each agent
run with an answer into a case (input, full state, context events, tool results, answers
with their recorded verdicts, radio and air-time outcome). Offline,
`evals.real.offline.score_case` re-scores the recorded answers with today's validator and
policy, the role checks of `evals/pitwall_checks.py`, the corner map and the B1 validity
rule; `tests/test_real_evals.py` compares the committed `spa_run` set with its
`baseline.json` (rebuild with `--baseline spa_run`, preview with `--diff spa_run`). Live,
`evals/run_pitwall.py --real spa_run` re-executes every input against today's prompt and
model, with tools answering from the recording, reports `spec_drift` per case and scores
the new answer with the same code; `--judge-provider gemini` adds an advisory judge score.

**Plan and projection evals** (stage 08). `evals/pitwall_checks.py` adds `projection_ok`
(every role agent: an answer that claims a catch in N laps, a lap-time loss per lap or a
fuel save per lap fails when that projection is ungrounded for the case state; a grounded
figure is the validator's business) and `plan_ok` (strategist: every revision it got
accepted in the case passes `plan.sanity_check` against the case state; a rejection is the
system working and is not scored). `evals/run_pitwall.py` gives each case
an `evals.pitwall_cases.PlanSandbox`, so a live strategist can propose without touching an
engine. Degraded cases: `plan-degraded-no-degradation-fit` and `plan-degraded-no-gap-basis`
(the strategist must not quote a degradation or catch figure that is not there), plus a
swapped-fact case that swaps `race_state.plan.next_stop_lap` with `fuel_to_add_l`. The
`spa_run` baseline carries both new keys (all true on the recorded answers).

**Plan in RTV Desktop.** `rtv.desktop.model.plan` (Python only, no golden case: the
frozen web pages have no counterpart) builds the live view's plan card, revision rows, the
review's "race plan" timeline lane (accepted revisions and rejected proposals; the shared
race-events lane drops `plan_revised` in the view only) and the audit's Plan column (what
each run's `propose_plan_revision` calls did, from `result_full`).
`rtv.desktop.widgets.plan.PlanPanel` sits in column B of the live view under the strategy
panel.

**Weekly live eval** (F4, `docs/WEEKLY_EVAL.md`). `scripts/weekly_eval.py` runs one 2x Spa
replay with agents on, the real-journal cases and golden sets live, and the coach eval with
the Gemini judge, then diffs everything with the previous week under
`/mnt/project-files/weekly-evals/` (`REPORT.md`, headlines first; `spec_drift`-style model
and prompt hashes per agent). It stops cleanly on a Kimi 429; `--dry-run` calls no model. A
Claude Code scheduled task runs it every Monday.

**Session-end hook** (`rtv.session_end`). `AppServices.on_session_end` is a list of
callbacks; each closed journal run fires them once, on a single worker thread (the frame
path never waits), with a `SessionEnded(session_id, run_id, journal_path, source,
session_num, session_type, reason, calls_used, calls_by_layer)`. A callback that raises is
logged and counted (`.errors`) and the rest still run. Built in: `store_run_summary`
writes `<run_id>.summary.json` (the `/summary` shape plus `session_num`, `session_type`),
then the notifier from `RTV_PITWALL_SESSION_END_NOTIFIER` (`none` default, or `file`: one
log line and `<run_id>.notice.txt`). Nothing on the default path touches the network.

**Post-race debrief** (`rtv.debrief`, appendix "Stage 09 part B"). Not a radio agent: it
never speaks, has no trigger or cooldown, is not in `AGENT_REGISTRY` and does not count
against the session's call budget (the session is over). `DebriefService` runs once per
race segment as the last `on_session_end` callback, over that segment's journal run, and on
demand. *Inputs* (`debrief/inputs.py`, no raw telemetry): the run's race events (state
snapshots left out, `plan_revised` kept apart), each with its lap and `payload.place` corner;
the radio with its air outcome (`aired | late | expired | superseded | refused`); agent run
outcomes per agent; plan revisions (`from`/`to` element values) and rejected proposals (lap,
agent, changed element names and reason, never the rejected numbers, and the reason is kept
out of the FactSet); the final state; one fuel and one tyre row per lap boundary (the
projection as it stood); the stored `<run_id>.summary.json`; the run's laps
(`journal.laps_for_run`; `recorded_laps` = the valid ones); and the Layer-1 findings of every
clean lap against the best one (`CoachingService.lap_findings`, unchanged), compacted to the
top losing corners per lap, per-corner totals over the race (`recurring`), and per corner the
full `CornerFinding` of the lap that lost most there. *The call*: `KimiProvider(thinking=True)`,
`structured_call` with the `DebriefAnswer` contract through the forced respond tool, model
`RTV_DEBRIEF_MODEL` else `RTV_LLM_KIMI_MODEL_REASONING`, `max_tokens` 16 000. *Contract*
(`debrief/contract.py`): `summary`, `plan`, `time_lost[]` and `recurring[]` (`{text, corner,
laps}`), `tyres_fuel`, `priorities[]` (`{text, basis, corner}`, at most five). *Checks*
(`debrief/checks.py`), per section: every figure against the typed FactSet over the inputs
(A1; "the right number, wrong fact" fails on its unit family); structured laps must be
recorded, prose lap numbers recorded or named by the plan; a structured corner through
`resolve_corner` (label and name); the prose through A2's `check_prose(mode="lap")` against the
Layer-1 corners, keeping its corner-label and brake-direction issues (its per-corner figure
rule is replaced by the FactSet over all inputs); a priority must rest on a kept, non-empty
section and name a corner that section discusses. A failing section gets one repair call
(the issues named); a section still failing is `refused` with its reason; kept sections are
`ok` or `repaired`. With no key, no `ai` extra, an exception or no valid answer, every section
is `unavailable` and `note` says so: the deterministic half is always there.
*Document* `<run_id>.debrief.json`: `schema, session_id, run_id, session_type, generated_at,
trigger (session_end | manual), model, model_calls, analysis {available, reason}, note,
kept_sections, sections[{name, title, status, text | items, reason, issues, first_issues}],
deterministic {result, laps, stints, fuel, tyres_by_lap, plan, reference_lap, time_lost,
recurring, events, radio, notes}, references {laps, corners, reference_lap}, inputs`; each
write also appends a `debrief` record (`journal.append_record`) to the closed run.
`GET /api/v1/sessions/{id}/debrief?run=` returns it plus a live `can_analyse` (404 when
none); `POST` regenerates it from the recorded journal (409 for a run still recording) and
never re-runs the agents. The notifier gets `notify_debrief` (`file`: one more line in
`<run_id>.notice.txt`). Settings: `RTV_DEBRIEF_AUTO` (`auto` = on whenever
`RTV_PITWALL_AGENTS` is on, or `true`/`false`), `RTV_DEBRIEF_SESSIONS` (`race`),
`RTV_DEBRIEF_MODEL`. In RTV Desktop the review has a Debrief panel under the summary
(`rtv.desktop.model.debrief`, `widgets/debrief.py`): the selected run's debrief, its laps and
corners as links that open the lap in the lap analysis against the debrief's reference lap
and highlight the corner, and a Regenerate button whose dialog says it calls Kimi first.

**Vision live worker** (`rtv.vision.live_worker`, details in `docs/VISION.md`). With
`RTV_VISION_MODE=live`, events in `RTV_VISION_LIVE_TRIGGERS` become moments; the worker
grabs frames from the running capture or a synced recording, runs the grounded
`VisionAnalyzer` and publishes kept notes as `visual_observation` events. They are review
material by default: the journal, the timeline and both reviews show them, and the
orchestrator's dispatch records them as `blocked: "vision_review_only"`. Only with
`RTV_VISION_LIVE_RADIO=true` do they wake the spotter and coach. Spend is capped by
`RTV_VISION_LIVE_MAX_CALLS` (an inner limit) and `RTV_VISION_LIVE_MIN_GAP_S`, and every
vision call also counts against the session budget below.

**Health and budget** (appendix stage 5). A frame that raises costs one frame; a
supervised task that dies is restarted and counted (`status()["restarts"]`); a failed
model call is retried once and then said out loud; `RTV_PITWALL_AGENT_FAILURE_LIMIT`
consecutive failures open an agent's breaker; `RTV_PITWALL_MAX_CALLS_PER_SESSION` caps
billable calls per session, every layer included: the live VLM reports each call through
`PitwallOrchestrator.record_call("vision")`, and a moment that would exceed the budget is
skipped (`over_budget`, also written as a `skip` record in the vision journal).
`status()["calls_by_layer"]` is `{"agents": n, "vision": m}`. `GET /api/v1/pitwall/health`
is the one poll for all of it, the budget and its split included.
Token usage per call is in `rtv.llm.USAGE` (`GET /api/v1/llm/usage`) and in the journal.

**Prompt caching.** The stable role prompt is the first system block and carries
`cache_control`; the state slice and the prefetched tool results go in the user turn and
are not cached. That is fine at today's volume (tens of runs per race) and is left as it
is on purpose.

**Race director** (`src/rtv/director/`, appendix stages 5 and 10). Rehearses scripted
cautions, rain and rule changes against the agents, and **never runs in the app**: a
director makes things happen that did not happen, so it is built only by the eval runner
`evals/run_director.py` (core in `evals/director_rehearsal.py`), over the synthetic
director race (`DIRECTOR_RACE`) or a stored session read with `store_source`. The app
keeps `NoopDirector`; `RTV_PITWALL_DIRECTOR_SCRIPT` only loads and validates a script.

- *Scheduler.* `ScriptedDirector` (`director/scheduler.py`) implements `DirectorEngine`.
  `poll(state)` walks the script in order and fires an entry when its conjunctive
  `InjectionTrigger` becomes true: `at_session_time` (clock at or past it), `at_lap` (on
  that lap or later; with `at_lap_dist_pct` the point on the lap has been reached),
  `at_lap_dist_pct` alone (at or past that fraction of the current lap), `after` +
  `delay_s` (session seconds since the parent last fired), `while_flag` (the *effective*
  flag: an active injected flag wins over the telemetry's) and `once`. Edge-triggered
  and idempotent per entry; synchronous, no I/O, never raises (`errors` counts a failed
  poll). `reset()` forgets what fired and the overlay (stage 07 resets the pitwall per
  segment); `describe()` reports `fired`, `fired_log`, `pending`, `active`.
- *Overlay.* Telemetry is never rewritten: under a scripted yellow `state.flags` still
  says green. `ScriptedDirector.overlay(state)` lists the injected *conditions* in force
  (the flag until a scripted green clears it, the newest weather step, each regulation;
  not momentary hazards) as `{kind, event_type, director_id, since, lap, payload}` plus
  `served` on a mandatory stop. `orch.injected_overlay(state)` hands it to every run;
  `AgentRuntime.invoke(injected=)` adds it to the slice as `injected` (so the validator
  cites `race_state.injected.*`), `agent_input.system_blocks` then appends
  `INJECTED_RULE` after the race state, and a policy with an `injected` keyword gets it.
  An empty overlay (every live run) adds nothing, so live inputs are byte-identical.
- *Weather and regulation.* Director-only event types with real consumers now: a
  `weather_change` that brings water (`overlay.is_rain`) wakes the strategist and the
  vehicle engineer; a `regulation_change` with `regulation: mandatory_pit_stop` wakes the
  strategist. `rtv.racestate.plan.mandatory_stop(events)` reads it from the bus history
  (served by a later pit exit): `derive` plans at least one stop, `sanity_check` and
  `propose_revision` require the stop inside `applies_from_lap`..`applies_to_lap` and
  allow it when the fuel needs none, and the strategist policy reads the overlay the same
  way (a scripted caution also makes `pit_under_yellow` available). The validator refuses
  rain or wet-track words without a weather fact (a `weather_change` payload, the injected
  weather condition or a wet `track_wetness` reading): a temperature trend is not a
  forecast.
- *Runner.* `rehearse(script, source=None, out_dir=, live=False, speed=0)`: frames go into
  the engine one at a time, the director is polled through `orch.poll_director` after
  each, and every bus event is routed as the app's pump routes it (`orch.dispatch`:
  template calls on the channel at once, woken agents into their one-slot inboxes, latest
  wins). The radio is pumped on the session clock *before* the agents run, so a template
  airs on its own tick as in the app (where the model calls are still in flight) instead
  of queueing behind a model answer to the same tick and going stale; then the agents run
  **inline** (`orch.drain_inboxes()`; the race clock waits for them). So a rehearsal is
  deterministic (the same script over the same race gives a byte-identical event log)
  and independent of speed and model latency (`speed` only paces frames). Offline the agents answer from the
  scripted `rehearsal_answer`; `--live` uses Kimi. The journal goes to
  `<out>/<session_id>/runs/*-eval.jsonl` (source `eval`, `scenario` on the start record),
  refused under `data/sessions`. The report (`report.json`, `REPORT.md`, `events.jsonl`,
  `radio.jsonl`) gives per injection: when it fired, the agents and template callers it
  woke and whether that equals what the same event wakes without provenance keys, what
  aired, the plan revisions, and the checks `provenance_ok`, `condition_ok`,
  `woke_as_real_ok`, `radio_ok` plus `plan_revised_ok` / `weather_wakes_ok` /
  `template_ok`. A replayed `.ibt` has no field, so a traffic injection gets a note.
- *Eval cases.* `evals.pitwall_cases.director_cases(agent)` snapshots the example script
  over the director race at each scripted weather and regulation event (`EvalCase.injected`
  carries the overlay); `expected_calls(state, injected)` makes `stay_out` indefensible
  under a pending mandatory stop the fuel does not need.

### Interfaces later stages consume

```python
services.engine / .replay / .pitwall / .activity / .recorder / .segments  # None when disabled
services.on_session_end.append(fn)    # fn(SessionEnded), worker thread; .wait(), .fired
engine.on_frame(frame, catalog); engine.snapshot() -> RaceState; engine.health()
engine.lifecycle (SessionLifecycle); engine.end_segment(reason); engine.reset_segment()
snapshot().lifecycle: state, segment, session_num, session_type, green_at, player_finished
orch.mode / .standby / .standby_reason / .observe_lifecycle(event) / .last_autonomy
journal.list_runs(...)[i]: session_num, session_type, segment, end_reason, calls_used
journal.summary_path(path); journal.laps_for_run(laps, records); session_end.run_summary
engine.bus.subscribe(...) / .subscribe_callback(fn) / .history(limit)
orch.handle_event(event, state)       # route + run inline (tests, smoke)
orch.open_inboxes(); orch.dispatch(e); await orch.drain_inboxes()  # app inboxes, inline (evals)
PitwallOrchestrator(..., callers=None)  # None = build_callers(); [] = no template calls
rtv.pitwall.callers: TemplateCall(spoken_text, detail_text, priority=, subject=, data=,
    expires_after_s=, subject_car_idx=, valid_until_lap=, max_age_s=, cooldown_s=)
    template_message(agent, event, call, max_age_s=None, state=None) -> RadioMessage
    DeterministicCaller(agent, {event_type: template}, max_age_s=).call(event, state)
    build_callers(settings=None, only=None); SPOTTER_TEMPLATES; spotter_max_age(settings)
orch.feed.subscribe(...) / .subscribe_callback(fn) / .pump(session_time)
orch.status(); orch.healthy(); orch.suspend(reason) / .resume(); orch.director
build_agents(models=None, enabled=None, only=None, settings=None) -> list[AgentSpec]
agent_model(spec, settings=None, override="") -> str
agent_max_age(spec, settings=None) -> float | None    # RTV_PITWALL_<NAME>_MAX_AGE_S
validity.stamp(message, spec, state, event, max_age_s=, pit_entry_pct=)
validity.check(message, state, now) -> (verdict, reason, age_s)
feed.take(now) / feed.expire(message, reason); RadioFeed(state_source=engine.snapshot)
engine.pit_entry_pct -> float | None                  # learned from OnPitRoad edges
AgentRuntime(spec, provider, ...).invoke(event, state, events=...) -> RadioMessage | None
FactSet, validate_output(...)         # pitwall/validator.py
rtv.director: DirectorEngine, NoopDirector, ScenarioScript, injected_event, is_injected
rtv.director: ScriptedDirector(script).poll(state) / .overlay(state) / .effective_flag(state)
    / .reset() / .describe()                      # eval path only, never the app
rtv.director.overlay: injected_caution, pending_mandatory_stop, is_rain, injected_flag
orch.injected_overlay(state) -> list[dict]; AgentRuntime.invoke(..., injected=)
agent_input.INJECTED_RULE   # system block appended when the slice has "injected"
rtv.racestate.plan: mandatory_stop(events), regulation_stop(payload), MandatoryStop,
    derive/refresh_plan/sanity_check(..., events=)
FactSet.has_weather; validator.weather_claims(text)
evals.director_rehearsal: rehearse(...), Rehearsal, DIRECTOR_RACE, rehearsal_answer,
    write_outputs, journal_root_allowed;  evals/run_director.py (CLI)
evals.pitwall_cases: director_cases(agent), EvalCase.injected
evals.pitwall_checks: expected_calls(state, injected=()), cross_check(..., injected=, events=)
snapshot().laps: list[LapRecord]; snapshot().plan: RacePlan | None   # stage 08
engine.propose_plan_revision(proposal, agent=) -> ProposalOutcome (.accepted, .to_tool, .payload)
engine.pit_lane_loss_s                                # from RTV_PITWALL_PIT_LANE_LOSS_S
rtv.racestate.plan: refresh_plan, propose_revision, sanity_check, plan_view(state, scope),
    proposal_facts, stops_needed, is_race_segment, ELEMENTS, RacePlan, PlanRevision
rtv.racestate.projection: lap_time_degradation, fuel_save_options, traffic_projection,
    undercut_overcut, clean_stint_laps
evals.pitwall_checks: projection_check(result, state), plan_check(state, revisions)
evals.pitwall_cases: PlanSandbox(state), plan_degraded_cases()
rtv.vision.live.publish_note(engine, session_id, note)
snapshot().race_control: RaceControlState     # stage 09: incidents vs limit, offs, penalty
rtv.racestate.race_control: RaceControlMonitor, incident_limit_from(info), stage_for(n),
    PENALTY_FLAGS, SERVE_IN_PIT, INCIDENT_LIMIT_KEY, STAGES
rtv.pitwall.callers: RACE_CONTROL_TEMPLATES, race_control_caller(settings)
rtv.pitwall.agents.race_control: RACE_CONTROL, RaceControlCall, race_control_policy
evals.pitwall_checks: race_control_check(result, state, event), race_control_consequences
evals.pitwall_cases: RACE_CONTROL_SCENARIO, race_control_degraded_cases()
services.debrief: DebriefService(settings, repo, coaching, provider=None, notifier=None)
    .generate(session_id, run_id, trigger=) -> doc; .load(...); .on_session_end(ended)
    .resolve_provider() -> (provider | None, reason); .can_analyse(); .model(); .auto()
rtv.debrief.inputs.build_inputs(session_id=, path=, repo=, coaching=) -> dict
rtv.debrief.checks: fact_set(inputs), known_corners(inputs), check_section, check_priorities
journal.debrief_path(path); journal.append_record(path, kind, data)   # closed runs only
evals.debrief_cases: faithful_answer(inputs), debrief_cases(inputs), scripted_handler(case)
evals.synthetic_debrief: seed_debrief_session(writer), debrief_source()   # tests only
```

The HTTP and WebSocket surface: `/api/v1/racestate` (and `/events`), `/api/v1/replay/*`,
`/api/v1/pitwall/{status,health,radio,activity,tts,enabled,reset,driver-message}`,
`WS /ws/pitwall` (state, event, radio and activity frames), the journal endpoints above,
`GET/POST /sessions/{id}/debrief` and `/vision/live`. FastAPI's `/docs` lists them all.

### Pending stages

Prompts in `prompts/` (done ones in `prompts/done/`), revised by plan task D0. Stages 07
(session lifecycle and autonomy), 08 (race plan and projections), 09 (race control, D6a,
and the post-race debrief, D6b) and 10 (the race director scheduler, evals only) are done;
no stage prompt is pending.

---

# Appendix: stage history

## Pitwall build state

Shared state between the staged headless builds. Each stage appends: what it
built, the public interfaces the next stage consumes, and any deviations from
its spec (with a one-line rationale).

| Stage | Scope | Status |
|-------|-------|--------|
| **1** | Deterministic race-state engine + event bus + replay | **done** |
| **2** | Agent framework + orchestrator + strategist | **done** |
| **3** | Vehicle engineer / spotter / coach agents | **done** |
| **4** | TTS radio voice | **done** |
| **5** | Race-director seam (interfaces only) + live-path hardening + docs | **done** |
| **6** | Pitwall UI | **done** |

> **Phase 1 cleanup (2026-10-30)** changed several stage 1-6 interfaces; see
> [the cleanup section](#phase-1-cleanup--real-data-only-kimi-only) before trusting
> the older stage notes on replay, scenarios, providers or model ids.

> The **race director** runs only from the eval path. Stage 5 shipped its schema,
> its protocol and the `NoopDirector` the app keeps, plus a test proving the seam
> carries a scripted full-course yellow indistinguishably from a real one; stage 10
> added the scheduler (`ScriptedDirector`), the `injected` overlay and the rehearsal
> runner `evals/run_director.py`. See
> [stage 5](#stage-5--the-director-seam-and-a-live-path-that-survives-a-race) and the
> stage 10 entry at the end.

---

## Stage 1 — deterministic race-state engine

`src/rtv/racestate/` — **no LLM calls anywhere in this package.** It consumes the
existing 60 Hz frame stream and maintains one continuously-updated `RaceState`
plus a typed event bus. Every number it reports is traceable to a channel present
in the session's runtime catalog; anything unbacked stays `None` and a capability
flag records why (the Layer-3b grounded-refusal discipline, applied to state).

### Module map

| Module | Role |
|--------|------|
| `channels.py` | The channel-name contract + capability groups. Nothing is required to exist. |
| `models.py` | `RaceState`, `RaceEvent` and every sub-model (Pydantic). |
| `detectors.py` | Pure functions over a frame window. No state, no event construction. |
| `bus.py` | `EventBus` / `Subscription`: thread-safe producer, sync **and** asyncio consumers. |
| `engine.py` | `RaceStateEngine`: the incremental per-frame update + detector latching. |
| `replay.py` | `ReplayDriver` + `store_source()`. |
| `evals/synthetic_race.py` | The scripted synthetic race used as test ground truth (test-only since phase 1). |

### The one ingress

```python
engine.on_frame(frame: Frame, catalog: Catalog) -> None
```

This is deliberately the *exact* signature of the live poller's existing
`on_frame` callback. `services.build_services()` wires it alongside
`hub.publish_frame`, and the replay driver calls the same method. **The engine
cannot tell live frames from replayed ones** — which is what makes the
"replay twice, compare the event log" test a real check on the live path.

The live wiring is defensive: a race-state exception is logged and swallowed so
it can never break telemetry capture or the v1 `/ws/live` feed.

---

## RaceState schema

One object, versioned by `version` (monotonic per update) and stamped with
`tick` / `session_time`.

| Group | Key fields | Backed by |
|-------|-----------|-----------|
| `session` | `state`, `time_remaining`, `laps_remaining`, `laps_total`, `car_id`, `track_id`, `track_name`, `lap_length_m`, `source` | `SessionState`, `SessionTimeRemain`, `SessionLapsRemainEx`, session-info YAML |
| `flags` | `raw`, `phase`, `active[]`, `since_session_time`, `time_in_state` | `SessionFlags` bitfield |
| `standings` | `cars[]`, `player_idx`, `gap_basis`, `lap_length_m`, `reference_lap_time` | `CarIdx*` per-car-index arrays |
| `player` | `lap`, `position`, `lap_dist_pct`, `speed`, `last/best_lap_time`, `on_pit_road`, `track_surface`, `incidents`, `stint`, `laps_on_tyres`, `stint_start_lap` | player scalars |
| `fuel` | `level`, `level_pct`, `capacity`, `per_lap`, `per_lap_std`, `samples`, `laps_remaining`, `laps_to_finish`, `fuel_to_finish`, `margin_l`, `margin_laps`, `pit_window_earliest_lap`, `pit_window_latest_lap`, `window_open` | `FuelLevel`, `FuelLevelPct` |
| `tyres` | `temps{}`, `pressures{}`, `temp_trend{}`, `pressure_trend{}`, `stint_laps` | `{LF,RF,LR,RR}tempCM` / `pressure`, or flattened `TyrePressure_0..3` |
| `car_health` | `oil_temp`, `water_temp`, `oil_pressure`, `fuel_pressure`, `voltage`, `tow_time`, `engine_warnings[]` | health scalars + `EngineWarnings` |
| `conditions` | `air_temp`, `track_temp`, `humidity`, `wind_*`, `track_wetness`, `air_temp_trend`, `track_temp_trend` | weather scalars |
| `capabilities` | one bool per group + `missing[]` | probed once per catalog bind |
| `metrics` | `frames`, `events`, `last/avg/max_update_ms` | engine self-timing |

`CarState` (one per active car): `idx`, `is_player`, `position`, `class_position`,
`lap`, `lap_dist_pct`, `laps_completed`, `last/best_lap_time`, `on_pit_road`,
`track_surface`, `gap_ahead`, `gap_behind`, `gap_to_player`.

### Grounding rules worth knowing

- **Gaps.** `gap_basis` names how lap-distance deltas became seconds:
  `"lap_time_pct"` (Δpct × a known lap time — preferred, stable) or
  `"lap_length_speed"` (Δpct × track length ÷ instantaneous speed). With neither
  available, `gap_basis` is `None` and **every gap is `None`** rather than guessed.
  `gap_ahead` / `gap_behind` are running-order gaps; `gap_to_player` is a signed
  *track-position* gap wrapped to ±half a lap (positive = ahead on track).
- **Fuel.** `capacity` is derived as `level / level_pct` — a real measurement, not
  a constant. `per_lap` is a rolling mean over the last N **green, non-pit** laps
  (`RTV_PITWALL_FUEL_LAPS`, default 5), with `per_lap_std` alongside so a consumer
  can see the spread. Derivations refresh at ~1 Hz *and* at lap boundaries, so
  `level` and `laps_remaining` are never mutually inconsistent.
- **Fuel distances** count from the car's position: `left = 1 - LapDistPct` (1.0 at
  the line). `laps_to_finish` is `left` on the white flag, `laps_remain - 1 + left`
  with a lap counter, and for a timed race `left + ceil(max(0, time_remain -
  left x pace) / pace)`, where pace is the median of recent clean laps. The timed
  rule (finish at the first crossing after the clock runs out) is checked on the
  Spa file: white at the line with 98 s left, checkered one lap later. Counting
  whole laps while the tank drained made `margin_laps` fall by up to a lap inside
  every lap and fired a phantom `fuel_margin_low` after the stop.
- **Pit window.** `latest = lap + floor(level / per_lap - left)` (the last lap whose
  end the car reaches); `earliest = lap + max(0, ceil(laps_to_finish - left -
  capacity / per_lap))` (stopping earlier would force a second stop). Both used to
  be a lap late: at Spa `latest` read 14 where the car would have run dry on 14. `window_open` additionally requires a stop
  to actually be **needed** (`margin_laps < 0`) — otherwise "open" would be
  technically true but useless advice, and would re-announce after every stop.
- **Tyre trends** are cleared on pit exit: a trend from the set that just came off
  is not stale data, it is data about a different tyre.
- **Missing channels** never produce a zero. `capabilities.<group>` goes `False`,
  the fields stay `None`, and `capabilities.missing[]` names what was looked for.

---

## Event catalog

`RaceEvent` = `{event_type, tick, session_time, lap, severity, payload, state_version}`.
`state_version` pins the event to the `RaceState` version current at emission.
There is **no wall-clock field** — that is what makes the replay log reproducible.

`event.key` is a stable discriminator used by tests and the UI: the event type,
except for flag changes which render as `flag_change:<phase>`.

| `event_type` | Severity | Fires when | Key payload |
|---|---|---|---|
| `flag_change` | critical (red/yellow) else info | `SessionFlags` phase changes | `from`, `to`, `active[]` |
| `lap_completed` | info | `Lap` increments | `lap`, `lap_time`, `stint`, `green` |
| `personal_best` | info | lap time beats the player's best | `lap`, `lap_time` |
| `session_fastest_lap` | info | lap time beats the session best seen | `lap`, `lap_time` |
| `lockup` | advisory | front wheel speed collapses under braking | `slip`, `corner`, `speed`, `brake` |
| `wheelspin` | info | driven wheel outruns the car under power | `slip`, `corner`, `speed`, `throttle` |
| `offtrack` | advisory | `PlayerTrackSurface` enters off-track | `surface`, `from`, `lap_dist_pct` |
| `incident` | critical | incident counter increases | `incidents`, `delta` |
| `pit_entry` / `pit_exit` | info | `OnPitRoad` edge | `lap`, `stint`, `fuel` |
| `stint_start` | info | immediately after `pit_exit` | `stint`, `lap` |
| `pit_window_open` | advisory | fuel window opens (see rules above) | `earliest_lap`, `latest_lap`, `laps_remaining`, `per_lap` |
| `pit_window_closing` | advisory | current lap reaches `latest_lap - 1` while open | `latest_lap`, `laps_remaining`, `per_lap` |
| `fuel_margin_low` | advisory | `margin_laps` drops below `RTV_PITWALL_FUEL_MARGIN_LAPS` | `margin_laps`, `margin_l`, `threshold_laps` |
| `fuel_critical` | critical | `laps_remaining < 1.5`; re-arms on refuel | `level`, `laps_remaining` |
| `stint_lap_milestone` | info | every N green laps on the current set | `stint`, `laps_on_tyres`, `every`, `lap_time` |
| `rival_pitted` | advisory | a nearby car's `CarIdxOnPitRoad` goes false→true | `car_idx`, `position`, `gap_to_player` |
| `blue_flag` | advisory | a car ≥0.7 laps up is closing within 2.5 s | `car_idx`, `gap`, `laps_ahead` |
| `recurring_issue` | advisory | *N* of the same issue at the same corner inside *W* laps | `issue`, `corner`, `occurrences`, `laps[]`, `worst_slip` |
| `tyre_out_of_band` | advisory | a tyre measure leaves its band (see stage 3) | `measure`, `corner`, `value`, `threshold` |
| `car_health_warning` | critical / advisory | an engine warning bit, a tow, or oil/water over the limit | `measure`, `warnings[]`, `value`, `threshold` |
| `traffic_close` | advisory | a car is inside the gap threshold *and* closing | `car_idx`, `gap`, `side`, `closing_rate_s_per_s` |
| `car_alongside` | advisory (`clear`: info) | `CarLeftRight` changes; key `car_alongside:<side>`; the first clear of a session and the sensor turning off are silent (B2) | `side` (`car_left`, `car_right`, `car_left_right`, `two_cars_left`, `two_cars_right`, `clear`), `from`, `raw` |
| `weather_change` | — | **never** — director-only (eval rehearsals); wakes strategist and engineer on rain | `measure`, `from`, `to`, … |
| `regulation_change` | — | **never** — director-only (eval rehearsals); a mandatory stop wakes the strategist | `regulation`, `applies_from_lap`, … |
| `visual_observation` | info | **never** — published by `rtv.vision.live`, see `docs/VISION.md` | `source: "vision"`, `kind`, `text`, `confidence`, `trigger` |

`visual_observation` is the third member no detector emits: a kept VLM note about a
moment the telemetry flagged, words only, published straight to the bus and never read
by the engine. The spotter and coach take it, always at `info` priority (see
`docs/VISION.md`).

The `weather_change` and `regulation_change` rows are director-only. They
exist so a scripted scenario can say "rain in eight minutes" or "the stop is now
mandatory" *as an ordinary event*: nothing in the channel contract announces
either, and inferring one from a temperature drift would be exactly the invented
figure this codebase refuses everywhere else. Since stage 10 the strategist and the
vehicle engineer take them (rain, a mandatory stop) and the race plan honours a mandatory
stop, but only the eval path's `ScriptedDirector` ever emits them, so a live session
never sees one.

The five events added in stage 2 exist so the strategist can be woken by *facts*
rather than by a clock. `stint_lap_milestone` in particular is the periodic
wake-up, and it is deliberately lap-driven: a timer would fire under a red flag,
in the pits, and at 4× replay speed.

`pit_window_closing` and `fuel_margin_low` are distinct from the events they sit
next to. "Open" says a stop is *available*; "closing" says it is now urgent.
`margin_laps` is slack against the **finish**, so it goes negative many laps
before `fuel_critical` (slack against **running dry**) — which is exactly the
lead time a strategist needs.

**Latching.** Continuous detectors (lockup, wheelspin, blue flag, traffic) fire
once per episode: they re-arm only after the condition clears, with a cooldown
floor. Edge detectors (pit, off-track, incident, lap, flag) are naturally
one-shot. `tyre_out_of_band` and `car_health_warning` latch by **measure**, so an
oil warning after a water one still fires; the tyre latch re-arms on new rubber.

The last four rows were added in stage 3 and are all **aggregates**: they exist so
an agent is never woken by a single observation. See "Deterministic aggregation"
below.

---

## Event bus

```python
sub = engine.bus.subscribe(maxsize=256, name="strategist")   # queue style
events = sub.drain()                                          # sync
events = await sub.next_events(timeout=1.0)                   # asyncio
engine.bus.subscribe_callback(fn)                             # sync fan-out
engine.bus.history(limit)                                     # bounded ring buffer
```

Producer is a plain thread; async consumers are woken via
`loop.call_soon_threadsafe`. Each subscriber has a **bounded** queue that drops
the *oldest* event on overflow and counts the drops (`sub.dropped`) — a pitwall
that silently discarded a black flag would be worse than one that admits it.
A raising callback is logged and cannot stop the loop.

---

## Replay

```python
from rtv.racestate import RaceStateEngine
from rtv.racestate.replay import ReplayDriver, store_source

engine = RaceStateEngine(source="replay")
ReplayDriver(engine).run(store_source(db, parquet_dir, session_id), speed=1.0)

# In the app the driver also publishes every frame to the /ws/live hub:
ReplayDriver(engine, publish=hub.publish_frame, on_lifecycle=...).start(source, speed=4.0)

# Tests only:
from evals.synthetic_race import replay_scenario, scenario_source
replay_scenario(engine)                                  # scripted race, instant
```

`speed`: `1.0` real time, `N` for N× real time, `0` as-fast-as-possible.
`store_source()` streams **lap partition by lap partition**, so a full race
distance never lands in memory at once.

### The scripted scenario (test ground truth)

`ScenarioSpec` defaults to a 6-lap, 8-car race at 60 Hz with a 10.0 s lap:

| Lap / position | Scripted |
|---|---|
| 3, 30–80 % | full-course yellow, then green |
| 3 → 4 boundary | fuel pit window opens |
| 4, ~40 % | front-axle lock-up under braking |
| 5, 85 % | pit entry |
| 6, 15 % | pit exit + refuel + new stint |

Fuel arithmetic is chosen so the window lands on lap 4 *exactly*: a 4.0 L tank at
0.5 L/lap covers 8 laps, the race is 12 laps, and the car starts on 3.0 L — so a
stop at the end of lap 4 leaves the 8 laps a full tank covers, and the 1.5 L
aboard at the lap-4 line runs dry after lap 6 (window 4-6; it read 5-7 before the
fuel distances counted the lap the car was on). Grid offsets are in laps, so a 10 s lap turns a 0.03-lap
offset into exactly a 0.30 s gap, making gap assertions exact.

`ScenarioSpec(exclude=(...))` omits channels from both the catalog and the frames
— this is how the degradation tests strip tyres / fuel / wheels / standings.

`seed_scenario_session(writer, session_id)` writes the same race through the real
storage writer, so it can also be replayed back out of DuckDB/Parquet.

---

## API surface (added; all v1 endpoints untouched)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/racestate` | Current `RaceState` snapshot |
| GET | `/api/v1/racestate/events?limit=` | Recent events from the ring buffer |
| POST | `/api/v1/replay/start` | `{session_id, speed}`; `session_id:"scenario"` for the scripted race |
| POST | `/api/v1/replay/stop` | Stop the running replay |
| GET | `/api/v1/replay/status` | `{running, session_id, speed, frames, total, finished, error}` |

### WebSocket `/ws/pitwall`

```jsonc
// client -> server
{"op":"subscribe","rate_hz":10}
{"op":"set_rate","rate_hz":2}

// server -> client
{"type":"state","state":{ /* full RaceState */ }}      // at rate_hz, latest-wins
{"type":"event","event":{ /* RaceEvent + key */ }}     // immediately, never coalesced
{"type":"ack","op":"subscribe","rate_hz":10}
{"type":"error","code":"unknown_op"}
```

Two payloads, two policies, for a reason: an old **state** snapshot is worthless
once a newer one exists, so slow clients skip ahead. An **event** is a discrete
fact — dropping one to save bandwidth would silently lie to the strategist.
A snapshot is pushed immediately on connect so a client is never blank.

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `RTV_PITWALL` | `true` | Mounts the engine, the routes above and `/ws/pitwall`. `false` ⇒ v1 surface only, `services.engine is None`. |
| `RTV_PITWALL_GAP_INTERVAL` | `6` | Recompute standings gaps every Nth frame (6 @ 60 Hz = 10 Hz). |
| `RTV_PITWALL_FUEL_LAPS` | `5` | Green laps in the rolling consumption mean. |

---

## Performance

The spec's budget is <16 ms per frame. Measured on the 3 360-frame scenario:
**avg 0.03 ms, max 0.24 ms** per frame — ~500× inside budget. Reported live in
`state.metrics` rather than assumed.

Nothing is recomputed wholesale per tick. Per frame: player, flags, fuel level,
tyres, health, conditions, detectors. Decimated: standings gaps (10 Hz), fuel
derivations (~1 Hz), conditions trend (1 Hz). At lap boundaries: fuel statistics,
tyre trends. Capabilities are probed once per catalog bind.

---

## Interfaces stage 2 consumes

```python
from rtv.racestate import RaceStateEngine, RaceState, RaceEvent, EventType, Severity

services.engine                      # RaceStateEngine | None (None when RTV_PITWALL=false)
services.replay                      # ReplayDriver | None

engine.snapshot() -> RaceState       # deep, consistent copy; safe from any thread
engine.state                         # live mutable object (prefer snapshot())
engine.bus.subscribe(...)            # -> Subscription: .drain() / await .next_events()
engine.bus.subscribe_callback(fn)    # sync fan-out on the producer thread
engine.bus.history(limit)            # replay recent events to a late joiner
engine.reset()                       # clear accumulators between sessions/replays
```

An agent stage should **subscribe to events for triggering** and **read
`snapshot()` for context** — never poll raw frames. `RaceState` is already
LLM-sized (a few KB of JSON), the same way `LapFindings` is for the Layer-1 coach.

---

## Deviations from the stage-1 spec

Each is a conservative choice made autonomously, per `CLAUDE.md`.

1. **Gap basis prefers lap time over instantaneous speed.** The spec says "derive
   from lap-dist deltas and speed". Instantaneous speed makes gaps swing wildly
   through slow corners, so a known lap time is used when available and the
   speed-based form is the fallback. Both are implemented and the choice is
   reported in `standings.gap_basis`, so nothing is hidden.
2. **`window_open` requires a stop to be needed.** A bounds-only window also reads
   open when the car can already reach the flag — true but useless, and it
   re-announced after every stop. Added the `margin_laps < 0` condition.
3. **Fuel derivations refresh at ~1 Hz, not only at lap boundaries.** Lap-boundary-only
   left `level` (per-frame) inconsistent with `laps_remaining` (per-lap) after a
   pit stop. Same incremental discipline, finer cadence.
4. **Live sessions do not populate `lap_length_m`.** `set_session_info()` exists and
   the replay driver calls it, but wiring session-info YAML into the live path
   would mean changing `LivePoller`. Deferred rather than touch v1 ingest; gaps
   simply use the lap-time basis, which is preferred anyway.
5. **No pre-existing "demo-session seeder" to extend.** The spec assumed one; the
   repo had ad-hoc synthetic data in `tests/helpers.py` and `scripts/smoke_offline.py`.
   Generalised that pattern into `racestate/scenario.py` instead, leaving both
   originals untouched.
6. **Added `GET /api/v1/racestate/events`** beyond the specified surface — the
   event ring buffer is needed by the smoke script and stage 6's UI, and exposing
   it costs nothing.
7. **`blue_flag` is standings-derived, as specified**, and does not consult
   `CarIdxSessionFlags`. Keeping one source of truth makes it testable offline
   without the sim having waved the flag.
8. **Detector accessors (`as_float`/`as_int`/`as_bool`) are public** in
   `detectors.py` because `engine.py` uses them; they return `None` for
   missing/NaN/unparseable values rather than defaulting to `0.0`.

### A store bug found and fixed en route

DuckDB identifiers are case-insensitive, so the store's meta `lap` column and the
`Lap` telemetry variable collapse into a single result key on `SELECT *` — every
`row["Lap"]` read back as `None`. `replay._column_resolver()` resolves catalog
columns exact-match-first, then case-insensitively. Without this, replay from the
store silently produced no lap events at all. Worth knowing for any future code
that reads frames back out of Parquet.

---

## Verification

```powershell
pytest                            # 110 passed (50 pre-existing + 60 new), fully offline
python scripts/smoke_pitwall.py   # scripted replay + ordered milestone assertions
python scripts/smoke_offline.py   # v1 surface, unchanged
ruff check src tests scripts evals
```

New test files: `tests/test_racestate_detectors.py` (30), `tests/test_racestate_engine.py`
(19), `tests/test_racestate_api.py` (11). No iRacing, no `ANTHROPIC_API_KEY`, no
network. All 50 pre-existing tests still pass unmodified.

---
---

# Stage 2 — the agentic layer

`src/rtv/pitwall/` — the first package in the v2 build that talks to a model, and
it does so as rarely as possible. Everything continuous already happened in
`racestate/`; by the time an agent wakes up, the numbers exist and its job is
purely judgement.

## The hybrid cost model, concretely

**A model is called only when a `RaceEvent` fires an agent's trigger.** No timer,
no polling, nothing per tick. The scripted 6-lap race (3 360 frames, 20 events)
wakes the strategist **6 times**. A real hour-long race with a 5-lap milestone
cadence and the cooldowns below lands in the low tens of calls — the same order
as one Layer-3 coaching run, spread over the whole race.

Four things hold that down, in order of how much they matter:

1. **Triggers are events, not states.** `pit_window_open` fires once per stint.
2. **Predicates veto before the call.** A checkered flag is a `flag_change`, but
   `_yellow_or_red` rejects it; a milestone with three laps of fuel slack is
   rejected by `_stop_is_needed`. Neither costs a token.
3. **Per-trigger cooldowns**, measured in **session** time so a 4× replay behaves
   like the live race.
4. **Kill switches** — global and per agent — checked in `dispatch()`, before the
   worker queue, so "off" means zero spend, not a quiet radio.

---

## Module map

| Module | Role |
|--------|------|
| `framework.py` | `AgentSpec`, `Trigger`, `ToolSpec`, `AgentRuntime`, `RadioMessage`. The only code that talks to a model. |
| `provider.py` | `KimiProvider` (real) and `ScriptedProvider` (offline), behind one `complete()` method. |
| `tools.py` | The deterministic tool registry over `racestate` + `strategy_math`. |
| `validator.py` | `FactSet` + `validate_output`: the in-loop citation check. |
| `radio.py` | `RadioFeed`: one channel, priority-ordered, self-superseding, paced. |
| `orchestrator.py` | `PitwallOrchestrator`: bus → routing → workers → merged feed. |
| `agents/__init__.py` | The registry. **This is the only file stage 3 must edit.** |
| `agents/strategist.py` | The reference agent: prompt, tools, triggers, contract. |
| `racestate/strategy_math.py` | `simulate_pit_outcome` and friends — pure, LLM-free, tested standalone. |

## An agent is four things

```python
STRATEGIST = AgentSpec(
    name="strategist",
    role_prompt=STRATEGIST_PROMPT,       # who it is, what it may say
    output_model=PitCall,                # the contract (a Pydantic model)
    tools=(GET_FUEL_PROJECTION, SIMULATE_PIT_OUTCOME, ...),   # a scoped subset
    triggers=(Trigger("pit_window_open", cooldown_s=0.0), ...),
    model="claude-sonnet-4-6",
    state_slice=strategist_slice,        # the compact context it reasons over
    subject=lambda event, out: "pit_stop",  # the radio supersede key
)
```

There is no strategist-specific branch anywhere in the runtime, the orchestrator,
the radio feed or the API. `tests/test_pitwall_orchestrator.py::test_a_second_agent_is_pure_configuration`
adds a second agent with three lines of `dataclasses.replace` and asserts it routes.

## The runtime loop

```
trigger fires
  -> assemble context: role prompt (cached) + state slice + event + recent radio
  -> run the spec's prefetch tools locally; results go in the first message
  -> model calls its scoped tools; every tool result is added to the FactSet
  -> tools are withdrawn on the last pass, so the model must answer
  -> structured output validated against the FactSet
  -> one repair turn naming the exact rejected figures
  -> still ungrounded? emit a grounded refusal instead
  -> RadioMessage onto the merged feed
```

Prompt caching follows the Layer-3b coach: the stable role prompt is the first
system block and carries `cache_control`; the volatile state slice comes after it,
uncached. Reversing them would cache nothing, since caching is a prefix match.

**Prefetch.** `AgentSpec.prefetch` is a tuple of `(tool, args)` (args may be a
callable of `(state, event)`) that the runtime runs before the first model turn.
The results go in the first message as tool results and into the FactSet, and each
is traced as a `tool_call` with `prefetched: true`. On the Spa replay every run
opened with the same one to three local tool calls, and a model turn averaged
about 11 s, so a run took two to four turns (15 to 58 s). With the answers already
in hand, the common case is one turn. Measured on the second Spa replay (kimi-k3): 39
model calls for 27 runs (was 75 for 30), median run 15 s (was 24 s). Prefetch made
kimi-k3 answer in prose rather than call the respond tool on many turns, so
`KimiProvider` now sends `tool_choice: any` whenever tools are offered with thinking
off, and a prose-only turn gets one forced respond pass. The validator also accepts a
fraction said as a percentage (lap_dist_pct 0.9498 as "94.98 %"). The prefetch sets are: strategist
`get_fuel_projection` and `simulate_pit_outcome` at the planned stop lap; engineer
`get_car_health`, `get_tyre_trend` and the last 10 detector events; spotter
standings plus the last 10 detector events; coach the last 10 detector events plus
`get_corner_detail` at the trigger's lap distance. Every tool remains callable.

**Token usage.** `KimiProvider` reads `usage` off every response
(`rtv.llm.usage_of`) and returns it on `ProviderResponse.usage`. Each call is also
recorded in the process-wide `rtv.llm.USAGE` ledger under its layer (the agent
name, `orchestrator.<Schema>`, `eval_judge`, `coach_chat` through a LangChain
callback, vision), and logged as one INFO line. `llm_call_end` journal records
carry `input_tokens`, `output_tokens` and `cache_read_input_tokens`. Agent `stats()`
total the tokens, the race summary adds `model_input_tokens` and
`model_output_tokens` (null for older journals), and `GET /api/v1/llm/usage` serves
the ledger since server start. Counts only: no prompt text, no key.

---

## Grounding: how a number is allowed to be said

Every number an agent utters must be traceable to something it was *actually
shown* — the state slice, the triggering event, or a tool result. The check is
deterministic and in-loop, because a race engineer cannot afford a second LLM call
to fact-check the first one.

Two rules, and they are deliberately different:

| Where | Rule | Why |
|---|---|---|
| `spoken_text` / `detail_text` | tolerant: ±0.05 or 2 %, and rounded/absolute forms of a fact count | "2.5 laps" for 2.487, or "6.3 laps short" for a margin of −6.298, is reporting, not inventing |
| the structured payload (`rejoin_position`, `target_lap`, …) | **exact** against raw facts only | these are meant to be copied from a tool verbatim. Allowing rounding here would let a tyre pressure of 18.9 "support" a claimed P19 — the false negative that would make the whole check theatre |

Also checked in-loop: clock notation (`1:32.4` scores as 92.4 s, not as 1 and
32.4), and the 25-word radio limit.

**On failure**: one repair turn naming the exact figures. If it still fails, the
message becomes a **grounded refusal** — priority downgraded to `info`,
`spoken_text` replaced with "Standby — I can't back that call with data yet.",
`refused=True`, and the offending text preserved in `detail_text` so an operator
can see what was blocked. Silence would have been safer than a wrong number, but
silently dropping it would have hidden a broken agent.

The state slice is therefore also a **permission**: the strategist is not shown
car health or weather, so it cannot quote an oil temperature as a gap. Scoping
tools scopes speech.

---

## Radio discipline

One driver, one channel. `RadioFeed` is a priority queue plus fan-out:

- **Order** is `(priority, sequence)` — `critical` pre-empts anything queued.
- **Supersede**: a newer non-critical message with the same `(agent, subject)`
  replaces the older one *while it is still queued*. Two pit-window updates are
  one current answer, not a log of them.
- **A critical message is never superseded and never dropped.** Losing a "box now,
  you're on fumes" to a tidier queue is the one unforgivable bug in here.
- **Airtime**: a message holds the channel for ~0.4 s per spoken word. This is
  what makes superseding meaningful, and it is the hook stage 4's TTS hangs off.
- `pump(session_time)` is the synchronous, clock-injected twin of `run()`, so a
  replayed race produces a reproducible radio log the same way it produces a
  reproducible event log.

## Concurrency

- **Per-agent serialisation**: one worker and a one-slot inbox per agent, so an
  agent can never overlap itself. A newer event replaces a waiting one
  (latest-wins) — **except** that a queued `critical` event is never displaced by
  a lesser one.
- **Global cap**: a semaphore bounds in-flight model calls (`RTV_PITWALL_MAX_INFLIGHT`,
  default 2), so a safety-car burst cannot fan out into a dozen requests.

---

## The STRATEGIST

Triggers, all deterministic:

| Trigger | Predicate | Cooldown |
|---|---|---|
| `pit_window_open` | — | 0 s |
| `pit_window_closing` | — | 0 s |
| `fuel_critical` | — | 0 s |
| `flag_change` | into/out of yellow or red only | 20 s |
| `fuel_margin_low` | only when `margin_laps < 0` (short of the flag) | 60 s |
| `rival_pitted` | within 30 s of us | 45 s |
| `stint_lap_milestone` | only when `margin_laps < 3` | 60 s |

Tools: `get_race_state_slice`, `get_fuel_projection`, `get_tyre_trend`,
`get_standings_around_player`, `get_stint_history`, `simulate_pit_outcome`.

Output contract `PitCall`: `recommendation` ∈ {`box_now`, `box_next_lap`,
`pit_under_yellow`, `plan_stop`, `extend`, `stay_out`, `hold`}, plus `target_lap`,
`fuel_to_add_l`, `rejoin_position`, `confidence`, `rationale`, `risks[]`.
`hold` is a first-class answer, not a failure mode: it is what the agent says when
the data does not support a call.

### `stop_plan` and the strategist policy

`strategy_math.stop_plan(state)` decides *when* to stop from the fuel window
alone: `target_lap` is the latest lap that keeps 0.5 laps of fuel in hand
(clamped to the window), and `call` is `no_data`, `no_stop_needed`, `stop_later`,
`box_next_lap` or `box_this_lap`, with `fuel_to_add_l` from `simulate_pit_outcome`
at that lap, `caution_stop_available` and a `confidence` that is `low` under three
fuel samples. It is in the strategist's state slice and in `get_fuel_projection`.
On the Spa file it plans lap 13 early on, settles on lap 12 by lap 6 as consumption
rises, and says `box_this_lap` on lap 12, the lap the driver pitted.

`AgentSpec.policy(output, state, event) -> reason | None` is a deterministic check
on the *decision*, beside the validator's check on the numbers. A violation gets the
one repair turn (with the reason); if it persists the message is published
`refused=True, speak=False`, priority info, subject `<subject>:withheld`, with the
reason in `data.withheld`, so it is audited but never said and never supersedes a
standing call. `strategist_policy` rules out: a stop call or `plan_stop` when the
tank reaches the flag or there is no plan; a `target_lap` outside the window;
`pit_under_yellow` without a full-course caution in the window; and `box_now` /
`box_next_lap` more than one lap before `target_lap` unless a caution is out or a
`rival_pitted` trigger fired with live standings. The eval's `expected_calls`
follows the plan the same way.

### `simulate_pit_outcome` — the deterministic core

In `racestate/strategy_math.py`, tested independently in `tests/test_strategy_math.py`.

```
rejoin_position = position_before + |{cars behind whose cumulative gap < effective_loss}|
effective_loss  = pit_lane_loss_s x (0.45 under yellow, else 1.0)
fuel_to_add     = min(capacity, (laps_after_stop + 0.5) x per_lap) - fuel_at_stop
laps_before_stop = (stop_lap - lap) + left   # a stop this lap still burns this lap
```

The model is stated rather than hidden: it assumes the field holds station over
the stop, which is exactly the assumption a race engineer makes on the radio, and
it is returned in `assumptions[]` for the agent to hedge with.

Grounding carries all the way down. No `gap_basis` ⇒ `rejoin_position is None` and
an assumption line saying why; no learned `per_lap` ⇒ no refuel target; neither ⇒
`grounded=False` with a `reason`. Nothing is ever estimated into existence.

---

## API surface (added; v1 and stage 1 untouched)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/pitwall/status` | Agents, triggers, per-agent stats, radio counters |
| GET | `/api/v1/pitwall/radio?limit=` | Recent radio + what is still queued |
| POST | `/api/v1/pitwall/enabled` | `{enabled}` — the global kill switch |
| POST | `/api/v1/pitwall/agents/{name}/enabled` | `{enabled}` — one agent |
| POST | `/api/v1/pitwall/reset` | Clear cooldowns and the channel |

`/pitwall/status` answers even when the layer is **not** mounted (`available:false`
plus the reason), because a UI needs to explain why the radio is quiet rather than
show an error.

### `/ws/pitwall` gains a third frame type

```jsonc
{"type":"radio","message":{
  "agent":"strategist", "priority":"critical",
  "spoken_text":"Box this lap, 3.0 litres, we come out P8.",
  "detail_text":"Window is lap 5 to 7. ...",
  "data":{"recommendation":"box_now","target_lap":5,"rejoin_position":8,...},
  "event_ref":{"key":"pit_window_open","tick":2400,"lap":5,"state_version":2400},
  "grounded":true, "refused":false, "tools_used":["get_fuel_projection","simulate_pit_outcome"]
}}
```

Never coalesced: the feed has already decided what was worth saying, and the
socket must not second-guess it. `event_ref` pins every call to the deterministic
observation that caused it, so the UI can always show its working.

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `RTV_PITWALL_AGENTS` | `false` | Mount the agent layer. **Opt-in** — see deviation 1. |
| `RTV_PITWALL_AGENTS_LIVE` | `true` | Startup value of the kill switch. |
| `RTV_PITWALL_AGENT_MODEL_FAST` | `claude-haiku-4-5-20251001` | High-frequency, low-stakes agents. |
| `RTV_PITWALL_AGENT_MODEL_REASONING` | `claude-sonnet-4-6` | Strategy-grade reasoning. |
| `RTV_PITWALL_STRATEGIST_MODEL` | *(empty)* | Per-agent override. |
| `RTV_PITWALL_MAX_INFLIGHT` | `2` | Cap on concurrent LLM calls. |
| `RTV_PITWALL_AGENT_COOLDOWN_S` | `30` | Default per-trigger cooldown. |
| `RTV_PITWALL_PIT_LANE_LOSS_S` | `25` | Input to the rejoin projection. |
| `RTV_PITWALL_RADIO_HISTORY` | `200` | Radio ring buffer. |
| `RTV_PITWALL_STINT_MILESTONE_LAPS` | `5` | Green laps between milestones. |
| `RTV_PITWALL_FUEL_MARGIN_LAPS` | `1.0` | Threshold for `fuel_margin_low`. |

---

## Evals

`evals/pitwall_cases.py` **generates** the golden set instead of shipping one. A
lap of telemetry cannot be written by hand, but a race state can: replaying the
scripted race yields the exact triggering events with the exact state current at
each, using the same `AgentRuntime.match()` the live path uses. Four degraded
cases are appended — no fuel model, no gap basis, a caution with a stop owed, and
a rival stopping when we don't need fuel — because a set where everything is known
cannot tell a grounded strategist from a confident one.

`evals/pitwall_checks.py` scores six things deterministically:

| Check | Ground truth |
|---|---|
| `grounding_ok` | every figure traces to the state or a tool |
| `radio_ok` | `spoken_text` ≤ 25 words |
| `decision_ok` | the call is inside the defensible **band** for this state |
| `window_ok` | the named stop lap is inside the fuel window |
| `rejoin_ok` | the claimed rejoin matches `simulate_pit_outcome` (±1) |
| `refusal_ok` | it held rather than guessed when the data was absent |

`decision_ok` scores a *band*, not one right answer: box-now vs box-next-lap are
both defensible on an open window, staying out on an empty tank is not.

```powershell
python evals/run_pitwall.py --dry-run   # cases + ground truth, no key needed
python evals/run_pitwall.py             # the real strategist (needs a key)
```

---

## Interfaces stage 3 consumes

Adding the vehicle engineer, spotter and coach is **one new module plus one
registry entry**. (Stage 3 did exactly that — the real specs are below; the sketch
here is what stage 2 predicted, kept because the prediction held.)

```python
# src/rtv/pitwall/agents/spotter.py
class SpotterCall(AgentOutput):
    threat: Literal["overlap_left", "overlap_right", "clear"]

SPOTTER = AgentSpec(
    name="spotter",
    role_prompt=SPOTTER_PROMPT,
    output_model=SpotterCall,
    tools=(T.GET_STANDINGS_AROUND_PLAYER, T.GET_RACE_STATE_SLICE),
    triggers=(Trigger(EventType.BLUE_FLAG.value, cooldown_s=10.0),),
    model=FAST_MODEL,
    state_slice=spotter_slice,
    subject=lambda event, out: "traffic",
)

# src/rtv/pitwall/agents/__init__.py
AGENT_REGISTRY = (STRATEGIST, SPOTTER)      # <- the only edit outside the module
```

Everything else — routing, cooldowns, the tool loop, grounding, refusal, radio
merging, the API, the WebSocket frames, the status payload — already works for it.

```python
from rtv.pitwall import (
    AgentSpec, AgentOutput, AgentRuntime, Trigger, ToolSpec, ToolContext,
    RadioMessage, RadioPriority, RadioFeed, PitwallOrchestrator,
    ScriptedProvider, KimiProvider, tool_use_turn,
    FactSet, validate_output, build_agents, ALL_TOOLS, TOOLS_BY_NAME,
)

services.pitwall                       # PitwallOrchestrator | None
orch.feed.subscribe(name="tts")        # -> .drain() / await .next_messages()
orch.feed.subscribe_callback(fn)       # sync fan-out (stage 4's TTS hook)
orch.feed.pump(session_time)           # deterministic, replay-stable pacing
await orch.handle_event(event, state)  # route + run inline (tests, smoke, evals)
orch.status()                          # the /api/v1/pitwall/status payload
```

New tools are added to `tools.py` and listed in an agent's `tools=(...)`; a tool
not in an agent's tuple is unreachable *and* uncitable by it.

---

## Deviations from the stage-2 spec

1. **`RTV_PITWALL_AGENTS` defaults to `false`.** The spec implies the layer is on.
   But a developer who exported `ANTHROPIC_API_KEY` for the Layer-3 coach would
   otherwise have live race radio start billing the moment they drove. Opt-in is
   the conservative reading of "kill switch + per-agent enable flags"; everything
   is one env var away.
2. **`RadioMessage` lives in `framework.py`, but so do `ToolSpec`/`Trigger`.** The
   spec listed only the three classes; splitting the rest into a fourth module
   bought nothing, and `radio.py` is separate because it holds behaviour (priority,
   supersede, airtime) rather than the shape.
3. **The radio feed is not the race-state `EventBus`.** The spec says "publishes
   RadioMessages onto the bus". `EventBus` is typed to `RaceEvent` and has no
   notion of priority, supersede or airtime — all three of which are the point.
   `RadioFeed` reuses the bus's bounded/latest-wins subscription semantics and adds
   what radio actually needs. `/ws/pitwall` fans both out on one socket, so the
   observable contract is what the spec asked for.
4. **No `thinking` on agent calls by default.** The Layer-3b coach uses adaptive
   thinking because it runs post-hoc. A pit call that lands two corners late is
   worse than no call, and every number is already computed deterministically
   upstream, so latency wins. `KimiProvider(thinking=True, effort=...)` opts
   back in per deployment.
5. **Five new deterministic events, not agent-side timers.** The spec asked for
   triggers like `stint_lap_milestones` and `fuel_margin_below_threshold` "from a
   deterministic event, not a timer". None existed, so they were added to the
   stage-1 engine rather than synthesised in the agent layer. This keeps one source
   of truth and makes each trigger unit-testable without a model.
6. **The eval golden set is generated, not a JSON file.** `evals/golden_laps.json`
   exists because telemetry can't be hand-written; a race state can be, and
   generating it from the scripted race means every case carries its own ground
   truth and cannot drift from the engine. `test_the_golden_set_is_reproducible`
   pins it.
7. **The validator checks the structured payload exactly and prose tolerantly.**
   The spec says "reuse/adapt the in-loop citation validator". Applying one rule to
   both halves fails either way: strict prose rejects "2.5 laps" for 2.487, and
   tolerant payload lets a tyre pressure of 18.9 support a claimed P19. Both
   failure modes are covered by tests.
8. **`ScriptedProvider` takes a callable, not a canned list.** A fixed list of
   turns cannot express "answer using whatever the tool returned", which is the
   only way to test the tool loop end to end without a network.
9. **A session-scoped `tests/conftest.py` unsets `ANTHROPIC_API_KEY`.** An offline
   suite that only passes on a machine without a key is not an offline suite;
   `pytest` is now green either way, and that is asserted by running it both ways.

### Two bugs found and fixed en route

- **`ScriptedProvider` counted turns across its lifetime, not per conversation.**
  Every agent invocation after the first skipped its tool calls entirely. The
  smoke script caught it; `turn` is now derived from the message history.
- **The smoke script scored agents against the *finished* race.** It replayed
  first and dispatched afterwards, so every call saw the end-of-race snapshot. It
  now drives frame by frame, and `evals/pitwall_cases.py` does the same — worth
  knowing for any future code that replays and then inspects.

---

## Verification (stage 2)

```powershell
pytest                                   # 202 passed (110 stage 1 + 92 new), fully offline
python scripts/smoke_pitwall_agents.py   # the agent layer end to end, scripted provider
python scripts/smoke_pitwall.py          # stage 1, unchanged
python scripts/smoke_offline.py          # v1 surface, unchanged
python evals/run_pitwall.py --dry-run    # the golden set + its ground truth
ruff check src tests scripts evals
```

New test files: `tests/test_strategy_math.py` (17), `tests/test_pitwall_framework.py`
(32), `tests/test_pitwall_orchestrator.py` (13), `tests/test_pitwall_evals.py` (19),
`tests/test_pitwall_agents_api.py` (11). Green with and without
`ANTHROPIC_API_KEY` exported. No iRacing, no network. All 110 stage-1 tests still
pass unmodified.

---
---

# Stage 3 — the rest of the pitwall

Three more agents: the **vehicle engineer**, the **spotter** and the live
**coach**. The stage-2 claim was that this would be configuration rather than
framework work, and it very nearly was: three new modules under
`src/rtv/pitwall/agents/`, one registry line, four new tools, four new
deterministic events — and exactly one field added to the framework.

## The four agents, and why they cannot say each other's sentences

| agent | owns | wakes on | model tier |
|---|---|---|---|
| `strategist` | when we stop and what we take on | fuel, flags, rivals | reasoning |
| `vehicle_engineer` | what state the car is in | recurrence, tyres, health, stint end | fast |
| `spotter` | what is around us right now | traffic, blue flags, hazards | fast |
| `coach` | how the car is being driven | repeated mistakes, offs, stint pace | fast |

Separation is enforced three ways, none of which is a prompt instruction:

1. **The state slice is a permission.** The spotter is handed flags, the player
   and the running order — no fuel, no tyres, no car health. It is not that it is
   *told* not to quote a fuel number; the number is not in its fact set, so the
   validator rejects it and the call becomes a grounded refusal. Same for the
   engineer (no standings, no fuel) and the coach (no standings, no fuel).
2. **The tool list is a permission.** No role agent can reach
   `simulate_pit_outcome` or `get_fuel_projection`. A model that calls one anyway
   gets `{"error": "Unknown tool ..."}` back, and nothing it would have returned
   ever enters the fact set. Both halves are asserted in
   `tests/test_pitwall_roles.py`.
3. **The subject key scopes supersede.** The engineer's subject is the *system*
   (`tyres` / `grip` / `car_health`), so two tyre advisories collapse but a tyre
   note and an oil note both survive. The coach's is a single `coaching` slot: one
   cue stands at a time.

---

## Deterministic aggregation — where the money is saved

This is the substantive stage-3 idea. Stage 2 established that an agent is woken by
an *event*; the trouble is that a driver on a bad set locks the fronts twenty times
a stint, and twenty LLM calls to say "you're locking the fronts" is both expensive
and terrible radio.

So the aggregation happens in the engine, before any agent exists:

```
lockup (lap 1, C08)   --+
lockup (lap 2, C08)   --+-->  CornerRecurrence  -->  recurring_issue
lockup (lap 4, C08)   --+      (3 in 5 laps)          (one event, one call)
```

`racestate/detectors.CornerRecurrence` buckets lap distance into corners, counts
repeats per `(issue, corner)`, and returns a payload only on the *N*-th inside a
*W*-lap window. The singles still reach the bus — the UI wants them — but no
trigger references them. `scripts/smoke_pitwall_roles.py` asserts this directly:
three scripted lockups wake **nobody**, and the one aggregate wakes two agents.

Re-arming is by **new repeats since the last call**, not by running total. A
running-total rule ("fire again at 2N") can never fire twice, because the lap
window caps the total at *W*. Getting that wrong would have turned a recurring
problem into a one-time notification for the rest of the race.

| Knob | Env var | Default |
|---|---|---|
| corner buckets per lap | `RTV_PITWALL_CORNER_BUCKETS` | 20 (5 % of a lap) |
| repeats before it is a finding | `RTV_PITWALL_RECURRENCE_MIN` | 3 |
| lap window they must fall in | `RTV_PITWALL_RECURRENCE_WINDOW_LAPS` | 5 |

**Corner labels are `C00`–`C19`, not circuit corner numbers.** (Since E1 these are only
the counting key and the provisional label; agents cite `payload.place`, see the E1 note at
the end of the appendix.) Without a track map
we do not know where T7 is, and naming it would be exactly the unbacked figure the
rest of this system refuses to produce. The payload carries the lap-fraction range
so a UI can point at it, and every prompt says so explicitly.

### Tyre bands without inventing a "normal" tyre temperature

`detect_tyre_condition` reports the worst single way a set is out of band. The
absolute limits (`tyre_temp_max_c` and friends) are **`None` by default**: a
correct operating window is a per-car number nobody told us, and picking one would
be a guess dressed as a threshold. What is on by default are the two *relative*
measures, which need no per-car knowledge:

- **drift** — |per-lap trend| across the current stint, ≥ 3 °C/lap or 2 kPa/lap,
  and only once at least three stint laps have been completed;
- **axle imbalance** — left-to-right spread across one axle ≥ 15 °C.

An operator who knows their car sets the absolute band and gets that as well.

### Car health

The sim's own `EngineWarnings` bits come first, because that is the car speaking
rather than us inferring — minus `pit_speed_limiter` and `rev_limiter_active`,
which are normal driving. Then a tow (`PlayerCarTowTime > 0`, the closest thing to
a damage channel this catalog has), then oil/water over their configured limits.
The payload says which it was, so the engineer can distinguish "the car is telling
us" from "we think", and the prompt requires it to.

### Traffic

`traffic_close` needs the car to be inside `RTV_PITWALL_TRAFFIC_GAP_S` **and**
closing at ≥ 0.15 s per second, measured against a gap sampled a second earlier.
Proximity alone is not news — in a tight race someone is within a second for the
whole stint.

One subtlety worth recording: the sample is discarded when `gap_basis` changed
between the two readings. When a lap time first becomes known, gaps stop being
derived from instantaneous speed and *every* gap moves at once while no car moved.
Before this guard, the scripted race produced a phantom "car closing at 3.4 s/s"
on lap 1.

---

## New tools

| Tool | Returns | Unavailable when |
|---|---|---|
| `get_recent_detector_events` | filtered slice of the event ring buffer | nothing of those types yet |
| `get_car_health` | health scalars **plus the thresholds they were judged against** | no health channels in the catalog |
| `get_setup_snapshot` | `CarSetup` verbatim from the session-info YAML | no document, or no `CarSetup` section |
| `get_corner_detail` | live detector events at that corner **and** the Layer-1 corner analysis | nothing recorded / fewer than two clean laps |

`get_corner_detail` is the one that reaches outside `racestate`. Its `telemetry`
half calls the **same** `rtv.coaching.features.CoachingService` the v1 coaching
endpoints call — minimum speed against a reference lap, brake point, throttle
application, time lost — so the live coach and the post-hoc coach share a source of
truth rather than a re-implementation. It returns two halves on purpose:

```jsonc
{"requested_lap_dist_pct": 0.4033,
 "live":      {"corner": "C08", "count": 3, "events": [ /* the lockups here */ ]},
 "telemetry": {"available": true, "main_lap": 4, "ref_lap": 1,
               "corner": {"label":"T1","min_speed_kmh":75.4,"diagnostics":[...]}}}
```

The `live` half always works. The `telemetry` half needs a *written* session, so it
is unavailable during the opening laps and in any deployment that is not recording
— and says so with a reason rather than degrading quietly. `CoachCall.from_telemetry`
records which half the cue rested on, and the eval harness checks that a coach
never claims a trace it was not shown.

`get_setup_snapshot` returns the setup **verbatim**. Setup sections differ per car,
so flattening them into a fixed schema would either drop fields or invent them.

---

## The agents in detail

### VEHICLE ENGINEER

| Trigger | Predicate | Cooldown |
|---|---|---|
| `recurring_issue` | issue in {lockup, wheelspin, offtrack} | 45 s |
| `tyre_out_of_band` | — | 60 s |
| `car_health_warning` | — | 30 s |
| `pit_entry` | only when tyre data exists | 0 s |

Tools: `get_tyre_trend`, `get_recent_detector_events`, `get_corner_detail`,
`get_car_health`, `get_setup_snapshot`, `get_race_state_slice`.

`EngineerCall`: `finding` in {`tyre_temps`, `tyre_pressures`, `brake_lockup`,
`traction`, `car_health`, `damage`, `none`}, plus `corner`, `affected[]`, `trend`,
`driver_action`, `setup_note`, `confidence`, `rationale`. `none` is a real answer:
the trigger turned out to be benign.

**No numeric fields in the payload, deliberately.** Structured numbers are matched
*exactly* against raw facts (stage-2 deviation 7). There is no number the pitwall
UI needs from the engineer badly enough to be worth that, and everything
quantitative it says belongs in prose, where a rounded form is legitimate.

### SPOTTER

| Trigger | Predicate | Cooldown |
|---|---|---|
| `traffic_close` | — | 15 s |
| `blue_flag` | — | 10 s |
| `flag_change` | into yellow or red only | 10 s |
| `incident` | — | 10 s |

Tools: `get_standings_around_player`, `get_recent_detector_events`. Two tool
rounds, not four, and a 700-token budget: a spotter call that lands after the
corner is worthless.

`SpotterCall`: `threat` in {`lapped_traffic`, `car_closing`, `hazard`, `clear`},
`side` in {`ahead`, `behind`, `unknown`}, `car_idx`, `action`, `confidence`.

`car_idx` *is* a numeric payload field, and that is the point: it must be copied
from the event or the standings tool exactly, so a spotter that names a car nobody
mentioned is refused.

### COACH

| Trigger | Predicate | Cooldown |
|---|---|---|
| `recurring_issue` | issue in {lockup, wheelspin, offtrack} | 120 s |
| `offtrack` | under green only | 90 s |
| `stint_lap_milestone` | under green, at least 2 laps on the set | 180 s |

Tools: `get_corner_detail`, `get_recent_detector_events`, `get_stint_history`,
`get_tyre_trend`.

`CoachCall`: `theme` in {`braking`, `throttle`, `line`, `consistency`,
`tyre_management`, `none`}, `corner`, `cue`, `from_telemetry`, `confidence`,
`rationale`.

The coach and the engineer **share** the `recurring_issue` trigger, which is
intentional: three lockups at one corner is simultaneously a brake-bias question
and a technique question, and those are different sentences from different people.
The cooldowns are what bound the cost — the coach speaks at most a third as often,
so the car's advocate gets there first. Never coaching under a caution is the same
judgement: the driver has other things to think about.

---

## Deterministic evals for judgement calls

`evals/pitwall_cases.py` now generates a golden set **per agent**, using that
agent's own `AgentRuntime.match()` — so a case exists exactly when the live path
would have woken it, and there is no second, hand-maintained trigger list to
drift. The role scenario is the scripted race with two extra knobs
(`lockup_laps=(1,2)`, `corner_pcts=(0.44,)`) so that it actually contains a
repeated mistake at a real corner; both are opt-in, so the stage-1 ground-truth
scenario stays byte-identical.

`evals/pitwall_checks.py` scores each role against what the deterministic trigger
*said*. Grounding and radio discipline are identical for everyone; the rest is
role-specific:

| Agent | Checks beyond grounding + radio |
|---|---|
| `vehicle_engineer` | `finding_ok` (inside the band for that trigger), `corner_ok` (the event's corner, not another), `setup_ok` (no setup advice without a setup sheet), `refusal_ok` (no tyre finding without tyre channels) |
| `spotter` | `threat_ok`, `car_ok` (the car named in the event), `side_ok`, `brevity_ok` (14 words, tighter than radio), `refusal_ok` (no seconds without a gap basis) |
| `coach` | `theme_ok`, `corner_ok`, `telemetry_ok` (no claimed trace it never saw), `one_cue_ok` |

```powershell
python evals/run_pitwall.py --dry-run            # 24 cases across 4 agents, no key
python evals/run_pitwall.py --agent spotter      # one agent (needs a key)
python evals/run_pitwall.py                      # all four
```

`aggregate()` discovers its keys from the rows, so a mixed run reports the union
and omits a rate for an agent that does not carry that check.

---

## Configuration added

| Env var | Default | Meaning |
|---|---|---|
| `RTV_PITWALL_CORNER_BUCKETS` | `20` | Lap fractions used to decide "the same corner". |
| `RTV_PITWALL_RECURRENCE_MIN` | `3` | Repeats before `recurring_issue` fires. |
| `RTV_PITWALL_RECURRENCE_WINDOW_LAPS` | `5` | Lap window those repeats must fall in. |
| `RTV_PITWALL_TYRE_TEMP_TREND_C` | `3.0` | Degrees per lap of sustained drift. |
| `RTV_PITWALL_TYRE_AXLE_IMBALANCE_C` | `15.0` | Left-to-right spread across an axle. |
| `RTV_PITWALL_OIL_TEMP_MAX_C` | `130` | Oil limit for `car_health_warning`. |
| `RTV_PITWALL_WATER_TEMP_MAX_C` | `105` | Water limit. |
| `RTV_PITWALL_TRAFFIC_GAP_S` | `1.5` | Gap inside which a car counts as close. |
| `RTV_PITWALL_VEHICLE_ENGINEER_MODEL` | *(empty)* | Per-agent override; empty = the fast tier. |
| `RTV_PITWALL_SPOTTER_MODEL` | *(empty)* | " |
| `RTV_PITWALL_COACH_MODEL` | *(empty)* | " |
| `RTV_PITWALL_AGENTS_ONLY` | *(empty)* | Comma-separated subset to mount. Empty = all four. |

Absolute tyre bands are intentionally *not* shipped as live values in
`.env.example` — see "Tyre bands" above.

---

## Interfaces stage 4 consumes

Nothing about the radio contract changed, which is the point: stage 4's TTS layer
hangs off the same hook and now receives four agents' worth of traffic on it.

```python
from rtv.pitwall.agents import (
    AGENT_REGISTRY, STRATEGIST, VEHICLE_ENGINEER, SPOTTER, COACH, build_agents,
)
from rtv.pitwall.agents.vehicle_engineer import EngineerCall
from rtv.pitwall.agents.spotter import SpotterCall
from rtv.pitwall.agents.coach import CoachCall

orch.feed.subscribe_callback(fn)      # every agent's output, one channel
orch.feed.pump(session_time)          # deterministic, replay-stable pacing
orch.tool_extras                      # {"engine", "coaching", "repo"}

engine.setup_snapshot()               # CarSetup + car scalars, or a stated reason
engine.set_session_id(session_id)     # name a live session without resetting it

from rtv.racestate.detectors import (
    CornerRecurrence, corner_label,
    detect_tyre_condition, detect_car_health, detect_closing_traffic,
)
```

For a UI (stage 6): `RadioMessage.agent` is the channel label, `.subject` is the
supersede key, and `.data` is that agent's own contract — `EngineerCall`,
`SpotterCall` and `CoachCall` are all UI-ready payloads with no numeric fields to
mis-render except the spotter's `car_idx`.

---

## Deviations from the stage-3 spec

Each is a conservative choice made autonomously, per `CLAUDE.md`.

1. **One framework change: `ToolContext.extras`.** The spec's tool list includes
   `get_corner_detail` ("reuse coaching Layer-1") and `get_setup_snapshot` ("from
   session-info YAML"), and neither is reachable from a `RaceState` snapshot. So
   `ToolContext` gained one optional `extras` mapping, threaded through
   `AgentRuntime(tool_extras=...)` and `PitwallOrchestrator(tool_extras=...)`.
   The orchestrator always injects `engine`; `services.build_pitwall` adds
   `coaching` and `repo`. The alternative — putting the setup and the store handle
   on `RaceState` — would have widened *every* agent's citable fact set with setup
   numbers, which is precisely what the state slice exists to prevent.
2. **There is no `stint_end` event; the trigger is `pit_entry`.** The spec lists
   `stint_end` as a trigger. Pit entry *is* the end of a stint, and it already
   carries the lap, the stint number and the fuel remaining. A second event on the
   same edge would have been two names for one fact.
3. **Corner identity is a lap-distance bucket, not a corner number.** Naming "T7"
   needs a track map the engine does not have. `C00`–`C19` is honest and stable;
   the payload carries the lap-fraction range, and `get_corner_detail` maps it onto
   the Layer-1 label (`T1`) when a stored session is attached.
4. **Absolute tyre bands default to off.** The spec says "tyre temp/pressure
   drifting out of band". A default band for an unknown car would be a guess
   presented as a threshold — the exact failure this codebase refuses elsewhere.
   Drift and axle imbalance are relative, need no per-car knowledge and are on by
   default; absolute bands are one env var away.
5. **"Damage events" are `PlayerCarTowTime` plus the engine-warning bits.** The
   catalog contract in `racestate/channels.py` has no damage channels, so there was
   nothing else to detect. Reported under `car_health_warning` with
   `measure: "tow"`, so a real damage channel can be added later without a new
   event type.
6. **The coach shares `recurring_issue` with the engineer** rather than getting
   triggers of its own. Two agents on one event costs two calls, which is a real
   cost; but "your brake bias is wrong" and "brake ten metres earlier" are
   different sentences, and collapsing them would have meant one agent owning both
   the car and the driver. The 120 s vs 45 s cooldown split is how the cost is
   bounded.
7. **`lockup` / `wheelspin` payloads gained `wheel` and `lap_dist_pct`.** The
   existing `corner` key on those events means the *wheel* (LF/RF) and predates the
   track-corner labels, so it stays for compatibility; `wheel` is the unambiguous
   name, and `lap_dist_pct` is what the recurrence aggregator keys on.
8. **The scripted scenario gained two opt-in knobs, not new defaults.**
   `lockup_laps` (repeat the lockup on further laps) and `corner_pcts` (a speed dip
   deep enough for the Layer-1 corner detector to find — the default sinusoidal lap
   has no sufficiently prominent local minimum). Both default to empty, so the
   stage-1 ground-truth race is byte-identical and every stage-1/2 test is
   untouched.
9. **Session identity is attached in `services.on_state`, not in `LivePoller`.**
   `get_corner_detail`'s Layer-1 half needs the live session id and
   `get_setup_snapshot` needs the session-info document. Both are already written to
   the store by the poller *before* it announces the session, so services reads them
   back out. This closes stage-1 deviation 4 for the live path without touching v1
   ingest.
10. **`CornerRecurrence` is stateful, inside a module of pure functions.** The spec
    put the aggregator in `detectors.py` and that is where it belongs — it is a
    detector whose window is laps rather than frames — but it is the one object in
    that module that holds state. It still only *returns a payload*; constructing
    the `RaceEvent`, latching and cooldowns remain the engine's job, as for every
    other detector.

### A false positive found and fixed en route

The first traffic detector reported a car "closing at 3.4 s/s" on lap 1 of the
scripted race. Nobody had moved: the gap *basis* had switched from
`lap_length_speed` to `lap_time_pct` as soon as a lap time became known, and every
gap changed at once. `_check_traffic` now stores the basis alongside the sample and
discards a comparison across a change of it. Worth knowing for anything else that
differentiates a `RaceState` field over time.

---

## Verification (stage 3)

```powershell
pytest                                   # 309 passed (202 stage 1+2 + 107 new), fully offline
python scripts/smoke_pitwall_roles.py    # all four agents, real store, real Layer-1
python scripts/smoke_pitwall_agents.py   # stage 2, unchanged
python scripts/smoke_pitwall.py          # stage 1, unchanged
python scripts/smoke_offline.py          # v1 surface, unchanged
python evals/run_pitwall.py --dry-run    # 24 cases across 4 agents + their ground truth
ruff check src tests scripts evals
```

New test files: `tests/test_racestate_recurrence.py` (40),
`tests/test_pitwall_roles.py` (67). Green with and without `ANTHROPIC_API_KEY`
exported. No iRacing, no network. All 202 stage-1/2 tests still pass unmodified,
and the scripted race still produces the same 19 events it did in stage 1 — none of
the four new event types fires when nothing is wrong.

---
---

# Stage 4 — the voice

Stages 1–3 built something that decides what to say. This one makes it audible,
and the interesting half is not the speech engine — it is the **discipline**.
Four agents, one driver, one pair of ears: something has to decide what gets
said, what waits, what is replaced, and what is thrown away unheard.

That decision is made **twice on purpose**, once on each side of the socket:

| | `RadioFeed` (backend) | `RadioAudioManager` (browser) |
|---|---|---|
| unit | a message | an *utterance in progress* |
| pre-emption | critical sorts first in the queue | critical **cancels mid-sentence** |
| supersede | same `(agent, subject)`, while queued | same, on the browser's own queue |
| ageing | none | info is dropped after 15 s unheard |
| mutes | — | per agent + master |

The backend cannot do the browser's half: it has no idea a sentence is
*currently being spoken*, only that a message was emitted. The browser cannot do
the backend's half: it never sees the messages an agent superseded before they
aired. Two queues, two jobs — the second is not a re-implementation of the first.

---

## Module map (added)

| Module | Role |
|--------|------|
| `src/rtv/pitwall/tts.py` | Provider interface, registry, `NullProvider`, the REST reference impl, `RadioTTS` (clip cache + URLs). |
| `frontend/js/audio-manager.js` | **Pure** queue/priority/supersede/staleness/mute logic. No `speechSynthesis`, no DOM, no timers. |
| `frontend/js/speech.js` | The noisy half: Web Speech, backend audio, the WebAudio radio click, push-to-talk. |
| `frontend/js/radio-voices.js` | The offline copy of the role voice table. |
| `frontend/js/pitwall-radio.js` | Wiring: socket → manager, controls, PTT. |
| `frontend/radio.html` | The listening page. |
| `frontend/audio-test.html` | The in-page self-test (see *Testing the rules*). |
| `frontend/js/audio-manager.test.js` | The 20 assertions both the page and node run. |

## The default engine is the browser

`speechSynthesis` costs nothing, needs no key, works with the network unplugged,
and is already installed. It is not the fallback — it is **the** engine. The
backend path exists for deployments that want a particular voice, and every
failure in it lands back here rather than in silence.

Per role, the browser gets a voice, a rate and a pitch:

| agent | voice hints | rate | pitch |
|---|---|---|---|
| strategist | George / Daniel / *en-GB* | 0.98 | 0.85 |
| vehicle_engineer | Ryan / Arthur / *en-GB* | 1.02 | 1.00 |
| spotter | Guy / Christopher / *en-US* | 1.22 | 1.18 |
| coach | Sonia / Libby / *en-GB* | 0.95 | 1.10 |

Named voices differ by browser and OS, so the hint list is best-effort and the
resolver falls back to spreading roles deterministically across whatever voices
exist. **Rate and pitch are the guarantee**: every engine supports them, so two
roles that land on the same underlying voice are still told apart by ear. The
table lives in `rtv.pitwall.tts.DEFAULT_VOICES`, is served at
`GET /api/v1/pitwall/tts`, and is mirrored in `radio-voices.js` for when that
call fails — `tests/test_pitwall_audio_frontend.py` parses the JS literal and
asserts the two are identical, so they cannot drift.

## The premium path, and why it is lazy

```python
provider = build_tts_provider(TTSConfig(provider="rest", url=..., api_key=...))
service  = RadioTTS(provider, cache_size=64)
message.audio_url = service.url_for(message)     # a URL, not audio
clip = await service.clip_for(message)           # bytes, on demand, cached
```

`PitwallOrchestrator.publish()` stamps every message with an `audio_url` the
moment it is published, but **synthesises nothing**. The bytes are produced when
a browser fetches `GET /api/v1/pitwall/audio/{message_id}`. A message that gets
superseded before it airs therefore costs zero vendor calls, which is the common
case for a chatty strategist under a safety car.

`message_id` is a **content-derived** SHA-1 prefix of
`(agent, event key, tick, spoken_text)`. Not random, for the same reason the
event log has no wall-clock field: a replayed race must produce the same ids, so
a cached clip stays valid and the UI's de-duplication stays meaningful across a
re-run. It deliberately excludes `seq` — the sequence number is assigned when the
feed accepts the message, i.e. *after* the URL is stamped, and an id that changed
between those two moments would produce a URL resolving to nothing. (It did, for
about ten minutes; the test named
`test_a_published_message_carries_its_audio_url_when_a_provider_can_speak` is
that bug's tombstone.)

### The registry is the extension point

Vendors disagree about request bodies in ways no single config schema survives
(OpenAI puts the voice in the JSON, ElevenLabs puts it in the path). So a new
vendor is a factory registered under a name, not a branch:

```python
register_tts_provider("elevenlabs", lambda config: MyProvider(config))
# RTV_PITWALL_TTS=elevenlabs
```

`RestTTSProvider` is the shipped reference — an OpenAI-compatible
`/audio/speech` body with a bearer token — and doubles as the worked example. Its
HTTP call goes through an injected `Transport`, which is how the whole REST path
is asserted offline: the tests check the exact body it *would* send without
sending one.

**Unavailable is a first-class state.** No provider configured, no URL, no key,
no `httpx`, unknown name, factory raised — every one of those yields a
`NullProvider` carrying the reason. `build_tts_provider` never raises. The
frontend sees no `audio_url`, uses Web Speech, and the operator sees *why* on
`/api/v1/pitwall/tts` instead of a silent radio.

---

## Audio discipline, in detail

`RadioAudioManager` (`frontend/js/audio-manager.js`) is constructed with a
speaker and a clock and nothing else:

```js
const manager = new RadioAudioManager({ speaker, now: () => Date.now() });
manager.push(message);            // -> {action: 'speaking'|'queued'|'interrupted'|'dropped', reason}
manager.finished(message_id);     // the host reports an utterance ended
manager.tick();                   // drop stale info, start anything waiting
manager.setAgentMuted('coach', true);
manager.setMasterMuted(true);
manager.setVolume(0.6);
manager.snapshot();               // {speaking, queue, volume, masterMuted, muted, counters}
```

The rules, and the judgement in each:

- **Single speaker.** One utterance at a time, always.
- **Critical interrupts** — `speaker.cancel()` then straight into the critical.
  The cut-off message is **discarded, not requeued**: re-reading half a stale
  advisory after a "car left!" is worse radio than dropping it. It is kept on
  `lastInterrupted` so the UI can show what was lost.
- **Critical never interrupts critical.** Two urgent calls are both urgent; the
  second waits. This is the browser twin of the backend's "a critical message is
  never superseded and never dropped".
- **Advisory queues** by priority then arrival, and **never ages out**. A
  decision is still a decision ten minutes later.
- **Info is opportunistic**: spoken only if the channel is idle, and dropped once
  it has waited **15 s**. Stale context read out three corners later is worse
  than nothing.
- **Supersede** on `(agent, subject)` while queued, mirroring `RadioFeed`.
- **Mutes drop at the door.** A muted agent's calls are never queued, so unmuting
  does not unleash a backlog of history the driver has already driven past.
- **`speak: false` is never spoken** — that is the driver's own transcript.
- **De-duplication by `message_id`**, so a socket reconnect that replays the log
  does not say everything twice.

A **radio click** (two clipped WebAudio blips, no asset files) plays ahead of a
critical call only. The manager decides — it passes `{cue: true}` — so the rule
is testable and the noise is not.

**Volume and mutes live in the URL** (`?volume=0.6&master=1&muted=coach,spotter`)
and in memory. No `localStorage`, no `sessionStorage`, anywhere; a test greps the
whole frontend for them.

### Push-to-talk

`PushToTalk` (`speech.js`) wraps `SpeechRecognition` / `webkitSpeechRecognition`,
feature-detected: where it is absent the button is **hidden**, not disabled and
not broken. A transcript is POSTed to `/api/v1/pitwall/driver-message`, which
puts it on the channel as `agent: "driver"`, `speak: false`.

Driver messages are **emitted, not queued**: the driver has already used the
airtime, so making the transcript wait behind an advisory would misrepresent when
it happened. They join the radio ring buffer, which means agents see them in
their recent-radio context for free.

---

## API surface (added; stages 1–3 and all of v1 untouched)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/pitwall/tts` | Provider, availability + reason, the role voice table, `engine: backend` or `webspeech` |
| GET | `/api/v1/pitwall/audio/{message_id}` | Synthesised audio bytes for one message |
| POST | `/api/v1/pitwall/driver-message` | `{text, source}` — push-to-talk onto the channel |

`GET /pitwall/tts` answers whether or not the agent layer is mounted, because a
browser needs the voice table before it needs a radio message.

`GET /pitwall/audio/{id}` is deliberately **not** a "speak this text" endpoint.
The only thing that can be synthesised is something an agent actually put on the
radio, looked up by its content-derived id — there is no path from arbitrary text
to the driver's ear, which keeps the grounding discipline intact all the way to
the speaker.

| Status | Means | The frontend does |
|---|---|---|
| 200 | audio bytes (`Cache-Control: immutable`) | plays them |
| 404 | no such message in the ring buffer | speaks it itself |
| 502 | the vendor failed | speaks it itself |
| 503 | no backend provider (the default) | speaks it itself |

Every non-200 has the same consequence — the browser's voice — so a flaky vendor
costs a change of voice, never a missed call.

### `RadioMessage` gained three fields

```jsonc
{"type":"radio","message":{
  "message_id":"9bda500fde3f6475",     // content-derived; audio key + UI de-dup
  "speak": true,                        // false = show it, never say it
  "audio_url": null,                    // null = use the browser's Web Speech API
  "agent":"strategist", "priority":"critical", "spoken_text":"...", ...
}}
```

Everything else about the radio contract is unchanged, and `/pitwall/status`
gained a `tts` block (`null` when no `RadioTTS` is attached).

### Serving the frontend

`create_app()` now mounts `frontend/` at `/` — vanilla JS, no build step, no
frameworks, no CDN. It is mounted **after** every router, and through a
`FrontendFiles` subclass that answers **404 rather than 405** for a non-GET:
a catch-all mount is what an unmatched request finally reaches, and Starlette's
default would have turned every unknown `POST /api/v1/...` into a method error.
Mounting a frontend must not change what the API says about paths it does not
have (`test_pitwall_routes_absent_when_disabled` caught exactly that).

| Page | What it is |
|---|---|
| `/` | placeholder index; the real pitwall UI is stage 6 |
| `/radio.html` | the listening page: live radio, mutes, volume, PTT, demo |
| `/audio-test.html` | the audio-discipline self-test, in-page |

---

## Testing the rules

The queue logic is the part that can be subtly wrong in a way you only discover
mid-race, so it is pure and it is asserted — 20 cases against a fake speaker and
an injected clock, in `frontend/js/audio-manager.test.js`:

```powershell
node frontend/js/run-audio-tests.mjs        # headless; exit code is the result
start http://127.0.0.1:8000/audio-test.html # same module, rendered in-page
```

The repo has **no JS toolchain and did not gain one**: the runner is node plus ES
modules, no `package.json`, no dependencies. `pytest` runs it when a `node`
binary happens to be on PATH and skips it otherwise, so the suite's offline
guarantee is unchanged either way. The page also publishes
`window.__RTV_AUDIO_TEST__` for any later browser automation, and puts
`PASS`/`FAIL` in `document.title`.

What the JS cases pin down, beyond the happy path: a critical cuts an advisory
off and the advisory is *not* requeued; a critical does not cut off a critical;
a queued critical is never superseded; info ages out at 15 s but an advisory
never does; unmuting replays no backlog; a replayed `message_id` is spoken once;
a late `onend` from a cancelled utterance cannot steal the channel.

Python covers the rest: `tests/test_pitwall_tts.py` (36) for the registry and the
providers, `tests/test_pitwall_audio_api.py` (19) for the HTTP contract,
`tests/test_pitwall_audio_frontend.py` (29) for the files being served, the two
voice tables agreeing, and the house rules (no storage APIs, no build step, the
audio manager importing nothing from a browser).

---

## Configuration added

| Env var | Default | Meaning |
|---|---|---|
| `RTV_PITWALL_TTS` | *(empty)* | Provider name. Empty = browser Web Speech only. |
| `RTV_PITWALL_TTS_URL` | *(empty)* | Speech endpoint for `rest`/`openai`. |
| `RTV_PITWALL_TTS_API_KEY` | *(empty)* | Bearer token. Missing means the provider reports unavailable. |
| `RTV_PITWALL_TTS_MODEL` | *(empty)* | Vendor model id. |
| `RTV_PITWALL_TTS_FORMAT` | `mp3` | Requested audio format. |
| `RTV_PITWALL_TTS_MEDIA_TYPE` | `audio/mpeg` | Content-Type served for it. |
| `RTV_PITWALL_TTS_VOICES` | *(empty)* | `strategist=onyx,spotter=fable` overrides. |
| `RTV_PITWALL_TTS_TIMEOUT_S` | `8` | Timeout on one synthesis request. |
| `RTV_PITWALL_TTS_CACHE` | `64` | Clips kept in memory, keyed by `message_id`. |

---

## Hearing it

```powershell
uvicorn rtv.main:app                      # then open http://127.0.0.1:8000/radio.html
```

Press **Enable audio** once (browsers make no sound before a gesture), then:

- **Play synthetic scenario** — six scripted calls with a critical spotter shout
  landing mid-sentence and a superseded pit call. Needs no key and no agents.
- **Replay the scripted race** — drives `POST /api/v1/replay/start` with
  `session_id: "scenario"`. With `RTV_PITWALL_AGENTS=true` and a key, the radio
  is live agent output. Without them the page **says so** and plays the scripted
  calls instead, labelled as scripted — a canned line presented as an agent's
  call would be exactly the ungrounded claim this codebase refuses everywhere
  else.

Mute buttons, the volume slider and the master mute all take effect on the next
utterance boundary (master mute and muting the speaking agent stop the current
one immediately).

---

## Interfaces stage 5 consumes

```python
from rtv.pitwall.tts import (
    RadioTTS, TTSConfig, TTSProvider, VoiceProfile, NullProvider, RestTTSProvider,
    build_radio_tts, build_tts_provider, register_tts_provider, tts_provider_names,
    DEFAULT_VOICES, AUDIO_URL_PREFIX, TTSError, TTSUnavailable,
)

services.tts                          # RadioTTS, always present (usually NullProvider)
orch.publish(message)                 # stamp audio_url + queue -- use this, not feed.publish
orch.driver_message(text, source=...) # push-to-talk onto the channel
orch.tts                              # RadioTTS | None
feed.find(message_id)                 # history then pending
message.message_id                    # content-derived, replay-stable
message.speak / message.audio_url
```

```js
import { RadioAudioManager } from '/js/audio-manager.js';   // pure, testable
import { RadioSpeaker, PushToTalk } from '/js/speech.js';   // browser adapters
```

A director agent (stage 5) that wants to hold the channel should publish through
`orch.publish` like everything else; the browser's manager will apply the same
discipline to it with no frontend change. A new agent gets a voice automatically
(`FALLBACK_VOICE`) and a distinct one by adding a row to `DEFAULT_VOICES` **and**
`radio-voices.js` — the test that compares them will insist.

---

## Deviations from the stage-4 spec

Each is a conservative choice made autonomously, per `CLAUDE.md`.

1. **`POST /api/v1/pitwall/driver-message` did not exist; this stage added it.**
   The spec calls it "existing". It was not in stage 2 or 3, so it is implemented
   here as the smallest honest thing: the transcript joins the radio log as
   `agent: "driver"`, `speak: false`, emitted rather than queued. It is
   deliberately **not** routed to the agents — waking a model on a driver
   utterance is a trigger design decision, and inventing one at the end of a TTS
   stage would have been the wrong place to make it.
2. **There was no `frontend/` directory to build in.** `CLAUDE.md` describes one
   and the v1 analysis UI is built separately, outside this repo. So this stage
   created it, mounted it at `/`, and kept it to what stage 4 needs plus a
   placeholder index — the real pitwall UI is stage 6, and this should be
   replaced rather than extended.
3. **The static mount answers 404, not 405, for a non-GET.** See "Serving the
   frontend": a catch-all mount changes what the API says about unknown paths,
   and an existing stage-1 test was right to object.
4. **The interrupted message is discarded, not requeued.** The spec says
   "critical interrupts current playback (cancel + play)" and is silent on the
   victim. Resuming a half-spoken advisory after an emergency call is worse radio
   than losing it; it is preserved on `lastInterrupted` for the UI.
5. **Master mute silences criticals too.** A mute the driver set that a critical
   could override is not a mute. The control for "only the important ones" is the
   per-agent toggles.
6. **Only one cloud provider shape ships.** The spec asks for "one reference
   implementation stubbed for a cloud TTS (e.g. OpenAI/ElevenLabs-style REST)".
   An ElevenLabs body written from memory would be a guess presented as an
   integration; `RestTTSProvider` implements the OpenAI-compatible shape it can
   be honest about, and the registry — with a test proving a third-party provider
   is one registration — is the seam for the rest.
7. **Synthesis is lazy and cached, not eager on publish.** The spec says
   "backend synthesises to audio bytes served at GET ...". Synthesising at publish
   time would pay for every superseded message and put a vendor round-trip on the
   agent's critical path. The URL is stamped at publish; the bytes are made on
   first fetch and cached by `message_id`.
8. **`message_id` excludes `seq`.** See "The premium path" — including it made
   the stamped URL unresolvable.
9. **A backend-audio failure falls back to Web Speech rather than being retried.**
   Same reasoning as the 502 mapping: a different voice is a much smaller failure
   than a missed radio call.
10. **The replay button plays the scripted radio when no agents are mounted**, and
    labels it as scripted in the UI. Without this the definition-of-done demo is
    silent on a machine with no API key, which is every machine this suite runs
    on; without the label it would be a fake pit call presented as a real one.
11. **`pytest` runs the JS suite only when `node` is on PATH.** Making node a hard
    dependency of the Python suite would break the offline guarantee on a machine
    that has no reason to have it. The same assertions are always reachable in a
    browser via `audio-test.html`.

### A Chrome quirk worth recording

`speechSynthesis.cancel()` followed by `speak()` in the same tick silently drops
the new utterance in Chrome — and that is *exactly* the interrupt path a critical
call takes, so the one thing this stage exists to demonstrate would have failed
there. `_playWebSpeech` schedules the new utterance one frame (40 ms) after the
cancel, guarded so a second interrupt inside that frame still wins. There is also
a 5 s pause/resume keepalive, because Chrome stops speaking after ~15 s.

---

## Verification (stage 4)

```powershell
pytest                                   # 393 passed (309 stage 1-3 + 84 new), fully offline
node frontend/js/run-audio-tests.mjs     # 20/20 audio-discipline cases
python scripts/smoke_pitwall_voice.py    # the voice path end to end, fake vendor transport
python scripts/smoke_pitwall_roles.py    # stage 3, unchanged
python scripts/smoke_pitwall_agents.py   # stage 2, unchanged
python scripts/smoke_pitwall.py          # stage 1, unchanged
python scripts/smoke_offline.py          # v1 surface, unchanged
python evals/run_pitwall.py --dry-run    # 24 cases across 4 agents, unchanged
ruff check src tests scripts evals
```

New test files: `tests/test_pitwall_tts.py` (36), `tests/test_pitwall_audio_api.py`
(19), `tests/test_pitwall_audio_frontend.py` (29; one case skips itself when node
is absent). Green with and without `ANTHROPIC_API_KEY` exported. No iRacing, no
network, no API key. All 309 stage-1/2/3 tests still pass unmodified, and the
scripted race still produces the same 19 events and the same radio log it did
before — the message ids are new metadata on it, not a change to it.

---
---

# Stage 5 — the director seam, and a live path that survives a race

Two jobs, and they are the same job seen from opposite ends. The **race
director** is about making things go wrong on purpose; the **hardening** is about
what happens when they go wrong by accident. Stage 5 closes the backend by
building the first as an interface and the second as a set of asserted
properties.

---

## The race director — the seam (stage 5) and the scheduler (stage 10)

> **Status: built, eval path only.** The app runs `NoopDirector`, which injects
> nothing. Stage 10 added `ScriptedDirector`, the scheduler that walks a script, and
> it is built only by `evals/run_director.py`: a director makes things happen that
> did not happen, so it never runs in the app, never writes to `data/sessions` and
> nothing it injects is shown in the live view or the review as if it happened. The
> current-architecture page has the firing rules, the overlay and the runner.

A race director injects things that did not happen: a full-course yellow on
lap 7, rain arriving at half distance, a mandatory stop nobody asked for. It is
how you rehearse a strategist against a race that never runs the same way twice,
and how a demo shows a safety car without waiting for one.

What stage 5 was asked for, and what it therefore built, is the **seam** — and
the seam is the interesting half. The scheduler is a loop over conditions;
whether an injected event is a first-class citizen of the rest of the system is
an architectural question, and stage 5 answered it with a test. That is why stage
10 was cheap: `ScriptedDirector` is one file that calls `injected_event()` and
hands the result to `poll_director()`, and the same equivalence tests pass against
it unchanged. The only new idea it needed is the overlay: the telemetry is never
rewritten, so the agents are told separately which scripted conditions are in force.

### Module map

| Module | Role |
|--------|------|
| `director/models.py` | `ScenarioScript`, `ScriptedInjection`, `InjectionTrigger`, `InjectionKind`. Pydantic, validated, JSON-loadable. |
| `director/engine.py` | The `DirectorEngine` protocol, `NoopDirector`, `injected_event()`, `is_injected()`. |
| `director/scheduler.py` | `ScriptedDirector` (stage 10): the walker and the `injected` overlay. |
| `director/overlay.py` | Reading the overlay: `injected_caution`, `pending_mandatory_stop`, `is_rain`. |
| `evals/director_rehearsal.py`, `evals/run_director.py` | The rehearsal runner (stage 10), eval path only. |
| `docs/director_scenario.example.json` | A worked five-entry script: FCY → restart → rain → mandatory stop → hazard. |

### A script is a list of conditional injections

```jsonc
{
  "id": "fcy_lap_3",
  "kind": "flag",                       // flag | weather | regulation | hazard | custom
  "event_type": "flag_change",          // a real member of the event catalog
  "severity": "critical",
  "when": { "at_lap": 3, "at_lap_dist_pct": 0.30 },
  "payload": { "from": "green", "to": "yellow", "active": ["yellow", "caution"] },
  "note": "why this entry exists"
}
```

`InjectionTrigger` conditions are **conjunctive**: `at_session_time`, `at_lap`,
`at_lap_dist_pct`, `after` + `delay_s`, `while_flag`, `once`. Combining them says
things a clock cannot — `at_lap: 7` plus `at_lap_dist_pct: 0.30` is "coming
through the first sector on lap 7", and `after: "fcy_lap_3"` plus `delay_s: 90`
is "ninety seconds after the yellow", whenever that turned out to be. Delays are
**session** seconds, so a 4× replay rehearses the same race.

Three things are rejected at load rather than at runtime, because a scenario that
fails three laps into a demo has already failed:

- a trigger with **no condition at all** (a silent default of "now" would be a guess);
- `delay_s` with no `after` to be delayed from;
- duplicate ids, self-references, and an `after` naming an entry not in the script.

`RTV_PITWALL_DIRECTOR_SCRIPT` loads and validates a script at boot and binds it to
the `NoopDirector`. In the app that is a **linter**, not a runner, and stays one: the
runner is `evals/run_director.py`.

### The seam, and what "indistinguishable" means

```python
PitwallOrchestrator(engine, provider, agents, director=my_director)

orch.poll_director()          # -> [RaceEvent], already published on engine.bus
```

The pump calls `poll_director()` on **every** pass, including the idle ones — a
scripted yellow has to be able to land in a quiet minute, not only in the wake of
a detector event. Whatever comes back is published onto `engine.bus`: the same
bus, the same ring buffer, the same `/ws/pitwall` frame, the same subscriber
fan-out, and back around into the orchestrator's *own* subscription. No consumer
anywhere knows a director exists.

```
                    detectors ──┐
                                ├─► engine.bus ─► ring buffer ─► /ws/pitwall
   director.poll(state) ────────┘        └─► orchestrator ─► agents ─► radio
```

Injected events are indistinguishable in every way that changes behaviour: same
`EventType`, same severity, same `key`, same routing, same cooldowns, same agent
output. They are **not** anonymous in the log. Three additive payload keys —
`injected`, `director_kind`, `director_id` — record provenance, and a script
cannot overwrite them (`injected_event` stamps them last).

That is a deliberate reading of "indistinguishable". This codebase refuses to let
an agent assert a number it cannot trace; letting the event log confuse "the sim
threw a yellow" with "we made one up" would be the same failure one level down.
Behaviour is identical, provenance is honest, and the two are not in tension.

### The proof

`tests/test_director.py::test_a_scripted_fcy_produces_the_same_downstream_behaviour`
takes the real `flag_change:yellow` the scripted race throws on lap 3, builds the
director's version from `docs/director_scenario.example.json`, and runs both
through two fresh orchestrators over the *same* race state:

| Compared | Result |
|---|---|
| `event_type`, `key`, `severity` | identical |
| payload keys | identical, plus exactly the three provenance keys |
| agents woken (`dispatch`) | identical — strategist **and** spotter |
| radio: agent, priority, subject, spoken text | identical, call for call |

A second test runs the real pump: director → `engine.bus` → the orchestrator's
subscription → an agent → the radio feed, with nothing in between told what
happened. If any layer had grown a "was this real?" branch, both would fail.

The one-shot director those tests use is **twelve lines** and lives in the test
file, not in `rtv.director`. That is the argument stage 5 is making: the
implementation is small, and the seam it plugs into is the part worth getting
right first. Stage 10 adds twins of those tests that run against `ScriptedDirector`,
and the originals still pass unmodified.

---

## Hardening the live path

Eight failure modes. Every one of them is **silent** by default — the pitwall
does not crash, it just quietly stops being right — which is exactly why each is
now asserted rather than assumed.

### 1. A frame that raises must cost a frame, not the pitwall

`RaceStateEngine(resilient=True)` catches a per-frame fault, counts it in
`state.metrics.dropped_frames`, and carries on. `services.build_services` sets it;
the constructor default is **off**.

That split is the point. In a live race, 16 ms of missing state is a far smaller
failure than a pitwall that stops at 250 km/h. In a test or a replay it is the
opposite: an engine that silently emitted no events would pass every assertion
about what it does *not* emit, which is a large part of this suite. So the live
path is resilient and the test path is strict, and
`test_resilience_does_not_change_the_event_log_when_nothing_fails` pins that the
two agree when nothing is broken.

Faults are logged **at most once a second**. Whatever broke on this frame will
break on the next fifty-nine, and sixty stack traces a second turns a bug into an
outage of its own. The count survives in `dropped_frames` after the log has moved
on.

### 2. `engine.health()` — "is anything still watching?"

`/api/v1/racestate` says what the race is doing. It cannot say whether anyone is
still looking at it: a `RaceState` full of `None` reads identically whether the
session has not started, the catalog carries none of the channels we wanted, or
frames stopped arriving four minutes ago. Those need three different responses.

```jsonc
{"bound": true, "stale": false, "stale_reason": null,
 "source": "replay", "frames": 3360, "events": 19,
 "faults": 0, "last_fault": null, "seconds_since_frame": 0.4,
 "avg_update_ms": 0.03, "capabilities": {...}, "missing_channels": 0,
 "bus": {"published": 19, "subscribers": 2, "dropped": 0}}
```

`seconds_since_frame` is `null` before the first frame — "never" and "just now"
are different answers, and zero would be a lie about one of them. It is the only
wall-clock number in the package, it is monotonic, and it never enters a
`RaceEvent`, so the replay log stays byte-reproducible.

### 3. iRacing disconnecting mid-session

This is the subtlest one in the list. **The engine has no clock of its own** — it
only moves when a frame moves it — so a disconnect does not corrupt the race
state. It *freezes* it, perfectly, which is worse: "P4, 2.1 s behind" reads
identically whether it is current or four minutes old.

So `RaceState` gained two fields, `stale` and `stale_reason`, and the connection
transition is wired in `services.on_state` — the same callback that already
attaches the session id, so `LivePoller` (v1 ingest) stays untouched:

| | on disconnect | on reconnect |
|---|---|---|
| engine | `mark_stale(reason)` — state frozen and **flagged**, numbers kept | `mark_live()` — flag cleared, detector window dropped |
| agents | `orch.suspend(reason)` — dispatch stops | `orch.resume()` — cooldowns cleared |
| radio | one info notice, "Telemetry lost, pitwall standing by" | "Telemetry back, pitwall live" |

Three judgements worth recording:

- **The last known numbers are kept, not cleared.** They are still the best
  picture anyone has of the race; what was missing was the *label*.
- **The detector window is dropped on resume.** Every frame-window detector —
  lock-up, wheelspin, off-track, pit and lap edges — works on consecutive
  samples, and the frame before a four-minute gap is not the predecessor of the
  frame after it. Without this, a reconnect manufactures a lap completion, a pit
  transition or a wheel-lock out of a discontinuity nobody drove. The test
  replays a whole race with a gap in the middle and asserts the event log is
  **identical** to the uninterrupted one.
- **Suspension is a second flag, not the kill switch.** Telemetry dropping out
  and an operator saying "stop" are different facts; folding them together would
  mean a reconnect quietly re-enabling a layer somebody had turned off on
  purpose. `resume()` also clears the trigger cooldowns — session time did not
  advance while we were gone, but the race did.

`dispatch()` additionally refuses any event whose state is `stale`, so an agent
can never reason over a race that has stopped, whatever else went wrong.

### 4. A supervised task that dies must be restarted, and *visibly*

The failure mode of an unsupervised `asyncio` task is the worst one available: it
stops, nothing crashes, and the radio simply goes quiet. `PitwallOrchestrator`
now runs its pump, its workers and the radio feed under `_supervise()`, which
restarts on any exception with a one-second floor and counts the restarts.

`status()` reports `restarts` and a `healthy` flag alongside `running`, because a
layer that has restarted its pump forty times *is* running and is *not* healthy,
and a UI needs to be able to say the second thing.

### 5. A model call that fails: retry once, then say so out loud

One retry, with a backoff, and then the agent **announces its own failure**:

```
call → 502 → wait RTV_PITWALL_RETRY_BACKOFF_S → call → 502
     → radio: "Pitwall AI degraded - no strategist calls for now." (info)
```

Deliberately *one* retry, not an exponential ladder: a pit call that lands three
corners late is worse than no pit call, so the policy that fits a race is "cover
a dropped connection, then get out of the way". The message history is rebuilt
from the caller's list on the retry, because a half-finished tool exchange is not
a prefix a second attempt can safely continue from.

Saying it out loud matters more than it looks. The failure mode of a silent agent
layer is a driver who believes nobody has anything to tell them. The notice is
`info` priority, carries `{"degraded": true, "agent": ...}` for the UI, and fires
**once per episode** — re-armed by a success, because a chatty failure notice
would be its own outage. Everything deterministic keeps running underneath it:
the engine, the event log, the ring buffer, `/ws/pitwall`, the other three
agents. That is the whole argument for doing the arithmetic upstream.

### 6. A dead provider must not bill for a whole race

`AgentRuntime.invoke` swallows its own errors and returns `None` — right for one
bad call, wrong for a hundred. A revoked key, a retired model id or a vendor
outage would otherwise cost one doomed request per race event until the chequered
flag.

So each agent carries a **circuit breaker**: `RTV_PITWALL_AGENT_FAILURE_LIMIT`
(default 3) consecutive failures take it off the air, with the reason recorded on
`/api/v1/pitwall/status`:

```jsonc
{"name": "strategist", "enabled": false, "consecutive_failures": 3,
 "disabled_reason": "Disabled automatically after 3 consecutive failed invocations.
   Re-enable with POST /api/v1/pitwall/agents/strategist/enabled once the cause is fixed."}
```

Any success re-arms it — this is a cost guard, not a quarantine — and an explicit
re-enable clears it, because a latched breaker would disable the agent again on
its very next failure. `0` turns it off.

### 7. The session cost guard

The breaker catches an agent that is *failing*. This catches one that is
*working* and simply costs more than anyone intended — a trigger storm, a
misconfigured cooldown, a scenario nobody foresaw.

`RTV_PITWALL_MAX_CALLS_PER_SESSION` (default **400**) is a hard ceiling on
billable model calls across every agent in one session. When it trips, `dispatch`
refuses everything, the channel carries one info notice, and the deterministic
layers carry on. `orch.reset()` — between sessions or replays — starts a fresh
budget without discarding the lifetime counters the status endpoint reports.

It is counted in **model calls, not wake-ups**: one wake-up is a tool round trip
or two plus the answer, so the wake-up count would systematically understate the
bill. `AgentRuntime` times and counts every `provider.complete()`, which is also
what puts per-agent call counts and latencies on `/pitwall/status`:

```jsonc
{"calls_used": 24, "calls_remaining": 376, "max_calls_per_session": 400,
 "budget_exhausted": false,
 "agents": [{"name": "strategist", "invocations": 6, "llm_calls": 12,
             "retries": 0, "last_latency_ms": 812.4, "avg_latency_ms": 774.1}]}
```

The default is deliberately generous — it is a runaway guard, not a budget. The
scripted race spends 24 calls; a measured race hour is a low-tens number (the
arithmetic is in `README.md`).

### 8. Two frame sources into one engine

`POST /api/v1/replay/start` now answers **409** while the live poller is running.
Interleaving a replayed race with a live one does not produce a degraded race
state, it produces a nonsensical one — lap counters, fuel and gaps alternating
between two different races. Refusing is the only honest answer.

---

## API surface (added; stages 1–4 and all of v1 untouched)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/pitwall/health` | Engine, live poller, replay, agent layer and director in one payload |

Plus a documented **60-second demo path** (`python scripts/demo_pitwall.py`):
one command starts the server, opens `/radio.html`, waits for the audio gesture,
and replays the scripted race at 4×. See `README.md`.

```jsonc
{"ok": true, "pitwall": true,
 "engine": { /* engine.health() */ },
 "live":   {"running": false, "state": "disconnected", "session_id": null},
 "replay": {"running": false, "finished": true, "frames": 3360},
 "agents": {"mounted": true, "enabled": true, "running": true, "healthy": true,
            "restarts": {}, "degraded": [], "director": {"status": "planned"}}}
```

It is the **only** pitwall route that never returns an error: `/pitwall/radio`
correctly 503s when the agent layer is not mounted, but "is the deterministic
path alive?" is still a question with an answer, and an operator mid-race should
not have to distinguish a 503 that means "off" from one that means "broken".

`ok` is the one judgement in the payload: false when something that should be
producing frames is not, or when the agent layer has restarted a task or tripped
a breaker. Everything else is reported for the caller to weigh.

`/pitwall/status` gained `healthy`, `restarts`, `injected`, `failure_limit`,
`director`, and per-agent `consecutive_failures` / `disabled_reason`.

---

## Configuration added

| Env var | Default | Meaning |
|---|---|---|
| `RTV_PITWALL_AGENT_FAILURE_LIMIT` | `3` | Consecutive failures before an agent is taken off the air. `0` disables the breaker. |
| `RTV_PITWALL_MAX_CALLS_PER_SESSION` | `400` | Hard ceiling on billable model calls in one session. `0` = unlimited. |
| `RTV_PITWALL_RETRY_BACKOFF_S` | `0.5` | Pause before the single retry of a failed model call. |
| `RTV_PITWALL_DIRECTOR_SCRIPT` | *(empty)* | Path to a `ScenarioScript` JSON. Loaded and **validated** at boot, then bound to `NoopDirector` — which injects nothing. |

---

## Interfaces stage 6 (the pitwall UI) consumes

```python
from rtv.director import (
    ScenarioScript, ScriptedInjection, InjectionTrigger, InjectionKind,
    DirectorEngine, NoopDirector, injected_event, is_injected,
    INJECTED_KEY, DIRECTOR_KIND_KEY, DIRECTOR_ID_KEY,
)

engine.health()                  # the operator payload behind /pitwall/health
engine.resilient                 # True on the live path, False in tests/replay
engine.mark_stale(reason)        # telemetry stopped: freeze *and flag*
engine.mark_live()               # frames back: clear the flag and the window
engine.stale                     # bool; also RaceState.stale / .stale_reason

orch.director                    # DirectorEngine, never None
orch.poll_director(state=None)   # -> [RaceEvent], already on engine.bus
orch.suspend(reason) / .resume() # connection lifecycle (not the kill switch)
orch.suspended                   # bool, distinct from .enabled
orch.calls_used / .calls_remaining / .budget_exhausted
orch.healthy()                   # no restarts, no breakers, budget intact
orch.status()["restarts"]        # {"pump": 2, "agent:spotter": 1}
```

For the UI: `GET /api/v1/pitwall/health` is one poll for the whole status bar,
and `is_injected(event)` is how a scripted yellow gets a "DIRECTOR" badge once a
director exists to produce one.

To implement the director, write a class satisfying `DirectorEngine`, build its
events with `injected_event()`, and pass it as `PitwallOrchestrator(director=...)`
— `services.build_director` is the one place that needs to change to select it
from configuration. Nothing else in the system does.

---

## Deviations from the stage-5 spec

Each is a conservative choice made autonomously, per `CLAUDE.md`.

1. **Injected events carry three provenance keys** (`injected`, `director_kind`,
   `director_id`) rather than being byte-anonymous. The spec says
   "indistinguishable from detector events"; that is implemented for everything
   that changes behaviour, and asserted. But an event log that could not tell a
   scripted safety car from a real one would be the same class of failure as an
   agent quoting a number it cannot trace. The keys are additive, so a consumer
   that does not care never sees them.
2. **Two director-only event types were added** — `weather_change` and
   `regulation_change`. The spec names weather and forced-pit regulation as
   things a script must be able to inject, and no detector-backed event could
   express either (the channel contract has nothing that announces rain which has
   not arrived). Adding them to `EventType` is what lets an injection stay an
   *ordinary* event, which is the whole argument for the seam. They are marked as
   director-only in the catalog and no agent trigger references them.
3. **The director hangs off the orchestrator, not the engine.** The spec says
   "orchestrator accepts an optional director", so that is where it is — with the
   consequence that a director does not run when `RTV_PITWALL_AGENTS` is false.
   Moving the poll into `RaceStateEngine` would make injections work without the
   agent layer, but it would also put third-party-ish code on the 60 Hz hot path,
   and the spec did not ask for it. Recorded here as the obvious future move.
4. **`InjectionKind` is descriptive, not behavioural.** A script says
   `kind: "weather"` for a human and for a UI; what actually happens is decided by
   `event_type`. Making the kind dispatch behaviour would have meant writing the
   scheduler this stage was told not to write.
5. **`RTV_PITWALL_DIRECTOR_SCRIPT` validates rather than runs.** The env var
   exists because a schema nobody can point a server at is hard to trust. A bad
   path is a warning, not a boot failure: losing a rehearsal is a smaller failure
   than refusing to start the pitwall, and the reason is reported on
   `/pitwall/status`.
6. **Engine resilience is opt-in, and the live path opts in.** Making it the
   constructor default would have quietly weakened much of this test suite, which
   asserts what the engine does *not* emit as often as what it does.
7. **The disconnect handling is wired in `services.on_state`, not `LivePoller`.**
   The spec says "engine pauses, state flagged stale, agents suspended, clean
   resume" without saying where. `on_state` is already the seam that attaches the
   session id (stage-3 deviation 9), so using it again keeps v1 ingest untouched
   — the constraint `CLAUDE.md` puts above everything else in this build.
8. **"Engine pauses" is implemented as a flag, not a gate.** The engine has no
   clock; with no frames arriving it is *already* paused. What was missing was
   the label, so `mark_stale()` flags and freezes rather than gating `on_frame`.
   A frame arriving is treated as its own announcement of a resume, so the
   recovery works even when nothing tells us about it.
9. **The cost guard counts model calls, not agent wake-ups.** The spec says "hard
   cap on LLM calls per session". A wake-up is a tool round trip or two *plus*
   the answer, so counting wake-ups would have understated the bill by 2–3×.
   `AgentRuntime` counts and times `provider.complete()` itself, which is also
   what supplies the per-agent latencies the spec asks for.
10. **The "Pitwall AI degraded" notice is attributed to the failing agent, not to
    a system channel.** The spec says "emit an info RadioMessage". Using the
    agent's own name means the browser speaks it in that role's voice and the
    per-agent mute applies to it, which is what a listener expects; a separate
    `pitwall` speaker exists only for whole-layer notices (suspension, budget).
11. **The full-system test replaces `services.build_pitwall` with a scripted
    provider.** The spec asks for an integration test that boots the app and
    asserts state + event + radio frames. The agent layer cannot mount offline by
    design (stage-2 deviation 1), so the test monkeypatches the one factory
    function and lets everything else — lifespan, supervised tasks, engine, bus,
    radio feed, WebSocket — be real. The alternative, making the app fall back to
    a scripted provider on its own, would ship fake agent output to production.
12. **The example script is one file, not a directory of them.** The spec asks for
   "one example scenario JSON". It is deliberately the *hardest* interesting race
   — a yellow that lands just before the fuel window, weather that turns the tyre
   call over, and a regulation that removes "stay out" from the answer set —
   rather than a minimal one, because a schema is only as good as the scenario it
   turns out not to be able to express.
13. **`GET /pitwall/health` lives in `routes_racestate.py`, not
    `routes_pitwall.py`.** The latter 503s as a body when the agent layer is
    absent, which is right for the radio and wrong for a health check. Routing it
    with the race-state surface is what makes it answerable whenever
    `RTV_PITWALL` is on.

---

## Verification (stage 5)

```powershell
pytest                                   # 493 passed (393 stage 1-4 + 100 new), fully offline
python scripts/demo_pitwall.py           # the 60-second demo: server + browser + race
python scripts/smoke_pitwall_director.py # the seam + the eight hardening properties
python scripts/smoke_pitwall_voice.py    # stage 4, unchanged
python scripts/smoke_pitwall_roles.py    # stage 3, unchanged
python scripts/smoke_pitwall_agents.py   # stage 2, unchanged
python scripts/smoke_pitwall.py          # stage 1, unchanged
python scripts/smoke_offline.py          # v1 surface, unchanged
python evals/run_pitwall.py --dry-run    # 24 cases across 4 agents, unchanged
node frontend/js/run-audio-tests.mjs     # 20/20 audio-discipline cases, unchanged
ruff check src tests scripts evals
```

New test files: `tests/test_director.py` (36), `tests/test_pitwall_resilience.py`
(25), `tests/test_pitwall_hardening.py` (19), `tests/test_pitwall_health_api.py`
(12), `tests/test_pitwall_fullsystem.py` (9). Green with and without
`ANTHROPIC_API_KEY` exported. No iRacing, no network, no API key. All 393
stage-1/2/3/4 tests still pass unmodified, and the scripted race still produces
the same 19 events and the same radio log it did in stage 1 — which is the check
that matters most here, because stage 5 touched the engine's ingress, the
`RaceState` schema and the orchestrator's task lifecycle.

### The full-system test

`tests/test_pitwall_fullsystem.py` is the one test that boots everything: the
real FastAPI app through its real lifespan — race-state engine, event bus,
orchestrator with its supervised tasks, radio feed, WebSocket — replays the
scripted race, and asserts that `state`, `event` **and** `radio` frames all
arrive on `/ws/pitwall` in contract-valid form (validated against `RaceState`,
`RaceEvent` and `RadioMessage` themselves, not against hand-written shapes).

The only thing that is not real is the model: `rtv.services.build_pitwall` is
replaced with one that hands the orchestrator a `ScriptedProvider`. That is the
same seam the evals and every smoke script use, and it is the only way this can
be a *default* test — the suite must run with no API key and no network, and the
agent layer is opt-in precisely so that it never mounts by accident.

---
---

# Stage 6 — the pit stand

Everything before this stage produced numbers and sentences. This one is the
first that a human looks at during a race, which changes what "correct" means:
a value that is subtly wrong is worse than one that is missing, and a value you
have to hunt for is worse than one that is not there at all.

The result is a second top-level mode of the app, switched in the header:

```
[ Analysis | Pitwall ]
```

**Analysis is untouched.** The MoTeC-i2-style worksheets are a separately built
vanilla-JS app served on its own origin against this API (see deviation 1), so
Analysis mode is a launcher for it plus the v1 session list — it does not
re-implement a single worksheet, and no v1 endpoint changed.

---

## The screen

```
+--------------------------------------------------------------------------+
| Racing Telemetry Visualiser   [Analysis|Pitwall]          radio . api     |
+--------------------------------------------------------------------------+
|                    YELLOW - FULL COURSE CAUTION                          |  <- 4
| session  |  LAPS LEFT 7/12  | track/air | feed: REPLAY | AI LIVE 24/400  |
| replay > [scenario] [4x] [Start] [Stop]              scenario . 4x . 812f |
+------------------------------+-------------------------------------------+
| # STRATEGIST  Box this lap.. | BOX NOW . lap 5 . 3.0 L . rejoin P8       |  <- 2
| +--------------------------+ |  |------#########--+------|   lap ^  dry  |
| | SPOTTER  crit  L5 12:04  | |  4   5   6   7   8   9  10  11  12  13    |
| | Yellow, yellow! Car..    | | margin -1.00 | fuel 6.00 | tank | per lap |
| |  > why                   | | +----+----+  LF 88 ^   RF 91 -            |
| | DRIVER   L5 12:06        | | +----+----+  LR 84     RR 85              |
| |   `- after your message  | +-------------------------------------------+
| |     ENGINEER  Fronts..   | | pos car   gap     last lap                |  <- 3
| +--------------------------+ | P3  #2   +0.60   1:32.418                 |
| [Enable audio][Radio on] vol | P4  #1   +0.30   1:32.104   <- player     |
| [mutes x4]  [say something.] | P5  #0   -0.30   1:32.550   BLUE          |
+------------------------------+-------------------------------------------+
| events  L4 0:42 lockup RF slip 0.42 . L5 0:50 pit window lap 5-7 . ...   |  <- 5
+--------------------------------------------------------------------------+
```

One socket feeds all five panels, with the three frame types routed by what they
are rather than by where they are shown:

```
/ws/pitwall --+-- state  --> coalesced to <=10 Hz --> status strip / strategy / tower
              +-- radio  --> radio feed + RadioAudioManager (stage 4, unchanged)
              +-- event  --> ticker (+ tower warnings + the fuel sparkline)
```

State is coalesced because an old snapshot is worthless once a newer one exists;
radio and events are never coalesced, for the reasons stages 1 and 2 already gave.
The socket is subscribed at 10 Hz because that is also the render ceiling — asking
for 60 would buy frames the app would only throw away, and
`test_the_socket_subscribes_within_the_render_ceiling` pins the two together.

---

## `n/a` is the whole design

`RaceState.capabilities` says which groups this session's catalog could actually
back. The UI treats that as load-bearing rather than as diagnostics: **every**
number on screen goes through a formatter that returns `n/a` for `null`, and no
panel has a zero-valued default anywhere.

| Missing | What the pit stand shows |
|---|---|
| no fuel channels | the pit-window bar draws its axis and says *"no fuel model — pit window unavailable"*; margin, laps of fuel and per-lap are `n/a` |
| no tyre channels | all four corners render, all four read `n/a`, and the header says *"n/a — no tyre channels"* |
| no `gap_basis` | every gap is `n/a` and the tower says *"gaps unavailable: no lap time or track length to convert from"* **once**, at the top |
| no standings channels | *"n/a — this session has no per-car standings channels"* |
| a corner nobody measured | that corner is `n/a`; the other three still show |
| `last_lap_time == 0` | unknown, not "zero seconds" |

That last row is the one that would have slipped through. A lap time of `0.0` is
what the sim reports before a lap has been set; printing `0.000` in a timing tower
is a number nobody can distinguish from a real one.

---

## Module map (added)

| Module | Role |
|--------|------|
| `frontend/index.html` | The app shell: mode switch, status strip, the grid, the ticker, and Analysis mode. |
| `frontend/css/app.css` | Layout and widgets, scoped under `body.app`. `pitwall.css` stays the single source of the palette. |
| `frontend/js/pitwall-app.js` | The wiring: socket, modes, render tick, replay bar, driver input, radio controls, polls. |
| `frontend/js/pitwall/format.js` | **Pure.** Every number-to-string in the app, and the only place `n/a` is produced. |
| `frontend/js/pitwall/view-model.js` | **Pure.** `RaceState` → what each panel shows. Gap trends, fuel bands, the window axis, tower windowing, ticker lines. |
| `frontend/js/pitwall/dom.js` | `el()` / `clear()` / `canvas2d()` / `cssVar()`. |
| `frontend/js/pitwall/status-panel.js` | The strip and the flag band. |
| `frontend/js/pitwall/strategy-panel.js` | The pit-window canvas, the fuel block, the consumption sparkline, the 2x2 tyres. |
| `frontend/js/pitwall/tower-panel.js` | The timing tower. |
| `frontend/js/pitwall/radio-panel.js` | The feed, the on-air strip, driver threading. |
| `frontend/js/pitwall/ticker-panel.js` | The deterministic event strip. |
| `frontend/js/pitwall/view-model.test.js` | The 34 assertions both the page and node run. |
| `frontend/pitwall-test.html` | The in-page self-test. |
| `frontend/js/run-pitwall-tests.mjs` | The headless runner. |

The split is stage 4's, restated: **what a number means** is pure and tested
without a browser; **what it looks like** is a panel that takes a view object and
paints it. A panel with a bug renders badly; a view model with a bug tells the
driver the wrong thing, so that is the half with the test suite.

---

## The four judgements worth recording

### 1. The flag band is the panel

Green is the absence of news, so it is a 4-pixel hairline. Anything else expands
to a 30-pixel full-width band in that flag's colour with the phase written across
it, and yellow and red pulse. "Is there a caution" must never be something you go
looking for on a screen you are glancing at from a rig.

### 2. A gap trend needs the same two guards the backend needed

`GapTrendTracker` marks a car closing, opening or steady. It refuses to compare
two samples less than a second apart (noise), **and** it discards any comparison
that straddles a change of `standings.gap_basis` — when a lap time first becomes
known, gaps stop being derived from instantaneous speed and *every* gap moves at
once while nobody has moved. That is the exact false positive stage 3's traffic
detector had to fix, one layer up, and it is asserted here too.

### 3. The consumption sparkline is the engine's number, not a second one

One point per completed lap, taken from `fuel.per_lap` — the engine's rolling
mean over green, non-pit laps. Subtracting fuel levels client-side would have
been easy and would have produced a second answer to "what does a lap cost",
disagreeing with the strategist's by a tenth. There is one fuel model in this
system.

### 4. Driver messages thread, but are not called replies

A message you send appears as a `DRIVER` entry, and the calls that follow it
within 45 s of session time nest underneath — which is how a transcript reads.
The thread is labelled **"after your message"**, not "reply", because the backend
deliberately does not route driver messages to the agents (stage-4 deviation 1).
An unrelated pit call presented as an answer to the driver would be exactly the
unbacked causal claim the rest of this system refuses to make.

---

## The pit-window bar

A lap axis with four marks, each drawn only if the state carries it:

| Mark | Source | Meaning |
|---|---|---|
| caret + line | `player.lap` | where we are |
| filled span | `fuel.pit_window_earliest_lap` … `_latest_lap` | the window (green when `window_open`) |
| red edge, labelled `dry` | `player.lap + fuel.laps_remaining` | fractional; where the tank actually empties |
| amber edge | `player.lap + fuel.laps_to_finish` | the chequered flag |

The axis is computed to contain every mark it has to draw plus a lap either side.
Drawing a window edge clamped to the end of the axis would read as a decision
nobody made, so an axis that cannot contain its marks is not drawn at all — the
panel says why instead. `the pit-window axis contains every mark it has to draw`
is the check, in the JS suite.

---

## Replay controls, which are also the demo

The replay bar appears when a replay is running **or** when nothing else is
feeding the engine — which is the same state you start a demo from. It posts to
the documented API (`/replay/start` with a session id and a speed, `/replay/stop`)
and reflects `/pitwall/health`'s view of both sources, so it greys out rather than
producing a 409 when the live poller owns the engine.

Analysis mode's session list carries a **"replay on the pitwall"** button per
session, which feeds a stored race back through the race-state engine and switches
modes. `session_id: "scenario"` is the built-in scripted race and needs no capture
at all — that is the zero-setup demo, and it is what the smoke script drives.

Starting a replay resets the client-side accumulators (gap trends, tower warnings,
the fuel sparkline). They are per-race by definition; a re-run that showed a
closing arrow against the previous run's numbers would be worse than showing none.

---

## What the UI consumes (nothing new was added to the API)

Stage 6 added **no endpoints, no env vars and no Python modules**. Everything it
needs was already exposed by stages 1-5:

| Used for | Endpoint |
|---|---|
| everything live | `WS /ws/pitwall` — `state` (10 Hz), `event`, `radio` |
| ticker prefill | `GET /api/v1/racestate/events?limit=` |
| radio prefill | `GET /api/v1/pitwall/radio?limit=` (503 = agent layer off, and the panel says so) |
| AI badge, calls used | `GET /api/v1/pitwall/status` |
| connection / replay / live state | `GET /api/v1/pitwall/health` |
| role voices | `GET /api/v1/pitwall/tts` |
| replay bar | `POST /api/v1/replay/start` · `/stop` |
| driver input | `POST /api/v1/pitwall/driver-message` |
| Analysis mode | `GET /api/v1/sessions` (v1) |

`test_the_app_only_calls_documented_endpoints` resolves every `${API}/...` in the
app against the app's own route table, so a UI that calls a path nobody serves
fails the suite rather than the race.

Seeded radio history is **shown but never spoken** (`speak: false` on the way into
the audio manager): the driver has already driven past it, and a reconnect that
read forty messages back would be its own outage.

---

## Testing something with no build step

Three layers, none of which needed a toolchain.

**1. The pure rules, in JavaScript.** 34 cases over plain objects — degradation,
fuel bands, the window axis, tower windowing, gap trends, warning expiry, the
sparkline, ticker lines, the strategist summary.

```powershell
node frontend/js/run-pitwall-tests.mjs          # headless; exit code is the result
start http://127.0.0.1:8000/pitwall-test.html   # same module, rendered in-page
```

**2. The wiring, from Python.** No bundler means no compiler, so the things a
compiler would have caught are asserted instead: every `import` in the app graph
resolves to a file **and** is served as `text/javascript`; every element id the
app reaches for exists on the page; every `data-role` a panel queries and every
`data-field` a panel writes exists in the markup. A `_set('fuel-margin', ...)`
against a field that is not there is a silent no-op — precisely the dead control
this project has no other way of noticing.

**3. The whole thing, booted.** `scripts/smoke_pitwall_ui.py` serves the real
frontend from the real app, replays the scripted race over `/ws/pitwall` at 10 Hz,
and checks that state and event frames arrive contract-valid, that the replay bar's
start/stop round trip works, that all three REST polls answer with **no agent
layer mounted**, and that a session stripped of its fuel and tyre channels reports
that in `capabilities` rather than zeroing it.

---

## Deviations from the stage-6 spec

Each is a conservative choice made autonomously, per `CLAUDE.md`.

1. **"The existing analysis app" is not in this repo.** The spec says to add a mode
   to it. The MoTeC-i2-style worksheets are built separately and served on their
   own origin against this API (stage-4 deviation 2 recorded the same gap), so
   there was no analysis UI here to add a mode *to*. The mode switcher is real and
   both modes are top-level; Analysis mode is a launcher — the v1 session list plus
   a field for wherever you serve the worksheets, remembered in the page URL. It
   deliberately re-implements nothing: building a second analysis UI would have
   been the one thing `CLAUDE.md` forbids most clearly.
2. **The pitwall is the root page; `/radio.html` stays.** The spec's "new top-level
   mode" wants one app, and the placeholder index this replaced said it should be.
   Stage 4's listening page is untouched and still linked, because it is the
   minimal reproduction when the question is *"is the audio broken or is the UI?"*.
3. **The timing tower shows `#idx`, not a car number.** `CarState` carries
   `idx` — the sim's car-index — and no race number; the channel contract in
   `racestate/channels.py` has nothing that would supply one. Printing a plausible
   number would be exactly the invented figure this codebase refuses. `#idx` is
   honest and stable, and a real number channel can replace it without a layout
   change.
4. **Driver threading is labelled "after your message", not "reply".** See
   judgement 4 above.
5. **The replay bar appears when nothing is feeding the engine**, not only when a
   replay is already running. The spec says "when in replay mode", but a control
   that only exists once you are already in replay mode cannot start one — and the
   spec also says the same control doubles as the demo. It hides itself when the
   live poller owns the engine, which is the state where a replay would be refused
   with a 409 anyway.
6. **Speed applies to the next replay, not the running one.** `ReplayDriver.start`
   takes a speed; nothing changes one mid-run, and adding an endpoint for it would
   have been backend work this stage was not asked for. The bar says so when you
   change it while a replay is in flight.
7. **The fuel sparkline plots the engine's rolling mean, not per-lap deltas.** See
   judgement 3.
8. **No new env var for the analysis app's URL.** It lives in the page URL
   (`?analysis=...`), like volume, mutes and mode. A server-side setting would have
   meant a new env var, a new endpoint to read it and a restart to change it, for
   something that is per-operator rather than per-deployment. `.env.example` is
   therefore unchanged — stage 6 added no configuration at all.
9. **The tower's "expand" is a toggle, not a scroll.** The spec says "P±3 at
   minimum, expandable to full field". Collapsed is exactly P±3 around the player;
   expanded is the whole running order in the same rows. A full field that is
   always rendered and scrolled would have meant the player row is sometimes off
   screen, which is the one row that must never be.
10. **A second in-page self-test page rather than one combined with the audio
    one.** `audio-test.html` asserts what the driver *hears*; `pitwall-test.html`
    asserts what the driver *sees*. Merging them would have coupled two suites that
    fail for entirely different reasons.
11. **The pitwall does not inherit `/radio.html`'s scripted-radio fallback.** With
    no agent layer mounted the radio column stays empty and the page says why (on
    the AI badge, and again when you start a replay). `/radio.html` may play canned
    calls because it labels them as scripted and its whole subject is the audio
    queue; a panel whose entire job is showing what the agents said cannot fill
    itself with lines no agent produced. `scripts/demo_pitwall.py` therefore still
    opens `/radio.html` — its documented promise is the audible demo — and now
    points at `/` as the second thing to look at, driven by the same replay.

### One bug found and fixed en route

The audio manager's `onChange` paints the radio panel, and applying the URL's
mutes at boot fires it — so the panels had to be constructed **before** the
manager, not after. In the original order it was a temporal-dead-zone
`ReferenceError` on the first line of the app, i.e. a completely blank pitwall,
and nothing in a Python test suite would have seen it. It was caught by booting
the real `index.html` under a throwaway DOM shim; the ordering now carries a
comment saying why it is what it is.

---

## Interfaces a later stage consumes

```js
import { statusView, strategyView, towerView, tyreView, recommendationView,
         eventLine, marginBand, connectionView, llmView,
         GapTrendTracker, TowerWarnings, FuelHistory } from '/js/pitwall/view-model.js';
import { NA, isNum, num, lapTime, clock, gap, signed, trendArrow } from '/js/pitwall/format.js';
import { el, clear, stat, setText, setClass, canvas2d, cssVar } from '/js/pitwall/dom.js';

window.RTV_PITWALL   // {app, manager, speaker, panels:{status,strategy,tower,radio,ticker}}
```

A new panel is a class with `render(view)` plus a `data-role` host in
`index.html`; a new *number* is a field on one of the view builders, which is
where its `n/a` behaviour gets asserted. A new agent needs nothing here — the
radio panel keys its badge colour off `RadioMessage.agent`, and an unknown agent
gets the neutral one.

When a race director is implemented (stage 5), its events already arrive on the
ticker with a `DIRECTOR` tag: `eventLine()` reads the `injected` / `director_id`
provenance keys, and the case *"a director-injected event keeps its provenance in
the ticker"* pins it.

---

## Verification (stage 6)

```powershell
pytest                                   # 539 passed (493 stage 1-5 + 46 new), fully offline
python scripts/smoke_pitwall_ui.py       # the UI end to end: shell, socket, replay, degradation
node frontend/js/run-pitwall-tests.mjs   # 34/34 view-model cases
node frontend/js/run-audio-tests.mjs     # 20/20 audio-discipline cases, unchanged
python scripts/smoke_pitwall_director.py # stage 5, unchanged
python scripts/smoke_pitwall_voice.py    # stage 4, unchanged
python scripts/smoke_pitwall_roles.py    # stage 3, unchanged
python scripts/smoke_pitwall_agents.py   # stage 2, unchanged
python scripts/smoke_pitwall.py          # stage 1, unchanged
python scripts/smoke_offline.py          # v1 surface, unchanged
python evals/run_pitwall.py --dry-run    # 24 cases across 4 agents, unchanged
ruff check src tests scripts evals
```

New test file: `tests/test_pitwall_ui_frontend.py` (46; one case skips itself when
node is absent). Green with and without `ANTHROPIC_API_KEY` exported. No iRacing,
no network, no API key. All 493 stage-1-to-5 tests still pass unmodified — stage 6
added no Python outside `tests/` and `scripts/`, and touched no v1 route, no
endpoint and no env var.

## Seeing it

```powershell
uvicorn rtv.main:app          # then open http://127.0.0.1:8000/
```

Press **Enable audio** once (browsers make no sound before a gesture), then
**Start** on the replay bar with `scenario` — the scripted race drives the whole
screen with no key, no iRacing and no captured session. With
`RTV_PITWALL_AGENTS=true` and a key exported, the radio column fills with live
agent output; without them it stays empty and the AI badge says why, which is the
honest version of a quiet radio.

---

## Phase 1 cleanup — real data only, Kimi only

Not a staged prompt: the first step of the UI revamp (2026-10-30). The offline
suite stayed green throughout, and v1 behaviour is unchanged except where noted.

**No fake data at runtime.**
- `src/rtv/demo/` and `RTV_SEED_DEMO` are gone; a fresh store is empty. The
  generator lives on as `evals/synthetic_session.py` (`seed_synthetic_session`),
  for tests only.
- `src/rtv/racestate/scenario.py` moved to `evals/synthetic_race.py`, together with
  `scenario_source()`, `replay_scenario()` and `SCENARIO_SESSION_ID`. The replay API
  no longer accepts `"scenario"`: `POST /api/v1/replay/start` requires a stored
  `session_id` (422 without one). Tests seed the scripted race into the store with
  `seed_scenario_session()` and replay it like any import.
- Deleted `scripts/demo_pitwall.py` (scripted radio over the synthetic race),
  `scripts/seed_demo.py` and the six stage-era `scripts/smoke_pitwall*.py`; the
  pytest suite covers what they drove.

**Replay is live-shaped.** `ReplayDriver(engine, publish=..., on_lifecycle=...)`: the
app passes `hub.publish_frame`, so `/ws/live` streams a replay exactly like live
capture, and the hub's state is `ConnectionState.REPLAY` (`"replay"`) while it runs,
`disconnected` after. `store_source()` now reports `total` from the session's sample
count. `POST /api/v1/live/start` is refused (409) while a replay runs, the mirror of
the existing guard.

**Real `.ibt` ingest.** `POST /api/v1/import/upload?filename=x.ibt` takes the file
as the raw body. The real Spa GT3 race (`scripts/fetch_fixtures.py`,
`tests/test_real_ibt.py`) exposed and fixed three bugs that the synthetic race could
not:
- iRacing publishes `LapLastLapTime` ~1-2 s after the `Lap` counter moves, so every
  lap time was the *previous* lap's. `LapTracker` now waits up to
  `LAP_TIME_SETTLE_S` for the official time (else uses start-to-start session time),
  and `detect_lap_completion` only trusts a value that changed on the crossing
  frame; the engine falls back to session time.
- After the start, iRacing clears the green bit and leaves `start_hidden`, so every
  real lap was `UNKNOWN`, never green: no fuel model, no stint milestones (the
  strategist's wake-up). `flag_phase` treats `start_hidden` (outside the formation
  lap) as green.
- In/out laps now come from pit-road transitions during the lap; lap 0 (pre-start)
  and the unfinished last segment are `is_valid = False`; `finalize()` closes at the
  last tick seen instead of a caller-supplied zero or sample index.

Known gap: `.ibt` files do not record `CarIdxLapDistPct`, so a replayed `.ibt` has
no standings (`capabilities.standings = false`, listed in `missing`), and the
spotter's traffic triggers cannot fire on it. Live capture has them.

**Kimi Code only.** `rtv.llm` has no provider switch: `RTV_LLM_PROVIDER`,
`RTV_LLM_STRUCTURED_OUTPUT`, `RTV_COACHING_MODEL` and
`RTV_PITWALL_AGENT_MODEL_FAST/_REASONING` are gone (old `.env` lines are ignored).
`llm.model(tier, override)` replaces `resolve_model`. `AnthropicProvider` is
`KimiProvider`, tool-forced only; `structured_call()` is the one-shot helper the
Layer-3a orchestrator and the eval judge now use. Both previously called
`messages.parse(output_format=...)`, which Kimi's endpoint does not implement.
`CoachOrchestrator(provider, model=...)` replaces `CoachOrchestrator(client=...)`.
Agent default model ids are `kimi-k3`.

**Kimi tool-call markup is a malformed turn (R2).** Kimi sometimes fails to parse its
own tool call and returns the raw template tokens (`<|close|>`, `<|sep|>`,
`<|oopen|>argument key=...`) inside an argument; on the A2 coach eval a whole second
respond call sat in the orchestrator's `corner` field in 2 of 5 cases, and the schema
accepted it. `rtv.llm.find_markup` detects them; `KimiProvider` then discards the whole
turn and returns `ProviderResponse(malformed=...)` with no output and no tool calls.
`structured_call` asks once more (and takes a `check=` callable for contracts beyond the
schema), the pitwall `AgentRuntime` retries a malformed turn once per run (counted in
`stats()["malformed"]` and the journal's `llm_call_end.malformed`), and the graph coach
bounces it through its validation feedback, then its grounded refusal. The orchestrator
also requires each specialist's `corner` to be the corner it was asked about (the prompt
read `corner None` before: top3 entries carry `label`) and every plan priority to be a
label in the findings; with no valid plan it returns `plan=None` with a message. The
Spa journals (`spa-kimi-run-combined`, `spa-kimi-run-B2`) hold no markup, but they do
not record a discarded payload, so their 3 and 2 calls without a structured answer
cannot be attributed; the new `malformed` field records that from now on.

**Coaching in one place.** `LapFindings` gained `kpis` (`main`/`ref`
`DriverKPIs`: trail-brake %, average throttle %, throttle smoothness, steering
corrections, coasting %), `agent_stats` (count and attributed seconds per roster
analyst), `CornerFinding.entry_m/exit_m` and `SectorDelta.start_m/end_m`;
`GET /api/v1/coaching/agents` lists the roster. These were the parts of
`frontend/app/coach.js` the Python port lacked, so a UI can drop `coach.js`.
`chief.time_spread` now uses clean laps only.

---

# The live agent activity view (UI revamp)

The root page is now the **live view**: the race state as before, plus the agent
layer visibly at work. Every moving part on it is a record the backend emitted while
doing that piece of work. Nothing animates on a timer, and nothing is scripted.

## The activity stream

`src/rtv/pitwall/activity.py` adds `ActivityLog`, an `EventBus` subclass (same
bounded, drop-oldest, drops-counted subscriptions) carrying plain dicts:

```
{ "seq", "wall_ms", "session_time", "tick", "kind", "agent", "run_id", "event_key", "data" }
```

| kind | emitted in | data |
|---|---|---|
| `dispatch` | `PitwallOrchestrator.dispatch` | `accepted`, `skipped` (agent -> `cooldown`/`disabled`), `blocked` (`disabled`/`suspended`/`budget_exhausted`/`stale`) |
| `inbox_superseded` | `PitwallOrchestrator._offer` | `dropped`, `kept` event keys |
| `run_start` | `AgentRuntime.invoke` | `trigger`, `severity`, `model`, `context_radio` (message ids + agents the run was shown), `context_events`; journal only: `input` (F1) |
| `llm_call_start` / `llm_call_end` | `AgentRuntime._complete` | `call`, `model`, `latency_ms`, `ok`, `error` (class name), `tool_calls`, `answered` |
| `tool_call` | `AgentRuntime._converse` | `tool`, `args` (long strings cut), `ok`, `result` (shape only: type, keys, size), `latency_ms`; journal only: `result_full` (F1) |
| `validate` | `AgentRuntime.invoke` | `ok`, `unsupported`, `word_count`, `over_word_limit`, `repaired` |
| `run_end` | `AgentRuntime.invoke` | `outcome` (`radio`/`refused`/`error`), `message_id`, `priority`, `tools_used`, `turns`, `latency_ms` |
| `radio_queued` / `radio_superseded` / `radio_aired` | `RadioFeed.publish` / `emit` | `message_id`, `seq`, `priority`, `subject`, `refused` |

The four agents never call each other; they hear each other only through the recent
radio each run is shown. `run_start.context_radio` records exactly that, and the
view draws it as radio -> agent edges.

Hooks are an optional `tracer` on `AgentRuntime`, `PitwallOrchestrator` and
`RadioFeed` (`activity=` on the orchestrator wires all three). `None` costs one
check. A tracer that raises is logged and ignored. `AppServices.activity` exists
whenever the engine does; records without their own time take the engine's current
`session_time`/`tick`.

Surface: `WS /ws/pitwall` gains a fourth frame type `{"type": "activity",
"activity": {...}}` (pushed as it happens, never coalesced), and
`GET /api/v1/pitwall/activity?since=<seq>&limit=<n>` serves the backlog.

## Frontend

`frontend/js/shared/` holds the pieces the post-race review reuses:
`activity-model.js` (pure reducer: `createModel`, `apply`, `reduce(records, upTo)`,
`edgesFor`; ignores unknown kinds), `timeline.js` (`Timeline` with follow and scrub
modes, `setRange`, `setCursor`, `onSeek`, `onSelect`) and `flow-graph.js` (the SVG
pipeline). `frontend/js/live/` has the run waterfall and the raw-telemetry strip
(`/ws/live`). `node frontend/js/run-shared-tests.mjs` runs the pure tests;
`tests/test_live_view_frontend.py` runs it under pytest when node is present.

The analysis-mode launcher is gone: the header links to `/review/` (post-race review,
which also takes over lap analysis). The replay picker lists stored sessions only.

## Race journal — the post-race review's record

Race events and radio calls otherwise live only in ring buffers, so nothing but the
telemetry outlived a session. `rtv.store.journal.SessionRecorder` (built in
`build_services` whenever `RTV_PITWALL` is on, `AppServices.recorder`) writes one
JSON-lines file per *run* of a session:

    data/sessions/<session_id>/runs/<YYYYMMDDTHHMMSSZ>-<nn>-<live|replay>.jsonl

A run opens when a replay starts (`ReplayDriver` `on_lifecycle`) or when the live
poller reports `IN_SESSION` for a new session id, and closes when the replay ends
or iRacing disconnects, writing a final state snapshot. Runs never overwrite each
other, so a free agents-off replay of an `.ibt` cannot clobber a live run.

Each line is the activity envelope from `rtv.pitwall.activity` (`seq`, `wall_ms`,
`session_time`, `tick`, `kind`, `agent`, `run_id`, `event_key`, `data`). `seq` is
renumbered per file across every kind (an activity record's own counter becomes
`data.activity_seq`). The recorder's own kinds:

| kind | source | data |
|---|---|---|
| `lifecycle` | run start / end | `phase`, `source`, `session_id`, `agents` (name, model, enabled), `agents_enabled`; `reason` on end |
| `race_event` | `engine.bus` callback | `RaceEvent.to_api()` |
| `radio` | `RadioFeed` callback (aired) | full `RadioMessage` dump incl. `message_id` |
| `state` | per frame, every `RTV_JOURNAL_STATE_INTERVAL_S` session seconds (default 1) and on each lap change | `RaceState.to_api()`; the last one has `"final": true` |

Every `AppServices.activity` record (dispatch, run_start, llm_call_*, tool_call,
validate, run_end, radio_*) is passed through as-is via a bus callback.

Endpoints (always mounted, read-only):

- `GET /api/v1/sessions/{id}/pitwall-runs` lists runs with counts per kind,
  session-time bounds, `has_agents`, `complete`, and `default_run` (the newest run
  the agents spoke in, else the newest).
- `GET /api/v1/sessions/{id}/pitwall-log?run=&kinds=&exclude=state&agent=&from=&to=&after_seq=&limit=`
  returns records in file order; `state` is excluded unless asked for.
- `GET /api/v1/sessions/{id}/pitwall-log/state?t=&run=` returns the snapshot at or just before `t`.

On the real 19-lap Spa fixture an agents-off replay writes 72 race events and 2,512
snapshots in 6.5 MB, the log reads in about 0.2 s, and a state lookup takes about
0.15 s. This covers item 3 of `prompts/07` (the journal) with JSON lines rather
than DuckDB: append-only on the hot path, no second writer on the database the
poller is using, and a crash loses at most one flush interval (1 s).

## Post-race review (`/review/`)

`frontend/review/index.html` + `frontend/js/review/` replace the old `/app/` analysis
app (deleted, with its `docs/` Pages copy, `scripts/build_pages.py` and the
`publish-pages` skill; the Pages showcase returns later as an export of a real
recorded session). It reads only recorded data and never re-runs the agents.

- **Replay**: one journal run on the shared `Timeline` in scrub mode (lanes: laps,
  flags, race events, one per agent, radio). Each seek rebuilds the agent layer with
  `activity-model.js` `reduce(records, t)` and feeds the live view's own `FlowGraph`
  and `RunsPanel`; the car panel reads `/pitwall-log/state?t=`. Playback (1-60x)
  pulses the graph edges of the records it passes.
- **Race summary**: `GET /api/v1/sessions/{id}/summary?run=` (`rtv.racestate.summary`),
  deterministic from the lap table and the journal: result (first classified and final
  position), best / median / spread of clean laps, stints split at in-laps, pit stops,
  fuel used and added (tank drops and rises of at least 0.5 L between snapshots),
  incidents, event counts, radio calls, agent runs and model calls. A figure is null
  when the session did not record it, and `notes` says why.
- **Lap analysis**: `/compare` traces (speed, throttle, brake, steering, gear) for any
  two laps, with the elapsed-time delta from the new `GET /api/v1/coaching/delta`
  (`CoachingService.delta_trace`, the same curve `lap_findings` uses, kept out of
  `LapFindings` so it never reaches an LLM), and a track map with every detector event
  that carries `lap_dist_pct` (off-tracks, incidents, lock-ups) pinned on it.
- **Coaching findings**: `/coaching/lap-findings` rendered as-is (chief summary, corners
  with each analyst's diagnostics, sectors, driver-input KPIs); clicking a corner
  highlights its entry-exit window on the traces and on the map.
- **Agent audit**: one row per agent run from the journal (trigger, model, calls,
  tools, validator verdict, outcome, latency, what aired), filterable and sortable.
- "Record a run from the telemetry" starts `POST /replay/start` for the session (max
  speed while the agents are off, which is free; with agents mounted it asks first,
  because that calls Kimi). "Import .ibt" uses `POST /import/upload`.
- URL state: `?session=&run=&main=&ref=&t=` (`lap=` is accepted as `main=`).
- Pure grouping in `review-model.js`, tested by `node frontend/js/run-review-tests.mjs`
  (run by `tests/test_review_frontend.py`).

---

## H0 — current-architecture page and per-agent models (2026-10-31)

Plan task H0 of the project review. This file gained the current-architecture page at
the top; everything that was here before moved, unchanged, under this appendix.

`AgentSpec.model` is no longer hard-coded: it defaults to `""` and the new
`AgentSpec.tier` (`"fast"` or `"reasoning"`) says which `rtv.llm` default applies.
`rtv.pitwall.framework.agent_model(spec, settings, override)` resolves it (override, the
spec's own model, `RTV_PITWALL_<NAME>_MODEL`, the tier default) and is used by
`build_agents` (new `settings=` argument), by `AgentRuntime` when handed a bare spec, and
by `evals/run_pitwall.py`. `rtv.services` no longer spells out the four agents.
`FAST_MODEL` / `REASONING_MODEL` are gone from `rtv.pitwall.agents`.

Deviation: the plan named the settings `RTV_PITWALL_MODEL_<AGENT>`, but
`RTV_PITWALL_<AGENT>_MODEL` already existed (wired in `services.build_pitwall` only), so
those names stay and existing `.env` files keep working. Behaviour with nothing set is
unchanged: every agent runs on `kimi-k3`.

---

## B1 — late and expired radio calls (2026-10-31)

Plan task B1 (REVIEW P1-2, decision Q3). A Kimi run takes 15-60 s and the radio used to
air whatever reached the front. Now:

- **Stamped at the run.** `orchestrator._run_agent` calls `validity.stamp` with the
  snapshot the run started from: `state_version`, `computed_at_session_time`, the
  agent's `max_age_s`, and whatever the spec's new `validity` hook returns from the
  structured answer. The strategist's hook (`strategist_validity`) gives a box call
  (`box_now`, `pit_under_yellow`, `box_next_lap`, `plan_stop` with `target_lap`)
  `valid_until_lap`, `invalid_if_on_pit_road` and, when the session has shown where the
  pit entry is, `invalid_if_lap_dist_past`. The spotter's names `subject_car_idx` from the
  event's `car_idx`. The engineer and coach have only an age limit.
- **Checked at air time.** `RadioFeed.take(now)` (used by `pump`, `flush` and `run`) reads
  the race state from `state_source` (the orchestrator wires `engine.snapshot`) and calls
  `validity.check`. Ages are on the session clock, so a 2x replay doubles them.
- **Defaults**: spotter 3 s, vehicle engineer 20 s, coach 30 s, strategist 30 s (for the
  late tag; its calls are dropped only when their lap has gone). Override with
  `RTV_PITWALL_<AGENT>_MAX_AGE_S`; 0 turns the late tag off.
- **Pit entry**: `engine.pit_entry_pct` is the median lap fraction at which any car's
  `OnPitRoad` went true this session (player or field). An `.ibt` has no field, so before
  the player's first stop a box call is dropped only once the lap is over or the car is in
  the pit lane. Deriving the entry from the OSM pit lane is left for later.

Journal records and fields added (F1 and the review read these):

- `radio_late` (activity kind): `message_id`, `agent`, `priority`, `subject`,
  `late_by_s`, `max_age_s`, `computed_at_session_time`; `session_time` is the air time.
- `radio_expired` (activity kind): the same ids plus `reason`, `age_s`,
  `computed_at_session_time`, `spoken_text`; `session_time` is when it was dropped.
- `radio_aired` gains `late: true` and `late_by_s` for a late call (absent otherwise).
- The `radio` record (the message itself) gains `state_version`,
  `computed_at_session_time`, `max_age_s`, `valid_until_lap`, `expires_at_session_time`,
  `invalid_if_lap_dist_past`, `invalid_if_on_pit_road`, `subject_car_idx`,
  `air_session_time`, `late`, `late_by_s`, `expired`, `expired_reason`. A dropped call is
  journalled as a `radio` record with `expired: true`, `speak: false` and no
  `radio_aired`.
- `RadioMessage.message_id` ignores the "Late call: " prefix, so the id a call was
  queued, journalled and given an audio URL under does not change when it airs late.
- `feed.stats()` gains `late` and `expired`.

UI: the desktop radio list and review show "late +Ns" and strike a dropped call with its
reason (`rtv.desktop.model.review.air_badge`, mirrored as `airBadge` in
`review-model.js`, both checked by the view golden file). The web live pages skip
expired messages; the web review lists them struck, with the badge. Nothing else changed
on the web.


---

## G4 — live visual notes off the radio, one call budget (2026-10-31)

Plan task G4 of the project review (REVIEW.md P2-5). Live VLM notes are review material
by default: `PitwallOrchestrator.dispatch` routes a `visual_observation` to no agent and
traces it as `blocked: "vision_review_only"` unless `RTV_VISION_LIVE_RADIO=true`
(`Settings.vision_live_radio`, orchestrator argument `vision_radio`). The event still goes
on the bus, so the journal, `/ws/pitwall`, the timeline and both reviews are unchanged.

Vision calls now count against `RTV_PITWALL_MAX_CALLS_PER_SESSION`. The orchestrator gained
`record_call(layer, n=1)` (thread-safe) and `calls_by_layer`; `calls_used` sums the agents
and every recorded layer, and `reset()` clears the layers with the agent baseline.
`LiveVisionWorker` takes `budget=` (anything with `calls_remaining` and
`record_call(layer)`; `services` passes the orchestrator through `VisionService(budget=)`),
checks it when a moment is admitted and again before the call, and counts a skip as
`over_budget` plus a `skip` record in the vision journal. `RTV_VISION_LIVE_MAX_CALLS`
stays as the inner cap. `/pitwall/status` has `calls_by_layer`; `/pitwall/health`'s
`agents` block now carries `calls_used`, `calls_by_layer`, `max_calls_per_session` and
`budget_exhausted`. With the agent layer off there is no shared budget and the vision cap
alone applies. `radio.py` is untouched (B1 owns it).

---

## E1 — live corner labels match the review (2026-10-31)

Plan task E1 (REVIEW.md P2-2). The live pitwall now names places the way the post-race
review does.

- `rtv.tracks.live.LiveCornerMap` keeps the lap being driven in `floor(LapDistPct * 1500)`
  bucket means (the store's own resampling) and, at each clean lap, runs Layer 1's apex
  detector (`rtv.coaching.features.detect_apexes`, the same `_detect_corners`) and the
  venue name lookup (`TrackReference.name_at` at each apex). The engine feeds it every
  frame and offers it every completed green, pit-free, timed lap.
- **Deviation:** the plan said "after one clean lap". Layer 1 numbers corners per lap, and
  on the real Spa race the start lap has an extra apex (Raidillon) and lap 8 misses
  Speaker's Corner, so one lap can renumber everything. The map is adopted when two clean
  laps have the same apexes (each within 2 % of a lap); on Spa that is laps 2 and 5 (3-4
  ran under yellow), and the result equals the review's corners for its default laps.
  Since R3 Layer 1 itself labels every lap pair from the same procedure run on the stored
  laps (`rtv.tracks.service.session_corner_map`, `docs/TRACKS.md`), so `get_corner_detail`'s
  `telemetry.corner.label` (now with `name`) is the live map's label on any lap.
- `RaceState.track = {corner_map_lap, provisional, corners: [{label, name, lap_dist_pct}]}`,
  a new state section (`get_race_state_slice` lists it), in the coach's and the vehicle
  engineer's slices.
- `payload.place = {label, name, lap_dist_pct, provisional}` on `lockup`, `wheelspin`,
  `offtrack`, `incident` and `recurring_issue` (engine `PLACE_EVENTS`). A place belongs to
  the nearest apex around the lap. A recurring issue is placed at the middle of its hits.
  `CornerRecurrence` still counts by bucket, and `payload.corner` stays the bucket.
- The coach and engineer prompts cite the place ("T1 La Source", or "T4" without a name)
  and keep "the same corner" for a provisional bucket. `get_corner_detail` accepts `T7` or
  `T7 Pouhon` and filters live events by the map; its `live` block gains `place`.
- RTV Desktop's live ticker shows the place (`view_model.place_title`), the same
  `T1 La Source` text as the review's findings table.
- Names need `Lat`/`Lon`, which the `.ibt` has; a stream without them gets labels and
  `name: null`, the same as its own review.
- The coaching checks (`rtv.coaching.agent.validation.split_corner` / `resolve_corner`)
  accept `T7 Campus`, `T7 — Campus` or `Turn 7 (Campus)` as T7 when Campus is T7's mapped
  name, and report any other name as made up; `evals.checks.cross_check` uses them.

## F1 — each agent run's full input in the journal (2026-10-31)

The journal is the eval source (REVIEW P2-3), so a run must be re-executable from it.

- **`rtv.pitwall.agent_input`** (new, imports nothing from the runtime). `build_input`
  makes the record; `model_input(record)` returns `system`, `messages`, `model`,
  `max_tokens`, `tools` (names) and `output_contract` of the run's first model call;
  `input_of(record)` is `None` for a version-1 run; `spec_drift(record, spec,
  system_prompt)` lists what changed since (`prompt`, `tools`, `output_contract`,
  `model`). The pure `system_blocks` and `instruction` are what the runtime itself uses,
  and the runtime builds its first call *from the record*, so the rebuild is
  byte-identical by construction (`tests/test_agent_input.py`, all four agents).
- **Record** (`run_start.data.input`, `v: 1`): `agent`, `model`, `max_tokens`,
  `max_tool_iterations`, `provider` (`KimiProvider.describe()`: name, thinking, effort),
  `system_prompt` (role prompt + grounding rules) and `prompt_sha`, `state_slice`,
  `event` (`RaceEvent.to_api()`), `radio` (the five calls shown: id, agent, priority,
  spoken text), `prefetched` (`tool`, `args`, `result`), `tools` + `tools_sha`,
  `output_contract` + `output_schema_sha`. `tool_call.data.result_full` is the whole tool
  result, so later turns can be rebuilt too.
- **Order**: `run_start` is now written after the prefetch (it needs the results); the
  prefetch `tool_call` records are held and written right after it, so a run still
  starts with `run_start`. A run that fails before its input exists still gets one.
- **Schema**: `rtv.store.journal.JOURNAL_SCHEMA_VERSION = 2` on the lifecycle start
  record; `schema_version_of`, `summarize_run()["schema_version"]` (1 when absent) and
  `agent_inputs(path)`. `run_stats` adds `schema_version` and `runs_with_input`, and the
  compare table two rows. Old journals read unchanged (tested on the run-3 excerpt,
  including the desktop review and activity models).
- **Live feed**: `input` and `result_full` are journal-only; `rtv.pitwall.activity.public`
  strips them from `/ws/pitwall` and `/pitwall/activity`. `/pitwall-log` serves them.
- Not recorded: the in-loop model outputs beyond what `tool_call.args` (cut at 120
  chars) and the radio record hold; F3 replays model output from its own fixtures.

---

## B2 — deterministic spotter (2026-10-31)

Plan task B2 of the project review (REVIEW.md P1-3). The spotter's per-event calls are
templates (`rtv.pitwall.callers`, described under "Deterministic callers" above); the
LLM spotter's per-event triggers (`traffic_close`, `blue_flag`, yellow/red `flag_change`,
`incident`) are gone. It keeps a `lap_completed` trigger for a traffic summary, gated on
a measured field within `SUMMARY_GAP_S` (5 s) and a 300 s cooldown, plus the
`visual_observation` trigger (off the radio unless `RTV_VISION_LIVE_RADIO`). Its
`max_age_s` is now `None` (a summary is never tagged late) and `spotter_validity` makes
a summary expire 60 s after its snapshot. `RTV_PITWALL_SPOTTER_MAX_AGE_S` now sets the
template calls' limit (default 3 s); when set it also applies to the summaries.

Engine: new `EventType.CAR_ALONGSIDE`, `channels.CAR_LEFT_RIGHT`, `REQUIRED["car_alongside"]`,
`Capabilities.car_alongside`, pure `detectors.detect_car_alongside(previous, raw)` and
`alongside_state(raw)`, checked every frame. The Spa `.ibt` has no `CarLeftRight`, so it
reports the capability off and lists the channel in `capabilities.missing`.

`RadioMessage.source` is new: `"model"` (default), `"template"`, `"driver"` (push-to-talk)
or `"pitwall"` (a notice); the journal's `radio` record carries it. Template messages
also put `source` and the event key in `data`.

Evals: `SPOTTER_THREATS["lap_completed"]` and a `spotter-traffic-summary` case; the
per-event spotter cases stay, grading the agent contract. `evals/synthetic_race.py`
gained `ScenarioSpec.alongside` (test-only; the channel is absent unless it is set, so the
ground-truth scenario is unchanged) and `_headers(spec)`.

Changed tests, by design: in `test_pitwall_roles.py` a traffic call and a local yellow
now wake no agent and produce a template call, and a caution wakes the strategist plus a
spotter template; `test_default_max_ages_and_the_settings_override` checks the spotter's
summary age is `None` and the 3 s template default. New: `tests/test_pitwall_callers.py`.

Not done: template calls need a mounted agent layer, which `services.build_pitwall`
only builds with a Kimi key. Running the radio and the callers without any key is a
possible follow-up. Desktop: template calls are ordinary radio messages, so the live view
shows them like any other; no view field changed, so the golden file is unchanged.

---

## A1 — typed grounding in the pitwall validator (2026-10-31)

Plan task A1 (REVIEW.md P1-1). The validator used to accept any spoken number near *any*
number the run had been shown. Now facts are typed and a figure said with a unit must match
a fact of that unit family.

- `rtv.grounding.units`: the Layer-3b prose check's figure patterns, moved unchanged
  (`prose.py` imports them back), plus `read_figures(text)` (figure, unit family, unit;
  spelled-out numbers up to ninety-nine, "two point four", "half a second", "a lap and a
  half", "three tenths"; "a couple" is not a number), `field_unit(path)` (the unit an engine
  field is reported in, from its key) and `convert()` within a family.
- `FactSet(typed=None)`: `None` reads `RTV_PITWALL_TYPED_GROUNDING` (default true). Typed facts
  are `(value, unit, path)` per family (`FactSet.facts(family)`); detector findings' generic
  `value`/`threshold`/`excess` take the unit from the finding's `unit`/`measure`, and numbers
  inside tool strings carry the unit written next to them. `supports_figure(Figure)` converts
  the fact into the spoken unit (kPa to psi, m/s to km/h, fraction to %) and then applies the
  old rounding and tolerance. Bare figures and `_ALWAYS_ALLOWED` (0-3, bare only) are as
  before; structured payload fields are still exact.
- `GroundingReport.unsupported_detail` / `.typed`; the repair turn now quotes the detail
  ("166.5 laps (no laps figure in the data matches; 166.5 is race_state.tyres.pressures.LF
  (pressure))"). `unsupported` is unchanged.
- Evals: `evals.pitwall_cases.swapped_fact_cases()` (one per agent, the right fact swapped
  for another real fact in the agent's FactSet) and `agent_facts(spec, case)` (the FactSet
  the runtime would build). All four pass the old check and are caught by the typed one;
  `run_pitwall.py --dry-run` prints them.
- Kimi Spa replays (`project-files/spa-kimi-run-A1/`): refusals 2 (run 3) to 5, then 4 after
  fixing "seventy-two" being read as "two". The new refusals are a speed from an event
  payload (m/s) quoted with another unit, and derived counts ("three laps running" from a
  recurrence). Task A3 is the planned fix for the derived counts.

## F3 — real-journal eval cases (`evals/real/spa_run`)

The F1 journal held the input but not the answer or the full state, so a recorded run
could be re-executed but not re-scored. F3 closes that and turns a Spa replay into cases.

- **Journal schema 3**: `run_start.data.state_full` (`RaceState.to_api()` of the snapshot
  the run was handed; the policy, the validity rule and the role checks read more than the
  agent's slice) and `validate.data.output_full` (`AgentOutput.model_dump`, one per pass:
  first answer, then the repair). Both journal-only. `JOURNAL_SCHEMA_VERSION = 3`.
- **`evals/real/cases.py`**: `extract_cases(journal)` (schema-3 runs with at least one
  answer), `write_case_set(cases, dir, source=)` (`cases.jsonl`, `prompts.json` keyed by
  `prompt_sha`, `source.json`), `load_case_set(name_or_dir)`. `context_events` is the last
  `run_start.context_events` race events before the run; `air.state` is the nearest
  journal state snapshot at or before air time, slimmed to what `validity.check` reads.
- **`evals/real/offline.py`**: `score_case`, `score_set`, `summary`, `write_baseline`,
  `read_baseline`, `diff`. The fact set is rebuilt as the runtime builds it (slice, event
  payload, then tool results in call order); `same_as_recorded` says whether today's
  validator and policy agree with the verdict recorded live. Validity re-stamps the rule
  (the pit entry comes from the recorded `invalid_if_lap_dist_past`, since the engine's
  value is not on the message) and checks it at the recorded air time.
- **`evals/real/live.py`**: `run_case(case, provider, model=, tool_config=, judges=)`,
  `recorded_tools`, `judge_answer` (advisory `PitwallJudgeScore`: usefulness,
  actionability, grounding, 1-5), `aggregate`. `evals/run_pitwall.py --real <set>
  [--agent] [--limit] [--model] [--judge-provider gemini[,kimi]] [--out file.json]`;
  `--dry-run` with `--real` prints the recorded calls and offline scores.
- **Set**: `evals/real/spa_run/` from the F3 Kimi Spa replay (full journal in project files
  `spa-kimi-run-F3/`). See its README for how to regenerate it.
- **Deviation**: the PLAN name was `spa_run3`; the set is `spa_run`, since it is cut from a
  fresh replay, not run 3 (which has no inputs).

---

## Stage 07 — session lifecycle and autonomous arm/disarm (2026-10-31)

Plan task D3; prompt `prompts/done/07-session-lifecycle-autonomy.md`. Built: the
`SessionLifecycle` state machine (`racestate/lifecycle.py`) and the four lifecycle events
plus `session_state_change`, which nothing emitted before; session segments with
`engine.reset_segment()` / `end_segment()`; `services.SegmentJournal` (one journal run
per segment, `pitwall.reset()` at each `session_start`); `pitwall.reset()` in
`POST /replay/start`; standby and autonomous arm/disarm in the orchestrator; the
session-end hook with a stored summary and a file notifier (`rtv/session_end.py`); the
desktop's LIFECYCLE cell (session, phase, armed / standby and why / off) and run labels
in the review picker (`rtv.desktop.model.session`, Python-only, not in the golden file
because the frozen web pages have no counterpart). Settings: `RTV_PITWALL_AUTONOMOUS`,
`RTV_PITWALL_AUTONOMOUS_SESSIONS`, `RTV_PITWALL_ROSTER_PRACTICE`,
`RTV_PITWALL_ROSTER_QUALIFY`, `RTV_PITWALL_SESSION_END_NOTIFIER`. Test-only generator:
`evals/synthetic_weekend.py` (practice, qualifying and race in one stream);
`ScenarioSpec.session_num` / `.sessions` add the SessionNum channel and the session list
without changing the default scenario. Tests: `tests/test_session_lifecycle.py`,
`tests/test_desktop_session.py`, and a real-Spa test in `tests/test_real_ibt.py`.

Deviations:

- The lap table has no session number (and live laps are keyed by lap number, so a race
  lap overwrites the practice lap of the same number). A run's laps are therefore the lap
  numbers its own state snapshots saw, inside its session-time bounds; the summary each
  run stores at its end is the record of that moment.
- `session_checkered` is the player's flag, not the leader's. The leader's flag is the
  `session_state_change` to `checkered`, and the lifecycle state follows it.
- The real Spa green arrives with iRacing's `Lap` still 0 (98.7 % round the formation
  lap); the test asserts it lands before the first lap completion rather than `lap == 1`.
- A live disconnect closes the journal run (as before) but not the segment, so the hook
  fires for that run and the reconnect opens a second run of the same segment. Ending the
  segment would have thrown away the fuel model across a short gap.
- `tests/test_pitwall_resilience.py::test_a_reconnect_produces_no_phantom_events` counted
  18 bus events on the synthetic race; it now counts the 18 race events plus the three
  lifecycle events the first frame adds.
- B2's deterministic spotter templates merged while this was in review. They cost no
  model call, so standby does not hold them: in standby `dispatch` runs the template
  callers and then blocks the agents (`blocked: "standby"`, `templated` listing the
  callers). The kill switch, `suspend()` and stale state still silence them, as before.

## B3: cross-agent decision groups

`rtv.pitwall.decisions`: `DECISION_GROUPS` (one group, `pit_stop`: `strategist/pit_stop`,
`vehicle_engineer/damage_stop`, `vehicle_engineer/tyre_stop`), `CONFLICT_WINDOW_S = 60`,
`group_of(message)`, `action_of(message)` (`box_now`, `stop_later`, `stay_out` or `None`
from the structured answer; refusals and `hold` take no side) and `opposed(a, b)` (box_now
vs stay_out or stop_later). The coach's `coaching` and the engineer's `grip` were
considered and left out: they complement each other on a lockup rather than decide one
thing.

`EngineerCall.pit_request` (`none` | `box_now`) is new, with a prompt paragraph: box_now only
when the car cannot safely reach the planned stop. With it the engineer's subject is
`damage_stop` (or `tyre_stop` for a tyre finding) and `vehicle_engineer_validity` stamps the
box-call rules for the current lap. This changes the engineer's output contract, so
`spec_drift` reports `output_contract` and `prompt` against older journals.

`RadioFeed.publish` rules inside a group: (1) a newer call replaces queued non-critical calls
of the same or lower priority from any agent (`rule` `same_agent` / `group`, or `critical`
when the newer one is critical; a critical call still replaces nothing of its own agent's);
(2) a call opposed to a strictly higher-priority call from another agent, queued or aired
within the window, is dropped itself (`conflict`). Equal priority is a revision: the newer
airs. Outside groups nothing changed. Every superseded message (same-agent ones too, which
before were only traced) now reaches subscribers and callbacks with `superseded`,
`superseded_by`, `superseded_reason`, `speak=False` and `air_session_time`, so the journal
has its `radio` record. `radio_superseded` gains `by_message_id`, `by_agent`, `by_priority`,
`rule`, `group`, `reason`, `spoken_text` (the old `by` field is gone). `feed.stats()` adds
`conflicts`; `feed.take_superseded(agent)` hands out, once, the agent's last call another
agent's call beat.

Agent context: the orchestrator passes it to `AgentRuntime.invoke(superseded=)`; the
instruction gets one `NOT AIRED: ...` line (`agent_input.superseded_line`) after the recent
radio, and `run_start.data.input.superseded` records it only when present, so other runs'
inputs are byte-identical to before. `run_stats` adds `radio_superseded_cross_agent`,
`radio_conflicts` (n/a for older journals) and `radio_superseded_by_rule`; compare_runs
prints the first two.

Views: `air_badge` returns `{"kind": "superseded", "text": "not aired", "reason"}` (Python
and JS), and superseded calls are struck through with the reason like expired ones in the
desktop live radio and review, and in the web review. The web live pages skip them like
expired calls. Golden cases: a test-only engineer box call superseded by the strategist's
real critical yellow-flag call in `journal_air`, plus airBadge cases.


## C1 — the driver asks, the pitwall answers (2026-10-31)

Plan task C1 (REVIEW.md P1-4). Described under "Driver questions" above. Pieces:
`EventType.DRIVER_QUESTION`; `RadioMessage.in_reply_to`; `rtv.pitwall.questions`
(`route_question`, `Route`, `LOOKUPS`, `answer_lookup`, `QuestionCaller`,
`question_trigger(agent)`); every agent spec gains `question_trigger(AGENT_NAME)`;
`agent_input.instruction` adds the `THE DRIVER ASKED` block for a `driver_question` event
only, so other runs' inputs (and their journal rebuilds) are unchanged.
`PitwallOrchestrator.ask`, `_reply_unanswered`, `_holds_slot`; `status()` adds
`questions` and `agents_mounted`; the orchestrator always appends a `QuestionCaller` to
its callers. `services.build_template_pitwall` fixes B2's gap (template calls without a
key). `POST /pitwall/driver-message` calls `ask` and also returns `question` (the route).

Desktop: the radio box is now "Ask"; `hub.ask(text, source)` is the entry point for C2's
voice input (`send_driver` calls it). `model.radio.thread_feed` nests a message whose
`in_reply_to` matches a driver message under it as `answer`, wherever it lands;
`question_status` shows "asked the coach · waiting for the answer" until it does. The
quota dialog's `agents_will_run` is false for the template-only mount. The frozen web
radio panel keeps its 45 s "after your message" rule.

Evals: `evals.pitwall_cases.question_cases(agent)`, one `question-<agent>` case per agent
(`QUESTIONS`); the strategist's sits before its degraded cases. `scripts/ask_during_replay.py`
asks ten scripted questions at fixed laps of a running replay and records each answer and
its latency.

Changed tests, by design: `test_a_driver_message_joins_the_log_but_is_never_read_back`
now finds the transcript among the messages (a reply is also queued), and the LLM
spotter's trigger set includes `driver_question`. New: `tests/test_driver_questions.py`.

---

## D1 — each agent sees its own previous call (2026-10-31)

REVIEW P2-1, third bullet: an agent restated its standing call instead of revising it,
because every run started from the state slice and the recent radio only.

- **Context**: the run's first user message gains, before `RECENT RADIO`, a block
  `YOUR EARLIER CALLS (revise or confirm them; do not restate one that still stands word
  for word, and say what changed if you change it)`, one line per subject, at most the
  last two subjects (`agent_input.PREVIOUS_SUBJECTS`), oldest first:
  `You said on lap 4: "Plan to box lap 13." (aired late by 9 s)`. Statuses come from the
  B1 radio fields: `aired`, `late` (`late_by_s`), `expired` (`never aired, dropped:
  <expired_reason>`) and `queued` (not aired yet). The spoken text is the agent's own,
  without the `Late call: ` tag.
- **Where**: `PitwallOrchestrator` keeps each agent's newest model call per subject
  (`_remember_call`, after the validity stamp; refusals, withheld calls and non-model
  sources are skipped) and `previous_calls(name)` turns them into entries with
  `agent_input.previous_call(message, queued=)`. It holds the message objects, so the
  status is what the radio did by the time the next run starts. `reset()` clears them.
  `AgentRuntime.invoke(..., previous_calls=)` passes them on. `radio.py` is unchanged.
- **Record**: `run_start.data.input.previous` (`message_id`, `subject`, `lap`,
  `session_time`, `spoken_text`, `status`, `late_by_s`, `expired_reason`), written only
  when the agent has an earlier call, like B3's `superseded`, so other runs' records and
  rebuilds are unchanged and `v` stays 1. The first call is still built from the record,
  so the rebuild stays byte-identical (`tests/test_agent_input.py`,
  `tests/test_previous_calls.py`).
- **With B3**: a call another agent's call replaced shows as `superseded` (`never aired,
  replaced: <superseded_reason>`); the run that gets B3's one-off `NOT AIRED` line for it
  does not list it again. Answers to driver questions (C1, `in_reply_to`) are not kept.
- Prompt text only: role prompts and `GROUNDING_RULES` are unchanged, so `prompt_sha`
  and `spec_drift` do not move. The strategist has one subject (`pit_stop`), so it is
  shown one line.

## A3 — derived numbers and readable audit values (2026-10-31)

On the A1 Spa replay most refusals were arithmetic or units, not inventions. Changes:

- **Speeds in km/h.** Detectors keep the sim's m/s, but what an agent is shown (its trigger
  event, and every row of `get_recent_detector_events` / `get_corner_detail`) goes through
  `rtv.racestate.derived.agent_payload`: `speed`, `mean_speed`, `wheel_speed` become
  `speed_kmh`, `mean_speed_kmh`, `wheel_speed_kmh`. Three of A1's four refusals were 27.7 m/s
  said as "27.7 km/h". The FactSet holds the same view (`framework._invoke`).
- **Derived fields to copy.** Recurring issues carry `consecutive_laps`, `laps_spanned`,
  `laps_with_issue`; `get_corner_detail.live` carries `laps_with_events`, `consecutive_laps`.
  `TyreState` gains `{temp,pressure}_change_since_stint_start` and `_vs_previous_lap`
  (engine, per completed lap); `get_tyre_trend` returns them as `since_stint_start` /
  `vs_previous_lap` plus `axles` (front-minus-rear, left-minus-right, spread).
  `get_stint_history.current_stint_laps` has lap-time deltas vs the previous lap, the stint's
  first lap and the best, and `green_streak_laps`; fuel projection and stint history carry
  `race_total_laps`.
- **Validator.** `FactSet.supports_difference`: an unmatched figure is grounded when it is
  the difference of two grounded figures said in the same sentence, in its unit family.
  A spelled-out laps count ("three laps running") also matches a count fact
  (`occurrences`, `count`), never a digit laps quantity. `explain()` names a same-family
  fact in another unit ("27.74 is race_state.player.speed in m/s, which is 99.86 km/h").
  `tools._target_pct` no longer recurses on an unmatched corner label.
- **Audit.** `validate` records carry `unsupported_detail`; the desktop and web view models
  round long decimals (`roundFigures` / `round_figures` in the activity model) and expose
  `reasons` (desktop live run detail lines, review audit tooltip). Golden cases added.
- `evals/real/spa_run` baseline rebuilt: F3's two coach false positives pass again.
- Kimi Spa replay deferred: the Kimi Code quota was blocked when A3 merged, so it merged on
  the offline checks; one combined Spa replay later measures A3 against `spa-kimi-run-A1/`.

---

## Stage 08 — race plan and projection layer (2026-11-01)

Plan task D4; prompt `prompts/done/08-race-plan-projection.md`. Built: the projection
layer (`racestate/projection.py`), the lap log and the versioned `RacePlan`
(`racestate/models.py`, logic in `racestate/plan.py`), `engine.propose_plan_revision`,
the `plan_revised` event, the six plan and projection tools, plan slices for the
strategist, engineer and coach, `projection_ok` and `plan_ok`, two degraded cases and a
plan swapped-fact case, the desktop plan panel, timeline lane and audit column. Tests:
`tests/test_projection.py`, `tests/test_race_plan.py`, `tests/test_race_plan_e2e.py` (the
synthetic race through the app with a scripted strategist: baseline plan at lap 2,
survives the yellow, a rejected proposal changes nothing, the accepted one is the next
version, the stop is consumed), `tests/test_desktop_plan.py`, four eval tests in
`tests/test_pitwall_evals.py` and a real-Spa test in `tests/test_real_ibt.py` (plan v1 on
lap 2 stopping on lap 13, revised to lap 12, the lap the driver pitted; no stops left
after it; stint-1 degradation fits; traffic ungrounded). The `spa_run` baseline was
rebuilt (new keys only; mean check score 0.958 to 0.964). The F1 round-trip is
unchanged.

Deviations:

- The schema lives in `racestate/models.py` next to `RaceState` (it is part of the state
  and its JSON); `racestate/plan.py` re-exports `RacePlan`, `PlanElement` and
  `PlanRevision`.
- The prompt's "fuel per stint" is two elements: `fuel_to_add_l` (the next stop's fill)
  and `fuel_per_lap_target_l` (the consumption target), each typed by its suffix.
- `get_undercut_overcut` is an extra tool over `projection.undercut_overcut`, which the
  prompt lists as a projection but gives no tool; the spotter gets
  `get_traffic_projection` because traffic is its subject.
- D1 (previous-call context) merged while this was in progress; its previous-call
  lines are left as they are, and the plan version reaches the agent through the plan
  slice instead.
- Rejected proposals are kept in `plan.revisions` (`accepted: false`) as well as the
  activity log, so the review can show them from the state snapshots alone.
- The plan exists only when `lifecycle.session_type == "race"`; practice and qualifying
  have `plan: None` (an `.ibt` without session info has no type, so no plan).
- The plan cases sit before the stage-3 degraded cases in `build_cases()` so the
  existing test that names the last four cases is unchanged.

---

## B4 — fast Kimi tier trial (2026-10-31)

Plan task B4 (REVIEW.md P1-2). Result: **no change**; every agent stays on `kimi-k3`.
Runs, journals and the full report are in project files `spa-kimi-run-B4/`.

The key's `GET /v1/models` lists `kimi-for-coding` (K2.8 Preview),
`kimi-for-coding-highspeed` (K2.7 Code Highspeed), `k3`, `k3-256k`, `k3.1-pro` and
`k3.1-pro-highspeed`; `kimi-k3` is not listed but is served. With a forced respond tool
and `thinking` disabled: `k3.1-pro*` return 400 ("does not support disabled thinking
mode"), `kimi-for-coding` ignores `tool_choice` and answers in text, and
`kimi-for-coding-highspeed` skipped the tool in 2 of 5 calls. `kimi-k3`, `k3` and
`k3-256k` behave the same (about 3.2 s for a tiny call, about 23 output tokens/s). Only
`kimi-for-coding-highspeed` was faster per output token (about 27 tokens/s), so it was
trialled.

Three Spa replays at 2x ran at the same time (control all `kimi-k3`, coach on the
candidate, engineer on the candidate), compared with `scripts/compare_runs.py` against run
3. The candidate was slower for the agent it ran: coach median call 16.7 s against 8.4 s,
engineer 15.9 s against 10.3 s. Refusals did not rise (coach 0/0, engineer 0 against 1)
and validator failures did not rise either, but there was no speed to gain.
`evals/run_pitwall.py` agreed: coach ungrounded numbers 14 against 8, average call 10.2 s
against 9.0 s; engineer 11.8 s against 9.5 s.

Run 3's 17.5 s median call was endpoint time: the unchanged control ran at 8.6 s today.
To repeat the trial on a new model id, set `RTV_PITWALL_<AGENT>_MODEL` and compare a
replay with a control run made at the same time.

## A2 — one prose check for all three coaches (2026-11-01)

REVIEW.md P2-4: the 3a orchestrator ran the deterministic prose check, the 3b graph coach only
checked structured figures, and the pitwall coach only its FactSet. Now all three call
`rtv.coaching.agent.prose.check_prose`:

- **Graph coach.** `validate_node` runs it over `prose.answer_prose(payload)` (headline,
  one_lap_focus, priority whys, claim statements) against every retrieved findings dict
  (`_merged_findings` now also carries `sectors` and one `chiefs` entry per lap pair). Issues
  are `prose:` lines in the existing validation turn, so retries and the degraded answer work
  as before.
- **Pitwall coach.** `FactSet.corners` keeps every Layer-1 corner a tool returned
  (`validator.layer1_corner`: a `get_corner_detail` result with `telemetry.available`, mapped
  back to `CornerFinding` field names). `validate_output(..., prose_check=True)` (set by
  `AgentSpec.prose_check`, true for the coach only) runs `prose_contradictions`, which calls
  `check_prose(..., mode="live")` over spoken_text, detail_text and the payload's prose fields
  when the answer's `corner` is one of those corners or names none. Live mode checks brake
  direction only: the FactSet already checks figures against every source, live detector
  events can justify a brake cue with no Layer-1 brake-point difference, and other corners of
  the map are not inventions. Results land in `GroundingReport.contradictions` (ok=false):
  the repair turn names them, a refusal's `detail_text` says "contradicts the corner
  analysis", and the `validate` trace has `contradictions` and appends them to
  `unsupported_detail` so both audits show them unchanged.
- `get_corner_detail` diagnostics gain `distance_m`, so a quoted brake point can be placed.
- `rtv.coaching.agent/__init__` loads `graph` and `provider` lazily: `prose` and
  `validation` (hence the pitwall validator and the orchestrator) import without langgraph.
- Two scripted graph-coach answers in tests said "late braking" for T4, whose fixture finding
  is "Brake point 9 m early"; the check caught it and the scripts now say "early".
- Offline: `evals/real/spa_run` baseline rebuilt (verdicts unchanged; answers gain an empty
  `contradictions`). On the F3 partial Spa journal, 5 coach answers held a Layer-1 corner
  (T4, T8) and none was flagged; coach-5's "brake a touch earlier" at T8 matches its
  "Brake point 14 m late". Tests: `tests/test_one_prose_check.py`.
- Kimi: one coach eval run (`evals/run.py`, Gemini judge) after the combined Spa replay.

## Stage 09 part A — race control (2026-11-01)

Plan task D6a; part A of `prompts/09-race-control-debrief.md` (dependencies 07, 08, A1, B2
all merged; stage 10 not merged when this was built, so no director scenario was added).
Built: `racestate/race_control.py` (`RaceControlMonitor`, `incident_limit_from`,
`stage_for`), `RaceState.race_control`, four capability flags, the `incident_limit`,
`track_limits`, `penalty` and `blue_flag_ignored` events, race control's template caller,
the `race_control` agent with its policy and `penalty_stop` subject in the `pit_stop`
group, the `incidents` question lookup and race-control routing, the agent in RTV
Desktop's lanes, flow graph, radio mutes and review audit, `race_control_check`
(`consequence_ok`, `count_ok`, `refusal_ok`), a golden set (three scripted-race cases,
three degraded, one question) and a swapped-fact case. Synthetic race knobs:
`ScenarioSpec.incidents`, `offtracks`, `penalty_flags`, `incident_limit` (all off by
default, so the ground-truth race is byte-identical). Tests: `tests/test_race_control.py`
and a real-Spa test in `tests/test_real_ibt.py` (unlimited, 4x, the incident events add up
to the counter, no penalty, two track-limits read-outs, blue flag a stated blind spot, the
agent never woken). The `spa_run` baseline did not change (`build_real_evals.py --diff
spa_run`: no change; race control has no runs in that journal).

Did "a new agent is configuration" survive? Mostly. The agent is one module and one
registry entry, its read-outs are one more `DeterministicCaller`, and the orchestrator,
radio feed, validator and API needed nothing. Four things outside the agent changed, each
small: `AgentSpec.withheld_text` (the framework's one change: the policy-withheld text was
hard-coded to the strategist's "contradicts the fuel plan"), one `pit_stop` group entry in
`decisions.py`, the router's owner tuple and table (a test requires every agent to be
routable), and two field keys (`incident_limit`, `incidents_remaining`) in
`grounding.units` so the limit figures are typed as incident counts. The engine side
(detectors, state section, capabilities) is new deterministic work, which is where the
design wants it.

Deviations:

- The per-agent model setting is `RTV_PITWALL_RACE_CONTROL_MODEL` (the existing
  `RTV_PITWALL_<NAME>_MODEL` pattern), not the prompt's `RTV_PITWALL_MODEL_RACE_CONTROL`;
  `RTV_PITWALL_RACE_CONTROL_MAX_AGE_S` likewise.
- "Track limits one off from a warning" has no channel: iRacing reports no warning count.
  Each off is a 1x, so the consequence that wakes the agent is repeated offs with 8 or
  fewer incidents left to the limit; the read-out names the count only.
- "One more incident ends the race" is modelled as 4 or fewer left (one 4x contact).
- With a numeric limit the incident read-out is race control's, and the spotter's incident
  template stays quiet, so the driver hears one sentence per incident. With no limit (the
  Spa race, the synthetic race) the spotter's call is unchanged.
- Blue-flag compliance needs a held time as well as corners: on the 5 s synthetic lap three
  corners is under a second, which a car passing normally takes.
- Race control also gets `question_trigger` and an `incidents` lookup: the C1 router test
  requires every registered agent to own some questions.
- Roster tests that list the agents by name now list five; no other test was rewritten.
- The frozen web pages keep their four-agent `AGENTS` list: their activity model creates
  an unseen agent on its first record, so race control's records do not break them (it has
  no lane there). The golden view file is unchanged.

## Stage 10 — race director scheduler, evals only (2026-11-01)

Plan task D5; prompt `prompts/done/10-director-scheduler.md`. Dependencies checked: stage
07 (PR #39), stage 08 (PR #49) and B2 (PR #40) are merged. Built: `ScriptedDirector`
(`director/scheduler.py`) with every `InjectionTrigger` condition, edge-triggered firing,
`reset` and `describe`; the `injected` overlay (`overlay()`, `orch.injected_overlay`,
`AgentRuntime.invoke(injected=)`, `agent_input.INJECTED_RULE`, policies with an
`injected` keyword) and `director/overlay.py`; consumers for the director-only types
(strategist triggers on rain and a mandatory-stop regulation, engineer trigger on rain,
`plan.mandatory_stop` in `derive` / `refresh_plan` / `sanity_check` / `propose_revision`,
the strategist policy, and the validator's weather-claim check); the rehearsal runner
`evals/director_rehearsal.py` + `evals/run_director.py` with its own journal; director eval
cases for weather and regulation. Tests: about 45 new ones at the end of
`tests/test_director.py` (each condition, the overlay, the stage-5 equivalence against the
scheduler, the example end to end, a byte-identical event log twice, a chained entry at 1x
and 4x with a fake clock, the eval journal, `.ibt` traffic note, the app with
`RTV_PITWALL_DIRECTOR_SCRIPT` set injecting nothing into `data/sessions`, rain refusal,
the regulation in the plan and the policy, the eval cases). `build_real_evals.py --diff
spa_run`: no change.

Deviations:

- Runner: a separate `evals/run_director.py` rather than a `--scenario` mode of
  `run_pitwall.py`, whose cases are single (state, event) pairs; a rehearsal is a whole
  replay. Agents run inline after each frame (the race clock waits), which is what makes a
  rehearsal deterministic and speed-independent; it is not a model of live latency. Since
  R4 the tick's template calls air before those inline runs (see *Runner*).
- `at_lap` holds on that lap *or later* (and `at_lap_dist_pct` alone at or past the
  fraction), so a poll that misses the exact frame fires late, stamped from the state it
  saw, instead of never. `while_flag` reads the effective flag (an injected flag wins).
- The overlay lives in the director and reaches the agent through the orchestrator, not
  as a `RaceState` field, so state snapshots, the journal's state records and every live
  agent input are unchanged. The policy hook is a keyword (`injected=`) detected on the
  policy's signature, so the other agents' policies did not change.
- The plan reads a regulation from the engine's bus history (`regulation_change` and
  `pit_exit` events), not from the overlay: the engine has no director. A regulation
  older than the ring buffer is forgotten (rehearsals are short).
- Weather grounding is a word check (rain, showers, drizzle, storm, wet, damp) added to
  `validate_output` for every agent, backed by a weather fact; the Spa real-eval baseline
  is unchanged by it.
- The example script's hazard now names car 1 (P4) with the traffic detector's full
  payload keys: car 11 is not in the synthetic field, and B1's air-time check rightly
  dropped a call about a car that is not there. Its description and notes no longer say
  the director is planned.
- The example's entries fire FCY (lap 3) -> restart (+90 s) -> mandatory stop (lap 5) ->
  rain (+120 s after the restart, green) -> hazard (420 s): the stop is an `at_lap` entry,
  so on 60 s laps it lands before the rain.
- Race control (stage 09 part A, merged first as PR #52) gets its director scenario here:
  `docs/director_incident_limit.example.json` (`--scenario incident_limit`) injects a 4x to
  14 of a 17x limit and the `incident_limit` stage-final event. The read-out airs from
  `RACE_CONTROL_TEMPLATES` with no model call (`template_ok`) and the stage event wakes the
  race-control agent (`race_control_ok`), on the synthetic race and the Spa .ibt.
  `race_control_policy` now also accepts an injected event's own count and limit (the
  counter is never rewritten); a real event's figures must still be the counter's.
- Kimi: not run in this stage (offline checks only). The operator's live runs (the example
  over the synthetic race with `--live`, and over the recorded Spa session) are queued in
  `project-review/waves/kimi-queue.md`.

## Stage 09 part B — the post-race debrief (plan task D6b)

Built from PART B of `prompts/done/09-race-control-debrief.md`; the current-architecture page has
the interfaces ("Post-race debrief"). Part A (race control, D6a, PR #52) merged first, so this PR
moves the prompt to `prompts/done/`.

- **Not an agent in the registry.** The debrief has no trigger, cooldown, state slice or radio
  subject, so it is a service (`rtv.debrief`) on `AppServices`, not an `AgentSpec`: the radio
  and the orchestrator are untouched, and part A's registry edits cannot collide with it.
- **One call, no tools.** Everything the model may cite is in one INPUTS document (about 13 KB
  on the synthetic race), so there is no tool loop; `structured_call` already re-asks once
  for an invalid payload. The repair is a second `structured_call` with the previous answer
  and the issues; per section the first passing version wins.
- **Figures against all inputs, prose rules from A2.** `check_prose` in `lap` mode checks
  every figure against one corner's finding, but a debrief quotes race totals (a corner's loss
  summed over the laps it lost on) that no single corner holds, so its figure rule is replaced
  by the typed FactSet over the whole input; its corner-label and brake-direction rules are
  kept (a brake claim is held to the finding of the lap that lost most at that corner). A4's
  name checks will flow through when they land as a new `ProseReport` kind.
- **Swapped facts.** The FactSet is typed, so a figure of the wrong unit family fails (the
  hermetic case says a litres figure as seconds). Two figures of the same family (best and
  median lap time) are not told apart, as in the pitwall validator.
- **Spec deviations.** DB5 says "driven by ScriptedModel": `ScriptedModel` drives the LangGraph
  coach; the debrief calls the pitwall provider interface, so the cases use its scripted twin
  `ScriptedProvider`. DB4 asks for a "tab": the desktop review is one scrolling page of panels,
  so the debrief is a panel under the race summary. `RTV_DEBRIEF_AUTO` is a string
  (`auto|true|false`) because an empty value for an optional bool does not parse from `.env`.
  `evals.synthetic_race.seed_scenario_session` gains an optional `frames` argument (default
  unchanged) so the store and the replay can hold the same race-phase stream.
- **Offline evidence.** `tests/test_debrief.py` (no-key session-end path, API, settings,
  notifier, inputs, nine hermetic cases from `evals/debrief_cases.py`, a failed call, the
  autonomy-on session end with the scripted model, the Kimi request shape through a fake
  client: `thinking: adaptive`, `tool_choice: any`, only the respond tool),
  `tests/test_desktop_debrief.py` (view model and panel) and, on the real Spa fixture,
  `test_the_no_key_debrief_of_the_real_race_references_only_recorded_laps` in
  `tests/test_real_ibt.py`. The `spa_run` real-eval baseline is unchanged: no validator or
  check the pitwall agents use was touched.
- **One corner numbering.** Layer-1 numbers the apexes of each lap pair T1..Tn afresh, so on
  Spa "T3" is Les Combes on one pair and Bruxelles on the next (the first Kimi debrief mixed
  the two and called Speaker's Corner T4 while the findings' T4 was Bruxelles). The digest
  relabels every pair's corners onto the session corner map the live events use
  (`RaceState.track` of the final state; else the first analysed pair's corners) by apex
  position (within 0.014 of a lap), keeps the pair's own label as `pair_label`, takes the
  bigger loss when two apexes land on one corner, and notes how many matched none.
- **Kimi (2026-11-01).** The combined Spa replay journal (project files
  `spa-kimi-run-combined/`), `kimi-k3` with thinking: 2 calls, 116 s, 6/6 sections kept.
  The summary, plan, time lost and tyres/fuel passed first time; recurring and priorities
  were repaired once (one quoted an unrecorded "lap 42"). The output is in
  `spa-kimi-run-combined/debrief-D6b/`, with the run made before the relabelling kept next to it.

### R4: director rehearsal schedules agents like the app

D5's Kimi run over the Spa .ibt failed `debris_hazard` `radio_ok`: the rehearsal ran each
event's agents inline before pumping the radio, so the strategist's answer to the restart
queued ahead of the spotter's same-tick hazard template (equal priority, earlier
sequence), held the channel for its airtime and the 3 s template expired; the synthetic
`incident_limit` read-out aired as "Late call:" behind race control's critical answer the
same way. The app never does this: templates go on air at dispatch while the model calls
are in flight. `rehearse()` now routes each pass with `orch.dispatch`, pumps the radio,
then runs the agents through `orch.open_inboxes()` / `orch.drain_inboxes()` (the app's
one-slot inboxes, so two events for one agent on one tick also resolve latest-wins, a
critical event or question holding its slot). Eval path only: the app never calls the two
new methods, and they refuse to run while the workers are started. Offline test:
`test_a_same_tick_template_airs_on_time_behind_a_slow_model_call` (fails on the old loop).
