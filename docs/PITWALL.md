# RTV v2 — Pitwall build state

Shared state between the staged headless builds. Each stage appends: what it
built, the public interfaces the next stage consumes, and any deviations from
its spec (with a one-line rationale).

| Stage | Scope | Status |
|-------|-------|--------|
| **1** | Deterministic race-state engine + event bus + replay | **done** |
| **2** | Agent framework + orchestrator + strategist | **done** |
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
| `pit_window_closing` | advisory | current lap reaches `latest_lap - 1` while open | `latest_lap`, `laps_remaining`, `per_lap` |
| `fuel_margin_low` | advisory | `margin_laps` drops below `RTV_PITWALL_FUEL_MARGIN_LAPS` | `margin_laps`, `margin_l`, `threshold_laps` |
| `fuel_critical` | critical | `laps_remaining < 1.5`; re-arms on refuel | `level`, `laps_remaining` |
| `stint_lap_milestone` | info | every N green laps on the current set | `stint`, `laps_on_tyres`, `every`, `lap_time` |
| `rival_pitted` | advisory | a nearby car's `CarIdxOnPitRoad` goes false→true | `car_idx`, `position`, `gap_to_player` |
| `blue_flag` | advisory | a car ≥0.7 laps up is closing within 2.5 s | `car_idx`, `gap`, `laps_ahead` |

The five events added in stage 2 exist so the strategist can be woken by *facts*
rather than by a clock. `stint_lap_milestone` in particular is the periodic
wake-up, and it is deliberately lap-driven: a timer would fire under a red flag,
in the pits, and at 4× replay speed.

`pit_window_closing` and `fuel_margin_low` are distinct from the events they sit
next to. "Open" says a stop is *available*; "closing" says it is now urgent.
`margin_laps` is slack against the **finish**, so it goes negative many laps
before `fuel_critical` (slack against **running dry**) — which is exactly the
lead time a strategist needs.

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
registry entry**:

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
