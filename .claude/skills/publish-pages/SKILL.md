---
name: publish-pages
description: Rebuild the GitHub Pages showcase in docs/ from frontend/ and check the offline demo still works. Use after any change under frontend/.
---

# Publish the Pages showcase

`frontend/` is the source; `docs/*.html|css|js` are copies served by GitHub Pages at
https://kruslim.github.io/racing-telemetry-visualiser/. With no backend there, `api.js`
falls back to the offline simulation (`sim.js`, `lapdata.js`).

1. Run `.venv/Scripts/python.exe scripts/build_pages.py`. It copies only
   `.html/.css/.js` and leaves `docs/*.md` and `docs/.nojekyll` alone.
2. Check every frontend file now matches its copy:
   `for f in frontend/*; do cmp -s "$f" "docs/${f##*/}" || echo "DIFF $f"; done`.
   A file deleted from `frontend/` stays in `docs/`; point it out rather than deleting it.
3. Preview the offline demo: serve `docs/` with
   `.venv/Scripts/python.exe -m http.server 8765 -d docs` in the background, open
   http://127.0.0.1:8765/ in the built-in browser, and check the console for errors, that
   the graphs and track map render, and that the chat answers from the deterministic
   findings (offline, so no API cost). Stop the server afterwards.
4. Report what changed under `docs/` (`git status docs`). Commit `frontend/` and `docs/`
   together only when the user asks; Pages deploys from `main` once it is pushed.
