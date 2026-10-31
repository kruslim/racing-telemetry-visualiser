# Vision: onboard video and visual notes

Onboard video, synced to the telemetry, plus short VLM-written "visual notes" on the
moments the telemetry already flagged. This page is the backend contract for the UI
redesign: everything a review page needs to show a synced video and the notes is here.
RTV Desktop shows it in the review's onboard video panel (`docs/DESKTOP.md`, §5.9 row); the
web review has no video panel yet.

The rule that shapes all of it: **telemetry picks the moment, the VLM describes it.** A
visual note is words about what the camera saw (kerb use, the line, other cars, damage,
debris). It never carries a figure, it never feeds the race-state engine, and the Layer-1
findings stay the source of every number.

Code: `src/rtv/vision/` (models, video, store, moments, grounding, analyzer, live,
service) and `src/rtv/api/routes_vision.py`. Tests: `tests/test_vision.py`,
`tests/test_vision_agents.py` (offline; the VLM is scripted).

## How it works

1. **Register a video** for a stored session, by a path on the server's disk or by
   upload. ffprobe reads its duration, frame rate and size.
2. **Sync it.** A video has its own clock (`video_time`, seconds from its first frame).
   Sync maps it onto `session_time`: `session_time = video_time * rate + offset`. Give
   one of:
   - `offset_s`: the session time at the video's first frame;
   - `lap_starts`: line crossings you can see in the video, `[{lap, video_time}]`. Each
     one is anchored to that lap's `start_session_time` from `/sessions/{id}/laps` (RTV
     lap numbers, as the review shows them; they match iRacing's `Lap`);
   - `anchors`: raw `[{video_time, session_time}]` pairs.

   Two or more anchors are fitted by least squares, which absorbs a capture clock that
   drifts against the sim (a 59.94 fps recording, dropped frames). A fit that implies the
   video runs more than 10 % fast or slow is refused as a typo. `residuals` shows the
   error per anchor.
3. **Plan the moments** (free, a dry run). Two deterministic sources:
   - race events from the session's race journal: `incident`, `offtrack`, `lockup`,
     `recurring_issue`, `traffic_close`, `wheelspin`. A journal exists once the session
     was replayed (`POST /api/v1/replay/start`, free with the agents off) or captured live;
   - Layer-1 findings: the chief summary's top-3 corners on each lap the video covers,
     against the fastest clean valid lap, located in time from the lap's `LapDist` trace.

   Each event gives a window from 2 s before to 1 s after it; a corner gives its entry to
   exit window, at most 4 s. Overlapping windows on the same lap merge into one moment
   (an off at the corner that also lost the most time is one look). Moments the video does
   not cover are dropped, and the rest are cut to the frame budget by trigger rank:
   incident, offtrack, lockup, recurring_issue, finding (larger gain first),
   traffic_close, wheelspin.
4. **Analyse** (paid: Kimi Code quota). For each moment, ffmpeg grabs
   `RTV_VISION_FRAMES_PER_MOMENT` frames spread over the window, scaled to
   `RTV_VISION_FRAME_WIDTH`, and one kimi-k3 call gets them as base64 image blocks
   through `rtv.llm` (thinking off, the answer forced through the respond tool). The
   model is told only why the moment was flagged, in words ("the car went off track",
   "time was lost through T7"), never a number.
5. **Ground** every observation before it is stored as kept:
   - **number-free:** any digit, or a spelled-out measurement ("half a second", "two car
     lengths", "third gear"), drops the observation. The moment's own corner label (`T7`)
     is the one allowed token with a digit;
   - **telemetry wins:** a kerb claim is checked against `VertAccel` in the window when
     the session has it. A claim of kerb contact on a flat trace (peak deviation under
     2 m/s²) is `contradicted`; one on a spike (over 5 m/s²) is `corroborated`; anything
     else is `uncheckable`;
   - if the model says the frames do not show the flagged moment (`trigger_visible: no`),
     the note's status is `trigger_not_seen` and its observations drop to low confidence.
     The telemetry still says it happened, so this almost always means the sync is off.
6. **Store** each note in the session's vision journal (below) and serve it to the review,
   the coach and the MCP server.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `RTV_VISION_MODE` | `off` | `off`: no VLM calls (videos, sync, frames, plans and stored notes all work). `post`: post-race analyses may call Kimi. `live`: `post`, plus kept notes for the session the engine is consuming become `visual_observation` events. |
| `RTV_VISION_MODEL` | empty | Empty = `RTV_LLM_KIMI_MODEL_REASONING` (kimi-k3). |
| `RTV_VISION_MAX_FRAMES` | 60 | Frame budget per analysis. `max_moments = max_frames // frames_per_moment`. |
| `RTV_VISION_FRAMES_PER_MOMENT` | 4 | Frames sent per moment. |
| `RTV_VISION_FRAME_WIDTH` | 768 | Frames are scaled down to this width (px); never up. |
| `RTV_VISION_FFMPEG` / `RTV_VISION_FFPROBE` | `ffmpeg` / `ffprobe` | Binaries, on PATH or full paths. |

ffmpeg is a system binary, not a Python package. On Windows: `winget install ffmpeg`.
`GET /api/v1/vision/status` reports whether both binaries were found.

Cost: the one verified call (a 320x200 JPEG) took 151 input tokens and 3.2 s. Real
768-wide frames cost more; measure on a real clip before raising the budget. At the
defaults one analysis is at most 15 calls of 4 frames.

## Storage

Beside the race journal of the same session (`data/sessions/<session_id>/`):

```
vision/videos.json                    registered videos (VideoInfo list)
vision/videos/<id8>_<name>            uploaded files (deleted when unregistered)
vision/runs/<analysis_id>.jsonl       one analysis, in the race-journal envelope
vision/frames/<analysis_id>/*.jpg     the frames each note sent
```

An analysis file uses the race journal's line envelope (`seq`, `wall_ms`, `session_time`,
`tick`, `kind`, `agent: "vision"`, `run_id: <analysis_id>`, `event_key`, `data`) with two
kinds: `lifecycle` (`phase: start` with the plan, `phase: end` with the status) and
`visual_note` (one per moment, `data` is a VisualNote, `session_time` is the moment's,
`event_key` its primary trigger). It is kept out of `runs/` on purpose, so
`journal.default_run` never picks a vision pass over the agents' run. The review joins the
two on `session_time`.

`analysis_id` is `<UTC stamp>-<nn>-<video_id>`, so analyses sort in the order they ran.
The default analysis everywhere is the newest complete one, else the newest.

## API

All under `/api/v1`. Errors: 400 bad input, 404 unknown session/video/analysis/lap,
409 a paid analysis while `RTV_VISION_MODE=off` (and a time conversion on an unsynced
video), 416 a frame outside the video, 503 no Kimi key or no ffmpeg.

### Status

`GET /vision/status`

```json
{"mode": "post", "analysis_enabled": true, "live_events": false, "key_present": true,
 "model": "kimi-k3", "tools": {"ffmpeg": true, "ffprobe": true},
 "budget": {"max_frames": 60, "frames_per_moment": 4, "max_moments": 15, "frame_width": 768}}
```

### Videos

| Method and path | Body / query | Returns |
|---|---|---|
| `GET /sessions/{sid}/videos` | | `{session_id, videos: [Video]}` |
| `POST /sessions/{sid}/videos` | `{path, label?, sync?}` | 201 `Video` |
| `POST /sessions/{sid}/videos/upload?filename=&label=` | raw bytes (`application/octet-stream`) | 201 `Video` |
| `GET /sessions/{sid}/videos/{vid}` | | `Video` |
| `DELETE /sessions/{sid}/videos/{vid}` | | `{deleted, file_deleted}`; only an uploaded file is deleted |
| `PUT /sessions/{sid}/videos/{vid}/sync` | exactly one of `{offset_s}`, `{lap_starts: [{lap, video_time}]}`, `{anchors: [{video_time, session_time}]}` | `Video` |
| `GET /sessions/{sid}/videos/{vid}/time?session_time=` or `?video_time=` | | `{session_time, video_time, in_video}` |
| `GET /sessions/{sid}/videos/{vid}/file` | | the file, with HTTP Range, for a `<video>` element |
| `GET /sessions/{sid}/videos/{vid}/frame?session_time=` or `?video_time=`, `&width=640` | | `image/jpeg` |

Accepted containers: `.mp4 .mkv .mov .webm .avi .m4v`. For a browser `<video>`, H.264 MP4
(what OBS records by default) plays everywhere.

`Video`:

```json
{
 "video_id": "9e4e6c3132", "session_id": "...", "path": "D:/Videos/spa onboard.mp4",
 "label": "spa onboard", "uploaded": false,
 "duration_s": 210.0, "fps": 60.0, "width": 1920, "height": 1080, "codec": "h264",
 "bytes": 104857600, "registered_wall_ms": 1793416265371,
 "sync": {
  "anchors": [
   {"video_time": 7.2, "session_time": 97.2, "source": "lap_start:2"},
   {"video_time": 106.5, "session_time": 196.47, "source": "lap_start:3"}
  ],
  "rate": 0.99966, "offset": 90.0024, "synced": true, "residuals": [0.0, 0.0]
 },
 "session_time_start": 90.0024, "session_time_end": 299.93
}
```

To keep a `<video>` element in step with the review's session-time cursor, the UI can do
the arithmetic itself: `video_time = (session_time - offset) / rate`, and back. The `/time`
endpoint does the same for a client that prefers not to.

### Plan and analyses

`AnalysisBody` (both endpoints):

```json
{"video_id": "9e4e6c3132",
 "sources": ["events", "findings"],
 "laps": null, "ref_lap": null, "event_run": null, "max_moments": null}
```

`laps` limits finding moments (default: every timed lap the video covers that is not an
in- or out-lap); `ref_lap` defaults to the fastest clean valid lap; `event_run` to the
newest race-journal run; `max_moments` is capped by the frame budget.

| Method and path | Returns |
|---|---|
| `POST /sessions/{sid}/vision/plan` | free dry run: `{model, moments: [Moment], count, frames, candidates, skipped_outside_video, skipped_over_budget, event_run, notes: [str]}` |
| `POST /sessions/{sid}/vision/analyses` | 202 `{analysis_id, status: "running", done, total, live_events, error}`; uses Kimi quota |
| `GET /sessions/{sid}/vision/analyses` | `{default_analysis, analyses: [AnalysisSummary]}` |
| `GET /sessions/{sid}/vision/analyses/{aid}` | `AnalysisSummary` plus `items: [Note]` (every note, kept or not) |
| `POST /sessions/{sid}/vision/analyses/{aid}/cancel` | stops after the moment in flight |
| `GET /sessions/{sid}/vision/frames/{aid}/{name}` | a frame the analysis sent (`image/jpeg`) |

`AnalysisSummary`: `{analysis_id, video_id, model, started_wall_ms, ended_wall_ms,
complete, status, moments_planned, notes, observations_kept, note_status: {ok: n, ...},
error, progress?: {done, total}}`. `status` is `running`, `complete`, `cancelled` or
`failed`. Poll the single-analysis endpoint while it is `running`; each note is written as
soon as its call returns, so the UI can show notes arriving.

### Notes

`GET /sessions/{sid}/vision/notes?analysis=&lap=&corner=&from=&to=&include_dropped=false`

Returns `{session_id, analysis_id, count, items: [Note]}`. By default only notes with at
least one kept observation. `corner` is a Layer-1 label (`T7`); `from` / `to` are session
times and match any moment whose window overlaps.

`Note`:

```json
{
 "note_id": "20261031T031106Z-01-9e4e6c3132:m2-finding-1184",
 "analysis_id": "20261031T031106Z-01-9e4e6c3132", "video_id": "9e4e6c3132",
 "moment": {
  "moment_id": "m2-finding-1184",
  "session_time": 118.42, "t_start": 116.42, "t_end": 120.42,
  "lap": 2, "corner": "T3", "lap_dist_pct": 0.19,
  "triggers": [{
   "kind": "finding", "session_time": 118.42, "lap": 2,
   "journal_seq": null, "journal_run": null,
   "corner": "T3", "ref_lap": 6, "gain_s": 0.934,
   "label": "time was lost through T3 against the reference lap",
   "detail": {"type": "Hairpin", "why": "Min speed -9 km/h vs reference", "distance_m": 761.4}
  }],
  "facts": {"triggers": ["finding"], "kerb_signal": null, "vert_accel_peak_dev_ms2": null}
 },
 "frames": [
  {"name": "m2-finding-1184-0.jpg", "video_time": 26.423, "session_time": 116.417}
 ],
 "status": "ok", "trigger_visible": "yes",
 "observations": [
  {"kind": "kerb_use", "text": "Rode the inside kerb hard at the apex and the car skipped wide.",
   "confidence": "medium", "status": "uncheckable", "reason": null},
  {"kind": "traffic", "text": "A car was about 2 lengths behind.",
   "confidence": "low", "status": "dropped", "reason": "Number-free rule: contains a number."}
 ],
 "kept": [
  {"kind": "kerb_use", "text": "Rode the inside kerb hard at the apex and the car skipped wide.",
   "confidence": "medium", "status": "uncheckable", "reason": null}
 ],
 "model": "kimi-k3", "latency_ms": 3210, "usage": null, "error": null
}
```

Field notes for the UI:

- `kept` is what to show by default. `observations` also has the dropped and contradicted
  ones with a `reason`, for an audit view.
- `status`: `ok`; `trigger_not_seen` (show a "check the video sync" hint, observations are
  low confidence); `no_answer` (the model gave no valid reply twice); `error` (`error` says
  why: a frame past the end of the video, a model failure).
- `observation.kind`: `kerb_use`, `line`, `traffic`, `car_behaviour`, `damage`,
  `track_state`, `other`. `status`: `corroborated` (telemetry agrees), `uncheckable`
  (nothing in the telemetry can check it), `contradicted` and `dropped` (never kept).
- A trigger's `journal_seq` and `journal_run` point at the race event in
  `GET /sessions/{sid}/pitwall-log?run=` that made the moment, so a note can sit on the
  same timeline marker. `gain_s`, `detail` and `facts` are telemetry-side numbers for
  display; none of them was sent to the model.
- Pin a note to the lap trace with `moment.lap` plus `moment.lap_dist_pct`, or to the
  findings' corner list with `moment.corner`. Event-only moments have no `corner` (the
  live detectors' `C09` buckets are not Layer-1 labels); their trigger `detail` keeps it.

### For the coach

- `GET /sessions/{sid}/vision/coach-notes?lap=` returns the compact shape the coach and
  the MCP server read: `{analysis_id, available, notes: [{lap, corner, trigger,
  trigger_seen, observations: [{kind, text, confidence, status}]}], caveat}`.
- `GET /coaching/lap-findings?...&include_visual=true` adds the same block for the main
  lap as `visual_notes`. Without the flag the findings are byte-for-byte unchanged.
- The Layer-3b graph coach has a `get_visual_notes` tool, and the MCP server a tool of the
  same name. The coach prompt says to use the notes for how a corner went wrong and never
  as a source of figures; its citation checks are unchanged, since notes carry no numbers.

### In the post-race summary

`GET /sessions/{sid}/summary` gains a `vision` block:

```json
{"videos": 1, "synced_videos": 1, "analysis_id": "20261031T031106Z-01-9e4e6c3132",
 "notes": 2, "observations_kept": 4, "by_kind": {"kerb_use": 2, "line": 2},
 "note_status": {"ok": 2}}
```

## Live

The research plan's order is post-race first, live later and event-triggered only, so
**live frame capture is not built**. The seam it will use is built and tested:

- `EventType.VISUAL_OBSERVATION` (`visual_observation`). No detector emits it and the
  engine never reads it. Payload: `{source: "vision", kind, text, confidence, status,
  note_id, corner, trigger: {kind, session_time, journal_seq}}`, severity `info`.
- `rtv.vision.live.publish_note(engine, session_id, note)` puts one event per kept
  observation on the engine's bus, only when the engine is consuming that session. With
  `RTV_VISION_MODE=live`, every analysis does this as notes arrive. From the bus the
  recorder journals them, `/ws/pitwall` streams them, and the agents may react.
- The **spotter** wakes on `traffic`, `track_state` and `damage` notes (cooldown 60 s);
  the **coach** on `kerb_use`, `line` and `car_behaviour` notes under green (cooldown
  120 s). The strategist and vehicle engineer never do. A call triggered by a visual
  observation always airs at `info` priority, whatever the agent asked for, so it never
  pre-empts a fuel, flag or traffic call.

Live capture, when built, would stamp each grabbed frame with the session time of the
telemetry frame received at the same moment (an offset of a few frames, fine for
descriptions), trigger on the same detector events, and call `VisionAnalyzer` as the
post-race path does.

## Not built yet

- Live frame capture (OBS or ffmpeg on the iRacing PC).
- Driving iRacing's replay through the SDK to capture any car and camera post-race.
- Automatic sync from the video itself; sync is set from line crossings or an offset.
- Token usage on notes (`usage` is null until the provider reports it).
- Video input to the VLM (Kimi lists it for K3; untested on the Kimi Code endpoint).
