---
name: publish-pages
description: Rebuild the GitHub Pages copy of the post-race review in docs/ (and optionally re-export the real Spa session) and check it renders. Use after any change to the web review, or to refresh the published session.
---

# Publish the post-race review on GitHub Pages

GitHub Pages serves `docs/` from `main` (Settings > Pages > Source: Deploy from a branch,
branch `main`, folder `/docs`). `docs/index.html` opens `review/?static=../data/spa/`: the
web review in static mode, reading a real session exported as JSON. Nothing generated is
published.

- `frontend/` is the source. `docs/review`, `docs/css`, `docs/js` are copies that
  `scripts/build_pages.py` deletes and rebuilds (the review page, its stylesheets and every
  module it imports). Never edit them by hand.
- `docs/data/spa/` is written by `scripts/export_session.py`; `docs/data/README.md` holds the
  data's attribution and license and is not touched by either script.

1. Re-export when the session or the server-side payloads changed (new journal, findings,
   summary or log fields). It imports the .ibt into a scratch store under the journal's
   session id and calls the real REST endpoints; no model is called (about 3 minutes):
   `.venv/Scripts/python.exe scripts/export_session.py --ibt <spa .ibt> --journal <run .jsonl> --out docs/data/spa`
   The fixture is `/mnt/project-files/fixtures/gt3-audi-r8-spa/audir8lmsevo2gt3_spa_race.ibt`
   in the cloud project files (or `tests/fixtures/real/` after `scripts/fetch_fixtures.py`);
   use the newest real Kimi journal. Update `docs/data/README.md` if the run changes.
2. Rebuild the copies: `.venv/Scripts/python.exe scripts/build_pages.py`.
3. Preview: `.venv/Scripts/python.exe -m http.server 8765 -d docs` in the background, open
   http://127.0.0.1:8765/ (Playwright or the built-in browser), check the console has no
   errors and that the summary, timeline, radio, lap traces, map and findings render. Stop
   the server afterwards.
4. Run pytest (`tests/test_export_session.py` fails if `docs/` is stale) and commit
   `frontend/` and `docs/` together.
