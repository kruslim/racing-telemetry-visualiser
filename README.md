# Racing Telemetry Visualiser

**A full-stack iRacing telemetry platform: real-time ingest → columnar storage → a MoTeC-i2-style web analysis UI → a tiered AI race engineer.**

**▶ [Live demo](https://kruslim.github.io/racing-telemetry-visualiser/)** — the actual
analysis app running in your browser on a simulated session: scrub the traces, compare
laps, open the Coach worksheet, and ask the AI race engineer where you're losing time.
No backend, no key — every worksheet and the grounded chat run entirely client-side.

Racing Telemetry Visualiser (RTV) captures *every* channel iRacing exposes — from
the live 60 Hz shared-memory feed or from recorded `.ibt` files — stores it
columnar in DuckDB/Parquet, and serves it three ways:

- a **REST API** for charts, lap comparison and track maps,
- a **WebSocket stream** for live dashboards, and
- a **layered AI coaching system** that turns a million raw points per lap into
  a prioritised, fact-checked driving plan — and refuses questions the telemetry
  can't answer instead of inventing one.

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

### 5. A MoTeC-i2-style web frontend — with an AI chat engineer
A vanilla-JS canvas UI (no build step, in `frontend/`, **served by FastAPI at
`/`**) reads the REST API and renders: multi-channel time/distance worksheets,
**GPS track maps**, histograms, suspension and track-report worksheets, and a
**Coach** worksheet. The layout puts an **AI race-engineer chat on the left and
the graphs on the right**: ask a question, the LLM answers grounded on the
telemetry, and the corners it flags are **annotated directly onto the graph**
(click a flag to jump the cursor there). If no `.ibt` is imported, a **simulated
demo session is generated through the real store** on startup, so every endpoint
— charts, coaching and chat — has realistic data to serve.

### 6. Layered AI coaching
The centrepiece. See below.

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
| **3a · Orchestration** | `src/rtv/coaching/orchestrator.py` | Code-controlled multi-agent Claude pipeline (fan-out → synthesise → verify) | ~cents (API) |
| **3b · Graph agent + evals** | `src/rtv/coaching/agent/`, `evals/` | LangGraph tool-calling coach: grounded refusal + in-loop citation validation, replayed by a hermetic regression harness | ~cents (API) |

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

**Layer 3a — multi-agent orchestration.** `CoachOrchestrator` runs a
code-controlled pipeline: **fan-out a specialist per priority corner (parallel) →
synthesise a session plan → adversarially verify it**, all through structured
Pydantic outputs.

**Layer 3b — the graph agent + eval harness.** `src/rtv/coaching/agent/` is a
**LangGraph** coach: a model drives the same coaching tools in-process
(`get_lap_findings`, `list_available_channels`, …) through an
`agent → tools → validate → refuse` state graph. Two behaviours make it more than a
chat loop: it **refuses questions the telemetry can't answer** — ask for tyre
temperatures the session never captured and it returns a grounded `CoachRefusal`
listing the channels that *are* available, instead of inventing a number — and it
**validates every asserted figure in-loop**, bouncing a claimed time gain or brake
point that no retrieved finding supports before it can reach the driver. The `evals/`
harness replays a seed set of these behaviours against a scripted model — fully
offline, no key — cross-checks the coach's figures against the Layer-1 findings
(`evals/checks.py`, shared with the in-loop validator), and diffs each run against a
baseline so a regression is a merge-blocker. An optional LLM judge scores the soft
qualities on top.

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
  coaching/  Layer-1 feature extraction, models, Layer-3a orchestrator
    agent/   Layer-3b LangGraph coach (provider, tools, graph, refusal, validator)
  services.py  wiring; main.py  app factory
mcp_server/  Layer-2 MCP server (telemetry_coach.py)
evals/       ground-truth checks, LLM judge, hermetic fixtures, trace, seed cases, runner
docs/        VARIABLES.md (catalog reference), COACHING.md (coaching deep-dive)
```

---

## Stack

Python 3.11+ · FastAPI · uvicorn · pyirsdk · DuckDB · PyArrow · NumPy · orjson ·
Pydantic · FastMCP · LangGraph · Anthropic SDK.

## Quick start (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pip install -e .

.\run.ps1          # http://127.0.0.1:8000  → the analysis app (chat + graphs)
                   #   API docs at /docs · a demo session is seeded on first run
pytest             # full suite runs offline — no iRacing, no API key
```

On any OS: `uvicorn rtv.main:app --app-dir src` then open **http://127.0.0.1:8000/**.
The chatbot needs `ANTHROPIC_API_KEY` in the environment; everything else (graphs,
track map, deterministic findings) works without one. Set `RTV_SEED_DEMO=false` to
skip the simulated demo session once you've imported real `.ibt` data.

> `pyarrow`/`pyirsdk` wheels may lag the newest Python. If install fails on 3.14,
> create the venv with Python 3.12: `py -3.12 -m venv .venv`.

**Optional extras:** `pip install -e ".[mcp]"` for the Layer-2 MCP server;
`pip install -e ".[ai]"` (plus `ANTHROPIC_API_KEY`) for Layer-3a orchestration;
`pip install -e ".[agent]"` for the Layer-3b LangGraph coach. The Layer-3b graph and
its eval harness run **offline with a scripted model** — no key — under the `dev` extra.

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
| POST | `/coaching/chat` | LLM race engineer: `{session_id, main_lap, ref_lap, question}` → grounded answer + graph annotations (needs `ANTHROPIC_API_KEY`) |
| POST | `/import` · GET `/import/{job_id}` | Async `.ibt` import + status |
| POST | `/live/start` · `/live/stop` · GET `/live/status` | Live poller control |
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

---

## Testing

The full `pytest` suite (~65 tests across catalog, decode, store round-trip, laps,
API, coaching features, MCP server, orchestrator, evals, and the Layer-3b graph
coach) runs **entirely offline** — DuckDB/PyArrow are import-guarded, the LLM layers
are mocked or driven by a scripted model, no iRacing install and no
`ANTHROPIC_API_KEY` required. The graph-coach suite exercises the headline behaviours
hermetically: a **grounded refusal** when a channel wasn't captured, the **in-loop
citation validator** bouncing a figure the findings don't support, iteration-cap
termination, and a **baseline-diffing regression runner** over a seed case set.
There's also an offline HTTP smoke test (`scripts/smoke_offline.py`) that pushes a
synthetic session through the real storage writer via FastAPI's `TestClient`.

## Docs

- `docs/VARIABLES.md` — the standard iRacing variable set and the six-type system.
- `docs/COACHING.md` — the tiered coaching architecture in depth.

## License

MIT.
