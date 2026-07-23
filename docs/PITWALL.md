# RTV v2 — Pitwall build state

Shared state between the staged headless builds. Each stage appends: what it
built, the public interfaces the next stage consumes, and any deviations from
its spec (with a one-line rationale).

| Stage | Scope | Status |
|-------|-------|--------|
| **1** | Deterministic race-state engine + event bus + replay | **done** |
| 2 | Agent framework + strategist | not started |
| 3 | Vehicle engineer / spotter / coach agents | not started |
| 4 | TTS radio voice | not started |
| 5 | Director + stub hardening | not started |
| 6 | Pitwall UI | not started |

---

## Stage 1 — deterministic race-state engine

`src/rtv/racestate/` — **no LLM calls anywhere in this package.** It consumes the
existing 60 Hz frame stream and maintains one continuously-updated `RaceState`
plus a typed event bus. Every number it reports is traceable to a channel present
in the session's runtime catalog; anything unbacked stays `None` and a capability
flag records why (the Layer-3b grounded-refusal discipline, applied to state).

### Module map

| Module | Role |
|--------|------|
| `channels.py` | The channel-name contract + capability groups. Nothing is required to exist. |
| `models.py` | `RaceState`, `RaceEvent` and every sub-model (Pydantic). |
| `detectors.py` | Pure functions over a frame window. No state, no event construction. |
| `bus.py` | `EventBus` / `Subscription`: thread-safe producer, sync **and** asyncio consumers. |
| `engine.py` | `RaceStateEngine`: the incremental per-frame update + detector latching. |
| `replay.py` | `ReplayDriver` + `scenario_source()` / `store_source()`. |
| `scenario.py` | The scripted synthetic race used as test ground truth. |

### The one ingress

```python
engine.on_frame(frame: Frame, catalog: Catalog) -> None
```

This is deliberately the *exact* signature of the live poller's existing
`on_frame` callback. `services.build_services()` wires it alongside
`hub.publish_frame`, and the replay driver calls the same method. **The engine
cannot tell live frames from replayed ones** — which is what makes the
"replay twice, compare the event log" test a real check on the live path.

The live wiring is defensive: a race-state exception is logged and swallowed so
it can never break telemetry capture or the v1 `/ws/live` feed.

---

## RaceState schema

One object, versioned by `version` (monotonic per update) and stamped with
`tick` / `session_time`.

| Group | Key fields | Backed by |
|-------|-----------|-----------|
| `session` | `state`, `time_remaining`, `laps_remaining`, `laps_total`, `car_id`, `track_id`, `track_name`, `lap_length_m`, `source` | `SessionState`, `SessionTimeRemain`, `SessionLapsRemainEx`, session-info YAML |
| `flags` | `raw`, `phase`, `active[]`, `since_session_time`, `time_in_state` | `SessionFlags` bitfield |
| `standings` | `cars[]`, `player_idx`, `gap_basis`, `lap_length_m`, `reference_lap_time` | `CarIdx*` per-car-index arrays |
| `player` | `lap`, `position`, `lap_dist_pct`, `speed`, `last/best_lap_time`, `on_pit_road`, `track_surface`, `incidents`, `stint`, `laps_on_tyres`, `stint_start_lap` | player scalars |
| `fuel` | `level`, `level_pct`, `capacity`, `per_lap`, `per_lap_std`, `samples`, `laps_remaining`, `laps_to_finish`, `fuel_to_finish`, `margin_l`, `margin_laps`, `pit_window_earliest_lap`, `pit_window_latest_lap`, `window_open` | `FuelLevel`, `FuelLevelPct` |
| `tyres` | `temps{}`, `pressures{}`, `temp_trend{}`, `pressure_trend{}`, `stint_laps` | `{LF,RF,LR,RR}tempCM` / `pressure`, or flattened `TyrePressure_0..3` |
| `car_health` | `oil_temp`, `water_temp`, `oil_pressure`, `fuel_pressure`, `voltage`, `tow_time`, `engine_warnings[]` | health scalars + `EngineWarnings` |
| `conditions` | `air_temp`, `track_temp`, `humidity`, `wind_*`, `track_wetness`, `air_temp_trend`, `track_temp_trend` | weather scalars |
| `capabilities` | one bool per group + `missing[]` | probed once per catalog bind |
| `metrics` | `frames`, `events`, `last/avg/max_update_ms` | engine self-timing |

`CarState` (one per active car): `idx`, `is_player`, `position`, `class_position`,
`lap`, `lap_dist_pct`, `laps_completed`, `last/best_lap_time`, `on_pit_road`,
`track_surface`, `gap_ahead`, `gap_behind`, `gap_to_player`.

### Grounding rules worth knowing

- **Gaps.** `gap_basis` names how lap-distance deltas became seconds:
  `"lap_time_pct"` (Δpct × a known lap time — preferred, stable) or
  `"lap_length_speed"` (Δpct × track length ÷ instantaneous speed). With neither
  available, `gap_basis` is `None` and **every gap is `None`** rather than guessed.
  `gap_ahead` / `gap_behind` are running-order gaps; `gap_to_player` is a signed
  *track-position* gap wrapped to ±half a lap (positive = ahead on track).
- **Fuel.** `capacity` is derived as `level / level_pct` — a real measurement, not
  a constant. `per_lap` is a rolling mean over the last N **green, non-pit** laps
  (`RTV_PITWALL_FUEL_LAPS`, default 5), with `per_lap_std` alongside so a consumer
  can see the spread. Derivations refresh at ~1 Hz *and* at lap boundaries, so
  `level` and `laps_remaining` are never mutually inconsistent.
- **Pit window.** `latest = lap + floor(level / per_lap)` (run-dry bound);
  `earliest = lap + max(0, ceil(laps_to_finish - capacity / per_lap))` (stopping
  earlier would force a second stop). `window_open` additionally requires a stop
  to actually be **needed** (`margin_laps < 0`) — otherwise "open" would be
  technically true but useless advice, and would re-announce after every stop.
- **Tyre trends** are cleared on pit exit: a trend from the set that just came off
  is not stale data, it is data about a different tyre.
- **Missing channels** never produce a zero. `capabilities.<group>` goes `False`,
  the fields stay `None`, and `capabilities.missing[]` names what was looked for.

---

## Event catalog

`RaceEvent` = `{event_type, tick, session_time, lap, severity, payload, state_version}`.
`state_version` pins the event to the `RaceState` version current at emission.
There is **no wall-clock field** — that is what makes the replay log reproducible.

`event.key` is a stable discriminator used by tests and the UI: the event type,
except for flag changes which render as `flag_change:<phase>`.

| `event_type` | Severity | Fires when | Key payload |
|---|---|---|---|
| `flag_change` | critical (red/yellow) else info | `SessionFlags` phase changes | `from`, `to`, `active[]` |
| `lap_completed` | info | `Lap` increments | `lap`, `lap_time`, `stint`, `green` |
| `personal_best` | info | lap time beats the player's best | `lap`, `lap_time` |
| `session_fastest_lap` | info | lap time beats the session best seen | `lap`, `lap_time` |
| `lockup` | advisory | front wheel speed collapses under braking | `slip`, `corner`, `speed`, `brake` |
| `wheelspin` | info | driven wheel outruns the car under power | `slip`, `corner`, `speed`, `throttle` |
| `offtrack` | advisory | `PlayerTrackSurface` enters off-track | `surface`, `from`, `lap_dist_pct` |
| `incident` | critical | incident counter increases | `incidents`, `delta` |
| `pit_entry` / `pit_exit` | info | `OnPitRoad` edge | `lap`, `stint`, `fuel` |
| `stint_start` | info | immediately after `pit_exit` | `stint`, `lap` |
| `pit_window_open` | advisory | fuel window opens (see rules above) | `earliest_lap`, `latest_lap`, `laps_remaining`, `per_lap` |
| `fuel_critical` | critical | `laps_remaining < 1.5`; re-arms on refuel | `level`, `laps_remaining` |
| `blue_flag` | advisory | a car ≥0.7 laps up is closing within 2.5 s | `car_idx`, `gap`, `laps_ahead` |

**Latching.** Continuous detectors (lockup, wheelspin, blue flag) fire once per
episode: they re-arm only after the condition clears, with a cooldown floor. Edge
detectors (pit, off-track, incident, lap, flag) are naturally one-shot.

---

## Event bus

```python
sub = engine.bus.subscribe(maxsize=256, name="strategist")   # queue style
events = sub.drain()                                          # sync
events = await sub.next_events(timeout=1.0)                   # asyncio
engine.bus.subscribe_callback(fn)                             # sync fan-out
engine.bus.history(limit)                                     # bounded ring buffer
```

Producer is a plain thread; async consumers are woken via
`loop.call_soon_threadsafe`. Each subscriber has a **bounded** queue that drops
the *oldest* event on overflow and counts the drops (`sub.dropped`) — a pitwall
that silently discarded a black flag would be worse than one that admits it.
A raising callback is logged and cannot stop the loop.

---

## Replay

```python
from rtv.racestate import RaceStateEngine, replay_scenario
from rtv.racestate.replay import ReplayDriver, scenario_source, store_source

engine = RaceStateEngine(source="replay")
replay_scenario(engine)                                  # scripted race, instant

ReplayDriver(engine).run(store_source(db, parquet_dir, session_id), speed=1.0)
ReplayDriver(engine).start(scenario_source(), speed=4.0) # background thread
```

`speed`: `1.0` real time, `N` for N× real time, `0` as-fast-as-possible.
`store_source()` streams **lap partition by lap partition**, so a full race
distance never lands in memory at once.

### The scripted scenario (test ground truth)

`ScenarioSpec` defaults to a 6-lap, 8-car race at 60 Hz with a 10.0 s lap:

| Lap / position | Scripted |
|---|---|
| 3, 30–80 % | full-course yellow, then green |
| 4, ~40 % | front-axle lock-up under braking |
| 4 → 5 boundary | fuel pit window opens |
| 5, 85 % | pit entry |
| 6, 15 % | pit exit + refuel + new stint |

Fuel arithmetic is chosen so the window lands on lap 5 *exactly*: a 4.0 L tank at
0.5 L/lap covers 8 laps, the race is 12 laps, and the car starts on 3.0 L — so
from lap 5 a full tank is both sufficient to reach the flag and necessary to
avoid running dry. Grid offsets are in laps, so a 10 s lap turns a 0.03-lap
offset into exactly a 0.30 s gap, making gap assertions exact.

`ScenarioSpec(exclude=(...))` omits channels from both the catalog and the frames
— this is how the degradation tests strip tyres / fuel / wheels / standings.

`seed_scenario_session(writer, session_id)` writes the same race through the real
storage writer, so it can also be replayed back out of DuckDB/Parquet.

---

## API surface (added; all v1 endpoints untouched)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/racestate` | Current `RaceState` snapshot |
| GET | `/api/v1/racestate/events?limit=` | Recent events from the ring buffer |
| POST | `/api/v1/replay/start` | `{session_id, speed}`; `session_id:"scenario"` for the scripted race |
| POST | `/api/v1/replay/stop` | Stop the running replay |
| GET | `/api/v1/replay/status` | `{running, session_id, speed, frames, total, finished, error}` |

### WebSocket `/ws/pitwall`

```jsonc
// client -> server
{"op":"subscribe","rate_hz":10}
{"op":"set_rate","rate_hz":2}

// server -> client
{"type":"state","state":{ /* full RaceState */ }}      // at rate_hz, latest-wins
{"type":"event","event":{ /* RaceEvent + key */ }}     // immediately, never coalesced
{"type":"ack","op":"subscribe","rate_hz":10}
{"type":"error","code":"unknown_op"}
```

Two payloads, two policies, for a reason: an old **state** snapshot is worthless
once a newer one exists, so slow clients skip ahead. An **event** is a discrete
fact — dropping one to save bandwidth would silently lie to the strategist.
A snapshot is pushed immediately on connect so a client is never blank.

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `RTV_PITWALL` | `true` | Mounts the engine, the routes above and `/ws/pitwall`. `false` ⇒ v1 surface only, `services.engine is None`. |
| `RTV_PITWALL_GAP_INTERVAL` | `6` | Recompute standings gaps every Nth frame (6 @ 60 Hz = 10 Hz). |
| `RTV_PITWALL_FUEL_LAPS` | `5` | Green laps in the rolling consumption mean. |

---

## Performance

The spec's budget is <16 ms per frame. Measured on the 3 360-frame scenario:
**avg 0.03 ms, max 0.24 ms** per frame — ~500× inside budget. Reported live in
`state.metrics` rather than assumed.

Nothing is recomputed wholesale per tick. Per frame: player, flags, fuel level,
tyres, health, conditions, detectors. Decimated: standings gaps (10 Hz), fuel
derivations (~1 Hz), conditions trend (1 Hz). At lap boundaries: fuel statistics,
tyre trends. Capabilities are probed once per catalog bind.

---

## Interfaces stage 2 consumes

```python
from rtv.racestate import RaceStateEngine, RaceState, RaceEvent, EventType, Severity

services.engine                      # RaceStateEngine | None (None when RTV_PITWALL=false)
services.replay                      # ReplayDriver | None

engine.snapshot() -> RaceState       # deep, consistent copy; safe from any thread
engine.state                         # live mutable object (prefer snapshot())
engine.bus.subscribe(...)            # -> Subscription: .drain() / await .next_events()
engine.bus.subscribe_callback(fn)    # sync fan-out on the producer thread
engine.bus.history(limit)            # replay recent events to a late joiner
engine.reset()                       # clear accumulators between sessions/replays
```

An agent stage should **subscribe to events for triggering** and **read
`snapshot()` for context** — never poll raw frames. `RaceState` is already
LLM-sized (a few KB of JSON), the same way `LapFindings` is for the Layer-1 coach.

---

## Deviations from the stage-1 spec

Each is a conservative choice made autonomously, per `CLAUDE.md`.

1. **Gap basis prefers lap time over instantaneous speed.** The spec says "derive
   from lap-dist deltas and speed". Instantaneous speed makes gaps swing wildly
   through slow corners, so a known lap time is used when available and the
   speed-based form is the fallback. Both are implemented and the choice is
   reported in `standings.gap_basis`, so nothing is hidden.
2. **`window_open` requires a stop to be needed.** A bounds-only window also reads
   open when the car can already reach the flag — true but useless, and it
   re-announced after every stop. Added the `margin_laps < 0` condition.
3. **Fuel derivations refresh at ~1 Hz, not only at lap boundaries.** Lap-boundary-only
   left `level` (per-frame) inconsistent with `laps_remaining` (per-lap) after a
   pit stop. Same incremental discipline, finer cadence.
4. **Live sessions do not populate `lap_length_m`.** `set_session_info()` exists and
   the replay driver calls it, but wiring session-info YAML into the live path
   would mean changing `LivePoller`. Deferred rather than touch v1 ingest; gaps
   simply use the lap-time basis, which is preferred anyway.
5. **No pre-existing "demo-session seeder" to extend.** The spec assumed one; the
   repo had ad-hoc synthetic data in `tests/helpers.py` and `scripts/smoke_offline.py`.
   Generalised that pattern into `racestate/scenario.py` instead, leaving both
   originals untouched.
6. **Added `GET /api/v1/racestate/events`** beyond the specified surface — the
   event ring buffer is needed by the smoke script and stage 6's UI, and exposing
   it costs nothing.
7. **`blue_flag` is standings-derived, as specified**, and does not consult
   `CarIdxSessionFlags`. Keeping one source of truth makes it testable offline
   without the sim having waved the flag.
8. **Detector accessors (`as_float`/`as_int`/`as_bool`) are public** in
   `detectors.py` because `engine.py` uses them; they return `None` for
   missing/NaN/unparseable values rather than defaulting to `0.0`.

### A store bug found and fixed en route

DuckDB identifiers are case-insensitive, so the store's meta `lap` column and the
`Lap` telemetry variable collapse into a single result key on `SELECT *` — every
`row["Lap"]` read back as `None`. `replay._column_resolver()` resolves catalog
columns exact-match-first, then case-insensitively. Without this, replay from the
store silently produced no lap events at all. Worth knowing for any future code
that reads frames back out of Parquet.

---

## Verification

```powershell
pytest                            # 110 passed (50 pre-existing + 60 new), fully offline
python scripts/smoke_pitwall.py   # scripted replay + ordered milestone assertions
python scripts/smoke_offline.py   # v1 surface, unchanged
ruff check src tests scripts evals
```

New test files: `tests/test_racestate_detectors.py` (30), `tests/test_racestate_engine.py`
(19), `tests/test_racestate_api.py` (11). No iRacing, no `ANTHROPIC_API_KEY`, no
network. All 50 pre-existing tests still pass unmodified.
