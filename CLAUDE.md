# Racing Telemetry Visualiser

RTV ingests iRacing telemetry (live 60 Hz shared memory or `.ibt` files), stores it in
DuckDB metadata tables plus per-lap Parquet, and serves it over a FastAPI REST API and
`/ws/live` to a vanilla-JS frontend (live view at `/`, post-race review at `/review/`). On top sits a tiered AI coach:
Layer 1 deterministic lap findings (the ground truth), Layer 2 an MCP server, Layer 3a
a multi-agent orchestrator, Layer 3b a LangGraph coach with grounded refusal and
in-loop citation checks, plus a hermetic eval harness. v2 adds a live pitwall: a
deterministic race-state engine, four event-driven agents (strategist, vehicle engineer,
spotter, coach) on one prioritised radio feed, browser TTS, and a live pit-stand UI.
`README.md` has the full tour; `docs/COACHING.md`, `docs/PITWALL.md`, `docs/TRACKS.md`,
`docs/VISION.md` and `docs/VARIABLES.md` go deeper.

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
  http://127.0.0.1:8000/review/ for the post-race review and lap analysis (API docs at `/docs`).
- Desktop app (native Qt, server embedded): `uv pip install --python .venv/Scripts/python.exe
  -e ".[desktop]"`, then `.\run-desktop.ps1`; `.\scripts\build_desktop.ps1` builds
  `dist\RTV.exe`. See `docs/DESKTOP.md`.
- Real-data fixture (162 MB Spa race .ibt, gitignored):
  `.venv/Scripts/python.exe scripts/fetch_fixtures.py`; `tests/test_real_ibt.py` skips
  without it, and CI fetches and caches it in a separate job.
- Check the LLM backend (one tiny real call): `.venv/Scripts/python.exe scripts/check_llm.py`
- Offline HTTP smoke test: `.venv/Scripts/python.exe scripts/smoke_offline.py`
- CI (`.github/workflows/ci.yml`) runs ruff, pytest and the smoke test on Python 3.11-3.13
  for every push to main and every PR, with no API key.

`ruff format` and `black` are configured but most files are not formatted with them.
Don't reformat whole files you didn't otherwise change; the edit hook formats only
files that were already formatted and sorts imports in every edited file.

## Layout

- `src/rtv/` the package: `catalog/` (runtime variable catalog from the telemetry
  header), `ingest/` (frame buffer, laps, `.ibt` import, live poller), `store/`
  (DuckDB, Parquet writer, queries), `stream/`, `api/` (routes, `ws.py`),
  `coaching/` (Layer 1 `features.py`, Layer 3a `orchestrator.py`, Layer 3b `agent/`),
  `tracks/` (venue corner names from OpenStreetMap, built by
  `scripts/build_track_refs.py`; see `docs/TRACKS.md`), `racestate/` (v2 race-state engine,
  detectors, event bus, replay), `pitwall/` (v2
  agents, provider, radio, TTS), `vision/` (onboard video sync and grounded VLM visual
  notes, `docs/VISION.md`), `director/` (interfaces only), `llm.py` (the Kimi
  Code client), `config.py`, `services.py`, `main.py`, and `desktop/` (RTV Desktop, the
  PySide6 client: `model/` holds pure Python ports of the frontend's tested view
  models, the rest is Qt; PySide6 is imported only inside `rtv.desktop`).
- `frontend/` (frozen web pages, see Conventions) served by FastAPI at `/`: the live view (`index.html`, `css/`, `js/`), the
  post-race review at `/review/` (`review/index.html`, `js/review/`), `radio.html` and
  two self-test pages; `js/shared/` is used by both views. `docs/*.md` are hand-written
  docs. GitHub Pages serves `docs/`: `docs/review`, `docs/css`, `docs/js` are copies
  made by `scripts/build_pages.py`, `docs/data/<name>/` is a session exported by
  `scripts/export_session.py`, read by the review's static mode (`?static=`). See the
  `publish-pages` skill.
- `mcp_server/telemetry_coach.py` Layer 2 MCP server over the REST API.
- `evals/` checks shared with the in-loop validator, fixtures, cases, runner, LLM judge,
  `run.py` (the live, paid eval), and the test-only generated data
  (`synthetic_race.py`, `synthetic_session.py`).
  `evals/real/` holds eval cases cut from a real race journal (`spa_run`, built by
  `scripts/build_real_evals.py`): offline in pytest against `baseline.json`, live via
  `evals/run_pitwall.py --real spa_run` (paid).
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
- No fake data in the app: nothing generated is seeded, served or replayed at runtime.
  Generated sessions live in `evals/synthetic_*.py` for tests only; real behaviour is
  checked against the real `.ibt` fixture.
- RTV Desktop (`rtv.desktop`) is the primary UI: new UI features go in the desktop app
  only. The web pages (`/`, `/review/`, `/radio.html`) are frozen: they keep working and
  get bug fixes, no new features (the web review stays for the GitHub Pages export).
  Web fixes are edited in `frontend/`; rebuild the Pages copy in `docs/` with
  `scripts/build_pages.py` in the same change (`tests/test_export_session.py` fails on a
  stale copy).
- View rules live in the JS modules and their Python ports in `rtv/desktop/model/`; both
  must pass the golden file `tests/fixtures/view_golden/` (`tests/test_view_golden.py`,
  `node frontend/js/run-golden-tests.mjs`). After an intended view change, rebuild it with
  `scripts/build_view_golden.py`, which refuses to write while the two sides disagree.
- Coaching heuristics exist once, in `src/rtv/coaching/features.py`; UIs render
  `/coaching/lap-findings` and never recompute it.
- Every session the engine consumes is written to a race journal
  (`data/sessions/<id>/runs/*.jsonl`, `rtv.store.journal`); the review replays it and
  never re-runs the agents.
- Pitwall agents never assert a number that isn't traceable to deterministic data; the
  race-state engine has no LLM in it. `docs/PITWALL.md` records the v2 interfaces and
  the pending build stages (`prompts/07-10`; done ones are in `prompts/done/`).
- Commit messages: one imperative sentence-case subject, no prefix (see `git log`).

## Environment

- Windows 11; Claude Code hooks run in Git Bash, the user's shell is PowerShell.
- Keys and settings come from `.env` (copy `.env.example`); `.env` is gitignored, never
  read it, print a key or commit it.
- LLM backend: Kimi Code only. Every LLM layer goes through `src/rtv/llm.py`: model
  `kimi-k3` on `https://api.kimi.com/coding`, key in `KIMI_CODE_API_KEY` (a Kimi Code
  key, not a Moonshot developer-API key; `KIMI_API_KEY` is still read as a fallback).
  The endpoint speaks the Anthropic Messages format, so the `anthropic` and
  `langchain-anthropic` packages are only the transport. Structured answers come from
  a forced respond tool (`KimiProvider`); never use `messages.parse(output_format=)`.
- The single exception to Kimi-only is the eval judge: `evals/judge_provider.py` grades on
  Gemini (`GEMINI_API_KEY`, `judge` extra) by default, set by `RTV_JUDGE_PROVIDER`. It stays in
  `evals/`; nothing under `src/rtv/` may import it or `google.genai` (a test enforces it).
- Runtime data lands in `./data/` (gitignored). A fresh store is empty.
- `.mcp.json` runs `${RTV_PYTHON:-python}`; set `RTV_PYTHON=.venv/Scripts/python.exe`
  since `python` is not on PATH here.

## Costs: ask first

Anything that calls Kimi uses the user's Kimi Code quota and needs the user's go-ahead
first: `evals/run.py` (coach on Kimi plus the judge on Gemini; `--no-judge`
still calls the coach), `evals/run_pitwall.py` without `--dry-run`,
`scripts/deep_coach.py` without `--findings-only`, `scripts/check_llm.py`, the
`orchestrator`, the pitwall agents (`RTV_PITWALL_AGENTS=true`), vision analyses
(`POST .../vision/analyses` with `RTV_VISION_MODE` on; the `/vision/plan` dry run is free), and
`POST /api/v1/coaching/chat` or the frontend chat while a key is set. The pytest suite,
the hermetic eval runner, `--dry-run` and `--findings-only` are free.

## Staged pitwall build

The v2 pitwall was built by headless prompts in `prompts/` (01-06 done, 07-10 pending),
run by `run_pitwall.ps1` / `run_pitwall.sh`. When running one of those stages, read
`docs/PITWALL.md` first (it is the shared state between stages) and finish by updating
it with what was built, the interfaces exposed and any deviations. Each stage must leave
the offline suite green and v1 behaviour (analysis app, coaching endpoints, MCP server)
unchanged, and must not delete or rewrite passing tests to make new code pass.
