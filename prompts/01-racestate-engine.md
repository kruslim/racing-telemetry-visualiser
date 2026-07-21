HEADLESS MODE — no human is watching this session. Rules:
- Work autonomously to completion. Never stop to ask a question; make the conservative choice that best matches existing repo patterns and record it in docs/PITWALL.md under "Deviations".
- Before finishing: run the FULL `pytest` suite and iterate until it is completely green (offline — no iRacing, no ANTHROPIC_API_KEY needed). Do not finish with failing or skipped-because-broken tests.
- Also run any smoke script mentioned below and make it pass.
- Update docs/PITWALL.md with what you built, the public interfaces the next stage will consume, and any deviations from this spec.
- Do not refactor or break existing v1 functionality (analysis UI, coaching API, MCP server, existing tests).

TASK:

Read the repo first: README.md, docs/COACHING.md, src/rtv/ (especially ingest/, store/, stream/, coaching/), and the existing test suite, so you follow the established patterns (import-guarded deps, offline-first tests, Pydantic models, services.py wiring).

GOAL
Build a deterministic, LLM-free "race state engine" — the hot loop of a race-engineering pitwall. It consumes the existing live 60 Hz frame stream (and, for offline dev/testing, replayed .ibt sessions) and maintains a single continuously-updated RaceState object plus an event bus that downstream AI agents will subscribe to in a later step. No LLM calls anywhere in this prompt.

NEW PACKAGE: src/rtv/racestate/

1. RaceState model (Pydantic) — one object, versioned by tick, containing:
   - session: session type, time remaining, laps remaining, track/car, weather/track temp if channels exist
   - flags: current flag state decoded from SessionFlags (green, yellow, FCY/caution, white, checkered), with time-in-state
   - standings: for each car index from the per-car-index array channels (CarIdxLapDistPct, CarIdxPosition, CarIdxLap, etc.): position, gap ahead/behind in seconds (derive from lap-dist deltas and speed), last lap time, pit status
   - player: current lap, position, lap-dist, current stint number, laps on tyres
   - fuel: current level, per-lap consumption (rolling mean over last N green laps + std), laps of fuel remaining, fuel needed to finish, pit window (earliest/latest lap to stop), margin
   - tyres: per-corner temps/pressures where available (LF/RF/LR/RR channels), rolling trend per stint
   - car_health: engine/oil/water temps, damage channels if present
   - conditions: track usage/rubber state, air/track temp trends
   Degrade gracefully: any channel absent from the runtime catalog → that field is None and a capability flag records it (mirror the grounded-refusal philosophy — never invent data).

2. Deterministic detectors (racestate/detectors.py) — pure functions over recent frame windows:
   - lockup: front wheel speed(s) drop vs car speed while brake > threshold
   - wheelspin: driven wheel speed exceeds car speed while throttle > threshold
   - offtrack / incident: from surface-type or incident-count channels
   - pit entry/exit, stint boundary
   - flag transitions (green→yellow etc.)
   - fastest-lap / personal-best lap completion
   - blue-flag / lapped-traffic-approaching from standings deltas
   Each detector emits a typed RaceEvent (Pydantic): {event_type, tick, session_time, lap, severity: info|advisory|critical, payload}.

3. Event bus (racestate/bus.py): in-process pub/sub. Sync + asyncio-friendly subscribe. Events carry the RaceState snapshot version at emission time. Bounded queue per subscriber with latest-wins coalescing for slow consumers (same philosophy as /ws/live).

4. Engine loop (racestate/engine.py): subscribes to the existing live frame buffer at full rate, updates RaceState incrementally (no full recompute per tick), runs detectors, publishes events. Target: state update well under 16 ms/frame on commodity hardware. Add a lightweight self-timing metric.

5. REPLAY DRIVER (racestate/replay.py): drive the identical engine from a stored session (the DuckDB/Parquet store or directly from an .ibt via the existing importer) at 1x/Nx/as-fast-as-possible speed. This is the offline dev path — the engine must not know whether frames are live or replayed. Also add a small synthetic race scenario generator (extend the existing demo-session seeder) that produces a scripted multi-car race with a fuel stop, a yellow-flag phase, one lockup, and a pit cycle — this becomes ground truth for tests.

6. API surface:
   - GET /api/v1/racestate → current RaceState snapshot
   - WebSocket /ws/pitwall → streams {type:"state"} snapshots at a client-chosen rate plus every {type:"event"} immediately (events are never coalesced away; state is latest-wins)
   - POST /api/v1/replay/start {session_id, speed} · POST /api/v1/replay/stop

7. Wire into services.py/main.py behind a feature flag RTV_PITWALL=true (default true), keeping all existing endpoints untouched.

TESTS (offline, no iRacing, no API key — match existing suite conventions):
- unit tests per detector with synthetic frame windows (lockup fires; near-miss doesn't)
- fuel model: known synthetic consumption → correct pit window and margin
- gap computation on the synthetic multi-car scenario
- replay determinism: same session replayed twice → identical event sequence (assert equality)
- missing-channel degradation: strip tyre channels from catalog → fields None, capability flags set, no crash
- /ws/pitwall contract test via TestClient

DEFINITION OF DONE
- pytest fully green offline
- scripts/smoke_pitwall.py: replays the synthetic scenario as-fast-as-possible, prints the event log, asserts the expected ordered event sequence (yellow, lockup, pit window open, pit entry, pit exit)
- docs/PITWALL.md started: RaceState schema, event catalog, replay usage
