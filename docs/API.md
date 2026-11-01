# REST and WebSocket API

The FastAPI server documents itself at `/docs` (OpenAPI) when it runs. This page is the
short reference for the routes the UIs and the MCP server use. The vision routes are in
[VISION.md](VISION.md#api).

## Routes (prefix `/api/v1`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/variables` | Full variable catalog (live or most recent session) |
| GET | `/sessions` | List sessions (`kind`, `car_id`, `track_id`, paging) |
| GET | `/sessions/{id}` | Session metadata |
| GET | `/sessions/{id}/variables` | This session's catalog |
| GET | `/sessions/{id}/info` | Session-info YAML → JSON (`?seq=`) |
| GET | `/sessions/{id}/laps` | Lap list with times/validity |
| GET | `/sessions/{id}/channels?names=Speed,Throttle&lap=5&max_points=2000&x=lap_dist_pct&mode=minmax` | Multi-channel downsampled query |
| GET | `/sessions/{id}/channels/{name}?lap=5` | Single channel |
| GET | `/sessions/{id}/compare?name=Speed&laps=3,7&grid=1000` | Lap comparison aligned on lap distance |
| GET | `/sessions/{id}/trackmap?color=Speed` | Decimated GPS polyline |
| GET | `/sessions/{id}/track?lap=` | Layout facts from the header + the venue's named corners located on the lap |
| GET | `/tracks` · `/tracks/{key}` | Pre-loaded venues (named corners, straights, pit lane; OSM, ODbL) |
| GET | `/coaching/lap-findings?session_id=…&main_lap=5&ref_lap=3` | Layer-1 corner findings, driver KPIs, analyst stats |
| GET | `/coaching/agents` | The deterministic analysts behind the findings (ids, names, codes) |
| POST | `/coaching/chat` | LLM race engineer: `{session_id, main_lap, ref_lap, question}` → grounded answer + graph annotations (needs `KIMI_CODE_API_KEY`) |
| POST | `/import` · GET `/import/{job_id}` | Async `.ibt` import from a server-side path + status |
| POST | `/import/upload?filename=x.ibt` | Upload an `.ibt` as the raw request body, then import it |
| POST | `/live/start` · `/live/stop` · GET `/live/status` | Live poller control |
| GET | `/racestate` · `/racestate/events` | Live race state + event log (pitwall) |
| POST | `/replay/start` · `/replay/stop` · GET `/replay/status` | Replay a stored session through the live path (`{session_id, speed}`) |
| GET | `/pitwall/status` · `/pitwall/radio` | Agent layer status + the merged radio feed |
| POST | `/pitwall/enabled` · `/pitwall/agents/{name}/enabled` | Kill switch + per-agent flags |
| GET | `/pitwall/tts` · `/pitwall/audio/{message_id}` | Role voice table + synthesised radio audio |
| POST | `/pitwall/driver-message` | Push-to-talk from the cockpit onto the channel |
| GET | `/pitwall/health` | Engine, live poller, replay, agents and director in one payload |
| GET · POST | `/sessions/{id}/debrief?run=` | The run's post-race debrief (404 when none); POST regenerates it from the journal (calls Kimi when a key is set; see PITWALL.md) |
| GET | `/health` | Liveness |

### WebSocket `/ws/live`

```jsonc
// client -> server
{"op":"subscribe","channels":["Speed","RPM","Throttle","SessionFlags"],"rate_hz":30,"include_flags":true}
{"op":"set_rate","rate_hz":10}
{"op":"unsubscribe","channels":["RPM"]}

// server -> client
{"type":"state","connection":"in_session","session_id":"..."}   // or "replay"
{"type":"catalog","schema_hash":"...","variables":[...]}
{"type":"data","tick":91234,"t":123.45,"v":{"Speed":61.2,"RPM":7400},"flags":{"SessionFlags":{"green":true}}}
```

### WebSocket `/ws/pitwall`

Full `RaceState` snapshots at a client-chosen rate (latest-wins), plus every race
event and every agent radio call pushed immediately and never coalesced. See
`docs/PITWALL.md`.

```jsonc
{"op":"subscribe","rate_hz":10}                        // client -> server
{"type":"state","state":{ /* RaceState */ }}           // server -> client
{"type":"event","event":{"key":"pit_window_open", "severity":"advisory", ...}}
{"type":"radio","message":{"agent":"strategist","priority":"critical",
                           "spoken_text":"Box this lap, 3.0 litres, we come out P8."}}
```

---

