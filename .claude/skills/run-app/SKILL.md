---
name: run-app
description: Start the RTV backend with the frontend, check the API and look at the analysis app in the browser. Use to see a backend or frontend change working in the real app.
---

# Run the app

1. Start the server in the background: `.\run.ps1` from PowerShell (add `-Reload` while
   iterating on Python). It binds http://127.0.0.1:8000, serves the live pitwall at `/` and the
   analysis app (`frontend/app/`) at `/app/`.
   If the store in `./data/` is empty, a simulated demo session is seeded on startup.
2. Wait for `GET http://127.0.0.1:8000/api/v1/health`, then check
   `/api/v1/sessions` lists at least one session.
3. Open http://127.0.0.1:8000/app/ in the built-in browser. Check the console for errors,
   that the worksheets and track map draw from the API (not the offline sim fallback),
   and that `GET /api/v1/coaching/lap-findings` returns corners for the session.
4. The chat panel calls `POST /api/v1/coaching/chat`, which calls the LLM backend (Kimi
   Code by default) and uses the user's quota when `KIMI_CODE_API_KEY` is set. Don't send a chat question unless the
   user said to; without a key the endpoint returns 503, which is expected.
5. To import real data instead, run `.\scripts\import_latest.ps1` against the running
   server (newest `.ibt` in `Documents\iRacing\telemetry`).
6. For the pitwall, open http://127.0.0.1:8000/ and press **Start** on the replay bar
   with `scenario` (no key, no iRacing).
7. Stop the server when done unless the user wants it left running.
