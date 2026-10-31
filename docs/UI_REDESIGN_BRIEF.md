# UI redesign brief — Racing Telemetry Visualiser

A handoff for a full visual redesign of the RTV frontend. It says what every screen
shows, where each number comes from, which states each panel must handle, and what
the delivered files must look like so they can be wired into the app without
changing the backend.

Everything below was checked against `main` on 2026-10-31 (PRs #1-#9 merged). The
example payloads are real responses from the app, captured by importing a real
iRacing race (Audi R8 LMS EVO II GT3, Spa-Francorchamps, 19 laps, ~42 min) and
replaying it with the AI agents off. The only exception is the agents-on section
(5.6). No agents-on run of a real session has been recorded yet, because that run
uses paid Kimi quota. Those records come from the offline test harness, so their
shapes are exact but their words are placeholders.

The full, untrimmed responses (all the JSON behind the examples below) are in
`spa-example-payloads.zip` next to this file in the project files. `agents_scripted.json`
in it is the offline harness run, not Spa.

---

## 0. TL;DR for the designer

- **Two pages, one product.** `/` is the **live pitwall**: a race in progress, with
  four AI race engineers visibly at work. `/review/` is the **post-race review**: a
  recorded race you scrub through, plus lap-vs-lap analysis and coaching findings.
  A small standalone page, `/radio.html`, shows just the radio.
- **Audience.** A sim racer on a rig glancing at a second screen mid-race, and the
  same person at a desk afterwards. The live page must read in peripheral vision:
  dark, dense, high contrast, and caution flags impossible to miss. The review can
  be calmer and more analytical.
- **Deliver static HTML + CSS** (vanilla, no framework, no build step, no CDN),
  filled with the real example values from this brief. We wire it to the live data.
  Section 9 has the exact file rules.
- **Never show a number the data does not back.** Missing data is shown as `n/a`
  with a reason, never as `0`, a flat line or a made-up placeholder. This is the
  single most important rule in the product (section 7).

---

## 1. Product in one paragraph

RTV ingests iRacing telemetry: either live at 60 Hz from the sim's shared memory, or
from an `.ibt` file recorded by iRacing. A deterministic **race-state engine** turns
the frames into a race state (fuel, tyres, flags, position…) and **race events** (pit
window opens, lock-up, off-track, incident…). On top of that, four LLM **agents**
(strategist, vehicle engineer, spotter, coach) wake on those events, call tools that
read the race state, pass a **citation validator** (every number they say must trace
back to the data), and speak on one prioritised **radio** feed, read aloud by
browser text-to-speech. Every session is written to a **race journal**, which the
post-race review replays without re-running the agents. Separately, a deterministic
**coaching** engine (no LLM) compares two laps corner by corner.

---

## 2. Pages and routes

| Route | File today | What it is |
|---|---|---|
| `/` | `frontend/index.html` + `js/pitwall-app.js` | Live pitwall (also shows a replay as if it were live) |
| `/review/` | `frontend/review/index.html` + `js/review/review-app.js` | Post-race review and lap analysis |
| `/radio.html` | `frontend/radio.html` + `js/pitwall-radio.js` | Radio-only page (for a tablet next to the rig) |
| `/pitwall-test.html`, `/audio-test.html` | self-test pages | Developer pages that run the JS unit tests in a browser. **Do not redesign.** |
| `/docs` | FastAPI | API docs (link only) |

The header on every page has the product name, a view switch (**Live** /
**Post-race review & lap analysis**) and small links (Pitwall radio, api).

Reference screenshots of the current UI (the "before") are in the project files:
`live-view/live-view-spa-replay.png`, `live-view/tower-no-field-spa.png`,
`review/review-spa.png`.

---

## 3. User flows

### 3.1 Import an `.ibt` (review page)
1. User clicks **Import .ibt…** and picks a file (often 100-200 MB).
2. The browser POSTs the raw bytes to `/api/v1/import/upload?filename=<name>`. That
   returns a `job_id`.
3. The UI polls `GET /api/v1/import/{job_id}` every ~700 ms and shows
   `uploading <name>…` and then `importing <name>… 63%`. The Spa file imports in
   about 35 s.
4. On `status: "complete"` the session list reloads and the new session is selected.
   On `status: "error"` the UI shows `import failed: <error>`.

Design needs: a file picker, an upload/import progress state, and success and error states.

### 3.2 Record a run / replay a session
- **Review page, "Record a run from the telemetry".** This replays the stored
  telemetry through the engine at max speed, so the journal gets a run that the
  review can show. With agents off it is free: the whole Spa race takes about 32 s.
  Progress text: `recording a run… 74 541 / 151 892 frames`. When it finishes, the
  run list reloads and the new run is selected.
- **Live page, replay bar.** Pick a stored session and a speed (1×, 2×, 4×, 8×,
  max), then **Start** or **Stop**. The live page then behaves exactly as if the race
  were live. The bar shows when a replay is running or when no live feed is
  connected. While the live poller owns the engine it is greyed out, because a
  replay would be refused with a 409.
- Only one replay runs at a time. A second start returns
  `409 {"detail": "A replay is already running; stop it first."}`.

### 3.3 Turn the agents on (and the quota prompt)
- The agents are mounted only when the server was started with
  `RTV_PITWALL_AGENTS=true` and a Kimi Code API key. **The UI cannot mount them.**
  When they are not mounted, `GET /pitwall/status` returns `available: false` with a
  `reason`. The UI must say *why the radio is quiet* and point to the setting. It
  must not look broken.
- When they are mounted, the API offers a **global kill switch**
  (`POST /pitwall/enabled {"enabled": bool}`), **per-agent toggles**
  (`POST /pitwall/agents/{name}/enabled`) and **reset**
  (`POST /pitwall/reset`, which clears cooldowns and the radio). The current UI does
  not expose these. **The redesign should**: an "AI on/off" master control plus four
  agent toggles, all disabled with an explanation when `available: false`.
- **Quota prompt.** Anything that makes the agents run calls Kimi and spends the
  user's quota. Before recording a run with the agents mounted and enabled, the
  review must ask for confirmation:
  *"The pitwall agents are switched on, so replaying this session will call Kimi
  and use your quota. Continue?"* That is a `window.confirm` today. Design a proper
  modal for it. Also show the session budget from `/pitwall/status`
  (`calls_used` / `max_calls_per_session`, `calls_remaining`, `budget_exhausted`).

### 3.4 Scrub a recorded race (review)
- Pick a session, then a **pitwall run** (a session can have several runs: live
  ones and replays). The default run is the newest one in which the agents spoke.
- A timeline spans the whole session (session time in seconds). It has lanes for
  **laps, flags, race events, strategist, vehicle engineer, spotter, coach,
  radio**. Drag to scrub, click an item to jump to it, or press play at 1×, 4×, 16×
  or 60×.
- At the cursor the page shows: the flow graph and agent runs as they stood at that
  moment, the **car state at the cursor**
  (`GET /sessions/{id}/pitwall-log/state?t=`), the radio so far, and the race events
  so far.
- State lives in the URL: `?session=&run=&main=&ref=&t=`.

### 3.5 Compare laps
- Pick the **lap** under review and a **reference** lap. The defaults are a mid-race
  clean lap against the best clean lap. A lap table lists every lap with its time,
  delta to best, and flags (in/out lap, invalid).
- The page shows stacked traces against lap distance: **speed, throttle, brake,
  steering, gear**, with both laps overlaid, plus a **time-delta** trace (seconds
  gained or lost along the lap), plus a **track map** of the reference lap with
  every off-track, incident and lock-up of the race pinned on it.

### 3.6 Pick a corner
- The coaching findings table lists corners (T1…T8 on Spa with this detector), each
  with time lost or gained and each analyst's diagnosis. **Clicking a corner**
  highlights its entry-to-exit window on all traces and on the track map, and
  should scroll or zoom to it.

### 3.7 Driver message (live)
- A text box ("say something to the pitwall…") and a hold-to-talk mic button (Web
  Speech recognition, feature-detected). This POSTs to `/pitwall/driver-message`.
  The message joins the radio log as a `DRIVER` entry, and calls within 45 s of
  session time nest under it as **"after your message"**. Never label those
  "reply": the agents are not told to answer it. When agents are not mounted this
  returns 503 and the UI says nobody heard it.

### 3.8 Audio controls (live and radio page)
- **Enable audio** (browsers need one click before speech), a master radio on/off,
  volume, and a mute per agent. All of these live in the **URL**
  (`?volume=0.9&master=1&muted=spotter,coach`), never in storage.
- An **on-air strip** shows who is speaking now and what is queued.
- Radio history loaded on connect is shown but **not spoken**.

---

## 4. Every panel and the data it shows

Agent colours are part of the product language and are used everywhere (flow graph,
timeline lanes, radio, audit): strategist, vehicle engineer, spotter and coach. Today
they are purple `#a371f7`, blue `#2f81f7`, red `#f85149` and green `#3fb950`. You may
change them, but they must stay four clearly distinct colours. Severity has its own
three levels: **critical**, **advisory**, **info**.

### 4.1 Live pitwall (`/`)

Today's layout: a status strip on top; a left column (flow graph, timeline, agent
runs, raw telemetry); a right column (radio, strategy, timing tower); and an event
ticker along the bottom. You may change the layout. You may not drop any of the
content.

| # | Panel | Shows | Source |
|---|---|---|---|
| L1 | **Flag band** | Green = a hairline. Any other phase (yellow, red, white, checkered, blue…) = a full-width band in that colour with the phase written across it. Yellow and red pulse. | `state.flags.phase`, `.active[]` |
| L2 | **Status strip** | Session state + track name; **time or laps remaining** (big); track / air temp; **feed** pill (live · replay · disconnected · reconnecting) + detail; **pitwall AI** pill (off / on / calls used `24/400` / degraded) + detail | `state.session.*`, `state.conditions.*`, `/pitwall/health`, `/pitwall/status` |
| L3 | **Replay bar** | Session picker, speed, Start/Stop, progress `frames / total` | `/sessions`, `/replay/*`, `/pitwall/health.replay` |
| L4 | **Pitwall at work (flow graph)** | A fixed pipeline graph: `telemetry → engine → detectors → bus → orchestrator → {strategist, vehicle_engineer, spotter, coach} ↔ llm / tools → validator → radio → driver`, plus `radio → agents` edges (an agent hears earlier radio as context). **Every packet that moves is a real record**: no idle animation. Each node has a small sub-label (e.g. the agent's last outcome, "no field data (live only)" on the spotter for an `.ibt`). Legend: four agents, "radio heard as context", "failed / blocked". | `activity` frames, `state` ticks |
| L5 | **Timeline** | The last 2 minutes of session time, following "now". Lanes: race events, 4 agents, radio aired. Items are clickable. | `event`, `activity` frames |
| L6 | **Agent runs** | A waterfall of agent wake-ups: one row per run (agent, trigger, outcome), spans for each model call and tool call on that run's wall clock, the validator verdict. Click a row for detail: what it was shown (`context_radio`, `context_events`), the tools it called with arguments and result shape, the latency, and what aired. Header: run count and mean latency. | `activity` frames (`run_start` … `run_end`) |
| L7 | **Raw telemetry** | Rolling 20 s traces: speed (km/h), throttle %, brake %, rpm, gear, steering (deg). A channel missing from the session's catalog reads "not in this session", not a flat line. Shows the real frame rate. | `/ws/live` |
| R1 | **Radio** | The feed of calls, newest first: agent, priority, lap and time, `spoken_text`, expandable `detail_text` ("why"), tools used, grounded/refused markers, driver threading. The on-air strip, queue count, audio controls, driver input (3.7, 3.8). **Empty state:** "Nothing on the radio yet. Agents speak when `RTV_PITWALL_AGENTS=true` and `KIMI_CODE_API_KEY` is set; the deterministic ticker below runs regardless." | `radio` frames, `/pitwall/radio` |
| R2 | **Strategy** | **Recommendation card** (the latest strategist call: BOX NOW / STAY OUT …, target lap, fuel to add, rejoin position, confidence). **Pit-window bar** (lap axis with marks: current lap, window span earliest→latest (green when open), red "dry" edge where the tank empties, amber edge at the flag). **Fuel block**: margin in laps (big, banded green/amber/red) + litres; laps of fuel vs laps to finish; in tank + stint; per-lap with a **consumption sparkline** (one point per lap) and ± std. **Tyres**: a 2×2 grid LF/RF/LR/RR with temp and pressure and trend arrows; header "stint N laps". | `state.fuel.*`, `state.tyres.*`, `state.player.lap`, strategist `radio.data` |
| R3 | **Timing tower** | Position, car, gap, last lap, status (pit, closing/opening trend, blue flag); windowed to ±3 around the player with a "full field" toggle. **No-field state** (every `.ibt`): no column header and no toggle; "No field data in this recording"; still shows **the player's own row** (position, last lap) and a note that other cars only come from live telemetry. Before any session: "waiting for telemetry". | `state.standings.*`, `state.player.*`, `state.capabilities` |
| B1 | **Event ticker** | A strip of deterministic race events, newest first, e.g. `L12 27:42 pit window closing · latest lap 13`. Count of events. | `event` frames, `/racestate/events` |

### 4.2 Post-race review (`/review/`)

| # | Panel | Shows | Source |
|---|---|---|---|
| V1 | **Session bar** | Session picker (label, track, date), pitwall-run picker (source live/replay, start time, has agents, complete), **Record a run**, **Import .ibt**, status text | `/sessions`, `/sessions/{id}/pitwall-runs`, `/import/*`, `/replay/*` |
| V2 | **Race summary** | Stat cards: result (start P10 → finish P5), laps / clean laps, best lap (2:20.618, lap 6), median, spread (σ), pit stops, fuel used / added / per lap, incidents, radio calls (by agent, refused, ungrounded), agent runs, model calls, errors, latency. **Stints table**: stint, laps from-to, laps, clean, best, median, σ. Event counts. `notes[]`: why a figure is missing. | `/sessions/{id}/summary?run=` |
| V3 | **Replay** | Play/speed/clock; full-session **timeline** (lanes: laps, flags, race events, 4 agents, radio); the **flow graph** and **agent runs** reduced to the cursor; **car at the cursor** (state grid: lap, position, speed, gear, fuel, margin, tyres, flags, temps); **radio so far**; **race events so far** | `/sessions/{id}/pitwall-log`, `/pitwall-log/state?t=` |
| V4 | **Lap analysis** | Lap picker + reference picker + legend; **lap table**; stacked **traces** vs distance (speed, throttle, brake, steering, gear, plus the time delta); **track map** with event pins | `/sessions/{id}/laps`, `/sessions/{id}/compare`, `/coaching/delta`, `/sessions/{id}/trackmap`, journal events with `lap_dist_pct` |
| V5 | **Coaching findings** | **Chief summary**: lap delta (+0.381 s), time lost in corners (0.501 s), corners losing (6 of 8), clean-lap spread; top 3 opportunities ("T3 — Low minimum speed — 0.14 s"). **Corners table** (click to highlight): label, type (hairpin / slow corner …), sector, min speed main vs ref, net Δt, each analyst's diagnosis line. **Sectors**: S1/S2/S3 main vs ref and delta. **Driver inputs (KPIs)**: trail-brake %, avg throttle %, throttle smoothness, steering corrections, coasting %, main vs ref. Notes. Subtitle: "deterministic, from the telemetry (Layer 1)". | `/coaching/lap-findings` |
| V6 | **Agent audit** | One row per agent run: time, lap, agent, trigger, model, model calls, tools, validator verdict, outcome (spoke / refused / no output / error / unfinished), latency, what aired. Filters: agent, outcome. Sort: time, slowest, most calls. **Empty state for an agents-off run** (the common case today). | journal records via `review-model.js auditRows()` |

### 4.3 Radio page (`/radio.html`)
Radio feed, on-air strip, audio controls and driver input only (R1 on its own), for a
tablet or phone. Same data as R1.

---

## 5. Data contracts

Base URL `/api/v1`. All JSON. Errors are FastAPI-style `{"detail": "<message>"}`
with a 4xx/5xx status, and the UI shows the `detail` text. Numbers are SI unless the
field name says otherwise. Speeds are **m/s** (the UI converts to km/h), time is
**seconds** (lap times are formatted `m:ss.sss`), and fractions like `level_pct`,
`humidity` and `lap_dist_pct` are **0-1**.

### 5.1 Sessions and laps

`GET /sessions?limit=50`
```json
{"count": 1, "sessions": [{
  "session_id": "fa2f9980-69ef-4c1f-9725-782cc9ed1f9c", "kind": "ibt",
  "car_id": "176", "track_id": "163", "track_name": "Circuit de Spa-Francorchamps",
  "ir_session_id": 223920003, "ir_subsession_id": 65487918,
  "started_at": "2026-10-31T00:27:48.226127", "ended_at": "2026-10-31T00:28:21.000844",
  "sample_count": 151892, "poll_hz": null, "status": "ready", "label": "Spa race"}]}
```
Fresh install: `{"count": 0, "sessions": []}`, so the **empty state** must invite
an import. (`started_at` is the import time for an `.ibt`, not the race date.)

`GET /sessions/{id}/laps`. There are 19 rows; lap 0 is the pre-start and the last
one is the unfinished segment (both `is_valid: false`).
```json
{"session_id": "fa2f…", "count": 19, "laps": [
  {"lap": 0, "start_tick": 180599, "end_tick": 184140, "start_session_time": 22.68, "lap_time": 59.03, "is_valid": false, "is_out_lap": false, "is_in_lap": false},
  {"lap": 1, "start_tick": 184140, "end_tick": 192892, "start_session_time": 81.72, "lap_time": 149.213, "is_valid": true, "is_out_lap": false, "is_in_lap": false},
  {"lap": 6, "lap_time": 140.618, "is_valid": true, "…": "…"}]}
```

`GET /sessions/{id}/variables`: the channel catalog (275 channels for this car),
mostly for developers. Each entry: `{name, ir_type, json_type, count, unit, desc, storage, columns}`.

### 5.2 Import

| Call | Response |
|---|---|
| `POST /import/upload?filename=spa.ibt` (raw bytes body) | `{"job_id": "38bf…", "status": "pending"}`. 400 for a non-`.ibt` file or an empty body |
| `GET /import/{job_id}` | `{"job_id", "session_id", "source_path", "status": "pending"\|"running"\|"complete"\|"error", "progress": 0.0-1.0, "error": null, "created_at", "updated_at"}` |

### 5.3 Replay

`POST /replay/start {"session_id": "fa2f…", "speed": 0}` (0 = max speed, otherwise
N× real time)
```json
{"running": true, "session_id": "fa2f…", "speed": 0.0, "frames": 0, "total": 151892, "finished": false, "error": null}
```
`GET /replay/status` has the same shape (the done state is `running: false, finished: true, frames == total`).
`POST /replay/stop` returns the same. Conflicts are 409, with `detail` text for
"already running" and "live poller is running".

### 5.4 Live WebSocket `/ws/pitwall` (the live page's main feed)

The client sends `{"op": "subscribe", "rate_hz": 10}`. The server sends a `state`
frame immediately, then an `ack`, then four frame types:

| `type` | Delivery | Body |
|---|---|---|
| `state` | latest-wins at `rate_hz` (the UI renders ≤ 10 Hz) | `{"type": "state", "state": RaceState}` |
| `event` | pushed instantly, never dropped | `{"type": "event", "event": RaceEvent}` |
| `radio` | pushed instantly | `{"type": "radio", "message": RadioMessage}` |
| `activity` | pushed instantly | `{"type": "activity", "activity": ActivityRecord}` |
| `ack` / `error` | control | `{"type": "ack", "op": "subscribe", "rate_hz": 10.0}` |

The socket reconnects with backoff (500 ms up to 8 s), and the UI shows
"reconnecting".

**RaceState.** This is a real one from lap 9 at Spa, mid-stint, the pit window open,
fuel short:
```json
{"version": 74541, "tick": 255139, "session_time": 1265.03, "wall_time": null, "stale": false, "stale_reason": null,
 "session": {"session_id": "fa2f…", "source": "replay", "state": "racing", "time_remaining": 1213.28,
   "laps_remaining": null, "laps_total": null, "car_id": "176", "track_id": "163",
   "track_name": "Circuit de Spa-Francorchamps", "lap_length_m": 6930.0},
 "flags": {"raw": 268697600, "phase": "green", "active": ["servicible", "start_hidden"],
   "since_session_time": 573.62, "time_in_state": 691.42},
 "standings": {"cars": [], "player_idx": null, "gap_basis": null, "lap_length_m": 6930.0, "reference_lap_time": null},
 "player": {"lap": 9, "position": 5, "class_position": 5, "lap_dist_pct": 0.339, "speed": 44.68, "gear": 4,
   "rpm": 6332.1, "last_lap_time": 143.436, "best_lap_time": 140.618, "on_pit_road": false,
   "track_surface": "on_track", "incidents": 0, "stint": 1, "laps_on_tyres": 6, "stint_start_lap": null},
 "fuel": {"level": 19.04, "level_pct": 0.183, "capacity": 104.0, "per_lap": 3.9138, "per_lap_std": 0.0502,
   "samples": 5, "laps_remaining": 4.866, "laps_to_finish": 9.0, "fuel_to_finish": 35.224,
   "margin_l": -16.181, "margin_laps": -4.134, "pit_window_earliest_lap": 9, "pit_window_latest_lap": 13,
   "window_open": true},
 "tyres": {"temps": {"LF": 55.1, "RF": 55.5, "LR": 54.9, "RR": 55.1},
   "pressures": {"LF": 186.1, "RF": 186.3, "LR": 184.7, "RR": 185.2},
   "temp_trend": {"LF": 0.0, "RF": 0.0, "LR": 0.0, "RR": 0.0},
   "pressure_trend": {"LF": 2.76, "RF": 2.76, "LR": 2.48, "RR": 2.52}, "stint_laps": 9},
 "car_health": {"oil_temp": 95.3, "water_temp": 91.8, "oil_pressure": 5.25, "fuel_pressure": 5.83,
   "voltage": 13.4, "tow_time": 0.0, "engine_warnings": []},
 "conditions": {"air_temp": 25.26, "track_temp": 26.11, "air_density": 1.122, "air_pressure": 96650.1,
   "humidity": 0.458, "wind_vel": 0.894, "wind_dir": 6.283, "track_wetness": null,
   "air_temp_trend": -0.00431, "track_temp_trend": 0.0},
 "capabilities": {"session": true, "flags": true, "standings": false, "player": true, "fuel": true,
   "tyres": true, "car_health": true, "conditions": true, "pit": true, "lockup": true,
   "wheelspin": true, "offtrack": true, "incidents": true, "missing": ["CarIdxLapDistPct"]},
 "metrics": {"frames": 74541, "events": 38, "last_update_ms": 0.136, "avg_update_ms": 0.055,
   "max_update_ms": 5.77, "dropped_frames": 0}}
```
Notes for design:
- Before any session is bound, every field is `null`, `flags.phase` is
  `"unknown"`, and every `capabilities.*` is `false`. This is the "waiting for
  telemetry" state.
- `flags.phase` is one of `green, yellow, red, white, checkered, unknown` (plus
  caution variants in `active[]`, e.g. `yellow_waving`, `caution`, `blue`,
  `black`). On the Spa race it goes unknown → green at 59 s and later
  white → checkered.
- When `standings.cars` is populated (live only), each car has position, car
  number, gap, last lap, on-pit-road and class. The tower windows ±3 around
  `player_idx`. `gap_basis: null` means gaps cannot be computed: show `n/a` with one
  message at the top.
- A **negative** `margin_l` / `margin_laps` means you cannot finish without a stop.
  That is normal mid-race in a GT3 (the tank does ~5 laps of a 17-lap race). The
  colour bands are green ≥ +1 lap, amber 0 to +1, red < 0.

**RaceEvent.** The full Spa race produces 72 events of 18 types. These are real ones:
```json
{"event_type": "pit_window_open", "tick": 193018, "session_time": 229.68, "lap": 2, "severity": "advisory",
 "payload": {"lap": 2, "earliest_lap": 2, "latest_lap": 14, "laps_remaining": 12.918, "per_lap": 3.6882},
 "state_version": 12420, "key": "pit_window_open"}
{"event_type": "offtrack", "session_time": 367.72, "lap": 2, "severity": "advisory",
 "payload": {"surface": "off_track", "from": "on_track", "lap_dist_pct": 0.979, "speed": 27.74}, "key": "offtrack"}
{"event_type": "recurring_issue", "session_time": 1006.5, "lap": 7, "severity": "advisory",
 "payload": {"issue": "offtrack", "corner": "C09", "occurrences": 3, "threshold": 3, "window_laps": 5,
   "laps": [5, 6, 7], "lap_dist_pct_from": 0.4801, "lap_dist_pct_to": 0.4809, "mean_speed": 42.894}, "key": "recurring_issue"}
{"event_type": "incident", "session_time": 1573.28, "lap": 11, "severity": "critical",
 "payload": {"incidents": 1, "delta": 1, "lap_dist_pct": 0.483}, "key": "incident"}
{"event_type": "fuel_critical", "session_time": 1731.68, "lap": 12, "severity": "critical",
 "payload": {"lap": 12, "level": 5.95, "laps_remaining": 1.497}, "key": "fuel_critical"}
{"event_type": "pit_entry", "session_time": 1787.62, "lap": 12, "severity": "info", "payload": {"lap": 12, "stint": 1, "fuel": 4.46}}
{"event_type": "pit_exit", "session_time": 1821.62, "lap": 13, "severity": "info", "payload": {"lap": 13, "stint": 2, "fuel": 24.96}}
{"event_type": "tyre_out_of_band", "session_time": 371.55, "lap": 3, "severity": "advisory",
 "payload": {"measure": "tyre_pressure_trend", "corner": "RF", "value": 4.184, "threshold": 2.0, "unit": "kPa/lap", "…": "…"}}
{"event_type": "flag_change", "session_time": 59.33, "lap": 0, "severity": "info",
 "payload": {"from": "unknown", "to": "green", "active": ["one_lap_to_green", "green_held", "servicible", "start_ready"]}, "key": "flag_change:green"}
```
Counts on Spa: lap_completed 18, offtrack 13, flag_change 7, wheelspin 6,
personal_best 4, session_fastest_lap 4, incident 4, pit_window_open 2,
fuel_margin_low 2, recurring_issue 2, pit_window_closing 2, fuel_critical 2,
car_health_warning 1, tyre_out_of_band 1, stint_lap_milestone 1, pit_entry 1,
pit_exit 1, stint_start 1. The other types can fire too, mostly in live sessions:
`lockup`, `traffic_close`, `blue_flag`, `rival_pitted`, `session_state_change`,
`weather_change` and `regulation_change`.
Design the ticker and timeline for **any** `event_type` string, using its severity
for colour.

`GET /racestate` returns the current RaceState. `GET /racestate/events?limit=60`
returns `{"count", "events": [RaceEvent…]}` (prefills the ticker on load).

### 5.5 Raw telemetry WebSocket `/ws/live`

The client sends `{"op": "subscribe", "channels": ["Speed","Throttle","Brake","RPM","Gear","SteeringWheelAngle"], "rate_hz": 20}`.
```json
{"type": "state", "connection": "replay", "session_id": "fa2f…"}
{"type": "catalog", "schema_hash": "857d1904e7e65af4", "source": "ibt", "car_id": "176", "track_id": "163",
 "count": 275, "variables": [{"name": "Speed", "unit": "m/s", "desc": "GPS vehicle speed", "…": "…"}]}
{"type": "data", "tick": 193628, "t": 239.85,
 "v": {"Gear": 2, "RPM": 7645.1, "Throttle": 1.0, "Brake": 0.0, "SteeringWheelAngle": -0.137, "Speed": 36.50},
 "flags": {"SessionFlags": {"green": false, "yellow": false, "servicible": true, "start_hidden": true, "…": false}}}
```
`connection` is one of `disconnected, connected, in_session, replay`. "Connecting"
and "reconnecting" are client-side states of the socket itself.
Throttle and brake are 0-1, steering is in radians, and speed is in m/s.

### 5.6 Agents (`/pitwall/*`)

**Agents not mounted** (today's default; every response below is real):
```json
GET /pitwall/status → 200 {"available": false,
  "reason": "Agent layer not mounted (RTV_PITWALL_AGENTS, the 'ai' extra or KIMI_CODE_API_KEY).",
  "known_agents": ["strategist", "vehicle_engineer", "spotter", "coach"]}
GET /pitwall/radio, POST /pitwall/enabled, POST /pitwall/driver-message → 503
  {"detail": "The pitwall agent layer is not running. It needs RTV_PITWALL_AGENTS=true, the \"ai\" extra and KIMI_CODE_API_KEY."}
GET /pitwall/activity → 200 {"count": 0, "last_seq": 0, "agents_mounted": false, "stats": {…}, "records": []}
```

**Agents mounted.** The shapes are exact, from an offline scripted run. The text,
the `data` values and the latencies are illustrative (a real model call takes 1-5 s).

`GET /pitwall/status`
```json
{"available": true, "enabled": true, "running": true, "healthy": true, "suspended": false, "suspend_reason": null,
 "calls_used": 2, "calls_remaining": 398, "max_calls_per_session": 400, "budget_exhausted": false,
 "radio": {"published": 1, "emitted": 1, "superseded": 0, "pending": 0, "history": 1},
 "agents": [{"name": "strategist", "model": "kimi-k3", "enabled": true, "consecutive_failures": 0,
   "disabled_reason": null, "degraded": false, "invocations": 1, "llm_calls": 2, "refusals": 0, "errors": 0,
   "last_latency_ms": 2140, "avg_latency_ms": 2140,
   "tools": ["get_race_state_slice", "get_fuel_projection", "get_tyre_trend", "get_standings_around_player", "get_stint_history", "simulate_pit_outcome"],
   "triggers": [{"event_type": "pit_window_open", "label": "pit window opens", "cooldown_s": 0.0}, "…"]}, "…"]}
```
`disabled_reason` set means a circuit breaker tripped (show it as degraded).
`budget_exhausted: true` means no more calls this session (show it loudly).

**RadioMessage** (`radio` frame, `GET /pitwall/radio` → `{"count", "messages": [...], "pending": [...]}`):
```json
{"message_id": "7f5f41b53d64c9e7", "agent": "strategist", "priority": "advisory",
 "spoken_text": "Box this lap, window closes after lap 13.",
 "detail_text": "Longer reasoning for the screen; every figure is cited.",
 "data": {"recommendation": "box_now", "target_lap": 12, "fuel_to_add_l": 21.0, "rejoin_position": 6,
          "confidence": "high", "rationale": "…", "risks": []},
 "event_ref": {"event_type": "pit_window_closing", "key": "pit_window_closing", "tick": 278998, "session_time": 1662.68, "lap": 12},
 "session_time": 1662.68, "timestamp": null, "subject": "pit_stop", "seq": 1,
 "grounded": true, "refused": false, "ungrounded": [], "tools_used": ["get_fuel_projection"],
 "model": "kimi-k3", "speak": true, "audio_url": null}
```
- strategist `recommendation` is one of `box_now, box_next_lap, pit_under_yellow, extend, stay_out, hold`.
- `priority`: `critical | advisory | info`. Critical pre-empts the audio queue.
- `refused: true` is a *grounded refusal*: the agent declined rather than invent a
  number. Show it as such, not as an error.
- `grounded: false` + `ungrounded[]` lists the figures the validator could not back.
- `agent: "driver"`, `speak: false` is the driver's own message.
- `data` depends on the agent:
  - strategist `PitCall`: `recommendation, target_lap, fuel_to_add_l, rejoin_position, confidence, rationale, risks[]`
  - vehicle engineer `EngineerCall`: `finding, corner, affected[]…`
  - spotter `SpotterCall`: `threat, side (ahead/behind/unknown), car_idx, action (hold_line/let_by/lift/defend/none), confidence`
  - coach `CoachCall`: `theme, corner, cue, from_telemetry, confidence, rationale`

**ActivityRecord** (`activity` frame, `GET /pitwall/activity?since=&limit=`, and
every journal line). Envelope:
`{seq, wall_ms, session_time, tick, kind, agent, run_id, event_key, data}`.
One complete strategist run, in order:
```json
{"seq": 1, "kind": "dispatch", "agent": null, "event_key": "pit_window_closing", "data": {"event_type": "pit_window_closing", "severity": "advisory", "accepted": ["strategist"], "skipped": {}, "blocked": null}}
{"seq": 2, "kind": "run_start", "agent": "strategist", "run_id": "strategist-1", "data": {"trigger": "pit_window_closing", "severity": "advisory", "model": "kimi-k3", "context_radio": [], "context_events": 19}}
{"seq": 3, "kind": "llm_call_start", "agent": "strategist", "run_id": "strategist-1", "data": {"call": 1, "model": "kimi-k3", "tools_offered": 6, "messages": 1}}
{"seq": 4, "kind": "llm_call_end", "agent": "strategist", "run_id": "strategist-1", "data": {"call": 1, "ok": true, "error": null, "latency_ms": 1840, "tool_calls": ["get_fuel_projection"], "answered": false}}
{"seq": 5, "kind": "tool_call", "agent": "strategist", "run_id": "strategist-1", "data": {"tool": "get_fuel_projection", "args": {}, "ok": true, "result": {"type": "object", "keys": ["level_l", "per_lap_l", "margin_laps", "…"], "size": 14}, "latency_ms": 0.07}}
{"seq": 6, "kind": "llm_call_start", "…": "call 2"}
{"seq": 7, "kind": "llm_call_end", "data": {"call": 2, "ok": true, "latency_ms": 900, "tool_calls": [], "answered": true}}
{"seq": 8, "kind": "validate", "agent": "strategist", "data": {"ok": true, "unsupported": [], "word_count": 8, "over_word_limit": false, "repaired": false}}
{"seq": 9, "kind": "run_end", "agent": "strategist", "data": {"outcome": "radio", "message_id": "7f5f41b53d64c9e7", "priority": "advisory", "tools_used": ["get_fuel_projection"], "turns": 2, "latency_ms": 2760}}
{"seq": 10, "kind": "radio_queued", "agent": "strategist", "data": {"message_id": "7f5f41b53d64c9e7", "seq": 1, "priority": "advisory", "subject": "pit_stop", "refused": false}}
{"seq": 11, "kind": "radio_aired", "agent": "strategist", "data": {"message_id": "7f5f41b53d64c9e7", "seq": 1, "priority": "advisory", "subject": "pit_stop", "refused": false}}
```
Other kinds: `inbox_superseded` (a newer event replaced a queued one) and
`radio_superseded` (a newer call on the same subject replaced a queued one).
`dispatch.skipped` maps agent → `cooldown` / `disabled`. `dispatch.blocked` is
`disabled | suspended | budget_exhausted | stale`. `run_end.outcome` is
`radio | refused | error`. `context_radio` lists the earlier radio messages (ids and
agents) this run was shown, and is drawn as radio → agent edges. Real model latency
is 1-5 s per call. The waterfall should make that legible.

`GET /pitwall/health`: one operator answer, `{ok, pitwall, engine: {frames, events, faults, seconds_since_frame, …}, live: {running, state, session_id}, replay: {…}, agents: {mounted, enabled, healthy, degraded[]}}`.
`ok: false` means something should be producing frames and isn't, or a breaker
tripped.

`GET /pitwall/tts`: `{provider: "none", available: false, reason, voices: {strategist: {lang, rate, pitch, label: "Strategist", browser_hints}, …}}`.
Speech is browser Web Speech unless a TTS provider is configured. If the browser
has none, show "This browser has no Web Speech API; radio is shown, not spoken."

### 5.7 Race journal (review)

`GET /sessions/{id}/pitwall-runs`
```json
{"session_id": "fa2f…", "default_run": "20261031T004541Z-01-replay", "recording": null,
 "runs": [{"run_id": "20261031T004541Z-01-replay", "source": "replay",
   "started_wall_ms": 1793407541718, "ended_wall_ms": 1793407574113, "complete": true,
   "session_time_start": 22.68, "session_time_end": 2554.22,
   "counts": {"lifecycle": 2, "race_event": 72, "state": 2512},
   "agents": [], "has_agents": false, "meta": {"agents": [], "agents_enabled": false}}]}
```
If there are no runs yet (`runs: []`, `default_run: null`), the review must offer
**Record a run**. `recording` is set while a run is being written.

`GET /sessions/{id}/pitwall-log?run=&kinds=&exclude=state&agent=&from=&to=&limit=`
returns `{"session_id", "run_id", "count", "truncated", "records": [ActivityRecord-shaped…]}`.
Besides the agent kinds above, it carries `lifecycle`
(`data: {phase: "start"|"end", source, session_id, agents, agents_enabled, reason?}`),
`race_event` (`data` = RaceEvent) and `radio` (`data` = RadioMessage).

`GET /sessions/{id}/pitwall-log/state?t=1200` returns
`{"session_id", "run_id", "record": {"kind": "state", "session_time": 1199.98, "data": RaceState}}`.
At t = 1200 s on Spa that is lap 8, P6, 21.0 L in the tank, the window open from lap 8 to 13.

`GET /sessions/{id}/summary?run=` (real):
```json
{"laps": 17, "clean_laps": 15, "best_lap": 6, "best_time": 140.618, "median_time": 141.030, "stdev_time": 2.219,
 "pit_stops": 1, "start_position": 10, "finish_position": 5, "incidents": 4,
 "fuel_used_l": 67.86, "fuel_added_l": 20.99, "fuel_per_lap_l": 3.992,
 "stints": [{"stint": 1, "from_lap": 1, "to_lap": 12, "laps": 12, "clean_laps": 11, "best_lap": 6, "best_time": 140.618, "median_time": 141.754, "stdev_time": 2.496},
            {"stint": 2, "from_lap": 13, "to_lap": 17, "laps": 5, "clean_laps": 4, "best_lap": 17, "best_time": 140.722, "median_time": 140.948, "stdev_time": 1.074}],
 "events": {"lap_completed": 18, "offtrack": 13, "…": "…"},
 "radio_calls": 0, "radio_by_agent": {}, "radio_refused": 0, "radio_ungrounded": 0,
 "agent_runs": 0, "agent_outcomes": {}, "model_calls": 0, "model_errors": 0, "model_latency_ms": null, "tool_calls": 0,
 "notes": []}
```
Any figure can be `null` when the session did not record it, and then `notes[]` says why.

### 5.8 Lap analysis and coaching

`GET /sessions/{id}/compare?name=Speed&laps=9,6&grid=1000`
```json
{"session_id": "fa2f…", "channel": "Speed", "x": "lap_dist_pct", "grid": 1000,
 "x_values": [0.0, 0.001, 0.002, "… 1000 points"],
 "laps": {"9": [45.55, 46.06, 46.58, "…"], "6": ["…"]}}
```
The review fetches Speed, Throttle, Brake, SteeringWheelAngle and Gear. Convert x
to metres with `lap_length_m` (6929 m).

`GET /coaching/delta?session_id=&main_lap=9&ref_lap=6&points=500`
returns `{"main_lap": 9, "ref_lap": 6, "lap_length_m": 6929.03, "distance_m": [2.49, 16.28, …], "delta_s": [0.0, 0.0011, …]}`.
A positive value means the main lap is slower at that point.

`GET /sessions/{id}/trackmap?lap=6&max_points=1500`
returns `{"lap": 6, "color": null, "count": 1407, "points": [{"lat": 50.44314, "lon": 5.96603, "lap_dist_pct": 0.00034}, …]}`.
Project lat/lon to the canvas, keeping the aspect ratio. Event pins are race events
with a `payload.lap_dist_pct` (offtrack, incident, lockup, wheelspin).

`GET /coaching/lap-findings?session_id=&main_lap=9&ref_lap=6` (real, trimmed):
```json
{"track_name": "Circuit de Spa-Francorchamps", "main_lap": 9, "ref_lap": 6,
 "main_time": 140.999, "ref_time": 140.618, "lap_length_m": 6929.03,
 "chief": {"lost": 0.501, "net_lap": 0.381, "losing_n": 6, "total": 8, "time_spread": 8.595,
   "top3": [{"index": 2, "label": "T3", "gain": 0.143, "why": "Low minimum speed"},
            {"index": 3, "label": "T4", "gain": 0.125, "why": "Min speed -7 km/h vs reference"},
            {"index": 6, "label": "T7", "gain": 0.085, "why": "Full throttle 37 m later than ref"}]},
 "corners": [{"index": 1, "label": "T2", "distance": 2474.4, "sector": 2, "type": "Slow corner",
   "min_main": 107.84, "min_ref": 105.12, "net_dt": 0.035, "entry_m": 2331.2, "exit_m": 2650.1,
   "diags": [{"agent": "speed", "text": "Min speed +3 km/h vs reference", "good": true, "time_loss": 0.0, "lockup": false, "variance": false},
             {"agent": "brake", "text": "Brake point 9 m late", "good": false, "time_loss": 0.029},
             {"agent": "grip", "text": "Peak 1.66g lateral - ref held 1.89g", "good": false, "time_loss": 0.007}]}, "…"],
 "sectors": [{"index": 0, "name": "S1", "main": 41.942, "ref": 41.626, "delta": 0.316, "start_m": 0.0, "end_m": 2309.7}, "…"],
 "kpis": {"main": {"trail_brake_pct": 42.5, "avg_throttle_pct": 80.2, "throttle_smoothness": 80.6, "steer_corrections": 17, "coasting_pct": 1.2},
          "ref":  {"trail_brake_pct": 38.5, "avg_throttle_pct": 79.4, "throttle_smoothness": 79.4, "steer_corrections": 15, "coasting_pct": 0.87}},
 "agent_stats": {"txt": {"count": 6, "time": 0.501}, "brake": {"count": 5, "time": 0.084}, "…": "…"},
 "notes": []}
```
`min_main` / `min_ref` are km/h. A corner can have zero diagnoses (`diags: []`).
The analyst roster comes from `GET /coaching/agents`:
`[{id: "txt", name: "Turn-by-Turn", code: "TXT", watch: "Corner-by-corner walkthrough"}, {id: "sector", code: "SEC"}, {id: "brake", code: "BRK"}, {id: "speed", code: "SPD"}, …]`,
plus throttle, gear, grip and consistency. Give each analyst a small code badge.
Bad laps give errors like `404 {"detail": "'Session … has no lap 999'"}`.

---

## 6. States every design must cover

| State | When | Live page | Review page |
|---|---|---|---|
| **Empty store** | fresh install, no sessions | replay bar: "Pick a recorded session (import an .ibt first)"; everything `n/a` / "waiting for telemetry" | "No sessions yet" with a prominent **Import .ibt** |
| **Waiting for telemetry** | socket open, no session bound | flag band neutral, feed pill `disconnected`, tower "waiting for telemetry", flow graph idle | n/a |
| **Loading** | any fetch, a socket connecting, an import, recording a run | feed pill `connecting…` | per-panel loading text; import/record progress with % or frames |
| **Agents off** (default) | `/pitwall/status.available == false` | AI pill "off" + reason; radio empty-state copy; flow graph agents dimmed; runs empty | audit empty: "This run was recorded with the agents off"; summary agent figures 0 |
| **Agents on, idle** | mounted, nothing triggered | AI pill "on · 0/400 calls" | n/a |
| **Agent degraded** | `agents[].disabled_reason` / `degraded` | that agent greyed out with its reason | audit shows `error` outcomes |
| **Budget exhausted** | `budget_exhausted` | a loud banner: no more calls this session | n/a |
| **Quota prompt** | starting a run while agents are mounted | n/a (live replays with agents on also spend quota, so ask there too) | a confirm modal before **Record a run** |
| **No field data** | every `.ibt` (`capabilities.session && !capabilities.standings`) | the tower's no-field state (4.1 R3); spotter node "no field data (live only)" | the car-at-cursor panel has no gaps or standings |
| **Replay running** | `/replay/status.running` | feed pill `replay`, a progress bar, Stop | "recording a run… N / M frames" |
| **Socket lost** | WS close | feed pill `reconnecting`, data greys or freezes (never zeros) | n/a |
| **Error** | 4xx/5xx | a notice bar showing the server's `detail` text (`#app-notice`) | the same, plus per-panel errors (findings can fail on a bad lap pair) |
| **Caution flag** | `flags.phase != green` | full-width band, pulsing for yellow/red | a flags lane on the timeline |
| **Stale** | `state.stale == true` | mark the numbers stale with `stale_reason` | n/a |

---

## 7. Real-data limits (design for them; don't paper over them)

- **`.ibt` files have no other cars.** iRacing's `.ibt` recordings do not carry the
  `CarIdx*` channels, so a replayed file has **no standings, no gaps, no rivals and
  no spotter traffic calls**. Only live telemetry has the field. The player's own
  position (`PlayerCarPosition`) is available.
- **Lap 0 and the last lap are not real laps** (pre-start and unfinished). They are
  `is_valid: false`, and lap pickers should mark them.
- **Every number goes through `n/a` when null.** No panel has a zero default.
  `last_lap_time == 0` means "not set yet", not 0.000. Missing tyre channels: all
  four corners read `n/a` with "no tyre channels". Missing fuel: the pit-window bar
  draws its axis and says "no fuel model — pit window unavailable".
- **No fake data, ever.** No demo session, no simulated race, no lorem ipsum in the
  shipped UI. Your mockups should use the values in this brief. The shipped HTML
  must contain no hard-coded data values: those come from the API.
- **Agents speak rarely.** In a 40-minute race expect tens of calls, not hundreds.
  The radio list must look good with 0, 3 and 60 entries.
- **The agents never talk to each other directly.** They only hear earlier radio as
  context. Don't draw agent-to-agent arrows.
- **Every coaching number is deterministic** (Layer 1). The UI renders
  `/coaching/lap-findings` as it is and never recomputes it.
- **No driver voice routing.** Driver messages are logged, not answered.

---

## 8. Scale and density (Spa fixture)

- 151 892 frames, 275 channels, 60 Hz, 19 laps of about 2:21, lap length 6 929 m.
- The journal for one agents-off run holds 2 512 state snapshots, 72 race events and
  2 lifecycle records (6.5 MB). An agents-on run adds roughly 10 activity records per
  agent run.
- Traces: 1 000 points per lap per channel. Delta: 500 points. Track map: ~1 400 points.
- The live socket sends state at 10 Hz. Render at ≤ 10 Hz.

---

## 9. Integration constraints: what the delivered files must look like

The app is a **FastAPI backend that serves `frontend/` as static files at `/`**. There
is no bundler and no Node toolchain at runtime. The offline test suite reads the
frontend files and enforces the rules below, so files that break them fail CI.

**Stack**
- Plain **HTML5 + CSS + vanilla ES modules**. No React/Vue/Svelte, no Tailwind
  build, no `package.json`, no `node_modules`, no bundler config, no `require()`.
- **No CDNs** (`unpkg`, `jsdelivr`, Google Fonts links …). The app runs offline at a
  race. Fonts must be system stacks or font files committed under
  `frontend/fonts/`. Icons: inline SVG or committed SVG files.
- Charts: `<canvas>` 2D or inline SVG drawn by our own code. No chart library.

**File layout** (keep these paths; add new ones freely)
```
frontend/
  index.html              live pitwall        → served at /
  review/index.html       post-race review    → served at /review/
  radio.html              radio-only page
  css/pitwall.css         design tokens (:root custom properties) — single source of the palette
  css/app.css, live.css, review.css   page/layout styles (rename or split if you like)
  fonts/, img/            (new, optional) self-hosted assets
  js/…                    our code — see "what we keep" below
```
- Use **absolute paths** for assets: `/css/x.css`, `/js/x.js`, `/fonts/x.woff2`.
- Put all colours, spacing, radii and type sizes in **CSS custom properties** on
  `:root` in one tokens file. The JS reads some of them (`cssVar('--strategist')`)
  to colour canvases, so keep the agent and severity tokens named:
  `--strategist --engineer --spotter --coach --critical --advisory --info` (and
  `--bg --panel --line --ink --muted --accent`). Add more as you like.

**Markup hooks.** The JS finds elements by `id`, `data-role` and `data-field`. The
tests check that every hook a script queries exists on its page. The easiest path:
- Keep the existing hooks on the equivalent elements in your markup. They are listed
  in the current `frontend/index.html` and `frontend/review/index.html`, e.g.
  `#status-strip`, `[data-field="remaining"]`, `[data-role="pit-window"]` (a
  `<canvas>`), `#replay-start`, `#radio-enable`, `#driver-text`,
  `[data-role="tower-body"]`, `#session-select`, `#ibt-file`, `[data-role="traces"]`.
- Or rename freely and **give us a mapping table** (old hook → new element). Either
  works. Don't drop content silently.
- For repeating items (radio entry, tower row, run row, ticker item, corner row,
  audit row, stat card), deliver **one static example** of each with the real values
  from this brief, plus every variant (critical/advisory/info, refused,
  ungrounded, driver message with a nested "after your message", tower player row,
  closing/opening trend, no-field). Mark each with a comment
  `<!-- template: radio-entry -->`. We turn them into `el()` builders. Dynamic text
  is always set with `textContent`, never `innerHTML`, so a template must not rely
  on HTML inside a text value.
- Canvases and SVGs that we draw (pit-window bar, fuel sparkline, traces, track map,
  flow graph, timeline, telemetry strip) should be delivered as a **styled
  container plus a static picture of the intended drawing** (SVG or PNG mock), with
  the sizes, colours and line weights annotated. We port the drawing code.

**What we keep (don't redesign the logic, only how it looks)**
- Pure modules, tested with node, that hold the meaning of every number:
  `js/pitwall/format.js` (all formatting and `n/a`), `js/pitwall/view-model.js`
  (state → panel view), `js/shared/activity-model.js` (agent activity reducer),
  `js/review/review-model.js`, `js/audio-manager.js`, `js/speech.js`.
- The panel classes (`js/pitwall/*-panel.js`, `js/live/*.js`, `js/shared/timeline.js`,
  `js/shared/flow-graph.js`, `js/review/charts.js`) are the **paint layer**. We
  rewrite them to the new markup.
- If you write JS for pure presentation (a toggle, a tab, an accordion), make it a
  small ES module with no data in it. We'll merge it.

**House rules the tests enforce**
- No `localStorage`, `sessionStorage` or `indexedDB`. Page state lives in the URL.
- Pointer events only (`pointerdown`, not `mousedown`). **iPadOS Safari is a
  target**: touch scrubbing on the timeline, tap targets ≥ 32 px, nothing that needs
  hover.
- No `innerHTML` for anything that came from the server.
- No hard-coded data and no synthetic race in the shipped pages.
- Self-test pages (`pitwall-test.html`, `audio-test.html`) stay as they are.

**Responsiveness**
- Live: designed for a 1920×1080 second monitor first, and must also work on an
  iPad landscape (1180×820) and at 1366×768. The radio page should work on a phone.
- Review: desktop first (1440 wide), with a usable iPad layout.
- A dark theme is required. A light theme for the review is optional.

**Accessibility**
- Severity and flags must not rely on colour alone (add an icon or a word).
- WCAG AA contrast for text. Visible focus rings. All controls have labels
  (`aria-label` where there's no visible text).
- Respect `prefers-reduced-motion` (no pulsing; packets don't animate).

---

## 10. Deliverables checklist (for Claude Design)

1. `frontend/index.html`, `frontend/review/index.html` and `frontend/radio.html`,
   static, with the real example values from this brief.
2. The CSS files, with a **tokens file** of `:root` custom properties.
3. Static examples of every repeating item and variant (section 9), marked with
   `<!-- template: … -->`.
4. A mock of each canvas or SVG drawing with annotations.
5. Every state in section 6 shown somewhere: either as extra static pages
   (`states/live-agents-off.html`, …) or as a screenshot set.
6. A hook mapping table (old `id` / `data-role` / `data-field` → new) if you renamed
   anything.
7. Any fonts or icons as committed files, with their licences.

Hand these back as files. We'll drop them into `frontend/`, rewire the paint
layer, and keep the offline test suite green.
