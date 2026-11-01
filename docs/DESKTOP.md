# RTV Desktop

A native Windows app (PySide6 / Qt 6) for the live pitwall, the radio and the
post-race review, built from the UI redesign brief
(`docs/ui-redesign/UI_REDESIGN_BRIEF.md`, PR #11). It is a client of the same
FastAPI API as the web frontend; by default the server runs **embedded** in the
app, so there is one thing to start.

**This is the primary UI.** New UI features are built here only. The web frontend
(`/`, `/review/`, `/radio.html`) is frozen: it is still served (the embedded server
serves it too, so `/radio.html` on a tablet keeps working) and gets bug fixes, but no
new features. The web review stays for the GitHub Pages export of a real session.

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

## Push-to-talk questions

Hold a key (or a wheel button), ask the pitwall a question, let go. The words go to
`POST /pitwall/driver-message` with `source: "voice"`, the same route as the typed
"ask the pitwall" box (C1): a lookup (fuel laps, gaps, lap times, position, laps to go,
tyre temps) is answered from the engine at once, anything else wakes the owning agent.
Speech-to-text runs **on this PC** with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
on the CPU (int8): no audio leaves it, and no Kimi call is made to hear you.

What you see: the radio column gets a **Hold to talk** row. While you hold the key it
says *Listening…*; after you let go it shows **Heard: "…"** before the answer arrives,
and your question joins the radio feed tagged **VOICE**. While the radio is speaking
the microphone is muted (*Radio on air: mic muted*), so the pitwall's own voice from
your speakers is never taken for yours. The journal records the question with
`source: "voice"` (the driver radio record and the `driver_question` event).

### Set it up (Windows)

1. Install the extra into the venv (one time):

   ```powershell
   uv pip install --python .venv\Scripts\python.exe -e ".[desktop,voice]"
   ```

2. Add to `.env` (see `.env.example`):

   ```
   RTV_VOICE_ENABLED=true
   RTV_VOICE_KEY=ScrollLock
   RTV_VOICE_MODEL=base.en
   ```

3. Allow microphone access for desktop apps: **Settings > Privacy & security >
   Microphone**, turn on *Microphone access* and *Let desktop apps access your
   microphone*. RTV records the Windows **default** input device (Settings > System >
   Sound > Input), so pick your headset mic there.
4. Start the app (`.\run-desktop.ps1`). The **Hold to talk** button shows the key.
5. First question: hold the button with the mouse (or the key), say *"How many laps
   of fuel?"*, release. The first use downloads the model (base.en is about 150 MB)
   to `<data dir>\models\whisper` and says *Loading the speech model…*; after that it
   stays on the PC and is loaded in the background at start-up. On a desktop CPU,
   base.en takes about a second for a short question.
6. Start a replay (or drive live), alt-tab into iRacing and hold the key there: the
   key is read system-wide, so it works while the sim has the focus.

If something is missing the button says **Voice off** and the row says why: the extra
is not installed, there is no microphone, or `RTV_VOICE_KEY` is not a key RTV knows.
`RTV_VOICE_ENABLED=false` (the default) hides the row.

### Choosing the key, or a wheel button

`RTV_VOICE_KEY` takes a key name: `ScrollLock` (the default), `Pause`, `F1`-`F24`,
`A`-`Z`, `0`-`9`, `Numpad0`-`Numpad9`, `Space`, `Insert`, `Home`, `End`, `PageUp`,
`PageDown`, `CapsLock`, `LCtrl`/`RCtrl`, `LShift`/`RShift`, `LAlt`/`RAlt`. Pick one
iRacing does not use, or the sim will act on it too.

A wheel or button-box button can be used directly: `RTV_VOICE_KEY=joy<device>:<button>`,
for example `joy1:5`. To find the numbers, run this and press the button:

```powershell
.venv\Scripts\python.exe -m rtv.desktop.voice --find-button
# Controller 2, button 7: RTV_VOICE_KEY=joy2:7
```

Buttons are numbered as in Windows' *Set up USB game controllers* (`joy.cpl`); RTV
reads them with `joyGetPosEx`, which sees the first 32 buttons of the first 16
controllers. If your button is beyond that, or you'd rather map it in software, have
the wheel's own software (Fanatec FanaLab, Simucube Tuner, Moza Pit House) or a
mapper such as JoyToKey send a key that nothing else uses, such as `F13`, and set
`RTV_VOICE_KEY=F13`. iRacing still sees the button as long as you don't also bind
it there.

`RTV_VOICE_MODEL=small.en` is more accurate on accents and noisy rigs (about 480 MB,
roughly three times slower); `tiny.en` is the quickest. Off Windows the key only works
while an RTV window has the focus; the button works everywhere.

`RTV.exe` does not bundle the voice extra yet; run from source (`run-desktop.ps1`)
for push-to-talk.

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
  voice.py           push-to-talk: key / wheel-button watcher, mic, faster-whisper
```

The pure models keep the meaning of every number in one tested place, as the web
frontend does in its node-tested modules. `tests/test_desktop_model.py` restates
the JS suites' rules and runs the ports over the real Spa payloads from the
brief's zip; `tests/test_view_golden.py` runs the shared golden file
(`tests/fixtures/view_golden/`) that the JS modules must also pass under node, so a
rule that drifts between the two clients fails a test; `tests/test_desktop_widgets.py` paints every panel with those payloads
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
| live onboard (`docs/VISION.md`, Live) | `LiveOnboardPanel` above the radio in the live pitwall: polls `GET /vision/live` each second; shows the capture's newest frame (`/vision/live/frame`, 4 per second) or, in a replay of a session with a synced recording, the server-grabbed frame at the replay's session time (2 per second, so at high replay speeds it trails by a moment); **Capture video** / **Stop video** (free; the stopped recording is registered on the session, synced); the live VLM's calls against its cap and its notes, newest first |
| §3.3 AI controls | `AiControls`: master switch, per-agent toggles, reset, budget; disabled with the reason when not mounted |
| §3.3 quota prompt | `QuotaDialog` before Record a run and before a live replay while agents are mounted and enabled |
| §6 states | empty store, waiting, loading, agents off, degraded, budget exhausted (banner), no field, replay running, socket lost (frozen and greyed), error notice with the server's `detail`, caution flag, stale |

## Deviations from the brief

- **Hold-to-talk mic via faster-whisper.** Qt has no speech recognition, so
  push-to-talk uses faster-whisper locally (the optional `[voice]` extra, see
  [Push-to-talk questions](#push-to-talk-questions)); without it the question is
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
