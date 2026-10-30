---
name: run-app
description: Start the RTV backend with the frontend, check the API and look at the live view or the post-race review in the browser. Use to see a backend or frontend change working in the real app.
---

# Run the app

1. Start the server in the background: `.\run.ps1` from PowerShell (add `-Reload` while
   iterating on Python). It binds http://127.0.0.1:8000, serves the live pitwall at `/` and the
   post-race review (`frontend/review/`) at `/review/`.
   A fresh store is empty: nothing is seeded. To get the real Spa race in, run
   `.venv/Scripts/python.exe scripts/fetch_fixtures.py` and upload
   `tests/fixtures/real/audir8lmsevo2gt3_spa_race.ibt` to `POST /api/v1/import/upload`.
2. Wait for `GET http://127.0.0.1:8000/api/v1/health`, then check
   `/api/v1/sessions` lists at least one session.
3. Open http://127.0.0.1:8000/review/ in the built-in browser. Check the console for
   errors, that the summary, lap traces, track map and findings draw, and that
   `GET /api/v1/coaching/lap-findings` returns corners for the session. The replay
   needs a recorded pitwall run: press "Record a run from the telemetry" (free while
   the agents are off; with `RTV_PITWALL_AGENTS=true` and a key it calls Kimi, so ask
   first).
4. `POST /api/v1/coaching/chat` calls Kimi Code and uses the user's quota when
   `KIMI_CODE_API_KEY` is set. Don't call it unless the user said to; without a key it
   returns 503, which is expected.
5. To import real data instead, run `.\scripts\import_latest.ps1` against the running
   server (newest `.ibt` in `Documents\iRacing\telemetry`).
6. For the pitwall, open http://127.0.0.1:8000/ and replay a stored session
   (`POST /api/v1/replay/start {"session_id": ..., "speed": 4}`; no key, no iRacing).
7. Stop the server when done unless the user wants it left running.
