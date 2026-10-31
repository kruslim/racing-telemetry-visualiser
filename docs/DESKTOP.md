# RTV Desktop

A native Windows app (PySide6 / Qt 6) for the live pitwall, the radio and the
post-race review, built from the UI redesign brief
(`docs/ui-redesign/UI_REDESIGN_BRIEF.md`, PR #11). It is a client of the same
FastAPI API as the web frontend; by default the server runs **embedded** in the
app, so there is one thing to start. The web frontend is unchanged and still
served (the embedded server serves it too, so `/radio.html` on a tablet keeps
working).

## Run

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[desktop]"
.\run-desktop.ps1                               # embedded server, free port
.\run-desktop.ps1 -Server http://127.0.0.1:8000 # attach to a running .\run.ps1
.\run-desktop.ps1 -DataDir D:\rtv-data          # RTV_DATA_DIR
```

`rtv-desktop` (a GUI entry point from the `desktop` extra) takes the same flags,
plus `--reduced-motion` (default: the Windows "show animations" setting) and
`--mute`. The agents are mounted exactly as for `run.ps1`: `RTV_PITWALL_AGENTS=true`
and `KIMI_CODE_API_KEY` in the environment. Anything that makes them run asks first
(the quota dialog), as the brief requires.

## Build `RTV.exe`

```powershell
.\scripts\build_desktop.ps1
```

Installs `.[desktop,ai]` and PyInstaller into the venv and builds a one-file,
windowed `dist\RTV.exe` from `packaging\rtv-desktop.spec`, with `frontend\` bundled.
The exe keeps its store in `%LOCALAPPDATA%\RTV\data` unless `RTV_DATA_DIR` is set
(`rtv.desktop.paths`), and points `RTV_FRONTEND_DIR` at its bundled frontend.

Keep the data directory short: the Parquet layout nests a session id and a lap
under it, and a long base path runs into Windows' 260-character limit on import.

## Architecture

```
src/rtv/desktop/
  __main__.py        rtv-desktop: args, theme, embedded server or --server, window
  server.py          EmbeddedServer: uvicorn on a daemon thread, waits for /health
  paths.py           frozen-build data and frontend locations
  model/             pure Python ports of the tested frontend logic (no Qt):
    format.py          ← js/pitwall/format.js (n/a, never zero)
    view_model.py      ← js/pitwall/view-model.js
    activity.py        ← js/shared/activity-model.js
    review.py          ← js/review/review-model.js (+ session_clock, aired_times)
    audio_queue.py     ← js/audio-manager.js (radio discipline)
    radio.py           driver threading ("after your message", 45 s)
    vision.py          onboard video sync, visual-note rows, camera lane (docs/VISION.md)
  client/            Api (QtNetwork REST) and JsonSocket (QtWebSockets, backoff)
  theme/             tokens.py (the palette) and qss.py (the stylesheet)
  widgets/           data-free panels and QPainter drawings
  views/             Hub (sockets, polls, radio), LiveView, ReviewView,
                     RadioWindow, MainWindow
  tts.py             QTextToSpeech speaker driven by the audio rules
```

The pure models keep the meaning of every number in one tested place, as the web
frontend does in its node-tested modules. `tests/test_desktop_model.py` restates
the JS suites' rules and runs the ports over the real Spa payloads from the
brief's zip; `tests/test_desktop_widgets.py` paints every panel with those payloads
offscreen; `tests/test_desktop_server.py` boots the embedded server and the window
on an empty store; `tests/test_desktop_vision.py` covers the onboard video panel and
drives the review's add/sync/plan/analyse flow against a fake API. CI runs the Qt tests in their own `desktop` job.

The visual design is the "RTV Desktop" Design canvas made for this work (six
artboards: live pitwall, live states, review, radio window, component sheet,
tokens). `theme/tokens.py` mirrors its Tokens artboard; agent and severity token
names match `frontend/css/pitwall.css`.

## Brief → desktop

| Brief | Desktop |
|---|---|
| L1 flag band | `FlagBand`: green hairline, any other phase full width with icon + word; yellow/red pulse unless reduced motion |
| L2 status strip | `StatusStrip`: session, time/laps left, lap/position/last/best, temps, feed and AI pills |
| L3 replay bar | `ReplayBar`, plus **Start/Stop live capture** (`/live/start`), since the app runs on the rig PC |
| L4 flow graph | `FlowGraph`: every packet is a real activity record (`edges_for`), radio → agent context path dashed |
| L5 timeline | `LaneTimeline`, last 2 min following now |
| L6 agent runs | `RunsWaterfall` on each run's wall clock, click for context/tools/validator/what aired |
| L7 raw telemetry | `TelemetryStrip` from `/ws/live`; a missing channel reads "not in this session" |
| R1 radio | `RadioFeed` (live column and the radio window): on-air strip, queue, audio controls, newest first, refusals as grounded refusals, driver threads |
| R2 strategy | `StrategyPanel`: recommendation card, pit-window bar, fuel block + sparkline, tyre grid |
| R3 tower | `TowerPanel` incl. the no-field state for `.ibt` |
| B1 ticker | `TickerStrip`, severity by icon and colour |
| V1–V6 review | `SessionBar`, `SummaryPanel`, replay lanes + `CarStatePanel` + radio/events so far + the agents at the cursor, `LapTable` + `TraceStack` + `TrackMap`, `FindingsPanel`, `AuditPanel` |
| §5.9 onboard video | `OnboardPanel` under the replay: add a video (by path with the embedded server, else upload), sync it by "lap N starts at video time" with a live frame preview, the frame at the review cursor, the kept visual notes (click to move the cursor; a corner picked in the findings filters them), a **camera** lane on the replay timeline, a free **Plan**, and **Analyse…** behind `VisionSpendDialog` (Cancel is the default), polled while it runs, with **Stop** |
| §3.3 AI controls | `AiControls`: master switch, per-agent toggles, reset, budget; disabled with the reason when not mounted |
| §3.3 quota prompt | `QuotaDialog` before Record a run and before a live replay while agents are mounted and enabled |
| §6 states | empty store, waiting, loading, agents off, degraded, budget exhausted (banner), no field, replay running, socket lost (frozen and greyed), error notice with the server's `detail`, caution flag, stale |

## Deviations from the brief

- **No hold-to-talk mic.** Qt has no speech recognition; the driver message is
  typed. Speech *output* uses the system voices (WinRT/SAPI) with the per-agent
  rate/pitch from `/pitwall/tts`.
- **State is not in a URL.** The window keeps the review's place in memory
  (`ReviewView.state_link()` renders it in the web review's URL form). `QSettings`
  stores only the window geometry; no race data is ever stored client-side.
- **Run spans on the race clock.** Agent records carry the session time of the
  event that woke them, so the desktop maps a run's end and a call's air time onto
  the race clock through records written at the moment (`review.session_clock`,
  `aired_times`). A call therefore shows "L2 · 3:49 → aired 5:17".
- **Frames, not a player.** The onboard panel shows the JPEG the server grabs at the
  cursor (`/videos/{vid}/frame`) instead of embedding a video player, so the app needs
  no codecs and the frame always matches the telemetry cursor. Playing the replay does
  not stream frames; the frame catches up when the cursor stops.
- **Import by path.** With the embedded server the app posts the file's path to
  `POST /import` instead of uploading 160 MB to itself; attached to another server
  (`--server`) it uploads, as the web review does.
