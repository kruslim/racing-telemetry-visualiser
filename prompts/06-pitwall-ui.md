HEADLESS MODE - no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md under "Deviations".
- Before finishing: run the FULL `pytest` suite and iterate until it is completely green (offline - no iRacing, no ANTHROPIC_API_KEY needed). Do not finish with failing or skipped-because-broken tests.
- Also run any smoke script mentioned below and make it pass.
- Update docs/PITWALL.md with what you built, the public interfaces the next stage will consume, and any deviations from this spec.
- Do not refactor or break existing v1 functionality (analysis UI, coaching API, MCP server, existing tests).

TASK:

Read first: frontend/ in full (vanilla-JS canvas architecture, no build step, served by FastAPI at /), docs/PITWALL.md, the /ws/pitwall contract, RadioMessage and RaceState schemas, and the audio manager from the TTS step. Follow the existing frontend's conventions exactly - no frameworks, no bundler, keep it vanilla JS + canvas/DOM, same styling system.

GOAL
Add a LIVE PITWALL mode to the existing analysis app - a during-the-race view that looks and feels like a modern F1/WEC pit stand, fed by /ws/pitwall. The existing MoTeC-style analysis worksheets stay untouched; the pitwall is a new top-level mode with a mode switcher in the header (Analysis | Pitwall).

LAYOUT (single screen, dark, information-dense but glanceable from a rig - assume it may run on a second monitor at 1080p; large type for critical numbers):

1. RADIO FEED (left column, the centrepiece):
  - scrolling feed of RadioMessages, newest at bottom, auto-scroll with pause-on-hover
  - each message: agent badge (color-coded per role: strategist/engineer/spotter/coach), priority styling (critical = high-contrast flash-in), spoken_text prominent, detail_text expandable, timestamp + lap
  - speaking indicator synced to the audio manager (which message is currently on air)
  - driver input at the bottom: text box + send, and the push-to-talk button where supported; sent messages appear in the feed as "DRIVER" entries with the agent's reply threaded under them
  - per-agent mute toggles + master volume live here

2. STRATEGY PANEL (top right):
  - pit window visual: horizontal lap-axis bar showing current lap marker, window open/close laps, fuel-limit lap; updates live
  - fuel: laps remaining vs laps of fuel, per-lap consumption trend sparkline, margin in laps (color: green/amber/red)
  - tyres: per-corner 2x2 temp/pressure widget with trend arrows, laps on set
  - current recommendation summary line (latest strategist RadioMessage data payload)

3. TIMING TOWER (right side, below strategy):
  - standings around the player (P+/-3 at minimum, expandable to full field): position, car #, gap, last lap, pit status
  - player row highlighted; gap-ahead/behind deltas with per-lap trend arrows (closing/opening)
  - blue-flag / traffic warnings inline

4. STATUS STRIP (top): session type, time/laps remaining, current flag state as a full-width color band when not green (yellow/FCY unmistakable at a glance), track/air temp, connection state (live/replay/stale), LLM status + calls-used from /api/v1/pitwall/status

5. EVENT TICKER (bottom): compact one-line stream of raw race events (lockups, off-tracks, rivals pitting) - the deterministic layer made visible, distinct from the AI radio feed above

BEHAVIOUR
- connects to /ws/pitwall; state frames drive panels 2-4 at ~10 Hz max render; radio frames drive panel 1 + audio manager; event frames drive the ticker
- replay controls appear when in replay mode (start/stop, speed) calling the replay API - this doubles as the demo mode
- degrade gracefully: fields the session doesn't capture (capability flags from RaceState) render as "n/a - not captured" rather than zeros; disconnected state is obvious, never silently frozen
- keep total added JS modular (one file per panel + a pitwall main), matching existing file/module conventions; no external CDN dependencies beyond what the frontend already uses
- NO localStorage/sessionStorage anywhere

VISUAL QUALITY BAR
This is a portfolio centrepiece: deliberate typography hierarchy (big numeric readouts, tabular figures for timing), restrained palette consistent with the existing app but with a distinct pit-stand identity, purposeful motion (flag band transitions, critical message flash) - not a grey grid of divs. Look at real F1 pit-wall/timing screens for reference density.

TESTS / DONE
- pitwall page works against the replay demo out of the box (the 60-second demo path from the previous step ends here)
- contract-driven rendering: a frontend/pitwall-test.html fixture page that feeds a canned frame sequence through the panels and verifies key DOM outcomes (window position, gap values, critical styling), documented in docs/PITWALL.md
- no console errors across Chrome + Edge; analysis mode fully unaffected (existing pages regression-checked)
