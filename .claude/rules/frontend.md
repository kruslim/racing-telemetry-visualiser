---
paths:
  - "frontend/**"
  - "docs/**"
---

# Frontend and the Pages copy

- `frontend/app/` (the analysis app, served at `/app/`) is the only source for Pages.
  `docs/*.html|css|js` are generated from it by `scripts/build_pages.py`; never edit
  them by hand. `docs/*.md` are real docs.
- The rest of `frontend/` is the live pitwall, served at `/`. It needs the backend, is
  not published to Pages, and uses ES modules under `js/` with absolute `/css/`,
  `/js/` paths. Its pure modules have node suites (`node frontend/js/run-*-tests.mjs`)
  that pytest runs when `node` is on PATH.
- Vanilla JS, no build step, no npm. In the analysis app each file is an IIFE `(function (global) {
  'use strict'; ... })` that hangs its module on `window`; `index.html` loads them in
  dependency order with plain `<script>` tags.
- The app must work in both modes: against the API, and with no backend (Pages), where
  `api.js` falls back to `sim.js` + `lapdata.js` and the chat coaches from the
  deterministic findings. Test a change in both.
- Input uses pointer events so mouse, touch and pen behave alike (iPadOS Safari is a
  target): no mouse-only handlers, and keep pinch-to-zoom working on the graphs.
- After a change to `frontend/app/`, run the `publish-pages` skill and commit
  `frontend/app/` and `docs/` together.
