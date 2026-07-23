# RTV v2 — Pitwall build state

Shared state between the staged headless builds. Each stage appends: what it
built, the public interfaces the next stage consumes, and any deviations from
its spec (with a one-line rationale).

| Stage | Scope | Status |
|-------|-------|--------|
| **1** | Deterministic race-state engine + event bus + replay | **done** |
| **2** | Agent framework + orchestrator + strategist | **done** |
| **3** | Vehicle engineer / spotter / coach agents | **done** |
| **4** | TTS radio voice | **done** |
| **5** | Race-director seam (interfaces only) + live-path hardening + docs | **done** |
| **6** | Pitwall UI | **done** |

> The **race director itself is planned, not implemented.** Stage 5 ships its
> schema, its protocol and a `NoopDirector` that injects nothing — plus a test
> proving the seam carries a scripted full-course yellow indistinguishably from a
> real one. See [stage 5](#stage-5--the-director-seam-and-a-live-path-that-survives-a-race).

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
| `pit_window_closing` | advisory | current lap reaches `latest_lap - 1` while open | `latest_lap`, `laps_remaining`, `per_lap` |
| `fuel_margin_low` | advisory | `margin_laps` drops below `RTV_PITWALL_FUEL_MARGIN_LAPS` | `margin_laps`, `margin_l`, `threshold_laps` |
| `fuel_critical` | critical | `laps_remaining < 1.5`; re-arms on refuel | `level`, `laps_remaining` |
| `stint_lap_milestone` | info | every N green laps on the current set | `stint`, `laps_on_tyres`, `every`, `lap_time` |
| `rival_pitted` | advisory | a nearby car's `CarIdxOnPitRoad` goes false→true | `car_idx`, `position`, `gap_to_player` |
| `blue_flag` | advisory | a car ≥0.7 laps up is closing within 2.5 s | `car_idx`, `gap`, `laps_ahead` |
| `recurring_issue` | advisory | *N* of the same issue at the same corner inside *W* laps | `issue`, `corner`, `occurrences`, `laps[]`, `worst_slip` |
| `tyre_out_of_band` | advisory | a tyre measure leaves its band (see stage 3) | `measure`, `corner`, `value`, `threshold` |
| `car_health_warning` | critical / advisory | an engine warning bit, a tow, or oil/water over the limit | `measure`, `warnings[]`, `value`, `threshold` |
| `traffic_close` | advisory | a car is inside the gap threshold *and* closing | `car_idx`, `gap`, `side`, `closing_rate_s_per_s` |
| `weather_change` | — | **never** — director-only, see stage 5 | `measure`, `from`, `to`, … |
| `regulation_change` | — | **never** — director-only, see stage 5 | `regulation`, `applies_from_lap`, … |

The last two rows are the only members of the catalog **no detector emits**. They
exist so a scripted scenario can say "rain in eight minutes" or "the stop is now
mandatory" *as an ordinary event*: nothing in the channel contract announces
either, and inferring one from a temperature drift would be exactly the invented
figure this codebase refuses everywhere else. No agent trigger references them,
so they cost nothing until a director exists to produce them.

The five events added in stage 2 exist so the strategist can be woken by *facts*
rather than by a clock. `stint_lap_milestone` in particular is the periodic
wake-up, and it is deliberately lap-driven: a timer would fire under a red flag,
in the pits, and at 4× replay speed.

`pit_window_closing` and `fuel_margin_low` are distinct from the events they sit
next to. "Open" says a stop is *available*; "closing" says it is now urgent.
`margin_laps` is slack against the **finish**, so it goes negative many laps
before `fuel_critical` (slack against **running dry**) — which is exactly the
lead time a strategist needs.

**Latching.** Continuous detectors (lockup, wheelspin, blue flag, traffic) fire
once per episode: they re-arm only after the condition clears, with a cooldown
floor. Edge detectors (pit, off-track, incident, lap, flag) are naturally
one-shot. `tyre_out_of_band` and `car_health_warning` latch by **measure**, so an
oil warning after a water one still fires; the tyre latch re-arms on new rubber.

The last four rows were added in stage 3 and are all **aggregates**: they exist so
an agent is never woken by a single observation. See "Deterministic aggregation"
below.

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

---
---

# Stage 2 — the agentic layer

`src/rtv/pitwall/` — the first package in the v2 build that talks to a model, and
it does so as rarely as possible. Everything continuous already happened in
`racestate/`; by the time an agent wakes up, the numbers exist and its job is
purely judgement.

## The hybrid cost model, concretely

**A model is called only when a `RaceEvent` fires an agent's trigger.** No timer,
no polling, nothing per tick. The scripted 6-lap race (3 360 frames, 20 events)
wakes the strategist **6 times**. A real hour-long race with a 5-lap milestone
cadence and the cooldowns below lands in the low tens of calls — the same order
as one Layer-3 coaching run, spread over the whole race.

Four things hold that down, in order of how much they matter:

1. **Triggers are events, not states.** `pit_window_open` fires once per stint.
2. **Predicates veto before the call.** A checkered flag is a `flag_change`, but
   `_yellow_or_red` rejects it; a milestone with three laps of fuel slack is
   rejected by `_stop_is_needed`. Neither costs a token.
3. **Per-trigger cooldowns**, measured in **session** time so a 4× replay behaves
   like the live race.
4. **Kill switches** — global and per agent — checked in `dispatch()`, before the
   worker queue, so "off" means zero spend, not a quiet radio.

---

## Module map

| Module | Role |
|--------|------|
| `framework.py` | `AgentSpec`, `Trigger`, `ToolSpec`, `AgentRuntime`, `RadioMessage`. The only code that talks to a model. |
| `provider.py` | `AnthropicProvider` (real) and `ScriptedProvider` (offline), behind one `complete()` method. |
| `tools.py` | The deterministic tool registry over `racestate` + `strategy_math`. |
| `validator.py` | `FactSet` + `validate_output`: the in-loop citation check. |
| `radio.py` | `RadioFeed`: one channel, priority-ordered, self-superseding, paced. |
| `orchestrator.py` | `PitwallOrchestrator`: bus → routing → workers → merged feed. |
| `agents/__init__.py` | The registry. **This is the only file stage 3 must edit.** |
| `agents/strategist.py` | The reference agent: prompt, tools, triggers, contract. |
| `racestate/strategy_math.py` | `simulate_pit_outcome` and friends — pure, LLM-free, tested standalone. |

## An agent is four things

```python
STRATEGIST = AgentSpec(
    name="strategist",
    role_prompt=STRATEGIST_PROMPT,       # who it is, what it may say
    output_model=PitCall,                # the contract (a Pydantic model)
    tools=(GET_FUEL_PROJECTION, SIMULATE_PIT_OUTCOME, ...),   # a scoped subset
    triggers=(Trigger("pit_window_open", cooldown_s=0.0), ...),
    model="claude-sonnet-4-6",
    state_slice=strategist_slice,        # the compact context it reasons over
    subject=lambda event, out: "pit_stop",  # the radio supersede key
)
```

There is no strategist-specific branch anywhere in the runtime, the orchestrator,
the radio feed or the API. `tests/test_pitwall_orchestrator.py::test_a_second_agent_is_pure_configuration`
adds a second agent with three lines of `dataclasses.replace` and asserts it routes.

## The runtime loop

```
trigger fires
  -> assemble context: role prompt (cached) + state slice + event + recent radio
  -> model calls its scoped tools; every tool result is added to the FactSet
  -> tools are withdrawn on the last pass, so the model must answer
  -> structured output validated against the FactSet
  -> one repair turn naming the exact rejected figures
  -> still ungrounded? emit a grounded refusal instead
  -> RadioMessage onto the merged feed
```

Prompt caching follows the Layer-3b coach: the stable role prompt is the first
system block and carries `cache_control`; the volatile state slice comes after it,
uncached. Reversing them would cache nothing, since caching is a prefix match.

---

## Grounding: how a number is allowed to be said

Every number an agent utters must be traceable to something it was *actually
shown* — the state slice, the triggering event, or a tool result. The check is
deterministic and in-loop, because a race engineer cannot afford a second LLM call
to fact-check the first one.

Two rules, and they are deliberately different:

| Where | Rule | Why |
|---|---|---|
| `spoken_text` / `detail_text` | tolerant: ±0.05 or 2 %, and rounded/absolute forms of a fact count | "2.5 laps" for 2.487, or "6.3 laps short" for a margin of −6.298, is reporting, not inventing |
| the structured payload (`rejoin_position`, `target_lap`, …) | **exact** against raw facts only | these are meant to be copied from a tool verbatim. Allowing rounding here would let a tyre pressure of 18.9 "support" a claimed P19 — the false negative that would make the whole check theatre |

Also checked in-loop: clock notation (`1:32.4` scores as 92.4 s, not as 1 and
32.4), and the 25-word radio limit.

**On failure**: one repair turn naming the exact figures. If it still fails, the
message becomes a **grounded refusal** — priority downgraded to `info`,
`spoken_text` replaced with "Standby — I can't back that call with data yet.",
`refused=True`, and the offending text preserved in `detail_text` so an operator
can see what was blocked. Silence would have been safer than a wrong number, but
silently dropping it would have hidden a broken agent.

The state slice is therefore also a **permission**: the strategist is not shown
car health or weather, so it cannot quote an oil temperature as a gap. Scoping
tools scopes speech.

---

## Radio discipline

One driver, one channel. `RadioFeed` is a priority queue plus fan-out:

- **Order** is `(priority, sequence)` — `critical` pre-empts anything queued.
- **Supersede**: a newer non-critical message with the same `(agent, subject)`
  replaces the older one *while it is still queued*. Two pit-window updates are
  one current answer, not a log of them.
- **A critical message is never superseded and never dropped.** Losing a "box now,
  you're on fumes" to a tidier queue is the one unforgivable bug in here.
- **Airtime**: a message holds the channel for ~0.4 s per spoken word. This is
  what makes superseding meaningful, and it is the hook stage 4's TTS hangs off.
- `pump(session_time)` is the synchronous, clock-injected twin of `run()`, so a
  replayed race produces a reproducible radio log the same way it produces a
  reproducible event log.

## Concurrency

- **Per-agent serialisation**: one worker and a one-slot inbox per agent, so an
  agent can never overlap itself. A newer event replaces a waiting one
  (latest-wins) — **except** that a queued `critical` event is never displaced by
  a lesser one.
- **Global cap**: a semaphore bounds in-flight model calls (`RTV_PITWALL_MAX_INFLIGHT`,
  default 2), so a safety-car burst cannot fan out into a dozen requests.

---

## The STRATEGIST

Triggers, all deterministic:

| Trigger | Predicate | Cooldown |
|---|---|---|
| `pit_window_open` | — | 0 s |
| `pit_window_closing` | — | 0 s |
| `fuel_critical` | — | 0 s |
| `flag_change` | into/out of yellow or red only | 20 s |
| `fuel_margin_low` | — | 60 s |
| `rival_pitted` | within 30 s of us | 45 s |
| `stint_lap_milestone` | only when `margin_laps < 3` | 60 s |

Tools: `get_race_state_slice`, `get_fuel_projection`, `get_tyre_trend`,
`get_standings_around_player`, `get_stint_history`, `simulate_pit_outcome`.

Output contract `PitCall`: `recommendation` ∈ {`box_now`, `box_next_lap`,
`pit_under_yellow`, `extend`, `stay_out`, `hold`}, plus `target_lap`,
`fuel_to_add_l`, `rejoin_position`, `confidence`, `rationale`, `risks[]`.
`hold` is a first-class answer, not a failure mode: it is what the agent says when
the data does not support a call.

### `simulate_pit_outcome` — the deterministic core

In `racestate/strategy_math.py`, tested independently in `tests/test_strategy_math.py`.

```
rejoin_position = position_before + |{cars behind whose cumulative gap < effective_loss}|
effective_loss  = pit_lane_loss_s x (0.45 under yellow, else 1.0)
fuel_to_add     = min(capacity, (laps_after_stop + 0.5) x per_lap) - fuel_at_stop
```

The model is stated rather than hidden: it assumes the field holds station over
the stop, which is exactly the assumption a race engineer makes on the radio, and
it is returned in `assumptions[]` for the agent to hedge with.

Grounding carries all the way down. No `gap_basis` ⇒ `rejoin_position is None` and
an assumption line saying why; no learned `per_lap` ⇒ no refuel target; neither ⇒
`grounded=False` with a `reason`. Nothing is ever estimated into existence.

---

## API surface (added; v1 and stage 1 untouched)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/pitwall/status` | Agents, triggers, per-agent stats, radio counters |
| GET | `/api/v1/pitwall/radio?limit=` | Recent radio + what is still queued |
| POST | `/api/v1/pitwall/enabled` | `{enabled}` — the global kill switch |
| POST | `/api/v1/pitwall/agents/{name}/enabled` | `{enabled}` — one agent |
| POST | `/api/v1/pitwall/reset` | Clear cooldowns and the channel |

`/pitwall/status` answers even when the layer is **not** mounted (`available:false`
plus the reason), because a UI needs to explain why the radio is quiet rather than
show an error.

### `/ws/pitwall` gains a third frame type

```jsonc
{"type":"radio","message":{
  "agent":"strategist", "priority":"critical",
  "spoken_text":"Box this lap, 3.0 litres, we come out P8.",
  "detail_text":"Window is lap 5 to 7. ...",
  "data":{"recommendation":"box_now","target_lap":5,"rejoin_position":8,...},
  "event_ref":{"key":"pit_window_open","tick":2400,"lap":5,"state_version":2400},
  "grounded":true, "refused":false, "tools_used":["get_fuel_projection","simulate_pit_outcome"]
}}
```

Never coalesced: the feed has already decided what was worth saying, and the
socket must not second-guess it. `event_ref` pins every call to the deterministic
observation that caused it, so the UI can always show its working.

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `RTV_PITWALL_AGENTS` | `false` | Mount the agent layer. **Opt-in** — see deviation 1. |
| `RTV_PITWALL_AGENTS_LIVE` | `true` | Startup value of the kill switch. |
| `RTV_PITWALL_AGENT_MODEL_FAST` | `claude-haiku-4-5-20251001` | High-frequency, low-stakes agents. |
| `RTV_PITWALL_AGENT_MODEL_REASONING` | `claude-sonnet-4-6` | Strategy-grade reasoning. |
| `RTV_PITWALL_STRATEGIST_MODEL` | *(empty)* | Per-agent override. |
| `RTV_PITWALL_MAX_INFLIGHT` | `2` | Cap on concurrent LLM calls. |
| `RTV_PITWALL_AGENT_COOLDOWN_S` | `30` | Default per-trigger cooldown. |
| `RTV_PITWALL_PIT_LANE_LOSS_S` | `25` | Input to the rejoin projection. |
| `RTV_PITWALL_RADIO_HISTORY` | `200` | Radio ring buffer. |
| `RTV_PITWALL_STINT_MILESTONE_LAPS` | `5` | Green laps between milestones. |
| `RTV_PITWALL_FUEL_MARGIN_LAPS` | `1.0` | Threshold for `fuel_margin_low`. |

---

## Evals

`evals/pitwall_cases.py` **generates** the golden set instead of shipping one. A
lap of telemetry cannot be written by hand, but a race state can: replaying the
scripted race yields the exact triggering events with the exact state current at
each, using the same `AgentRuntime.match()` the live path uses. Four degraded
cases are appended — no fuel model, no gap basis, a caution with a stop owed, and
a rival stopping when we don't need fuel — because a set where everything is known
cannot tell a grounded strategist from a confident one.

`evals/pitwall_checks.py` scores six things deterministically:

| Check | Ground truth |
|---|---|
| `grounding_ok` | every figure traces to the state or a tool |
| `radio_ok` | `spoken_text` ≤ 25 words |
| `decision_ok` | the call is inside the defensible **band** for this state |
| `window_ok` | the named stop lap is inside the fuel window |
| `rejoin_ok` | the claimed rejoin matches `simulate_pit_outcome` (±1) |
| `refusal_ok` | it held rather than guessed when the data was absent |

`decision_ok` scores a *band*, not one right answer: box-now vs box-next-lap are
both defensible on an open window, staying out on an empty tank is not.

```powershell
python evals/run_pitwall.py --dry-run   # cases + ground truth, no key needed
python evals/run_pitwall.py             # the real strategist (needs a key)
```

---

## Interfaces stage 3 consumes

Adding the vehicle engineer, spotter and coach is **one new module plus one
registry entry**. (Stage 3 did exactly that — the real specs are below; the sketch
here is what stage 2 predicted, kept because the prediction held.)

```python
# src/rtv/pitwall/agents/spotter.py
class SpotterCall(AgentOutput):
    threat: Literal["overlap_left", "overlap_right", "clear"]

SPOTTER = AgentSpec(
    name="spotter",
    role_prompt=SPOTTER_PROMPT,
    output_model=SpotterCall,
    tools=(T.GET_STANDINGS_AROUND_PLAYER, T.GET_RACE_STATE_SLICE),
    triggers=(Trigger(EventType.BLUE_FLAG.value, cooldown_s=10.0),),
    model=FAST_MODEL,
    state_slice=spotter_slice,
    subject=lambda event, out: "traffic",
)

# src/rtv/pitwall/agents/__init__.py
AGENT_REGISTRY = (STRATEGIST, SPOTTER)      # <- the only edit outside the module
```

Everything else — routing, cooldowns, the tool loop, grounding, refusal, radio
merging, the API, the WebSocket frames, the status payload — already works for it.

```python
from rtv.pitwall import (
    AgentSpec, AgentOutput, AgentRuntime, Trigger, ToolSpec, ToolContext,
    RadioMessage, RadioPriority, RadioFeed, PitwallOrchestrator,
    ScriptedProvider, AnthropicProvider, tool_use_turn,
    FactSet, validate_output, build_agents, ALL_TOOLS, TOOLS_BY_NAME,
)

services.pitwall                       # PitwallOrchestrator | None
orch.feed.subscribe(name="tts")        # -> .drain() / await .next_messages()
orch.feed.subscribe_callback(fn)       # sync fan-out (stage 4's TTS hook)
orch.feed.pump(session_time)           # deterministic, replay-stable pacing
await orch.handle_event(event, state)  # route + run inline (tests, smoke, evals)
orch.status()                          # the /api/v1/pitwall/status payload
```

New tools are added to `tools.py` and listed in an agent's `tools=(...)`; a tool
not in an agent's tuple is unreachable *and* uncitable by it.

---

## Deviations from the stage-2 spec

1. **`RTV_PITWALL_AGENTS` defaults to `false`.** The spec implies the layer is on.
   But a developer who exported `ANTHROPIC_API_KEY` for the Layer-3 coach would
   otherwise have live race radio start billing the moment they drove. Opt-in is
   the conservative reading of "kill switch + per-agent enable flags"; everything
   is one env var away.
2. **`RadioMessage` lives in `framework.py`, but so do `ToolSpec`/`Trigger`.** The
   spec listed only the three classes; splitting the rest into a fourth module
   bought nothing, and `radio.py` is separate because it holds behaviour (priority,
   supersede, airtime) rather than the shape.
3. **The radio feed is not the race-state `EventBus`.** The spec says "publishes
   RadioMessages onto the bus". `EventBus` is typed to `RaceEvent` and has no
   notion of priority, supersede or airtime — all three of which are the point.
   `RadioFeed` reuses the bus's bounded/latest-wins subscription semantics and adds
   what radio actually needs. `/ws/pitwall` fans both out on one socket, so the
   observable contract is what the spec asked for.
4. **No `thinking` on agent calls by default.** The Layer-3b coach uses adaptive
   thinking because it runs post-hoc. A pit call that lands two corners late is
   worse than no call, and every number is already computed deterministically
   upstream, so latency wins. `AnthropicProvider(thinking=True, effort=...)` opts
   back in per deployment.
5. **Five new deterministic events, not agent-side timers.** The spec asked for
   triggers like `stint_lap_milestones` and `fuel_margin_below_threshold` "from a
   deterministic event, not a timer". None existed, so they were added to the
   stage-1 engine rather than synthesised in the agent layer. This keeps one source
   of truth and makes each trigger unit-testable without a model.
6. **The eval golden set is generated, not a JSON file.** `evals/golden_laps.json`
   exists because telemetry can't be hand-written; a race state can be, and
   generating it from the scripted race means every case carries its own ground
   truth and cannot drift from the engine. `test_the_golden_set_is_reproducible`
   pins it.
7. **The validator checks the structured payload exactly and prose tolerantly.**
   The spec says "reuse/adapt the in-loop citation validator". Applying one rule to
   both halves fails either way: strict prose rejects "2.5 laps" for 2.487, and
   tolerant payload lets a tyre pressure of 18.9 support a claimed P19. Both
   failure modes are covered by tests.
8. **`ScriptedProvider` takes a callable, not a canned list.** A fixed list of
   turns cannot express "answer using whatever the tool returned", which is the
   only way to test the tool loop end to end without a network.
9. **A session-scoped `tests/conftest.py` unsets `ANTHROPIC_API_KEY`.** An offline
   suite that only passes on a machine without a key is not an offline suite;
   `pytest` is now green either way, and that is asserted by running it both ways.

### Two bugs found and fixed en route

- **`ScriptedProvider` counted turns across its lifetime, not per conversation.**
  Every agent invocation after the first skipped its tool calls entirely. The
  smoke script caught it; `turn` is now derived from the message history.
- **The smoke script scored agents against the *finished* race.** It replayed
  first and dispatched afterwards, so every call saw the end-of-race snapshot. It
  now drives frame by frame, and `evals/pitwall_cases.py` does the same — worth
  knowing for any future code that replays and then inspects.

---

## Verification (stage 2)

```powershell
pytest                                   # 202 passed (110 stage 1 + 92 new), fully offline
python scripts/smoke_pitwall_agents.py   # the agent layer end to end, scripted provider
python scripts/smoke_pitwall.py          # stage 1, unchanged
python scripts/smoke_offline.py          # v1 surface, unchanged
python evals/run_pitwall.py --dry-run    # the golden set + its ground truth
ruff check src tests scripts evals
```

New test files: `tests/test_strategy_math.py` (17), `tests/test_pitwall_framework.py`
(32), `tests/test_pitwall_orchestrator.py` (13), `tests/test_pitwall_evals.py` (19),
`tests/test_pitwall_agents_api.py` (11). Green with and without
`ANTHROPIC_API_KEY` exported. No iRacing, no network. All 110 stage-1 tests still
pass unmodified.

---
---

# Stage 3 — the rest of the pitwall

Three more agents: the **vehicle engineer**, the **spotter** and the live
**coach**. The stage-2 claim was that this would be configuration rather than
framework work, and it very nearly was: three new modules under
`src/rtv/pitwall/agents/`, one registry line, four new tools, four new
deterministic events — and exactly one field added to the framework.

## The four agents, and why they cannot say each other's sentences

| agent | owns | wakes on | model tier |
|---|---|---|---|
| `strategist` | when we stop and what we take on | fuel, flags, rivals | reasoning |
| `vehicle_engineer` | what state the car is in | recurrence, tyres, health, stint end | fast |
| `spotter` | what is around us right now | traffic, blue flags, hazards | fast |
| `coach` | how the car is being driven | repeated mistakes, offs, stint pace | fast |

Separation is enforced three ways, none of which is a prompt instruction:

1. **The state slice is a permission.** The spotter is handed flags, the player
   and the running order — no fuel, no tyres, no car health. It is not that it is
   *told* not to quote a fuel number; the number is not in its fact set, so the
   validator rejects it and the call becomes a grounded refusal. Same for the
   engineer (no standings, no fuel) and the coach (no standings, no fuel).
2. **The tool list is a permission.** No role agent can reach
   `simulate_pit_outcome` or `get_fuel_projection`. A model that calls one anyway
   gets `{"error": "Unknown tool ..."}` back, and nothing it would have returned
   ever enters the fact set. Both halves are asserted in
   `tests/test_pitwall_roles.py`.
3. **The subject key scopes supersede.** The engineer's subject is the *system*
   (`tyres` / `grip` / `car_health`), so two tyre advisories collapse but a tyre
   note and an oil note both survive. The coach's is a single `coaching` slot: one
   cue stands at a time.

---

## Deterministic aggregation — where the money is saved

This is the substantive stage-3 idea. Stage 2 established that an agent is woken by
an *event*; the trouble is that a driver on a bad set locks the fronts twenty times
a stint, and twenty LLM calls to say "you're locking the fronts" is both expensive
and terrible radio.

So the aggregation happens in the engine, before any agent exists:

```
lockup (lap 1, C08)   --+
lockup (lap 2, C08)   --+-->  CornerRecurrence  -->  recurring_issue
lockup (lap 4, C08)   --+      (3 in 5 laps)          (one event, one call)
```

`racestate/detectors.CornerRecurrence` buckets lap distance into corners, counts
repeats per `(issue, corner)`, and returns a payload only on the *N*-th inside a
*W*-lap window. The singles still reach the bus — the UI wants them — but no
trigger references them. `scripts/smoke_pitwall_roles.py` asserts this directly:
three scripted lockups wake **nobody**, and the one aggregate wakes two agents.

Re-arming is by **new repeats since the last call**, not by running total. A
running-total rule ("fire again at 2N") can never fire twice, because the lap
window caps the total at *W*. Getting that wrong would have turned a recurring
problem into a one-time notification for the rest of the race.

| Knob | Env var | Default |
|---|---|---|
| corner buckets per lap | `RTV_PITWALL_CORNER_BUCKETS` | 20 (5 % of a lap) |
| repeats before it is a finding | `RTV_PITWALL_RECURRENCE_MIN` | 3 |
| lap window they must fall in | `RTV_PITWALL_RECURRENCE_WINDOW_LAPS` | 5 |

**Corner labels are `C00`–`C19`, not circuit corner numbers.** Without a track map
we do not know where T7 is, and naming it would be exactly the unbacked figure the
rest of this system refuses to produce. The payload carries the lap-fraction range
so a UI can point at it, and every prompt says so explicitly.

### Tyre bands without inventing a "normal" tyre temperature

`detect_tyre_condition` reports the worst single way a set is out of band. The
absolute limits (`tyre_temp_max_c` and friends) are **`None` by default**: a
correct operating window is a per-car number nobody told us, and picking one would
be a guess dressed as a threshold. What is on by default are the two *relative*
measures, which need no per-car knowledge:

- **drift** — |per-lap trend| across the current stint, ≥ 3 °C/lap or 2 kPa/lap,
  and only once at least three stint laps have been completed;
- **axle imbalance** — left-to-right spread across one axle ≥ 15 °C.

An operator who knows their car sets the absolute band and gets that as well.

### Car health

The sim's own `EngineWarnings` bits come first, because that is the car speaking
rather than us inferring — minus `pit_speed_limiter` and `rev_limiter_active`,
which are normal driving. Then a tow (`PlayerCarTowTime > 0`, the closest thing to
a damage channel this catalog has), then oil/water over their configured limits.
The payload says which it was, so the engineer can distinguish "the car is telling
us" from "we think", and the prompt requires it to.

### Traffic

`traffic_close` needs the car to be inside `RTV_PITWALL_TRAFFIC_GAP_S` **and**
closing at ≥ 0.15 s per second, measured against a gap sampled a second earlier.
Proximity alone is not news — in a tight race someone is within a second for the
whole stint.

One subtlety worth recording: the sample is discarded when `gap_basis` changed
between the two readings. When a lap time first becomes known, gaps stop being
derived from instantaneous speed and *every* gap moves at once while no car moved.
Before this guard, the scripted race produced a phantom "car closing at 3.4 s/s"
on lap 1.

---

## New tools

| Tool | Returns | Unavailable when |
|---|---|---|
| `get_recent_detector_events` | filtered slice of the event ring buffer | nothing of those types yet |
| `get_car_health` | health scalars **plus the thresholds they were judged against** | no health channels in the catalog |
| `get_setup_snapshot` | `CarSetup` verbatim from the session-info YAML | no document, or no `CarSetup` section |
| `get_corner_detail` | live detector events at that corner **and** the Layer-1 corner analysis | nothing recorded / fewer than two clean laps |

`get_corner_detail` is the one that reaches outside `racestate`. Its `telemetry`
half calls the **same** `rtv.coaching.features.CoachingService` the v1 coaching
endpoints call — minimum speed against a reference lap, brake point, throttle
application, time lost — so the live coach and the post-hoc coach share a source of
truth rather than a re-implementation. It returns two halves on purpose:

```jsonc
{"requested_lap_dist_pct": 0.4033,
 "live":      {"corner": "C08", "count": 3, "events": [ /* the lockups here */ ]},
 "telemetry": {"available": true, "main_lap": 4, "ref_lap": 1,
               "corner": {"label":"T1","min_speed_kmh":75.4,"diagnostics":[...]}}}
```

The `live` half always works. The `telemetry` half needs a *written* session, so it
is unavailable during the opening laps and in any deployment that is not recording
— and says so with a reason rather than degrading quietly. `CoachCall.from_telemetry`
records which half the cue rested on, and the eval harness checks that a coach
never claims a trace it was not shown.

`get_setup_snapshot` returns the setup **verbatim**. Setup sections differ per car,
so flattening them into a fixed schema would either drop fields or invent them.

---

## The agents in detail

### VEHICLE ENGINEER

| Trigger | Predicate | Cooldown |
|---|---|---|
| `recurring_issue` | issue in {lockup, wheelspin, offtrack} | 45 s |
| `tyre_out_of_band` | — | 60 s |
| `car_health_warning` | — | 30 s |
| `pit_entry` | only when tyre data exists | 0 s |

Tools: `get_tyre_trend`, `get_recent_detector_events`, `get_corner_detail`,
`get_car_health`, `get_setup_snapshot`, `get_race_state_slice`.

`EngineerCall`: `finding` in {`tyre_temps`, `tyre_pressures`, `brake_lockup`,
`traction`, `car_health`, `damage`, `none`}, plus `corner`, `affected[]`, `trend`,
`driver_action`, `setup_note`, `confidence`, `rationale`. `none` is a real answer:
the trigger turned out to be benign.

**No numeric fields in the payload, deliberately.** Structured numbers are matched
*exactly* against raw facts (stage-2 deviation 7). There is no number the pitwall
UI needs from the engineer badly enough to be worth that, and everything
quantitative it says belongs in prose, where a rounded form is legitimate.

### SPOTTER

| Trigger | Predicate | Cooldown |
|---|---|---|
| `traffic_close` | — | 15 s |
| `blue_flag` | — | 10 s |
| `flag_change` | into yellow or red only | 10 s |
| `incident` | — | 10 s |

Tools: `get_standings_around_player`, `get_recent_detector_events`. Two tool
rounds, not four, and a 700-token budget: a spotter call that lands after the
corner is worthless.

`SpotterCall`: `threat` in {`lapped_traffic`, `car_closing`, `hazard`, `clear`},
`side` in {`ahead`, `behind`, `unknown`}, `car_idx`, `action`, `confidence`.

`car_idx` *is* a numeric payload field, and that is the point: it must be copied
from the event or the standings tool exactly, so a spotter that names a car nobody
mentioned is refused.

### COACH

| Trigger | Predicate | Cooldown |
|---|---|---|
| `recurring_issue` | issue in {lockup, wheelspin, offtrack} | 120 s |
| `offtrack` | under green only | 90 s |
| `stint_lap_milestone` | under green, at least 2 laps on the set | 180 s |

Tools: `get_corner_detail`, `get_recent_detector_events`, `get_stint_history`,
`get_tyre_trend`.

`CoachCall`: `theme` in {`braking`, `throttle`, `line`, `consistency`,
`tyre_management`, `none`}, `corner`, `cue`, `from_telemetry`, `confidence`,
`rationale`.

The coach and the engineer **share** the `recurring_issue` trigger, which is
intentional: three lockups at one corner is simultaneously a brake-bias question
and a technique question, and those are different sentences from different people.
The cooldowns are what bound the cost — the coach speaks at most a third as often,
so the car's advocate gets there first. Never coaching under a caution is the same
judgement: the driver has other things to think about.

---

## Deterministic evals for judgement calls

`evals/pitwall_cases.py` now generates a golden set **per agent**, using that
agent's own `AgentRuntime.match()` — so a case exists exactly when the live path
would have woken it, and there is no second, hand-maintained trigger list to
drift. The role scenario is the scripted race with two extra knobs
(`lockup_laps=(1,2)`, `corner_pcts=(0.44,)`) so that it actually contains a
repeated mistake at a real corner; both are opt-in, so the stage-1 ground-truth
scenario stays byte-identical.

`evals/pitwall_checks.py` scores each role against what the deterministic trigger
*said*. Grounding and radio discipline are identical for everyone; the rest is
role-specific:

| Agent | Checks beyond grounding + radio |
|---|---|
| `vehicle_engineer` | `finding_ok` (inside the band for that trigger), `corner_ok` (the event's corner, not another), `setup_ok` (no setup advice without a setup sheet), `refusal_ok` (no tyre finding without tyre channels) |
| `spotter` | `threat_ok`, `car_ok` (the car named in the event), `side_ok`, `brevity_ok` (14 words, tighter than radio), `refusal_ok` (no seconds without a gap basis) |
| `coach` | `theme_ok`, `corner_ok`, `telemetry_ok` (no claimed trace it never saw), `one_cue_ok` |

```powershell
python evals/run_pitwall.py --dry-run            # 24 cases across 4 agents, no key
python evals/run_pitwall.py --agent spotter      # one agent (needs a key)
python evals/run_pitwall.py                      # all four
```

`aggregate()` discovers its keys from the rows, so a mixed run reports the union
and omits a rate for an agent that does not carry that check.

---

## Configuration added

| Env var | Default | Meaning |
|---|---|---|
| `RTV_PITWALL_CORNER_BUCKETS` | `20` | Lap fractions used to decide "the same corner". |
| `RTV_PITWALL_RECURRENCE_MIN` | `3` | Repeats before `recurring_issue` fires. |
| `RTV_PITWALL_RECURRENCE_WINDOW_LAPS` | `5` | Lap window those repeats must fall in. |
| `RTV_PITWALL_TYRE_TEMP_TREND_C` | `3.0` | Degrees per lap of sustained drift. |
| `RTV_PITWALL_TYRE_AXLE_IMBALANCE_C` | `15.0` | Left-to-right spread across an axle. |
| `RTV_PITWALL_OIL_TEMP_MAX_C` | `130` | Oil limit for `car_health_warning`. |
| `RTV_PITWALL_WATER_TEMP_MAX_C` | `105` | Water limit. |
| `RTV_PITWALL_TRAFFIC_GAP_S` | `1.5` | Gap inside which a car counts as close. |
| `RTV_PITWALL_VEHICLE_ENGINEER_MODEL` | *(empty)* | Per-agent override; empty = the fast tier. |
| `RTV_PITWALL_SPOTTER_MODEL` | *(empty)* | " |
| `RTV_PITWALL_COACH_MODEL` | *(empty)* | " |
| `RTV_PITWALL_AGENTS_ONLY` | *(empty)* | Comma-separated subset to mount. Empty = all four. |

Absolute tyre bands are intentionally *not* shipped as live values in
`.env.example` — see "Tyre bands" above.

---

## Interfaces stage 4 consumes

Nothing about the radio contract changed, which is the point: stage 4's TTS layer
hangs off the same hook and now receives four agents' worth of traffic on it.

```python
from rtv.pitwall.agents import (
    AGENT_REGISTRY, STRATEGIST, VEHICLE_ENGINEER, SPOTTER, COACH, build_agents,
)
from rtv.pitwall.agents.vehicle_engineer import EngineerCall
from rtv.pitwall.agents.spotter import SpotterCall
from rtv.pitwall.agents.coach import CoachCall

orch.feed.subscribe_callback(fn)      # every agent's output, one channel
orch.feed.pump(session_time)          # deterministic, replay-stable pacing
orch.tool_extras                      # {"engine", "coaching", "repo"}

engine.setup_snapshot()               # CarSetup + car scalars, or a stated reason
engine.set_session_id(session_id)     # name a live session without resetting it

from rtv.racestate.detectors import (
    CornerRecurrence, corner_label,
    detect_tyre_condition, detect_car_health, detect_closing_traffic,
)
```

For a UI (stage 6): `RadioMessage.agent` is the channel label, `.subject` is the
supersede key, and `.data` is that agent's own contract — `EngineerCall`,
`SpotterCall` and `CoachCall` are all UI-ready payloads with no numeric fields to
mis-render except the spotter's `car_idx`.

---

## Deviations from the stage-3 spec

Each is a conservative choice made autonomously, per `CLAUDE.md`.

1. **One framework change: `ToolContext.extras`.** The spec's tool list includes
   `get_corner_detail` ("reuse coaching Layer-1") and `get_setup_snapshot` ("from
   session-info YAML"), and neither is reachable from a `RaceState` snapshot. So
   `ToolContext` gained one optional `extras` mapping, threaded through
   `AgentRuntime(tool_extras=...)` and `PitwallOrchestrator(tool_extras=...)`.
   The orchestrator always injects `engine`; `services.build_pitwall` adds
   `coaching` and `repo`. The alternative — putting the setup and the store handle
   on `RaceState` — would have widened *every* agent's citable fact set with setup
   numbers, which is precisely what the state slice exists to prevent.
2. **There is no `stint_end` event; the trigger is `pit_entry`.** The spec lists
   `stint_end` as a trigger. Pit entry *is* the end of a stint, and it already
   carries the lap, the stint number and the fuel remaining. A second event on the
   same edge would have been two names for one fact.
3. **Corner identity is a lap-distance bucket, not a corner number.** Naming "T7"
   needs a track map the engine does not have. `C00`–`C19` is honest and stable;
   the payload carries the lap-fraction range, and `get_corner_detail` maps it onto
   the Layer-1 label (`T1`) when a stored session is attached.
4. **Absolute tyre bands default to off.** The spec says "tyre temp/pressure
   drifting out of band". A default band for an unknown car would be a guess
   presented as a threshold — the exact failure this codebase refuses elsewhere.
   Drift and axle imbalance are relative, need no per-car knowledge and are on by
   default; absolute bands are one env var away.
5. **"Damage events" are `PlayerCarTowTime` plus the engine-warning bits.** The
   catalog contract in `racestate/channels.py` has no damage channels, so there was
   nothing else to detect. Reported under `car_health_warning` with
   `measure: "tow"`, so a real damage channel can be added later without a new
   event type.
6. **The coach shares `recurring_issue` with the engineer** rather than getting
   triggers of its own. Two agents on one event costs two calls, which is a real
   cost; but "your brake bias is wrong" and "brake ten metres earlier" are
   different sentences, and collapsing them would have meant one agent owning both
   the car and the driver. The 120 s vs 45 s cooldown split is how the cost is
   bounded.
7. **`lockup` / `wheelspin` payloads gained `wheel` and `lap_dist_pct`.** The
   existing `corner` key on those events means the *wheel* (LF/RF) and predates the
   track-corner labels, so it stays for compatibility; `wheel` is the unambiguous
   name, and `lap_dist_pct` is what the recurrence aggregator keys on.
8. **The scripted scenario gained two opt-in knobs, not new defaults.**
   `lockup_laps` (repeat the lockup on further laps) and `corner_pcts` (a speed dip
   deep enough for the Layer-1 corner detector to find — the default sinusoidal lap
   has no sufficiently prominent local minimum). Both default to empty, so the
   stage-1 ground-truth race is byte-identical and every stage-1/2 test is
   untouched.
9. **Session identity is attached in `services.on_state`, not in `LivePoller`.**
   `get_corner_detail`'s Layer-1 half needs the live session id and
   `get_setup_snapshot` needs the session-info document. Both are already written to
   the store by the poller *before* it announces the session, so services reads them
   back out. This closes stage-1 deviation 4 for the live path without touching v1
   ingest.
10. **`CornerRecurrence` is stateful, inside a module of pure functions.** The spec
    put the aggregator in `detectors.py` and that is where it belongs — it is a
    detector whose window is laps rather than frames — but it is the one object in
    that module that holds state. It still only *returns a payload*; constructing
    the `RaceEvent`, latching and cooldowns remain the engine's job, as for every
    other detector.

### A false positive found and fixed en route

The first traffic detector reported a car "closing at 3.4 s/s" on lap 1 of the
scripted race. Nobody had moved: the gap *basis* had switched from
`lap_length_speed` to `lap_time_pct` as soon as a lap time became known, and every
gap changed at once. `_check_traffic` now stores the basis alongside the sample and
discards a comparison across a change of it. Worth knowing for anything else that
differentiates a `RaceState` field over time.

---

## Verification (stage 3)

```powershell
pytest                                   # 309 passed (202 stage 1+2 + 107 new), fully offline
python scripts/smoke_pitwall_roles.py    # all four agents, real store, real Layer-1
python scripts/smoke_pitwall_agents.py   # stage 2, unchanged
python scripts/smoke_pitwall.py          # stage 1, unchanged
python scripts/smoke_offline.py          # v1 surface, unchanged
python evals/run_pitwall.py --dry-run    # 24 cases across 4 agents + their ground truth
ruff check src tests scripts evals
```

New test files: `tests/test_racestate_recurrence.py` (40),
`tests/test_pitwall_roles.py` (67). Green with and without `ANTHROPIC_API_KEY`
exported. No iRacing, no network. All 202 stage-1/2 tests still pass unmodified,
and the scripted race still produces the same 19 events it did in stage 1 — none of
the four new event types fires when nothing is wrong.

---
---

# Stage 4 — the voice

Stages 1–3 built something that decides what to say. This one makes it audible,
and the interesting half is not the speech engine — it is the **discipline**.
Four agents, one driver, one pair of ears: something has to decide what gets
said, what waits, what is replaced, and what is thrown away unheard.

That decision is made **twice on purpose**, once on each side of the socket:

| | `RadioFeed` (backend) | `RadioAudioManager` (browser) |
|---|---|---|
| unit | a message | an *utterance in progress* |
| pre-emption | critical sorts first in the queue | critical **cancels mid-sentence** |
| supersede | same `(agent, subject)`, while queued | same, on the browser's own queue |
| ageing | none | info is dropped after 15 s unheard |
| mutes | — | per agent + master |

The backend cannot do the browser's half: it has no idea a sentence is
*currently being spoken*, only that a message was emitted. The browser cannot do
the backend's half: it never sees the messages an agent superseded before they
aired. Two queues, two jobs — the second is not a re-implementation of the first.

---

## Module map (added)

| Module | Role |
|--------|------|
| `src/rtv/pitwall/tts.py` | Provider interface, registry, `NullProvider`, the REST reference impl, `RadioTTS` (clip cache + URLs). |
| `frontend/js/audio-manager.js` | **Pure** queue/priority/supersede/staleness/mute logic. No `speechSynthesis`, no DOM, no timers. |
| `frontend/js/speech.js` | The noisy half: Web Speech, backend audio, the WebAudio radio click, push-to-talk. |
| `frontend/js/radio-voices.js` | The offline copy of the role voice table. |
| `frontend/js/pitwall-radio.js` | Wiring: socket → manager, controls, PTT. |
| `frontend/radio.html` | The listening page. |
| `frontend/audio-test.html` | The in-page self-test (see *Testing the rules*). |
| `frontend/js/audio-manager.test.js` | The 20 assertions both the page and node run. |

## The default engine is the browser

`speechSynthesis` costs nothing, needs no key, works with the network unplugged,
and is already installed. It is not the fallback — it is **the** engine. The
backend path exists for deployments that want a particular voice, and every
failure in it lands back here rather than in silence.

Per role, the browser gets a voice, a rate and a pitch:

| agent | voice hints | rate | pitch |
|---|---|---|---|
| strategist | George / Daniel / *en-GB* | 0.98 | 0.85 |
| vehicle_engineer | Ryan / Arthur / *en-GB* | 1.02 | 1.00 |
| spotter | Guy / Christopher / *en-US* | 1.22 | 1.18 |
| coach | Sonia / Libby / *en-GB* | 0.95 | 1.10 |

Named voices differ by browser and OS, so the hint list is best-effort and the
resolver falls back to spreading roles deterministically across whatever voices
exist. **Rate and pitch are the guarantee**: every engine supports them, so two
roles that land on the same underlying voice are still told apart by ear. The
table lives in `rtv.pitwall.tts.DEFAULT_VOICES`, is served at
`GET /api/v1/pitwall/tts`, and is mirrored in `radio-voices.js` for when that
call fails — `tests/test_pitwall_audio_frontend.py` parses the JS literal and
asserts the two are identical, so they cannot drift.

## The premium path, and why it is lazy

```python
provider = build_tts_provider(TTSConfig(provider="rest", url=..., api_key=...))
service  = RadioTTS(provider, cache_size=64)
message.audio_url = service.url_for(message)     # a URL, not audio
clip = await service.clip_for(message)           # bytes, on demand, cached
```

`PitwallOrchestrator.publish()` stamps every message with an `audio_url` the
moment it is published, but **synthesises nothing**. The bytes are produced when
a browser fetches `GET /api/v1/pitwall/audio/{message_id}`. A message that gets
superseded before it airs therefore costs zero vendor calls, which is the common
case for a chatty strategist under a safety car.

`message_id` is a **content-derived** SHA-1 prefix of
`(agent, event key, tick, spoken_text)`. Not random, for the same reason the
event log has no wall-clock field: a replayed race must produce the same ids, so
a cached clip stays valid and the UI's de-duplication stays meaningful across a
re-run. It deliberately excludes `seq` — the sequence number is assigned when the
feed accepts the message, i.e. *after* the URL is stamped, and an id that changed
between those two moments would produce a URL resolving to nothing. (It did, for
about ten minutes; the test named
`test_a_published_message_carries_its_audio_url_when_a_provider_can_speak` is
that bug's tombstone.)

### The registry is the extension point

Vendors disagree about request bodies in ways no single config schema survives
(OpenAI puts the voice in the JSON, ElevenLabs puts it in the path). So a new
vendor is a factory registered under a name, not a branch:

```python
register_tts_provider("elevenlabs", lambda config: MyProvider(config))
# RTV_PITWALL_TTS=elevenlabs
```

`RestTTSProvider` is the shipped reference — an OpenAI-compatible
`/audio/speech` body with a bearer token — and doubles as the worked example. Its
HTTP call goes through an injected `Transport`, which is how the whole REST path
is asserted offline: the tests check the exact body it *would* send without
sending one.

**Unavailable is a first-class state.** No provider configured, no URL, no key,
no `httpx`, unknown name, factory raised — every one of those yields a
`NullProvider` carrying the reason. `build_tts_provider` never raises. The
frontend sees no `audio_url`, uses Web Speech, and the operator sees *why* on
`/api/v1/pitwall/tts` instead of a silent radio.

---

## Audio discipline, in detail

`RadioAudioManager` (`frontend/js/audio-manager.js`) is constructed with a
speaker and a clock and nothing else:

```js
const manager = new RadioAudioManager({ speaker, now: () => Date.now() });
manager.push(message);            // -> {action: 'speaking'|'queued'|'interrupted'|'dropped', reason}
manager.finished(message_id);     // the host reports an utterance ended
manager.tick();                   // drop stale info, start anything waiting
manager.setAgentMuted('coach', true);
manager.setMasterMuted(true);
manager.setVolume(0.6);
manager.snapshot();               // {speaking, queue, volume, masterMuted, muted, counters}
```

The rules, and the judgement in each:

- **Single speaker.** One utterance at a time, always.
- **Critical interrupts** — `speaker.cancel()` then straight into the critical.
  The cut-off message is **discarded, not requeued**: re-reading half a stale
  advisory after a "car left!" is worse radio than dropping it. It is kept on
  `lastInterrupted` so the UI can show what was lost.
- **Critical never interrupts critical.** Two urgent calls are both urgent; the
  second waits. This is the browser twin of the backend's "a critical message is
  never superseded and never dropped".
- **Advisory queues** by priority then arrival, and **never ages out**. A
  decision is still a decision ten minutes later.
- **Info is opportunistic**: spoken only if the channel is idle, and dropped once
  it has waited **15 s**. Stale context read out three corners later is worse
  than nothing.
- **Supersede** on `(agent, subject)` while queued, mirroring `RadioFeed`.
- **Mutes drop at the door.** A muted agent's calls are never queued, so unmuting
  does not unleash a backlog of history the driver has already driven past.
- **`speak: false` is never spoken** — that is the driver's own transcript.
- **De-duplication by `message_id`**, so a socket reconnect that replays the log
  does not say everything twice.

A **radio click** (two clipped WebAudio blips, no asset files) plays ahead of a
critical call only. The manager decides — it passes `{cue: true}` — so the rule
is testable and the noise is not.

**Volume and mutes live in the URL** (`?volume=0.6&master=1&muted=coach,spotter`)
and in memory. No `localStorage`, no `sessionStorage`, anywhere; a test greps the
whole frontend for them.

### Push-to-talk

`PushToTalk` (`speech.js`) wraps `SpeechRecognition` / `webkitSpeechRecognition`,
feature-detected: where it is absent the button is **hidden**, not disabled and
not broken. A transcript is POSTed to `/api/v1/pitwall/driver-message`, which
puts it on the channel as `agent: "driver"`, `speak: false`.

Driver messages are **emitted, not queued**: the driver has already used the
airtime, so making the transcript wait behind an advisory would misrepresent when
it happened. They join the radio ring buffer, which means agents see them in
their recent-radio context for free.

---

## API surface (added; stages 1–3 and all of v1 untouched)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/pitwall/tts` | Provider, availability + reason, the role voice table, `engine: backend` or `webspeech` |
| GET | `/api/v1/pitwall/audio/{message_id}` | Synthesised audio bytes for one message |
| POST | `/api/v1/pitwall/driver-message` | `{text, source}` — push-to-talk onto the channel |

`GET /pitwall/tts` answers whether or not the agent layer is mounted, because a
browser needs the voice table before it needs a radio message.

`GET /pitwall/audio/{id}` is deliberately **not** a "speak this text" endpoint.
The only thing that can be synthesised is something an agent actually put on the
radio, looked up by its content-derived id — there is no path from arbitrary text
to the driver's ear, which keeps the grounding discipline intact all the way to
the speaker.

| Status | Means | The frontend does |
|---|---|---|
| 200 | audio bytes (`Cache-Control: immutable`) | plays them |
| 404 | no such message in the ring buffer | speaks it itself |
| 502 | the vendor failed | speaks it itself |
| 503 | no backend provider (the default) | speaks it itself |

Every non-200 has the same consequence — the browser's voice — so a flaky vendor
costs a change of voice, never a missed call.

### `RadioMessage` gained three fields

```jsonc
{"type":"radio","message":{
  "message_id":"9bda500fde3f6475",     // content-derived; audio key + UI de-dup
  "speak": true,                        // false = show it, never say it
  "audio_url": null,                    // null = use the browser's Web Speech API
  "agent":"strategist", "priority":"critical", "spoken_text":"...", ...
}}
```

Everything else about the radio contract is unchanged, and `/pitwall/status`
gained a `tts` block (`null` when no `RadioTTS` is attached).

### Serving the frontend

`create_app()` now mounts `frontend/` at `/` — vanilla JS, no build step, no
frameworks, no CDN. It is mounted **after** every router, and through a
`FrontendFiles` subclass that answers **404 rather than 405** for a non-GET:
a catch-all mount is what an unmatched request finally reaches, and Starlette's
default would have turned every unknown `POST /api/v1/...` into a method error.
Mounting a frontend must not change what the API says about paths it does not
have (`test_pitwall_routes_absent_when_disabled` caught exactly that).

| Page | What it is |
|---|---|
| `/` | placeholder index; the real pitwall UI is stage 6 |
| `/radio.html` | the listening page: live radio, mutes, volume, PTT, demo |
| `/audio-test.html` | the audio-discipline self-test, in-page |

---

## Testing the rules

The queue logic is the part that can be subtly wrong in a way you only discover
mid-race, so it is pure and it is asserted — 20 cases against a fake speaker and
an injected clock, in `frontend/js/audio-manager.test.js`:

```powershell
node frontend/js/run-audio-tests.mjs        # headless; exit code is the result
start http://127.0.0.1:8000/audio-test.html # same module, rendered in-page
```

The repo has **no JS toolchain and did not gain one**: the runner is node plus ES
modules, no `package.json`, no dependencies. `pytest` runs it when a `node`
binary happens to be on PATH and skips it otherwise, so the suite's offline
guarantee is unchanged either way. The page also publishes
`window.__RTV_AUDIO_TEST__` for any later browser automation, and puts
`PASS`/`FAIL` in `document.title`.

What the JS cases pin down, beyond the happy path: a critical cuts an advisory
off and the advisory is *not* requeued; a critical does not cut off a critical;
a queued critical is never superseded; info ages out at 15 s but an advisory
never does; unmuting replays no backlog; a replayed `message_id` is spoken once;
a late `onend` from a cancelled utterance cannot steal the channel.

Python covers the rest: `tests/test_pitwall_tts.py` (36) for the registry and the
providers, `tests/test_pitwall_audio_api.py` (19) for the HTTP contract,
`tests/test_pitwall_audio_frontend.py` (29) for the files being served, the two
voice tables agreeing, and the house rules (no storage APIs, no build step, the
audio manager importing nothing from a browser).

---

## Configuration added

| Env var | Default | Meaning |
|---|---|---|
| `RTV_PITWALL_TTS` | *(empty)* | Provider name. Empty = browser Web Speech only. |
| `RTV_PITWALL_TTS_URL` | *(empty)* | Speech endpoint for `rest`/`openai`. |
| `RTV_PITWALL_TTS_API_KEY` | *(empty)* | Bearer token. Missing means the provider reports unavailable. |
| `RTV_PITWALL_TTS_MODEL` | *(empty)* | Vendor model id. |
| `RTV_PITWALL_TTS_FORMAT` | `mp3` | Requested audio format. |
| `RTV_PITWALL_TTS_MEDIA_TYPE` | `audio/mpeg` | Content-Type served for it. |
| `RTV_PITWALL_TTS_VOICES` | *(empty)* | `strategist=onyx,spotter=fable` overrides. |
| `RTV_PITWALL_TTS_TIMEOUT_S` | `8` | Timeout on one synthesis request. |
| `RTV_PITWALL_TTS_CACHE` | `64` | Clips kept in memory, keyed by `message_id`. |

---

## Hearing it

```powershell
uvicorn rtv.main:app                      # then open http://127.0.0.1:8000/radio.html
```

Press **Enable audio** once (browsers make no sound before a gesture), then:

- **Play synthetic scenario** — six scripted calls with a critical spotter shout
  landing mid-sentence and a superseded pit call. Needs no key and no agents.
- **Replay the scripted race** — drives `POST /api/v1/replay/start` with
  `session_id: "scenario"`. With `RTV_PITWALL_AGENTS=true` and a key, the radio
  is live agent output. Without them the page **says so** and plays the scripted
  calls instead, labelled as scripted — a canned line presented as an agent's
  call would be exactly the ungrounded claim this codebase refuses everywhere
  else.

Mute buttons, the volume slider and the master mute all take effect on the next
utterance boundary (master mute and muting the speaking agent stop the current
one immediately).

---

## Interfaces stage 5 consumes

```python
from rtv.pitwall.tts import (
    RadioTTS, TTSConfig, TTSProvider, VoiceProfile, NullProvider, RestTTSProvider,
    build_radio_tts, build_tts_provider, register_tts_provider, tts_provider_names,
    DEFAULT_VOICES, AUDIO_URL_PREFIX, TTSError, TTSUnavailable,
)

services.tts                          # RadioTTS, always present (usually NullProvider)
orch.publish(message)                 # stamp audio_url + queue -- use this, not feed.publish
orch.driver_message(text, source=...) # push-to-talk onto the channel
orch.tts                              # RadioTTS | None
feed.find(message_id)                 # history then pending
message.message_id                    # content-derived, replay-stable
message.speak / message.audio_url
```

```js
import { RadioAudioManager } from '/js/audio-manager.js';   // pure, testable
import { RadioSpeaker, PushToTalk } from '/js/speech.js';   // browser adapters
```

A director agent (stage 5) that wants to hold the channel should publish through
`orch.publish` like everything else; the browser's manager will apply the same
discipline to it with no frontend change. A new agent gets a voice automatically
(`FALLBACK_VOICE`) and a distinct one by adding a row to `DEFAULT_VOICES` **and**
`radio-voices.js` — the test that compares them will insist.

---

## Deviations from the stage-4 spec

Each is a conservative choice made autonomously, per `CLAUDE.md`.

1. **`POST /api/v1/pitwall/driver-message` did not exist; this stage added it.**
   The spec calls it "existing". It was not in stage 2 or 3, so it is implemented
   here as the smallest honest thing: the transcript joins the radio log as
   `agent: "driver"`, `speak: false`, emitted rather than queued. It is
   deliberately **not** routed to the agents — waking a model on a driver
   utterance is a trigger design decision, and inventing one at the end of a TTS
   stage would have been the wrong place to make it.
2. **There was no `frontend/` directory to build in.** `CLAUDE.md` describes one
   and the v1 analysis UI is built separately, outside this repo. So this stage
   created it, mounted it at `/`, and kept it to what stage 4 needs plus a
   placeholder index — the real pitwall UI is stage 6, and this should be
   replaced rather than extended.
3. **The static mount answers 404, not 405, for a non-GET.** See "Serving the
   frontend": a catch-all mount changes what the API says about unknown paths,
   and an existing stage-1 test was right to object.
4. **The interrupted message is discarded, not requeued.** The spec says
   "critical interrupts current playback (cancel + play)" and is silent on the
   victim. Resuming a half-spoken advisory after an emergency call is worse radio
   than losing it; it is preserved on `lastInterrupted` for the UI.
5. **Master mute silences criticals too.** A mute the driver set that a critical
   could override is not a mute. The control for "only the important ones" is the
   per-agent toggles.
6. **Only one cloud provider shape ships.** The spec asks for "one reference
   implementation stubbed for a cloud TTS (e.g. OpenAI/ElevenLabs-style REST)".
   An ElevenLabs body written from memory would be a guess presented as an
   integration; `RestTTSProvider` implements the OpenAI-compatible shape it can
   be honest about, and the registry — with a test proving a third-party provider
   is one registration — is the seam for the rest.
7. **Synthesis is lazy and cached, not eager on publish.** The spec says
   "backend synthesises to audio bytes served at GET ...". Synthesising at publish
   time would pay for every superseded message and put a vendor round-trip on the
   agent's critical path. The URL is stamped at publish; the bytes are made on
   first fetch and cached by `message_id`.
8. **`message_id` excludes `seq`.** See "The premium path" — including it made
   the stamped URL unresolvable.
9. **A backend-audio failure falls back to Web Speech rather than being retried.**
   Same reasoning as the 502 mapping: a different voice is a much smaller failure
   than a missed radio call.
10. **The replay button plays the scripted radio when no agents are mounted**, and
    labels it as scripted in the UI. Without this the definition-of-done demo is
    silent on a machine with no API key, which is every machine this suite runs
    on; without the label it would be a fake pit call presented as a real one.
11. **`pytest` runs the JS suite only when `node` is on PATH.** Making node a hard
    dependency of the Python suite would break the offline guarantee on a machine
    that has no reason to have it. The same assertions are always reachable in a
    browser via `audio-test.html`.

### A Chrome quirk worth recording

`speechSynthesis.cancel()` followed by `speak()` in the same tick silently drops
the new utterance in Chrome — and that is *exactly* the interrupt path a critical
call takes, so the one thing this stage exists to demonstrate would have failed
there. `_playWebSpeech` schedules the new utterance one frame (40 ms) after the
cancel, guarded so a second interrupt inside that frame still wins. There is also
a 5 s pause/resume keepalive, because Chrome stops speaking after ~15 s.

---

## Verification (stage 4)

```powershell
pytest                                   # 393 passed (309 stage 1-3 + 84 new), fully offline
node frontend/js/run-audio-tests.mjs     # 20/20 audio-discipline cases
python scripts/smoke_pitwall_voice.py    # the voice path end to end, fake vendor transport
python scripts/smoke_pitwall_roles.py    # stage 3, unchanged
python scripts/smoke_pitwall_agents.py   # stage 2, unchanged
python scripts/smoke_pitwall.py          # stage 1, unchanged
python scripts/smoke_offline.py          # v1 surface, unchanged
python evals/run_pitwall.py --dry-run    # 24 cases across 4 agents, unchanged
ruff check src tests scripts evals
```

New test files: `tests/test_pitwall_tts.py` (36), `tests/test_pitwall_audio_api.py`
(19), `tests/test_pitwall_audio_frontend.py` (29; one case skips itself when node
is absent). Green with and without `ANTHROPIC_API_KEY` exported. No iRacing, no
network, no API key. All 309 stage-1/2/3 tests still pass unmodified, and the
scripted race still produces the same 19 events and the same radio log it did
before — the message ids are new metadata on it, not a change to it.

---
---

# Stage 5 — the director seam, and a live path that survives a race

Two jobs, and they are the same job seen from opposite ends. The **race
director** is about making things go wrong on purpose; the **hardening** is about
what happens when they go wrong by accident. Stage 5 closes the backend by
building the first as an interface and the second as a set of asserted
properties.

---

## The race director — deliberately unimplemented

> **Status: planned.** `NoopDirector` is what runs, and it injects nothing.
> `src/rtv/director/` contains the schema, the protocol, one factory function and
> the default. There is no scheduler.

A race director injects things that did not happen: a full-course yellow on
lap 7, rain arriving at half distance, a mandatory stop nobody asked for. It is
how you rehearse a strategist against a race that never runs the same way twice,
and how a demo shows a safety car without waiting for one.

What stage 5 was asked for, and what it therefore built, is the **seam** — and
the seam is the interesting half. The scheduler is a loop over conditions;
whether an injected event is a first-class citizen of the rest of the system is
an architectural question, and it is the one that is now answered with a test.

### Module map

| Module | Role |
|--------|------|
| `director/models.py` | `ScenarioScript`, `ScriptedInjection`, `InjectionTrigger`, `InjectionKind`. Pydantic, validated, JSON-loadable. |
| `director/engine.py` | The `DirectorEngine` protocol, `NoopDirector`, `injected_event()`, `is_injected()`. |
| `docs/director_scenario.example.json` | A worked five-entry script: FCY → restart → rain → mandatory stop → hazard. |

### A script is a list of conditional injections

```jsonc
{
  "id": "fcy_lap_3",
  "kind": "flag",                       // flag | weather | regulation | hazard | custom
  "event_type": "flag_change",          // a real member of the event catalog
  "severity": "critical",
  "when": { "at_lap": 3, "at_lap_dist_pct": 0.30 },
  "payload": { "from": "green", "to": "yellow", "active": ["yellow", "caution"] },
  "note": "why this entry exists"
}
```

`InjectionTrigger` conditions are **conjunctive**: `at_session_time`, `at_lap`,
`at_lap_dist_pct`, `after` + `delay_s`, `while_flag`, `once`. Combining them says
things a clock cannot — `at_lap: 7` plus `at_lap_dist_pct: 0.30` is "coming
through the first sector on lap 7", and `after: "fcy_lap_3"` plus `delay_s: 90`
is "ninety seconds after the yellow", whenever that turned out to be. Delays are
**session** seconds, so a 4× replay rehearses the same race.

Three things are rejected at load rather than at runtime, because a scenario that
fails three laps into a demo has already failed:

- a trigger with **no condition at all** (a silent default of "now" would be a guess);
- `delay_s` with no `after` to be delayed from;
- duplicate ids, self-references, and an `after` naming an entry not in the script.

`RTV_PITWALL_DIRECTOR_SCRIPT` loads and validates a script at boot and binds it to
the `NoopDirector`. Today that is a **linter**, not a runner, and the status
endpoint says so in those words.

### The seam, and what "indistinguishable" means

```python
PitwallOrchestrator(engine, provider, agents, director=my_director)

orch.poll_director()          # -> [RaceEvent], already published on engine.bus
```

The pump calls `poll_director()` on **every** pass, including the idle ones — a
scripted yellow has to be able to land in a quiet minute, not only in the wake of
a detector event. Whatever comes back is published onto `engine.bus`: the same
bus, the same ring buffer, the same `/ws/pitwall` frame, the same subscriber
fan-out, and back around into the orchestrator's *own* subscription. No consumer
anywhere knows a director exists.

```
                    detectors ──┐
                                ├─► engine.bus ─► ring buffer ─► /ws/pitwall
   director.poll(state) ────────┘        └─► orchestrator ─► agents ─► radio
```

Injected events are indistinguishable in every way that changes behaviour: same
`EventType`, same severity, same `key`, same routing, same cooldowns, same agent
output. They are **not** anonymous in the log. Three additive payload keys —
`injected`, `director_kind`, `director_id` — record provenance, and a script
cannot overwrite them (`injected_event` stamps them last).

That is a deliberate reading of "indistinguishable". This codebase refuses to let
an agent assert a number it cannot trace; letting the event log confuse "the sim
threw a yellow" with "we made one up" would be the same failure one level down.
Behaviour is identical, provenance is honest, and the two are not in tension.

### The proof

`tests/test_director.py::test_a_scripted_fcy_produces_the_same_downstream_behaviour`
takes the real `flag_change:yellow` the scripted race throws on lap 3, builds the
director's version from `docs/director_scenario.example.json`, and runs both
through two fresh orchestrators over the *same* race state:

| Compared | Result |
|---|---|
| `event_type`, `key`, `severity` | identical |
| payload keys | identical, plus exactly the three provenance keys |
| agents woken (`dispatch`) | identical — strategist **and** spotter |
| radio: agent, priority, subject, spoken text | identical, call for call |

A second test runs the real pump: director → `engine.bus` → the orchestrator's
subscription → an agent → the radio feed, with nothing in between told what
happened. If any layer had grown a "was this real?" branch, both would fail.

The one-shot director those tests use is **twelve lines** and lives in the test
file, not in `rtv.director`. That is the argument stage 5 is making: the
implementation is small, and the seam it plugs into is the part worth getting
right first.

---

## Hardening the live path

Eight failure modes. Every one of them is **silent** by default — the pitwall
does not crash, it just quietly stops being right — which is exactly why each is
now asserted rather than assumed.

### 1. A frame that raises must cost a frame, not the pitwall

`RaceStateEngine(resilient=True)` catches a per-frame fault, counts it in
`state.metrics.dropped_frames`, and carries on. `services.build_services` sets it;
the constructor default is **off**.

That split is the point. In a live race, 16 ms of missing state is a far smaller
failure than a pitwall that stops at 250 km/h. In a test or a replay it is the
opposite: an engine that silently emitted no events would pass every assertion
about what it does *not* emit, which is a large part of this suite. So the live
path is resilient and the test path is strict, and
`test_resilience_does_not_change_the_event_log_when_nothing_fails` pins that the
two agree when nothing is broken.

Faults are logged **at most once a second**. Whatever broke on this frame will
break on the next fifty-nine, and sixty stack traces a second turns a bug into an
outage of its own. The count survives in `dropped_frames` after the log has moved
on.

### 2. `engine.health()` — "is anything still watching?"

`/api/v1/racestate` says what the race is doing. It cannot say whether anyone is
still looking at it: a `RaceState` full of `None` reads identically whether the
session has not started, the catalog carries none of the channels we wanted, or
frames stopped arriving four minutes ago. Those need three different responses.

```jsonc
{"bound": true, "stale": false, "stale_reason": null,
 "source": "replay", "frames": 3360, "events": 19,
 "faults": 0, "last_fault": null, "seconds_since_frame": 0.4,
 "avg_update_ms": 0.03, "capabilities": {...}, "missing_channels": 0,
 "bus": {"published": 19, "subscribers": 2, "dropped": 0}}
```

`seconds_since_frame` is `null` before the first frame — "never" and "just now"
are different answers, and zero would be a lie about one of them. It is the only
wall-clock number in the package, it is monotonic, and it never enters a
`RaceEvent`, so the replay log stays byte-reproducible.

### 3. iRacing disconnecting mid-session

This is the subtlest one in the list. **The engine has no clock of its own** — it
only moves when a frame moves it — so a disconnect does not corrupt the race
state. It *freezes* it, perfectly, which is worse: "P4, 2.1 s behind" reads
identically whether it is current or four minutes old.

So `RaceState` gained two fields, `stale` and `stale_reason`, and the connection
transition is wired in `services.on_state` — the same callback that already
attaches the session id, so `LivePoller` (v1 ingest) stays untouched:

| | on disconnect | on reconnect |
|---|---|---|
| engine | `mark_stale(reason)` — state frozen and **flagged**, numbers kept | `mark_live()` — flag cleared, detector window dropped |
| agents | `orch.suspend(reason)` — dispatch stops | `orch.resume()` — cooldowns cleared |
| radio | one info notice, "Telemetry lost, pitwall standing by" | "Telemetry back, pitwall live" |

Three judgements worth recording:

- **The last known numbers are kept, not cleared.** They are still the best
  picture anyone has of the race; what was missing was the *label*.
- **The detector window is dropped on resume.** Every frame-window detector —
  lock-up, wheelspin, off-track, pit and lap edges — works on consecutive
  samples, and the frame before a four-minute gap is not the predecessor of the
  frame after it. Without this, a reconnect manufactures a lap completion, a pit
  transition or a wheel-lock out of a discontinuity nobody drove. The test
  replays a whole race with a gap in the middle and asserts the event log is
  **identical** to the uninterrupted one.
- **Suspension is a second flag, not the kill switch.** Telemetry dropping out
  and an operator saying "stop" are different facts; folding them together would
  mean a reconnect quietly re-enabling a layer somebody had turned off on
  purpose. `resume()` also clears the trigger cooldowns — session time did not
  advance while we were gone, but the race did.

`dispatch()` additionally refuses any event whose state is `stale`, so an agent
can never reason over a race that has stopped, whatever else went wrong.

### 4. A supervised task that dies must be restarted, and *visibly*

The failure mode of an unsupervised `asyncio` task is the worst one available: it
stops, nothing crashes, and the radio simply goes quiet. `PitwallOrchestrator`
now runs its pump, its workers and the radio feed under `_supervise()`, which
restarts on any exception with a one-second floor and counts the restarts.

`status()` reports `restarts` and a `healthy` flag alongside `running`, because a
layer that has restarted its pump forty times *is* running and is *not* healthy,
and a UI needs to be able to say the second thing.

### 5. A model call that fails: retry once, then say so out loud

One retry, with a backoff, and then the agent **announces its own failure**:

```
call → 502 → wait RTV_PITWALL_RETRY_BACKOFF_S → call → 502
     → radio: "Pitwall AI degraded - no strategist calls for now." (info)
```

Deliberately *one* retry, not an exponential ladder: a pit call that lands three
corners late is worse than no pit call, so the policy that fits a race is "cover
a dropped connection, then get out of the way". The message history is rebuilt
from the caller's list on the retry, because a half-finished tool exchange is not
a prefix a second attempt can safely continue from.

Saying it out loud matters more than it looks. The failure mode of a silent agent
layer is a driver who believes nobody has anything to tell them. The notice is
`info` priority, carries `{"degraded": true, "agent": ...}` for the UI, and fires
**once per episode** — re-armed by a success, because a chatty failure notice
would be its own outage. Everything deterministic keeps running underneath it:
the engine, the event log, the ring buffer, `/ws/pitwall`, the other three
agents. That is the whole argument for doing the arithmetic upstream.

### 6. A dead provider must not bill for a whole race

`AgentRuntime.invoke` swallows its own errors and returns `None` — right for one
bad call, wrong for a hundred. A revoked key, a retired model id or a vendor
outage would otherwise cost one doomed request per race event until the chequered
flag.

So each agent carries a **circuit breaker**: `RTV_PITWALL_AGENT_FAILURE_LIMIT`
(default 3) consecutive failures take it off the air, with the reason recorded on
`/api/v1/pitwall/status`:

```jsonc
{"name": "strategist", "enabled": false, "consecutive_failures": 3,
 "disabled_reason": "Disabled automatically after 3 consecutive failed invocations.
   Re-enable with POST /api/v1/pitwall/agents/strategist/enabled once the cause is fixed."}
```

Any success re-arms it — this is a cost guard, not a quarantine — and an explicit
re-enable clears it, because a latched breaker would disable the agent again on
its very next failure. `0` turns it off.

### 7. The session cost guard

The breaker catches an agent that is *failing*. This catches one that is
*working* and simply costs more than anyone intended — a trigger storm, a
misconfigured cooldown, a scenario nobody foresaw.

`RTV_PITWALL_MAX_CALLS_PER_SESSION` (default **400**) is a hard ceiling on
billable model calls across every agent in one session. When it trips, `dispatch`
refuses everything, the channel carries one info notice, and the deterministic
layers carry on. `orch.reset()` — between sessions or replays — starts a fresh
budget without discarding the lifetime counters the status endpoint reports.

It is counted in **model calls, not wake-ups**: one wake-up is a tool round trip
or two plus the answer, so the wake-up count would systematically understate the
bill. `AgentRuntime` times and counts every `provider.complete()`, which is also
what puts per-agent call counts and latencies on `/pitwall/status`:

```jsonc
{"calls_used": 24, "calls_remaining": 376, "max_calls_per_session": 400,
 "budget_exhausted": false,
 "agents": [{"name": "strategist", "invocations": 6, "llm_calls": 12,
             "retries": 0, "last_latency_ms": 812.4, "avg_latency_ms": 774.1}]}
```

The default is deliberately generous — it is a runaway guard, not a budget. The
scripted race spends 24 calls; a measured race hour is a low-tens number (the
arithmetic is in `README.md`).

### 8. Two frame sources into one engine

`POST /api/v1/replay/start` now answers **409** while the live poller is running.
Interleaving a replayed race with a live one does not produce a degraded race
state, it produces a nonsensical one — lap counters, fuel and gaps alternating
between two different races. Refusing is the only honest answer.

---

## API surface (added; stages 1–4 and all of v1 untouched)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/pitwall/health` | Engine, live poller, replay, agent layer and director in one payload |

Plus a documented **60-second demo path** (`python scripts/demo_pitwall.py`):
one command starts the server, opens `/radio.html`, waits for the audio gesture,
and replays the scripted race at 4×. See `README.md`.

```jsonc
{"ok": true, "pitwall": true,
 "engine": { /* engine.health() */ },
 "live":   {"running": false, "state": "disconnected", "session_id": null},
 "replay": {"running": false, "finished": true, "frames": 3360},
 "agents": {"mounted": true, "enabled": true, "running": true, "healthy": true,
            "restarts": {}, "degraded": [], "director": {"status": "planned"}}}
```

It is the **only** pitwall route that never returns an error: `/pitwall/radio`
correctly 503s when the agent layer is not mounted, but "is the deterministic
path alive?" is still a question with an answer, and an operator mid-race should
not have to distinguish a 503 that means "off" from one that means "broken".

`ok` is the one judgement in the payload: false when something that should be
producing frames is not, or when the agent layer has restarted a task or tripped
a breaker. Everything else is reported for the caller to weigh.

`/pitwall/status` gained `healthy`, `restarts`, `injected`, `failure_limit`,
`director`, and per-agent `consecutive_failures` / `disabled_reason`.

---

## Configuration added

| Env var | Default | Meaning |
|---|---|---|
| `RTV_PITWALL_AGENT_FAILURE_LIMIT` | `3` | Consecutive failures before an agent is taken off the air. `0` disables the breaker. |
| `RTV_PITWALL_MAX_CALLS_PER_SESSION` | `400` | Hard ceiling on billable model calls in one session. `0` = unlimited. |
| `RTV_PITWALL_RETRY_BACKOFF_S` | `0.5` | Pause before the single retry of a failed model call. |
| `RTV_PITWALL_DIRECTOR_SCRIPT` | *(empty)* | Path to a `ScenarioScript` JSON. Loaded and **validated** at boot, then bound to `NoopDirector` — which injects nothing. |

---

## Interfaces stage 6 (the pitwall UI) consumes

```python
from rtv.director import (
    ScenarioScript, ScriptedInjection, InjectionTrigger, InjectionKind,
    DirectorEngine, NoopDirector, injected_event, is_injected,
    INJECTED_KEY, DIRECTOR_KIND_KEY, DIRECTOR_ID_KEY,
)

engine.health()                  # the operator payload behind /pitwall/health
engine.resilient                 # True on the live path, False in tests/replay
engine.mark_stale(reason)        # telemetry stopped: freeze *and flag*
engine.mark_live()               # frames back: clear the flag and the window
engine.stale                     # bool; also RaceState.stale / .stale_reason

orch.director                    # DirectorEngine, never None
orch.poll_director(state=None)   # -> [RaceEvent], already on engine.bus
orch.suspend(reason) / .resume() # connection lifecycle (not the kill switch)
orch.suspended                   # bool, distinct from .enabled
orch.calls_used / .calls_remaining / .budget_exhausted
orch.healthy()                   # no restarts, no breakers, budget intact
orch.status()["restarts"]        # {"pump": 2, "agent:spotter": 1}
```

For the UI: `GET /api/v1/pitwall/health` is one poll for the whole status bar,
and `is_injected(event)` is how a scripted yellow gets a "DIRECTOR" badge once a
director exists to produce one.

To implement the director, write a class satisfying `DirectorEngine`, build its
events with `injected_event()`, and pass it as `PitwallOrchestrator(director=...)`
— `services.build_director` is the one place that needs to change to select it
from configuration. Nothing else in the system does.

---

## Deviations from the stage-5 spec

Each is a conservative choice made autonomously, per `CLAUDE.md`.

1. **Injected events carry three provenance keys** (`injected`, `director_kind`,
   `director_id`) rather than being byte-anonymous. The spec says
   "indistinguishable from detector events"; that is implemented for everything
   that changes behaviour, and asserted. But an event log that could not tell a
   scripted safety car from a real one would be the same class of failure as an
   agent quoting a number it cannot trace. The keys are additive, so a consumer
   that does not care never sees them.
2. **Two director-only event types were added** — `weather_change` and
   `regulation_change`. The spec names weather and forced-pit regulation as
   things a script must be able to inject, and no detector-backed event could
   express either (the channel contract has nothing that announces rain which has
   not arrived). Adding them to `EventType` is what lets an injection stay an
   *ordinary* event, which is the whole argument for the seam. They are marked as
   director-only in the catalog and no agent trigger references them.
3. **The director hangs off the orchestrator, not the engine.** The spec says
   "orchestrator accepts an optional director", so that is where it is — with the
   consequence that a director does not run when `RTV_PITWALL_AGENTS` is false.
   Moving the poll into `RaceStateEngine` would make injections work without the
   agent layer, but it would also put third-party-ish code on the 60 Hz hot path,
   and the spec did not ask for it. Recorded here as the obvious future move.
4. **`InjectionKind` is descriptive, not behavioural.** A script says
   `kind: "weather"` for a human and for a UI; what actually happens is decided by
   `event_type`. Making the kind dispatch behaviour would have meant writing the
   scheduler this stage was told not to write.
5. **`RTV_PITWALL_DIRECTOR_SCRIPT` validates rather than runs.** The env var
   exists because a schema nobody can point a server at is hard to trust. A bad
   path is a warning, not a boot failure: losing a rehearsal is a smaller failure
   than refusing to start the pitwall, and the reason is reported on
   `/pitwall/status`.
6. **Engine resilience is opt-in, and the live path opts in.** Making it the
   constructor default would have quietly weakened much of this test suite, which
   asserts what the engine does *not* emit as often as what it does.
7. **The disconnect handling is wired in `services.on_state`, not `LivePoller`.**
   The spec says "engine pauses, state flagged stale, agents suspended, clean
   resume" without saying where. `on_state` is already the seam that attaches the
   session id (stage-3 deviation 9), so using it again keeps v1 ingest untouched
   — the constraint `CLAUDE.md` puts above everything else in this build.
8. **"Engine pauses" is implemented as a flag, not a gate.** The engine has no
   clock; with no frames arriving it is *already* paused. What was missing was
   the label, so `mark_stale()` flags and freezes rather than gating `on_frame`.
   A frame arriving is treated as its own announcement of a resume, so the
   recovery works even when nothing tells us about it.
9. **The cost guard counts model calls, not agent wake-ups.** The spec says "hard
   cap on LLM calls per session". A wake-up is a tool round trip or two *plus*
   the answer, so counting wake-ups would have understated the bill by 2–3×.
   `AgentRuntime` counts and times `provider.complete()` itself, which is also
   what supplies the per-agent latencies the spec asks for.
10. **The "Pitwall AI degraded" notice is attributed to the failing agent, not to
    a system channel.** The spec says "emit an info RadioMessage". Using the
    agent's own name means the browser speaks it in that role's voice and the
    per-agent mute applies to it, which is what a listener expects; a separate
    `pitwall` speaker exists only for whole-layer notices (suspension, budget).
11. **The full-system test replaces `services.build_pitwall` with a scripted
    provider.** The spec asks for an integration test that boots the app and
    asserts state + event + radio frames. The agent layer cannot mount offline by
    design (stage-2 deviation 1), so the test monkeypatches the one factory
    function and lets everything else — lifespan, supervised tasks, engine, bus,
    radio feed, WebSocket — be real. The alternative, making the app fall back to
    a scripted provider on its own, would ship fake agent output to production.
12. **The example script is one file, not a directory of them.** The spec asks for
   "one example scenario JSON". It is deliberately the *hardest* interesting race
   — a yellow that lands just before the fuel window, weather that turns the tyre
   call over, and a regulation that removes "stay out" from the answer set —
   rather than a minimal one, because a schema is only as good as the scenario it
   turns out not to be able to express.
13. **`GET /pitwall/health` lives in `routes_racestate.py`, not
    `routes_pitwall.py`.** The latter 503s as a body when the agent layer is
    absent, which is right for the radio and wrong for a health check. Routing it
    with the race-state surface is what makes it answerable whenever
    `RTV_PITWALL` is on.

---

## Verification (stage 5)

```powershell
pytest                                   # 493 passed (393 stage 1-4 + 100 new), fully offline
python scripts/demo_pitwall.py           # the 60-second demo: server + browser + race
python scripts/smoke_pitwall_director.py # the seam + the eight hardening properties
python scripts/smoke_pitwall_voice.py    # stage 4, unchanged
python scripts/smoke_pitwall_roles.py    # stage 3, unchanged
python scripts/smoke_pitwall_agents.py   # stage 2, unchanged
python scripts/smoke_pitwall.py          # stage 1, unchanged
python scripts/smoke_offline.py          # v1 surface, unchanged
python evals/run_pitwall.py --dry-run    # 24 cases across 4 agents, unchanged
node frontend/js/run-audio-tests.mjs     # 20/20 audio-discipline cases, unchanged
ruff check src tests scripts evals
```

New test files: `tests/test_director.py` (36), `tests/test_pitwall_resilience.py`
(25), `tests/test_pitwall_hardening.py` (19), `tests/test_pitwall_health_api.py`
(12), `tests/test_pitwall_fullsystem.py` (9). Green with and without
`ANTHROPIC_API_KEY` exported. No iRacing, no network, no API key. All 393
stage-1/2/3/4 tests still pass unmodified, and the scripted race still produces
the same 19 events and the same radio log it did in stage 1 — which is the check
that matters most here, because stage 5 touched the engine's ingress, the
`RaceState` schema and the orchestrator's task lifecycle.

### The full-system test

`tests/test_pitwall_fullsystem.py` is the one test that boots everything: the
real FastAPI app through its real lifespan — race-state engine, event bus,
orchestrator with its supervised tasks, radio feed, WebSocket — replays the
scripted race, and asserts that `state`, `event` **and** `radio` frames all
arrive on `/ws/pitwall` in contract-valid form (validated against `RaceState`,
`RaceEvent` and `RadioMessage` themselves, not against hand-written shapes).

The only thing that is not real is the model: `rtv.services.build_pitwall` is
replaced with one that hands the orchestrator a `ScriptedProvider`. That is the
same seam the evals and every smoke script use, and it is the only way this can
be a *default* test — the suite must run with no API key and no network, and the
agent layer is opt-in precisely so that it never mounts by accident.

---
---

# Stage 6 — the pit stand

Everything before this stage produced numbers and sentences. This one is the
first that a human looks at during a race, which changes what "correct" means:
a value that is subtly wrong is worse than one that is missing, and a value you
have to hunt for is worse than one that is not there at all.

The result is a second top-level mode of the app, switched in the header:

```
[ Analysis | Pitwall ]
```

**Analysis is untouched.** The MoTeC-i2-style worksheets are a separately built
vanilla-JS app served on its own origin against this API (see deviation 1), so
Analysis mode is a launcher for it plus the v1 session list — it does not
re-implement a single worksheet, and no v1 endpoint changed.

---

## The screen

```
+--------------------------------------------------------------------------+
| Racing Telemetry Visualiser   [Analysis|Pitwall]          radio . api     |
+--------------------------------------------------------------------------+
|                    YELLOW - FULL COURSE CAUTION                          |  <- 4
| session  |  LAPS LEFT 7/12  | track/air | feed: REPLAY | AI LIVE 24/400  |
| replay > [scenario] [4x] [Start] [Stop]              scenario . 4x . 812f |
+------------------------------+-------------------------------------------+
| # STRATEGIST  Box this lap.. | BOX NOW . lap 5 . 3.0 L . rejoin P8       |  <- 2
| +--------------------------+ |  |------#########--+------|   lap ^  dry  |
| | SPOTTER  crit  L5 12:04  | |  4   5   6   7   8   9  10  11  12  13    |
| | Yellow, yellow! Car..    | | margin -1.00 | fuel 6.00 | tank | per lap |
| |  > why                   | | +----+----+  LF 88 ^   RF 91 -            |
| | DRIVER   L5 12:06        | | +----+----+  LR 84     RR 85              |
| |   `- after your message  | +-------------------------------------------+
| |     ENGINEER  Fronts..   | | pos car   gap     last lap                |  <- 3
| +--------------------------+ | P3  #2   +0.60   1:32.418                 |
| [Enable audio][Radio on] vol | P4  #1   +0.30   1:32.104   <- player     |
| [mutes x4]  [say something.] | P5  #0   -0.30   1:32.550   BLUE          |
+------------------------------+-------------------------------------------+
| events  L4 0:42 lockup RF slip 0.42 . L5 0:50 pit window lap 5-7 . ...   |  <- 5
+--------------------------------------------------------------------------+
```

One socket feeds all five panels, with the three frame types routed by what they
are rather than by where they are shown:

```
/ws/pitwall --+-- state  --> coalesced to <=10 Hz --> status strip / strategy / tower
              +-- radio  --> radio feed + RadioAudioManager (stage 4, unchanged)
              +-- event  --> ticker (+ tower warnings + the fuel sparkline)
```

State is coalesced because an old snapshot is worthless once a newer one exists;
radio and events are never coalesced, for the reasons stages 1 and 2 already gave.
The socket is subscribed at 10 Hz because that is also the render ceiling — asking
for 60 would buy frames the app would only throw away, and
`test_the_socket_subscribes_within_the_render_ceiling` pins the two together.

---

## `n/a` is the whole design

`RaceState.capabilities` says which groups this session's catalog could actually
back. The UI treats that as load-bearing rather than as diagnostics: **every**
number on screen goes through a formatter that returns `n/a` for `null`, and no
panel has a zero-valued default anywhere.

| Missing | What the pit stand shows |
|---|---|
| no fuel channels | the pit-window bar draws its axis and says *"no fuel model — pit window unavailable"*; margin, laps of fuel and per-lap are `n/a` |
| no tyre channels | all four corners render, all four read `n/a`, and the header says *"n/a — no tyre channels"* |
| no `gap_basis` | every gap is `n/a` and the tower says *"gaps unavailable: no lap time or track length to convert from"* **once**, at the top |
| no standings channels | *"n/a — this session has no per-car standings channels"* |
| a corner nobody measured | that corner is `n/a`; the other three still show |
| `last_lap_time == 0` | unknown, not "zero seconds" |

That last row is the one that would have slipped through. A lap time of `0.0` is
what the sim reports before a lap has been set; printing `0.000` in a timing tower
is a number nobody can distinguish from a real one.

---

## Module map (added)

| Module | Role |
|--------|------|
| `frontend/index.html` | The app shell: mode switch, status strip, the grid, the ticker, and Analysis mode. |
| `frontend/css/app.css` | Layout and widgets, scoped under `body.app`. `pitwall.css` stays the single source of the palette. |
| `frontend/js/pitwall-app.js` | The wiring: socket, modes, render tick, replay bar, driver input, radio controls, polls. |
| `frontend/js/pitwall/format.js` | **Pure.** Every number-to-string in the app, and the only place `n/a` is produced. |
| `frontend/js/pitwall/view-model.js` | **Pure.** `RaceState` → what each panel shows. Gap trends, fuel bands, the window axis, tower windowing, ticker lines. |
| `frontend/js/pitwall/dom.js` | `el()` / `clear()` / `canvas2d()` / `cssVar()`. |
| `frontend/js/pitwall/status-panel.js` | The strip and the flag band. |
| `frontend/js/pitwall/strategy-panel.js` | The pit-window canvas, the fuel block, the consumption sparkline, the 2x2 tyres. |
| `frontend/js/pitwall/tower-panel.js` | The timing tower. |
| `frontend/js/pitwall/radio-panel.js` | The feed, the on-air strip, driver threading. |
| `frontend/js/pitwall/ticker-panel.js` | The deterministic event strip. |
| `frontend/js/pitwall/view-model.test.js` | The 34 assertions both the page and node run. |
| `frontend/pitwall-test.html` | The in-page self-test. |
| `frontend/js/run-pitwall-tests.mjs` | The headless runner. |

The split is stage 4's, restated: **what a number means** is pure and tested
without a browser; **what it looks like** is a panel that takes a view object and
paints it. A panel with a bug renders badly; a view model with a bug tells the
driver the wrong thing, so that is the half with the test suite.

---

## The four judgements worth recording

### 1. The flag band is the panel

Green is the absence of news, so it is a 4-pixel hairline. Anything else expands
to a 30-pixel full-width band in that flag's colour with the phase written across
it, and yellow and red pulse. "Is there a caution" must never be something you go
looking for on a screen you are glancing at from a rig.

### 2. A gap trend needs the same two guards the backend needed

`GapTrendTracker` marks a car closing, opening or steady. It refuses to compare
two samples less than a second apart (noise), **and** it discards any comparison
that straddles a change of `standings.gap_basis` — when a lap time first becomes
known, gaps stop being derived from instantaneous speed and *every* gap moves at
once while nobody has moved. That is the exact false positive stage 3's traffic
detector had to fix, one layer up, and it is asserted here too.

### 3. The consumption sparkline is the engine's number, not a second one

One point per completed lap, taken from `fuel.per_lap` — the engine's rolling
mean over green, non-pit laps. Subtracting fuel levels client-side would have
been easy and would have produced a second answer to "what does a lap cost",
disagreeing with the strategist's by a tenth. There is one fuel model in this
system.

### 4. Driver messages thread, but are not called replies

A message you send appears as a `DRIVER` entry, and the calls that follow it
within 45 s of session time nest underneath — which is how a transcript reads.
The thread is labelled **"after your message"**, not "reply", because the backend
deliberately does not route driver messages to the agents (stage-4 deviation 1).
An unrelated pit call presented as an answer to the driver would be exactly the
unbacked causal claim the rest of this system refuses to make.

---

## The pit-window bar

A lap axis with four marks, each drawn only if the state carries it:

| Mark | Source | Meaning |
|---|---|---|
| caret + line | `player.lap` | where we are |
| filled span | `fuel.pit_window_earliest_lap` … `_latest_lap` | the window (green when `window_open`) |
| red edge, labelled `dry` | `player.lap + fuel.laps_remaining` | fractional; where the tank actually empties |
| amber edge | `player.lap + fuel.laps_to_finish` | the chequered flag |

The axis is computed to contain every mark it has to draw plus a lap either side.
Drawing a window edge clamped to the end of the axis would read as a decision
nobody made, so an axis that cannot contain its marks is not drawn at all — the
panel says why instead. `the pit-window axis contains every mark it has to draw`
is the check, in the JS suite.

---

## Replay controls, which are also the demo

The replay bar appears when a replay is running **or** when nothing else is
feeding the engine — which is the same state you start a demo from. It posts to
the documented API (`/replay/start` with a session id and a speed, `/replay/stop`)
and reflects `/pitwall/health`'s view of both sources, so it greys out rather than
producing a 409 when the live poller owns the engine.

Analysis mode's session list carries a **"replay on the pitwall"** button per
session, which feeds a stored race back through the race-state engine and switches
modes. `session_id: "scenario"` is the built-in scripted race and needs no capture
at all — that is the zero-setup demo, and it is what the smoke script drives.

Starting a replay resets the client-side accumulators (gap trends, tower warnings,
the fuel sparkline). They are per-race by definition; a re-run that showed a
closing arrow against the previous run's numbers would be worse than showing none.

---

## What the UI consumes (nothing new was added to the API)

Stage 6 added **no endpoints, no env vars and no Python modules**. Everything it
needs was already exposed by stages 1-5:

| Used for | Endpoint |
|---|---|
| everything live | `WS /ws/pitwall` — `state` (10 Hz), `event`, `radio` |
| ticker prefill | `GET /api/v1/racestate/events?limit=` |
| radio prefill | `GET /api/v1/pitwall/radio?limit=` (503 = agent layer off, and the panel says so) |
| AI badge, calls used | `GET /api/v1/pitwall/status` |
| connection / replay / live state | `GET /api/v1/pitwall/health` |
| role voices | `GET /api/v1/pitwall/tts` |
| replay bar | `POST /api/v1/replay/start` · `/stop` |
| driver input | `POST /api/v1/pitwall/driver-message` |
| Analysis mode | `GET /api/v1/sessions` (v1) |

`test_the_app_only_calls_documented_endpoints` resolves every `${API}/...` in the
app against the app's own route table, so a UI that calls a path nobody serves
fails the suite rather than the race.

Seeded radio history is **shown but never spoken** (`speak: false` on the way into
the audio manager): the driver has already driven past it, and a reconnect that
read forty messages back would be its own outage.

---

## Testing something with no build step

Three layers, none of which needed a toolchain.

**1. The pure rules, in JavaScript.** 34 cases over plain objects — degradation,
fuel bands, the window axis, tower windowing, gap trends, warning expiry, the
sparkline, ticker lines, the strategist summary.

```powershell
node frontend/js/run-pitwall-tests.mjs          # headless; exit code is the result
start http://127.0.0.1:8000/pitwall-test.html   # same module, rendered in-page
```

**2. The wiring, from Python.** No bundler means no compiler, so the things a
compiler would have caught are asserted instead: every `import` in the app graph
resolves to a file **and** is served as `text/javascript`; every element id the
app reaches for exists on the page; every `data-role` a panel queries and every
`data-field` a panel writes exists in the markup. A `_set('fuel-margin', ...)`
against a field that is not there is a silent no-op — precisely the dead control
this project has no other way of noticing.

**3. The whole thing, booted.** `scripts/smoke_pitwall_ui.py` serves the real
frontend from the real app, replays the scripted race over `/ws/pitwall` at 10 Hz,
and checks that state and event frames arrive contract-valid, that the replay bar's
start/stop round trip works, that all three REST polls answer with **no agent
layer mounted**, and that a session stripped of its fuel and tyre channels reports
that in `capabilities` rather than zeroing it.

---

## Deviations from the stage-6 spec

Each is a conservative choice made autonomously, per `CLAUDE.md`.

1. **"The existing analysis app" is not in this repo.** The spec says to add a mode
   to it. The MoTeC-i2-style worksheets are built separately and served on their
   own origin against this API (stage-4 deviation 2 recorded the same gap), so
   there was no analysis UI here to add a mode *to*. The mode switcher is real and
   both modes are top-level; Analysis mode is a launcher — the v1 session list plus
   a field for wherever you serve the worksheets, remembered in the page URL. It
   deliberately re-implements nothing: building a second analysis UI would have
   been the one thing `CLAUDE.md` forbids most clearly.
2. **The pitwall is the root page; `/radio.html` stays.** The spec's "new top-level
   mode" wants one app, and the placeholder index this replaced said it should be.
   Stage 4's listening page is untouched and still linked, because it is the
   minimal reproduction when the question is *"is the audio broken or is the UI?"*.
3. **The timing tower shows `#idx`, not a car number.** `CarState` carries
   `idx` — the sim's car-index — and no race number; the channel contract in
   `racestate/channels.py` has nothing that would supply one. Printing a plausible
   number would be exactly the invented figure this codebase refuses. `#idx` is
   honest and stable, and a real number channel can replace it without a layout
   change.
4. **Driver threading is labelled "after your message", not "reply".** See
   judgement 4 above.
5. **The replay bar appears when nothing is feeding the engine**, not only when a
   replay is already running. The spec says "when in replay mode", but a control
   that only exists once you are already in replay mode cannot start one — and the
   spec also says the same control doubles as the demo. It hides itself when the
   live poller owns the engine, which is the state where a replay would be refused
   with a 409 anyway.
6. **Speed applies to the next replay, not the running one.** `ReplayDriver.start`
   takes a speed; nothing changes one mid-run, and adding an endpoint for it would
   have been backend work this stage was not asked for. The bar says so when you
   change it while a replay is in flight.
7. **The fuel sparkline plots the engine's rolling mean, not per-lap deltas.** See
   judgement 3.
8. **No new env var for the analysis app's URL.** It lives in the page URL
   (`?analysis=...`), like volume, mutes and mode. A server-side setting would have
   meant a new env var, a new endpoint to read it and a restart to change it, for
   something that is per-operator rather than per-deployment. `.env.example` is
   therefore unchanged — stage 6 added no configuration at all.
9. **The tower's "expand" is a toggle, not a scroll.** The spec says "P±3 at
   minimum, expandable to full field". Collapsed is exactly P±3 around the player;
   expanded is the whole running order in the same rows. A full field that is
   always rendered and scrolled would have meant the player row is sometimes off
   screen, which is the one row that must never be.
10. **A second in-page self-test page rather than one combined with the audio
    one.** `audio-test.html` asserts what the driver *hears*; `pitwall-test.html`
    asserts what the driver *sees*. Merging them would have coupled two suites that
    fail for entirely different reasons.
11. **The pitwall does not inherit `/radio.html`'s scripted-radio fallback.** With
    no agent layer mounted the radio column stays empty and the page says why (on
    the AI badge, and again when you start a replay). `/radio.html` may play canned
    calls because it labels them as scripted and its whole subject is the audio
    queue; a panel whose entire job is showing what the agents said cannot fill
    itself with lines no agent produced. `scripts/demo_pitwall.py` therefore still
    opens `/radio.html` — its documented promise is the audible demo — and now
    points at `/` as the second thing to look at, driven by the same replay.

### One bug found and fixed en route

The audio manager's `onChange` paints the radio panel, and applying the URL's
mutes at boot fires it — so the panels had to be constructed **before** the
manager, not after. In the original order it was a temporal-dead-zone
`ReferenceError` on the first line of the app, i.e. a completely blank pitwall,
and nothing in a Python test suite would have seen it. It was caught by booting
the real `index.html` under a throwaway DOM shim; the ordering now carries a
comment saying why it is what it is.

---

## Interfaces a later stage consumes

```js
import { statusView, strategyView, towerView, tyreView, recommendationView,
         eventLine, marginBand, connectionView, llmView,
         GapTrendTracker, TowerWarnings, FuelHistory } from '/js/pitwall/view-model.js';
import { NA, isNum, num, lapTime, clock, gap, signed, trendArrow } from '/js/pitwall/format.js';
import { el, clear, stat, setText, setClass, canvas2d, cssVar } from '/js/pitwall/dom.js';

window.RTV_PITWALL   // {app, manager, speaker, panels:{status,strategy,tower,radio,ticker}}
```

A new panel is a class with `render(view)` plus a `data-role` host in
`index.html`; a new *number* is a field on one of the view builders, which is
where its `n/a` behaviour gets asserted. A new agent needs nothing here — the
radio panel keys its badge colour off `RadioMessage.agent`, and an unknown agent
gets the neutral one.

When a race director is implemented (stage 5), its events already arrive on the
ticker with a `DIRECTOR` tag: `eventLine()` reads the `injected` / `director_id`
provenance keys, and the case *"a director-injected event keeps its provenance in
the ticker"* pins it.

---

## Verification (stage 6)

```powershell
pytest                                   # 539 passed (493 stage 1-5 + 46 new), fully offline
python scripts/smoke_pitwall_ui.py       # the UI end to end: shell, socket, replay, degradation
node frontend/js/run-pitwall-tests.mjs   # 34/34 view-model cases
node frontend/js/run-audio-tests.mjs     # 20/20 audio-discipline cases, unchanged
python scripts/smoke_pitwall_director.py # stage 5, unchanged
python scripts/smoke_pitwall_voice.py    # stage 4, unchanged
python scripts/smoke_pitwall_roles.py    # stage 3, unchanged
python scripts/smoke_pitwall_agents.py   # stage 2, unchanged
python scripts/smoke_pitwall.py          # stage 1, unchanged
python scripts/smoke_offline.py          # v1 surface, unchanged
python evals/run_pitwall.py --dry-run    # 24 cases across 4 agents, unchanged
ruff check src tests scripts evals
```

New test file: `tests/test_pitwall_ui_frontend.py` (46; one case skips itself when
node is absent). Green with and without `ANTHROPIC_API_KEY` exported. No iRacing,
no network, no API key. All 493 stage-1-to-5 tests still pass unmodified — stage 6
added no Python outside `tests/` and `scripts/`, and touched no v1 route, no
endpoint and no env var.

## Seeing it

```powershell
uvicorn rtv.main:app          # then open http://127.0.0.1:8000/
```

Press **Enable audio** once (browsers make no sound before a gesture), then
**Start** on the replay bar with `scenario` — the scripted race drives the whole
screen with no key, no iRacing and no captured session. With
`RTV_PITWALL_AGENTS=true` and a key exported, the radio column fills with live
agent output; without them it stays empty and the AI badge says why, which is the
honest version of a quiet radio.
