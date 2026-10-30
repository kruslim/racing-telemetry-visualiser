# Racing Telemetry Visualiser

RTV ingests iRacing telemetry (live 60 Hz shared memory or `.ibt` files), stores it in
DuckDB metadata tables plus per-lap Parquet, and serves it over a FastAPI REST API and
`/ws/live` to a vanilla-JS MoTeC-i2-style frontend. On top sits a tiered AI coach:
Layer 1 deterministic lap findings (the ground truth), Layer 2 an MCP server, Layer 3a
a Claude multi-agent orchestrator, Layer 3b a LangGraph coach with grounded refusal and
in-loop citation checks, plus a hermetic eval harness. `README.md` has the full tour;
`docs/COACHING.md` and `docs/VARIABLES.md` go deeper.

## Commands

`python` is not on PATH on this machine; use the repo virtualenv (`run.ps1` does too).
In Git Bash write `.venv/Scripts/python.exe`, in PowerShell `.venv\Scripts\python.exe`.

- Install: `uv venv .venv --python 3.12`, then
  `uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt -e ".[mcp]"`
  (plain `python -m pip install ...` inside the venv works the same). The `mcp` extra
  is pinned to `mcp<2`: mcp 2.x renamed FastMCP, so `mcp_server/` and
  `tests/test_mcp_server.py` break on it until they are migrated.
- Test: `.venv/Scripts/python.exe -m pytest` (offline, no iRacing, no API key).
- Lint: `.venv/Scripts/python.exe -m ruff check .`
- Run: `.\run.ps1` (or `-Reload`), then http://127.0.0.1:8000/ (API docs at `/docs`).
- Offline HTTP smoke test: `.venv/Scripts/python.exe scripts/smoke_offline.py`
- Publish the Pages showcase: `.venv/Scripts/python.exe scripts/build_pages.py`

`ruff format` and `black` are configured but most files are not formatted with them.
Don't reformat whole files you didn't otherwise change; the edit hook formats only
files that were already formatted and sorts imports in every edited file.

## Layout

- `src/rtv/` the package: `catalog/` (runtime variable catalog from the telemetry
  header), `ingest/` (frame buffer, laps, `.ibt` import, live poller), `store/`
  (DuckDB, Parquet writer, queries), `stream/`, `api/` (routes, `ws.py`),
  `coaching/` (Layer 1 `features.py`, Layer 3a `orchestrator.py`, Layer 3b `agent/`),
  `demo/` (simulated session seeded on startup), `config.py`, `services.py`, `main.py`.
- `frontend/` the web app, served by FastAPI. `docs/*.html|css|js` are a copy of it for
  GitHub Pages; `docs/*.md` are hand-written docs.
- `mcp_server/telemetry_coach.py` Layer 2 MCP server over the REST API.
- `evals/` checks shared with the in-loop validator, fixtures, cases, runner, LLM judge,
  and `run.py` (the live, paid eval).
- `tests/` pytest suite; `conftest.py` has the `ScriptedModel` that drives the coach.
- `scripts/` build, smoke, import and deep-coach helpers.

## Conventions

- Modules start with a docstring and `from __future__ import annotations`; line length
  100, ruff rules `E F I UP B` (B008 ignored for FastAPI `Depends()`).
- Settings live in `src/rtv/config.py` (pydantic-settings, `RTV_` prefix). A new setting
  also needs a line in `.env.example`; reads of `.env.*` are denied, so give the user the
  line to add.
- Optional dependencies (`anthropic`, `langgraph`, `mcp`, `duckdb`, `pyarrow`) are
  imported lazily in code and guarded with `pytest.importorskip` in tests.
- Every test runs offline: LLM layers are mocked or driven by `ScriptedModel`; never add a
  test that needs a key or the network.
- Raw 60 Hz telemetry never goes to an LLM; models see Layer-1 findings only.
- Edit `frontend/`, never `docs/` copies; after a frontend change run `build_pages.py`
  and commit both.
- Commit messages: one imperative sentence-case subject, no prefix (see `git log`).

## Environment

- Windows 11; Claude Code hooks run in Git Bash, the user's shell is PowerShell.
- Keys and settings come from `.env` (copy `.env.example`); `.env` is gitignored, never
  read it, print a key or commit it. `ANTHROPIC_API_KEY` enables the chat and Layer 3.
- Runtime data lands in `./data/` (gitignored). The demo session is seeded when the
  store is empty (`RTV_SEED_DEMO=true`).
- `.mcp.json` runs `${RTV_PYTHON:-python}`; set `RTV_PYTHON=.venv/Scripts/python.exe`
  since `python` is not on PATH here.

## Costs: ask first

Anything that calls the Anthropic API costs money and needs the user's go-ahead first:
`evals/run.py` (coach plus judge; `--no-judge` still calls the coach),
`scripts/deep_coach.py` without `--findings-only`, the `orchestrator`, and
`POST /api/v1/coaching/chat` or the frontend chat while a key is set. The pytest suite,
the hermetic eval runner and `--findings-only` are free.
