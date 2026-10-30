# Racing Telemetry Visualiser

iRacing telemetry platform: ingest (live SDK + `.ibt`) → DuckDB/Parquet store → FastAPI
REST + WebSocket → vanilla-JS MoTeC-i2-style frontend → tiered AI coach. See `README.md`
for the feature tour and `docs/COACHING.md` for the coaching architecture.

## Commands

```bash
pip install -r requirements-dev.txt -e ".[mcp]"   # dev setup (Python 3.11+)
pytest                                           # full suite, ~15s, fully offline
pytest tests/test_api.py -k trackmap             # single file / test
ruff check src tests evals scripts mcp_server    # lint
python scripts/smoke_offline.py                  # end-to-end HTTP smoke via TestClient
uvicorn rtv.main:app --app-dir src --reload      # run the app → http://127.0.0.1:8000/
python scripts/build_pages.py                    # sync frontend/ → docs/ (GitHub Pages)
```

On Windows the owner uses `.\run.ps1` (prefers `.venv\Scripts\python.exe`).

## Layout

- `src/rtv/` — the package. `main.py` is the app factory, `services.py` wires everything,
  `config.py` holds settings (all env vars are `RTV_*`, see `.env.example`).
  - `catalog/` runtime variable catalog from the telemetry header (never hard-code channels)
  - `ingest/` frame buffer, lap normalisation, `.ibt` importer, live poller
  - `store/` DuckDB metadata + per-session/per-lap Parquet
  - `api/` FastAPI routes under `/api/v1`, plus `/ws/live`
  - `coaching/` Layer 1 deterministic features + Layer 3a orchestrator;
    `coaching/agent/` is the Layer 3b LangGraph coach
  - `demo/` simulated session seeded on startup when the store is empty
- `mcp_server/telemetry_coach.py` — Layer 2 MCP server; a thin client over the REST API.
- `evals/` — hermetic eval harness for the graph coach; `evals/checks.py` is shared with
  the in-loop citation validator, so changing it changes runtime behaviour too.
- `frontend/` — the web app, served by FastAPI at `/`. No build step, no npm.
- `docs/` — GitHub Pages site. The `.html/.css/.js` files are **generated copies** of
  `frontend/`.

## Rules

- **Edit `frontend/`, never `docs/*.js|css|html`.** After changing the frontend, run
  `python scripts/build_pages.py` and commit both. `docs/*.md` are hand-written.
- **Tests must stay offline**: no iRacing, no network, no `ANTHROPIC_API_KEY`. LLM layers
  are driven by `ScriptedModel` in `tests/conftest.py`; optional deps are guarded with
  `pytest.importorskip`. Keep it that way for new tests.
- **Raw 60 Hz telemetry must never reach an LLM.** Coaching prompts and tools work from
  Layer 1 `LapFindings`. Any number the coach asserts must be traceable to a finding.
- Line length is 100 (ruff + black, `pyproject.toml`). Much of the repo isn't
  black-formatted yet, so format only the code you touch. Don't reformat whole files.
- `mypy` has known errors right now. Don't gate on it, but don't add new ones.
- `mcp` is pinned `<2`: 2.x renamed `FastMCP` and broke `telemetry_coach.py`.
- `pyirsdk` live capture only works on Windows with iRacing running. Everything else
  (including `.ibt` import) is cross-platform.
- Runtime data lives in `./data/` (gitignored). Delete it to reset the store; the demo
  session reseeds on the next startup.
