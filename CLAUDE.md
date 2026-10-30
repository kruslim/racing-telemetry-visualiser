# Racing Telemetry Visualiser

RTV ingests iRacing telemetry (live 60 Hz shared memory or `.ibt` files), stores it in
DuckDB metadata tables plus per-lap Parquet, and serves it over a FastAPI REST API and
`/ws/live` to a vanilla-JS MoTeC-i2-style frontend. On top sits a tiered AI coach:
Layer 1 deterministic lap findings (the ground truth), Layer 2 an MCP server, Layer 3a
a multi-agent orchestrator, Layer 3b a LangGraph coach with grounded refusal and
in-loop citation checks, plus a hermetic eval harness. v2 adds a live pitwall: a
deterministic race-state engine, four event-driven agents (strategist, vehicle engineer,
spotter, coach) on one prioritised radio feed, browser TTS, and a live pit-stand UI.
`README.md` has the full tour; `docs/COACHING.md`, `docs/PITWALL.md` and
`docs/VARIABLES.md` go deeper.

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
- Run: `.\run.ps1` (or `-Reload`), then http://127.0.0.1:8000/ for the live pitwall and
  http://127.0.0.1:8000/app/ for the analysis app (API docs at `/docs`).
- Pitwall demo, no key or iRacing: `.venv/Scripts/python.exe scripts/demo_pitwall.py`
- Check the LLM backend (one tiny real call): `.venv/Scripts/python.exe scripts/check_llm.py`
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
  `racestate/` (v2 race-state engine, detectors, event bus, replay), `pitwall/` (v2
  agents, provider, radio, TTS), `director/` (interfaces only), `demo/` (simulated
  session seeded on startup), `llm.py` (backend selection), `config.py`, `services.py`,
  `main.py`.
- `frontend/` served by FastAPI at `/`: the live pitwall (`index.html`, `css/`, `js/`),
  `radio.html` and two self-test pages. `frontend/app/` is the analysis app, served at
  `/app/`; `docs/*.html|css|js` are a copy of `frontend/app/` for GitHub Pages and
  `docs/*.md` are hand-written docs.
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
- Edit `frontend/app/`, never `docs/` copies; after a change there run `build_pages.py`
  and commit both.
- Pitwall agents never assert a number that isn't traceable to deterministic data; the
  race-state engine has no LLM in it. `docs/PITWALL.md` records the v2 interfaces and
  the pending build stages (`prompts/07-10`; done ones are in `prompts/done/`).
- Commit messages: one imperative sentence-case subject, no prefix (see `git log`).

## Environment

- Windows 11; Claude Code hooks run in Git Bash, the user's shell is PowerShell.
- Keys and settings come from `.env` (copy `.env.example`); `.env` is gitignored, never
  read it, print a key or commit it.
- LLM backend: every LLM layer goes through `src/rtv/llm.py`. The default is Kimi
  (`RTV_LLM_PROVIDER=kimi`, model `kimi-k3`) on the Kimi Code endpoint
  `https://api.kimi.com/coding`, with the key in `KIMI_CODE_API_KEY` (a Kimi Code key,
  not a Moonshot developer-API key; `KIMI_API_KEY` is still read as a fallback).
  `RTV_LLM_PROVIDER=anthropic` switches to Claude via `ANTHROPIC_API_KEY`.
- Runtime data lands in `./data/` (gitignored). The demo session is seeded when the
  store is empty (`RTV_SEED_DEMO=true`).
- `.mcp.json` runs `${RTV_PYTHON:-python}`; set `RTV_PYTHON=.venv/Scripts/python.exe`
  since `python` is not on PATH here.

## Costs: ask first

Anything that calls the LLM backend (Kimi or Anthropic) uses the user's quota or costs
money and needs the user's go-ahead first: `evals/run.py` (coach plus judge; `--no-judge`
still calls the coach), `evals/run_pitwall.py` without `--dry-run`,
`scripts/deep_coach.py` without `--findings-only`, `scripts/check_llm.py`, the
`orchestrator`, the pitwall agents (`RTV_PITWALL_AGENTS=true`), and
`POST /api/v1/coaching/chat` or the frontend chat while a key is set. The pytest suite,
the hermetic eval runner, `--dry-run` and `--findings-only` are free.

## Staged pitwall build

The v2 pitwall was built by headless prompts in `prompts/` (01-06 done, 07-10 pending),
run by `run_pitwall.ps1` / `run_pitwall.sh`. When running one of those stages, read
`docs/PITWALL.md` first (it is the shared state between stages) and finish by updating
it with what was built, the interfaces exposed and any deviations. Each stage must leave
the offline suite green and v1 behaviour (analysis app, coaching endpoints, MCP server)
unchanged, and must not delete or rewrite passing tests to make new code pass.
