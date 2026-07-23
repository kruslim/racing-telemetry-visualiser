# Racing Telemetry Visualiser

**A full-stack iRacing telemetry platform: real-time ingest → columnar storage → a MoTeC-i2-style web analysis UI → a tiered AI race engineer → a live multi-agent pitwall with a voice.**

**▶ [Live showcase](https://kruslim.github.io/racing-telemetry-visualiser/)**

Racing Telemetry Visualiser (RTV) captures *every* channel iRacing exposes — from
the live 60 Hz shared-memory feed or from recorded `.ibt` files — stores it
columnar in DuckDB/Parquet, and serves it three ways:

- a **REST API** for charts, lap comparison and track maps,
- a **WebSocket stream** for live dashboards,
- a **three-layer AI coaching system** that turns a million raw points per lap into
  a prioritised, fact-checked driving plan, and
- a **live multi-agent pitwall** — a deterministic race-state engine with four
  event-driven agents (strategist, vehicle engineer, spotter, coach) merging onto
  one prioritised radio channel that the browser speaks out loud.

It is deliberately built as an end-to-end system: catalog → ingest → storage →
query → visualisation → AI. Each layer is independently testable and the whole
suite runs offline with no game and no API key.

---

## Why this exists

Most sim-telemetry tools stop at either *storage* (a database of laps) or
*chat* (an MCP server that answers questions). RTV does both and adds the piece
that's usually missing: a real analysis frontend **and** an AI coaching pipeline
that is grounded against deterministic ground truth so it can't hallucinate lap
advice.

The design goal was to build the pieces properly rather than widely — runtime
schema discovery, latest-frame-only streaming, distance-aligned lap compare,
and an evals harness that checks the LLM's numbers against the physics.

---

## Feature tour

### 1. Runtime variable catalog — nothing hard-coded
The channel list is **read from the binary telemetry header at runtime**, so the
catalog is complete for *any* car or content iRacing loads — no per-car mapping to
maintain. `GET /api/v1/variables` is the frontend's contract: every variable with
its type, unit, description, array shape, and (for bitfields/enums) decode tables.
Six wire types (char/bool/int/bitfield/float/double); arrays ≤6 are flattened to
`Name_0..n`, per-car-index arrays (64) are stored as DuckDB `LIST` columns.

### 2. Dual ingest — live and offline
- **Live** — reads the iRacing shared-memory feed at `RTV_POLL_HZ` (default 60 Hz)
  while iRacing runs (`POST /api/v1/live/start`).
- **`.ibt` files** — parsed offline in tick-window chunks; works on any OS
  (`POST /api/v1/import`), with async import jobs and status polling.

### 3. Columnar storage tuned for chart queries
Metadata (sessions, laps, catalog, session-info snapshots, import jobs) lives in
DuckDB native tables. High-rate telemetry is written as **per-session/per-lap
Parquet** — `session_id=<uuid>/lap=<n>/part-*.parquet` — so every chart query
prunes to one or two small files instead of scanning a session.

### 4. Live streaming that respects slow clients
`/ws/live` delivers **latest-frame-only** at each client's requested rate (capped
at the poll rate). Slow clients coalesce to the newest sample instead of building
an ever-growing backlog — no unbounded buffering, no lag death-spiral.

### 5. A MoTeC-i2-style web frontend
A separately-built vanilla-JS canvas UI (no build step) reads the REST API and
renders: multi-channel time/distance worksheets, **GPS track maps** (with a
heading-integration fallback for live sessions that lack GPS), histograms,
suspension and track-report worksheets, a live **Pitwall** console, and a
**Coach** worksheet. It also supports **alien-lap cross-session compare** — merge
another driver's clean laps into your lap list (same track) and set any as
Main/Reference against your own.

### 6. Three-layer AI coaching
The centrepiece. See below.

### 7. A live multi-agent pitwall
A deterministic race-state engine maintains gaps, fuel consumption, tyre trends and
pit-window bounds at 60 Hz with **no LLM anywhere** (avg 0.03 ms/frame), and emits
typed race events. Four agents — strategist, vehicle engineer, spotter and live
coach — are woken **only** when an event fires one of their triggers, so a race
costs a handful of model calls rather than a per-tick bill. Aggregation happens
first and deterministically: one lockup is a driver having a moment, so nobody is
woken until the *same* one has happened at the *same* corner three times inside
five laps. Every figure an agent says is checked in-loop against
the data it was actually shown; one that can't be traced becomes a grounded refusal
rather than a confident guess. Output merges into a single prioritised radio feed
where critical calls pre-empt and stale advice supersedes.

That feed has a **voice**: the server ships a small vanilla-JS page (`frontend/`,
served at `/radio.html`) that speaks the radio through the browser's Web Speech
API — a distinct voice per role, a radio click ahead of critical calls, a
critical spotter shout that cancels an advisory mid-sentence, per-agent mutes,
and push-to-talk back to the pitwall where the browser supports it. Zero cost and
no setup; an optional backend TTS provider is one env var away. The queue rules
are a pure module with their own test suite (`frontend/audio-test.html`, or
`node frontend/js/run-audio-tests.mjs`). See `docs/PITWALL.md`.

It also has a **screen**. The server serves a live pit stand at `/` (still vanilla
JS + canvas, still no build step): a scrolling radio feed with per-agent mutes and
driver push-to-talk, a pit-window lap axis with the window, the fuel-limit lap and
the finish drawn on it, a 2×2 tyre widget with trend arrows, a timing tower around
the player with closing/opening gap arrows and blue-flag badges, a flag band that
takes the full width the moment it stops being green, and a ticker of raw
deterministic events beneath the AI radio so the two are never confused. Anything
this session's catalog cannot back reads **`n/a`**, never `0.0`. The header
switches between **Analysis** and **Pitwall**; the replay controls double as a
zero-setup demo (`scenario`, no key, no iRacing). Its view model is a pure module
with its own suite (`frontend/pitwall-test.html`, or
`node frontend/js/run-pitwall-tests.mjs`).

It is also built to survive a race rather than merely start one: a frame that
raises costs a frame and is counted, never the pitwall; the agent workers and the
event pump run under a supervisor that restarts them and *says* it did; an agent
whose provider is dead is taken off the air after three consecutive failures
instead of billing for a doomed call per race event; and a replay cannot be
started on top of a live session. `GET /api/v1/pitwall/health` answers "is
anything still watching?" in one payload — a question `/racestate` structurally
cannot answer, because a race state full of `null` looks the same whether the
session has not started or frames stopped arriving four minutes ago.

### 8. A race director — *planned*
`src/rtv/director/` ships the **interfaces only**: a validated `ScenarioScript`
schema (timed and conditional injections — a full-course yellow on lap 7, rain at
half distance, a mandatory stop), the `DirectorEngine` protocol, and a
`NoopDirector` default that injects nothing. There is no scheduler yet, and the
docs say so everywhere it matters.

What *is* built and tested is the **seam**: a director's events are published onto
the same event bus the detectors use, so a scripted safety car reaches the agents,
the ring buffer and the WebSocket through exactly one code path. A test takes the
real `flag_change:yellow` the synthetic race throws and the scripted one from
`docs/director_scenario.example.json` and asserts they wake the same agents and
produce the same radio, call for call. Provenance is still honest — an injected
event carries `injected` / `director_id` in its payload, because an event log that
could not tell a scripted safety car from a real one would be the same failure as
an agent quoting a number it cannot trace.

---

## The AI coaching system

A deliberately **tiered** design. The core insight: heuristics find brake points
and speed deltas faster, cheaper and more reliably than any LLM — so keep them
deterministic, and use Claude only where code is weak (synthesis, prioritisation,
explanation, open-ended Q&A). **Raw 60 Hz telemetry never reaches an LLM.**

| Layer | Code | What it is | Cost |
|---|---|---|---|
| **1 · Features** | `src/rtv/coaching/`, `GET /coaching/lap-findings` | Deterministic feature extraction → `LapFindings` | free |
| **2 · MCP server** | `mcp_server/telemetry_coach.py` | 6 tools over the API; chat-driven coaching in Claude Desktop/Code | **$0** (subscription) |
| **3 · Orchestration + evals** | `src/rtv/coaching/orchestrator.py`, `evals/` | Multi-agent Claude pipeline + eval harness | ~cents (API) |

**Layer 1 — deterministic features.** `CoachingService.lap_findings()` aligns a
main and reference lap on a uniform lap-distance grid, detects corners as prominent
speed minima, builds an elapsed-time delta curve scaled to the real lap time, and
runs eight diagnostics (speed, brake point, lock-up, throttle, gear, grip,
consistency, sector). ~1M points/lap collapse to ~20 compact corner findings. This
is *both* the token solution and the eval ground truth.

**Layer 2 — MCP server.** A FastMCP server exposing `list_sessions`, `list_laps`,
`get_lap_findings`, `compare_channel`, `get_corner_detail`, `get_session_info`.
Driven from Claude Desktop/Code on your existing subscription — no API bill.
"Review my lap 5 vs my fastest."

**Layer 3 — multi-agent orchestration + evals.** `CoachOrchestrator` runs a
code-controlled pipeline: **fan-out a specialist per priority corner (parallel) →
synthesise a session plan → adversarially verify it**, all through structured
Pydantic outputs. The `evals/` harness then cross-checks the coach's claimed
figures (e.g. "brake 7 m earlier into T4") against the Layer-1 findings — a
deterministic hallucination check — with an optional LLM judge on top.

---

## Architecture

```
        iRacing shared memory (60 Hz)          recorded .ibt files
                    │                                  │
                    ▼                                  ▼
          ┌───────────────────────────────────────────────────┐
          │  ingest/   frame buffer · normalize/laps · sink    │
          │  catalog/  runtime header → typed variable catalog │
          └───────────────────────────────────────────────────┘
                    │
                    ▼
          ┌───────────────────────────────────────────────────┐
          │  store/   DuckDB metadata + per-lap Parquet        │
          └───────────────────────────────────────────────────┘
                    │
        ┌───────────┼────────────────────────────┐
        ▼           ▼                            ▼
   REST /api/v1   WebSocket /ws/live      coaching/ (Layer 1 features)
        │           │                            │
        ▼           ▼                    ┌────────┴─────────┐
   web frontend  live dashboards         ▼                  ▼
   (charts, maps,                   MCP server         orchestrator + evals
   lap compare)                     (Claude chat)      (multi-agent + ground truth)
```

The v2 live path branches off the same 60 Hz frame stream — no LLM anywhere left
of the dashed line:

```
   frames (60 Hz) ──► racestate/  engine · detectors · aggregation   [no LLM]
                          │            avg 0.03 ms/frame
                          ▼
                     event bus ──────────────────────────► /ws/pitwall + ring buffer
                          │      ▲
                          │      └── director/  (PLANNED: scripted injections)
   - - - - - - - - - - - -│- - - - - - - - - - - - - - - - - - - - - - - - - - - -
                          ▼   a trigger fires, and only then
                    pitwall/  strategist · vehicle engineer · spotter · coach
                          │   scoped tools → in-loop citation validator
                          ▼
                    one radio feed ──► /ws/pitwall ──► browser voice (Web Speech)
                    priority · supersede · airtime      or a backend TTS provider
```

## Package layout

```
src/rtv/
  catalog/   type system, decode tables, runtime catalog builder
  ingest/    frame buffer, normalize/laps, .ibt importer, live poller, sink
  store/     duckdb, schema, parquet writer, query builders, repository
  stream/    live hub (fan-out)
  api/       FastAPI routes + /ws/live
  coaching/  Layer-1 feature extraction, models, Layer-3 orchestrator
  racestate/ v2 pitwall: deterministic race-state engine, detectors, event bus,
             replay, strategy math
  pitwall/   v2 pitwall: agent framework, tools, citation validator, radio feed,
             TTS, orchestrator, agents/ (strategist, vehicle engineer, spotter, coach)
  director/  v2 pitwall: race-director INTERFACES ONLY (scenario schema, protocol,
             NoopDirector). Planned -- see docs/PITWALL.md stage 5.
  services.py  wiring; main.py  app factory
mcp_server/  Layer-2 MCP server (telemetry_coach.py)
frontend/    vanilla-JS live pitwall UI (index.html + js/pitwall/), radio page,
             two self-test pages -- no build step, no frameworks
evals/       ground-truth checks, LLM judge, golden laps, runner
docs/        VARIABLES.md (catalog reference), COACHING.md (coaching deep-dive),
             PITWALL.md (v2 pitwall), director_scenario.example.json
```

---

## Stack

Python 3.11+ · FastAPI · uvicorn · pyirsdk · DuckDB · PyArrow · NumPy · orjson ·
Pydantic · FastMCP · Anthropic SDK.

## Quick start (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pip install -e .

.\run.ps1          # http://127.0.0.1:8000  (interactive docs at /docs)
pytest             # full suite runs offline — no iRacing, no API key
```

> `pyarrow`/`pyirsdk` wheels may lag the newest Python. If install fails on 3.14,
> create the venv with Python 3.12: `py -3.12 -m venv .venv`.

**Optional extras:** `pip install -e ".[mcp]"` for the Layer-2 MCP server;
`pip install -e ".[ai]"` (plus `ANTHROPIC_API_KEY`) for Layer-3 orchestration and
evals.

---

## The 60-second demo (no iRacing, no API key, no network)

**One command.**

```powershell
python scripts/demo_pitwall.py
```

It starts the server, opens the radio page, waits six seconds for you to press
**Enable audio** (browsers make no sound before a gesture), and replays the
scripted synthetic race through the *real* race-state engine at 4× — while
speaking the radio out loud.

You will see every race event land in the log as the engine detects it — a
full-course yellow on lap 3, a front-axle lock-up on lap 4, the fuel window
opening, a pit stop — and hear a critical spotter shout cut an advisory off
mid-sentence and a pit call superseded before it ever airs.

With no `ANTHROPIC_API_KEY` the page plays the **scripted** radio calls and
labels them as scripted — a canned line presented as a real agent's call would
be exactly the ungrounded claim this codebase refuses everywhere else. The
deterministic half — race state, events, replay, and the whole audio-discipline
layer — is fully live either way. `--agents` mounts the real four (needs a key),
`--speed 1` runs it in real time, `--no-browser` just prints the URL.

Open `/` in a second tab while it runs and the **live pit stand** is driven by
the same replay: the flag band goes yellow on lap 3, the pit-window bar fills in
on lap 5, the tyre corners and the timing tower move, and the ticker streams the
raw events beneath it. That page shows agent radio only when the agent layer is
actually mounted — it does not fall back to the scripted calls, and it says so.

Prefer to drive it by hand? `.\run.ps1` (or `uvicorn rtv.main:app --app-dir src`)
then open `/` and press **Start** on the replay bar with `scenario` — the whole
pit stand fills in. `/radio.html` is the listening page on its own; the two
self-tests are at `/audio-test.html` and `/pitwall-test.html`.

### Race with the pitwall (live)

Needs Windows, iRacing running, and an API key.

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:RTV_PITWALL_AGENTS = "true"       # opt-in: the agent layer spends money
.\run.ps1
# then: POST http://127.0.0.1:8000/api/v1/live/start
```

Open `/` for the live pit stand (radio, strategy, timing tower, ticker),
`/radio.html` for the voice on its own, `GET /api/v1/pitwall/health` for
one-glance status, and `POST /api/v1/pitwall/enabled {"enabled": false}` to stop spending
without stopping capture. `RTV_PITWALL_AGENTS_ONLY=strategist,spotter` runs a
subset. If you have no key, everything except the four agents still works — the
race-state engine, `/ws/pitwall`, the event log and the replay driver need
nothing but the sim.

### Replay a stored race (offline)

```powershell
curl -X POST http://127.0.0.1:8000/api/v1/replay/start `
     -H "content-type: application/json" `
     -d '{"session_id":"scenario","speed":1.0}'      # or a real stored session id
```

`speed: 1.0` is real time, `N` is N×, `0` is as fast as possible. The replay
driver calls the **same** `engine.on_frame` the live poller calls, so anything
that works in replay works live — and a replay is refused while the live poller
is running, because interleaving two frame sources into one race state produces
numbers nobody can use.

---

## What a race hour costs

Honest arithmetic, with the method stated so you can redo it for your own race.

**Measured** (from the scripted race, `scripts/smoke_pitwall_agents.py`): the
four agents woke **12 times** and made **24 model calls** — roughly two calls per
wake-up, because a wake-up is a tool round trip plus the answer. Mean prompt:
**~8 000 characters** (role prompt + state slice + tool schemas + the event),
which is **~2 000 tokens**. Replies are radio-length: a few hundred tokens.

**Extrapolated** to an hour of real racing, using the per-trigger cooldowns
rather than the toy race's compressed clock — call it ~35 wake-ups (a chatty
spotter in traffic, a strategist on a 5-lap milestone cadence, an engineer and a
coach held back by 45–180 s cooldowns) at ~2.5 calls each ≈ **90 model calls**.

At the published per-million-token rates (July 2026: Haiku 4.5 $1/$5,
Sonnet $3/$15 in/out) that is roughly:

| Deployment | Input | Output | **Per race hour** |
|---|---|---|---|
| All four on Haiku 4.5 | 180 K tok | 27 K tok | **~$0.32** |
| Default mix (strategist on Sonnet, three on Haiku) | 180 K tok | 27 K tok | **~$0.5** |
| All four on Sonnet | 180 K tok | 27 K tok | **~$0.95** |

**Under a dollar an hour**, dominated by the strategist's reasoning-tier calls.
The figures assume **no** prompt-cache hits, so they are an upper bound — the
role prompt is the first, `cache_control`-marked system block, and clustered
calls (a safety-car burst) read it at ~10 % of the input rate.

The reason it is cents rather than dollars is architectural, not a discount:
**no LLM sees a frame.** All of the continuous mathematics is deterministic, an
agent is woken only by an event that survived aggregation and a predicate, and
`RTV_PITWALL_MAX_CALLS_PER_SESSION` is a hard ceiling if any of that is wrong.
Verify against your own race with `GET /api/v1/pitwall/status`, which reports
`calls_used` and per-agent call counts and latencies. Check current prices at
[anthropic.com/pricing](https://www.anthropic.com/pricing) before budgeting.

## API (prefix `/api/v1`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/variables` | Full variable catalog (live or most recent session) |
| GET | `/sessions` | List sessions (`kind`, `car_id`, `track_id`, paging) |
| GET | `/sessions/{id}` | Session metadata |
| GET | `/sessions/{id}/variables` | This session's catalog |
| GET | `/sessions/{id}/info` | Session-info YAML → JSON (`?seq=`) |
| GET | `/sessions/{id}/laps` | Lap list with times/validity |
| GET | `/sessions/{id}/channels?names=Speed,Throttle&lap=5&max_points=2000&x=lap_dist_pct&mode=minmax` | Multi-channel downsampled query |
| GET | `/sessions/{id}/channels/{name}?lap=5` | Single channel |
| GET | `/sessions/{id}/compare?name=Speed&laps=3,7&grid=1000` | Lap comparison aligned on lap distance |
| GET | `/sessions/{id}/trackmap?color=Speed` | Decimated GPS polyline |
| GET | `/coaching/lap-findings?session_id=…&main_lap=5&ref_lap=3` | Layer-1 corner findings |
| POST | `/import` · GET `/import/{job_id}` | Async `.ibt` import + status |
| POST | `/live/start` · `/live/stop` · GET `/live/status` | Live poller control |
| GET | `/racestate` · `/racestate/events` | Live race state + event log (pitwall) |
| POST | `/replay/start` · `/replay/stop` · GET `/replay/status` | Replay a stored or synthetic session |
| GET | `/pitwall/status` · `/pitwall/radio` | Agent layer status + the merged radio feed |
| POST | `/pitwall/enabled` · `/pitwall/agents/{name}/enabled` | Kill switch + per-agent flags |
| GET | `/pitwall/tts` · `/pitwall/audio/{message_id}` | Role voice table + synthesised radio audio |
| POST | `/pitwall/driver-message` | Push-to-talk from the cockpit onto the channel |
| GET | `/pitwall/health` | Engine, live poller, replay, agents and director in one payload |
| GET | `/health` | Liveness |

### WebSocket `/ws/live`

```jsonc
// client -> server
{"op":"subscribe","channels":["Speed","RPM","Throttle","SessionFlags"],"rate_hz":30,"include_flags":true}
{"op":"set_rate","rate_hz":10}
{"op":"unsubscribe","channels":["RPM"]}

// server -> client
{"type":"state","connection":"in_session","session_id":"..."}
{"type":"catalog","schema_hash":"...","variables":[...]}
{"type":"data","tick":91234,"t":123.45,"v":{"Speed":61.2,"RPM":7400},"flags":{"SessionFlags":{"green":true}}}
```

### WebSocket `/ws/pitwall`

Full `RaceState` snapshots at a client-chosen rate (latest-wins), plus every race
event and every agent radio call pushed immediately and never coalesced. See
`docs/PITWALL.md`.

```jsonc
{"op":"subscribe","rate_hz":10}                        // client -> server
{"type":"state","state":{ /* RaceState */ }}           // server -> client
{"type":"event","event":{"key":"pit_window_open", "severity":"advisory", ...}}
{"type":"radio","message":{"agent":"strategist","priority":"critical",
                           "spoken_text":"Box this lap, 3.0 litres, we come out P8."}}
```

---

## Testing

The full `pytest` suite (450+ tests across catalog, decode, store round-trip, laps,
API, coaching features, MCP server, orchestrator, evals, the race-state engine, the
pitwall agent layer, the radio voice and the director seam) runs **entirely
offline** — DuckDB/PyArrow are import-guarded, the LLM layers are driven by
scripted providers, and the suite is green whether or not `ANTHROPIC_API_KEY` is
exported. Seven offline smoke scripts back it up:

| Script | What it drives end to end |
|---|---|
| `scripts/smoke_offline.py` | a synthetic session through the real storage writer via FastAPI's `TestClient` |
| `scripts/smoke_pitwall.py` | a scripted synthetic race through the race-state engine, asserting the event sequence and its reproducibility |
| `scripts/smoke_pitwall_agents.py` | the whole agent layer — tool loop, citation validator, radio queue — over that race |
| `scripts/smoke_pitwall_roles.py` | all four agents over a race that keeps locking up at one corner, with a real DuckDB store attached so the coach's corner analysis reaches the genuine Layer-1 extractor |
| `scripts/smoke_pitwall_voice.py` | the voice path, with a fake vendor transport standing in for a cloud TTS API |
| `scripts/smoke_pitwall_director.py` | the director seam (scripted vs real safety car) and the five live-path hardening properties |
| `scripts/smoke_pitwall_ui.py` | the pitwall UI: the shell and every module it imports served over HTTP, a scripted race over `/ws/pitwall` at 10 Hz, the replay round trip, and a session with no fuel or tyre channels degrading to `n/a` |

The browser-side modules have their own suites with no JS toolchain at all:
`node frontend/js/run-audio-tests.mjs` (the radio queue) and
`node frontend/js/run-pitwall-tests.mjs` (the pitwall view model), or open
`/audio-test.html` and `/pitwall-test.html`. `pytest` runs both when `node` happens
to be on `PATH` and skips them otherwise, so the offline guarantee is unchanged
either way.

## Docs

- `docs/VARIABLES.md` — the standard iRacing variable set and the six-type system.
- `docs/COACHING.md` — the tiered coaching architecture in depth.
- `docs/PITWALL.md` — the v2 live pitwall: race-state schema, event catalog, replay,
  the event-driven agent layer (framework, grounding, radio, and the four agents:
  strategist, vehicle engineer, spotter, coach), the TTS radio voice, the planned
  race-director seam, the live-path hardening and the live pitwall UI. Written as
  a build log: every
  stage records what it built, what the next one consumes, and every deviation
  from its spec with the reasoning.
- `docs/director_scenario.example.json` — a worked `ScenarioScript` for the
  **planned** race director (safety car → restart → rain → mandatory stop).

## License

MIT.
