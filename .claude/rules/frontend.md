---
paths:
  - "frontend/**"
---

# Frontend

- `frontend/` is served by FastAPI: the live pitwall at `/` (`index.html`), the
  post-race review at `/review/` (`review/index.html`), plus `radio.html` and the
  self-test pages. Everything needs the backend; nothing is published to Pages and
  nothing falls back to simulated data.
- ES modules under `js/` with absolute `/css/`, `/js/` paths. `js/shared/` (timeline,
  activity model, flow graph) is used by both the live view and the review.
- Pure modules have node suites (`node frontend/js/run-*-tests.mjs`) that pytest runs
  when `node` is on PATH. Keep grouping and maths in those pure modules; panels and
  `*-app.js` files only wire and draw.
- Vanilla JS, no build step, no npm. Text from agents or session files goes in through
  `textContent` (`pitwall/dom.js`), never `innerHTML`.
- Page state lives in the URL, never in storage.
- Input uses pointer events so mouse, touch and pen behave alike (iPadOS Safari is a
  target): no mouse-only handlers.
