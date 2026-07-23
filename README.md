# Racing Telemetry Visualiser

**A full-stack iRacing telemetry platform: real-time ingest → columnar storage → a MoTeC-i2-style web analysis UI → a tiered AI race engineer.**

**▶ [Live showcase](https://kruslim.github.io/racing-telemetry-visualiser/)**

Racing Telemetry Visualiser (RTV) captures *every* channel iRacing exposes — from
the live 60 Hz shared-memory feed or from recorded `.ibt` files — stores it
columnar in DuckDB/Parquet, and serves it three ways:

- a **REST API** for charts, lap comparison and track maps,
- a **WebSocket stream** for live dashboards, and
- a **three-layer AI coaching system** that turns a million raw points per lap into
  a prioritised, fact-checked driving plan.

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
             orchestrator, agents/ (strategist, vehicle engineer, spotter, coach)
  services.py  wiring; main.py  app factory
mcp_server/  Layer-2 MCP server (telemetry_coach.py)
evals/       ground-truth checks, LLM judge, golden laps, runner
docs/        VARIABLES.md (catalog reference), COACHING.md (coaching deep-dive)
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

The full `pytest` suite (~200 tests across catalog, decode, store round-trip, laps,
API, coaching features, MCP server, orchestrator, evals, the race-state engine and
the pitwall agent layer) runs **entirely offline** — DuckDB/PyArrow are
import-guarded, the LLM layers are driven by scripted providers, and the suite is
green whether or not `ANTHROPIC_API_KEY` is exported. Four offline smoke scripts
back it up: `scripts/smoke_offline.py` pushes a synthetic session through the real
storage writer via FastAPI's `TestClient`, `scripts/smoke_pitwall.py` replays a
scripted synthetic race through the race-state engine and asserts the resulting
event sequence, `scripts/smoke_pitwall_agents.py` drives the whole agent layer
— tool loop, citation validator and radio queue — over that same race, and
`scripts/smoke_pitwall_roles.py` runs all four agents over a race that keeps
locking up at one corner, with a real DuckDB store attached so the coach's
corner analysis reaches the genuine Layer-1 feature extractor.

## Docs

- `docs/VARIABLES.md` — the standard iRacing variable set and the six-type system.
- `docs/COACHING.md` — the tiered coaching architecture in depth.
- `docs/PITWALL.md` — the v2 live pitwall: race-state schema, event catalog, replay,
  and the event-driven agent layer (framework, grounding, radio, and the four
  agents: strategist, vehicle engineer, spotter, coach).

## License

MIT.
