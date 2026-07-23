"""The race-state engine: one incremental update per frame, plus detectors.

The engine is deliberately source-agnostic. It exposes a single ingress --
:meth:`RaceStateEngine.on_frame` -- with exactly the signature the live poller's
``on_frame`` callback already uses, so live telemetry and replayed sessions drive
identical code. That is what makes replay a real test of the live path rather
than a parallel implementation.

Cost discipline: nothing is recomputed from scratch per tick. Cheap scalars
(player, flags, fuel level, health) update every frame; standings gaps run on a
decimated cadence; fuel statistics and tyre trends only recompute at lap
boundaries. :class:`~rtv.racestate.models.EngineMetrics` reports the measured
per-frame cost so the budget is observed rather than assumed.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any

from rtv.catalog.enums import ENGINE_WARNINGS as ENGINE_WARNING_BITS
from rtv.catalog.enums import SESSION_STATE, TRK_LOC, decode_bitfield
from rtv.catalog.models import Catalog
from rtv.ingest.frame import Frame
from rtv.logging import get_logger
from rtv.racestate import channels as ch
from rtv.racestate import detectors as det
from rtv.racestate.bus import EventBus
from rtv.racestate.models import (
    Capabilities,
    CarState,
    EventType,
    FlagPhase,
    RaceEvent,
    RaceState,
    Severity,
)

log = get_logger("racestate.engine")

#: Cars with a lap-distance below this are not in the world (iRacing uses -1).
_INACTIVE_PCT = -0.5
#: Timed sessions report an absurd laps-remaining sentinel; treat it as unknown.
_LAPS_REMAIN_SENTINEL = 10_000
#: Tolerance on the lap-count arithmetic below. Consumption is an *estimate*, so
#: a value a hair over an integer boundary (0.5000001 L/lap from float32 storage)
#: must not cost a whole lap of pit window.
_LAP_EPS = 1e-6
#: Frame-window bounds: no detector looks back further than a handful of frames.
_WINDOW_KEEP = 32
_WINDOW_TRIM_AT = 64


def _lstsq_slope(xs: list[float], ys: list[float]) -> float | None:
    """Least-squares slope of y over x, or None when it is not determined."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _stdev(vals: list[float]) -> float | None:
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


class RaceStateEngine:
    """Maintains one :class:`RaceState` and publishes :class:`RaceEvent`s."""

    def __init__(
        self,
        *,
        bus: EventBus | None = None,
        config: det.DetectorConfig = det.DEFAULT_CONFIG,
        source: str = "live",
        fuel_laps: int = 5,
        gap_interval: int = 6,
        trend_window_s: float = 120.0,
        event_cooldown_s: float = 1.0,
    ) -> None:
        self.bus = bus or EventBus()
        self.config = config
        self.source = source
        self._fuel_laps = fuel_laps
        self._gap_interval = max(1, gap_interval)
        # Fuel derivations refresh ~1 Hz. Lap boundaries alone are not enough:
        # the tank drains continuously, and a snapshot showing a full tank beside
        # a stale "1 lap remaining" would be internally inconsistent.
        self._fuel_interval = max(1, gap_interval * 10)
        self._trend_window_s = trend_window_s
        self._event_cooldown_s = event_cooldown_s

        self._lock = threading.RLock()
        self._state = RaceState()
        self._state.session.source = source
        self._catalog: Catalog | None = None
        self._schema_hash: str | None = None
        self.reset()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Clear all accumulators. Called on bind and between replays."""
        with self._lock:
            state = RaceState()
            state.session.source = self.source
            if self._catalog is not None:
                state.capabilities = self._capabilities(self._catalog)
                state.session.car_id = self._catalog.car_id
                state.session.track_id = self._catalog.track_id
            self._state = state

            # A list, not a deque: detectors slice the tail, and deques do not
            # support slicing. Trimmed in bulk below so appends stay amortised O(1).
            self._window: list[dict[str, Any]] = []
            self._frames = 0
            self._avg_ms = 0.0
            self._max_ms = 0.0

            self._last_lap: int | None = None
            self._latched: set[str] = set()
            self._last_fire: dict[str, float] = {}

            # fuel
            self._fuel_used: deque[float] = deque(maxlen=self._fuel_laps)
            self._fuel_at_lap_start: float | None = None
            self._pit_window_announced = False
            self._fuel_critical_announced = False

            # stint / tyres
            self._lap_had_pit = False
            self._lap_all_green = True
            self._tyre_temp_sum: dict[str, float] = {}
            self._tyre_press_sum: dict[str, float] = {}
            self._tyre_n = 0
            self._stint_temp_laps: list[tuple[int, dict[str, float]]] = []
            self._stint_press_laps: list[tuple[int, dict[str, float]]] = []

            # laps
            self._player_best: float | None = None
            self._session_best: float | None = None

            # conditions trend
            self._cond_hist: deque[tuple[float, float | None, float | None]] = deque()
            self._last_cond_sample = -1e9

    def bind_catalog(self, catalog: Catalog, *, session_id: str | None = None) -> None:
        """Probe the catalog for capabilities and reset accumulators."""
        with self._lock:
            self._catalog = catalog
            self._schema_hash = catalog.schema_hash
            self._resolve_channels(catalog)
            self.reset()
            self._state.session.session_id = session_id
            self._state.session.car_id = catalog.car_id
            self._state.session.track_id = catalog.track_id
            log.info(
                "Race state bound to %s catalog (%d variables); capabilities: %s",
                catalog.source,
                len(catalog),
                ", ".join(
                    k
                    for k, v in self._state.capabilities.model_dump().items()
                    if v is True
                )
                or "none",
            )

    def set_session_info(self, info: Mapping[str, Any] | None) -> None:
        """Feed the session-info YAML so track length (and names) are known.

        Optional: without it, gaps simply fall back to the lap-time basis.
        """
        if not info:
            return
        weekend = info.get("WeekendInfo") or {}
        with self._lock:
            length = _parse_track_length(weekend.get("TrackLength"))
            if length is not None:
                self._state.session.lap_length_m = length
                self._state.standings.lap_length_m = length
            name = weekend.get("TrackDisplayName") or weekend.get("TrackName")
            if name:
                self._state.session.track_name = str(name)

    # ------------------------------------------------------------------
    # capability probing
    # ------------------------------------------------------------------
    def _resolve_channels(self, catalog: Catalog) -> None:
        """Pick the concrete channel names this catalog offers for each group."""
        self._tyre_temp_ch = {
            c: n for c, n in ch.TYRE_TEMP.items() if n in catalog
        }
        self._tyre_press_ch = {c: n for c, n in ch.TYRE_PRESSURE.items() if n in catalog}
        if not self._tyre_press_ch:
            # Fall back to the flattened 4-element TyrePressure array form.
            cols = {col for var in catalog.variables.values() for col, _ in var.columns}
            self._tyre_press_ch = {
                c: n for c, n in ch.TYRE_PRESSURE_FLAT.items() if n in cols
            }
        self._wheel_front = [c for c in ch.FRONT_WHEELS if ch.WHEEL_SPEED[c] in catalog]
        self._wheel_rear = [c for c in ch.REAR_WHEELS if ch.WHEEL_SPEED[c] in catalog]

    def _capabilities(self, catalog: Catalog) -> Capabilities:
        missing: list[str] = []

        def has(name: str) -> bool:
            if name in catalog:
                return True
            missing.append(name)
            return False

        def has_all(names) -> bool:
            # Evaluate every name so `missing` is complete, not short-circuited.
            return all([has(n) for n in names]) if names else False

        caps = Capabilities(
            session=has_all(ch.REQUIRED["session"]),
            flags=has_all(ch.REQUIRED["flags"]),
            standings=has_all(ch.REQUIRED["standings"]),
            player=has_all(ch.REQUIRED["player"]),
            fuel=has_all(ch.REQUIRED["fuel"]),
            pit=has_all(ch.REQUIRED["pit"]),
            incidents=has_all(ch.REQUIRED["incidents"]),
            offtrack=has_all(ch.REQUIRED["offtrack"]),
            tyres=bool(self._tyre_temp_ch or self._tyre_press_ch),
            car_health=any(n in catalog for n in ch.CAR_HEALTH_ANY),
            conditions=any(n in catalog for n in ch.CONDITIONS_ANY),
            lockup=bool(self._wheel_front) and ch.BRAKE in catalog and ch.SPEED in catalog,
            wheelspin=bool(self._wheel_rear)
            and ch.THROTTLE in catalog
            and ch.SPEED in catalog,
        )
        if not caps.tyres:
            missing.extend(sorted(set(ch.TYRE_TEMP.values()) | set(ch.TYRE_PRESSURE.values())))
        if not caps.lockup:
            missing.extend(ch.WHEEL_SPEED[c] for c in ch.FRONT_WHEELS)
        if not caps.wheelspin:
            missing.extend(ch.WHEEL_SPEED[c] for c in ch.REAR_WHEELS)
        caps.missing = sorted(set(missing))
        return caps

    # ------------------------------------------------------------------
    # ingress
    # ------------------------------------------------------------------
    def on_frame(self, frame: Frame, catalog: Catalog) -> None:
        """Ingest one decoded tick. Safe to call from any single producer thread."""
        started = time.perf_counter()
        with self._lock:
            if catalog.schema_hash != self._schema_hash:
                self.bind_catalog(catalog, session_id=self._state.session.session_id)
            events = self._update(frame)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._frames += 1
            self._avg_ms += (elapsed_ms - self._avg_ms) / self._frames
            self._max_ms = max(self._max_ms, elapsed_ms)
            m = self._state.metrics
            m.frames = self._frames
            m.last_update_ms = round(elapsed_ms, 4)
            m.avg_update_ms = round(self._avg_ms, 4)
            m.max_update_ms = round(self._max_ms, 4)
            m.events += len(events)
        # Publish outside the lock: consumer callbacks must not block the loop.
        for event in events:
            self.bus.publish(event)

    def snapshot(self) -> RaceState:
        """A deep, consistent copy of the current state."""
        with self._lock:
            return self._state.model_copy(deep=True)

    @property
    def state(self) -> RaceState:
        """The live (mutable) state object -- prefer :meth:`snapshot`."""
        return self._state

    # ------------------------------------------------------------------
    # the incremental update
    # ------------------------------------------------------------------
    def _update(self, frame: Frame) -> list[RaceEvent]:
        values = frame.values
        self._window.append(dict(values))
        if len(self._window) > _WINDOW_TRIM_AT:
            del self._window[:-_WINDOW_KEEP]
        state = self._state
        state.version += 1
        state.tick = frame.tick
        state.session_time = frame.session_time
        state.wall_time = frame.wall_time

        events: list[RaceEvent] = []
        caps = state.capabilities

        self._update_session(values)
        if caps.flags:
            self._update_flags(values, events)
        if caps.player:
            self._update_player(values)
        if caps.fuel:
            self._update_fuel_level(values)
            if state.version % self._fuel_interval == 0:
                self._recompute_fuel_plan(events)
        if caps.tyres:
            self._update_tyres(values)
        if caps.car_health:
            self._update_car_health(values)
        if caps.conditions:
            self._update_conditions(values)
        if caps.standings and (state.version % self._gap_interval == 0):
            self._update_standings(values)

        self._run_detectors(frame, events)
        self._check_lap_boundary(frame, events)

        for event in events:
            event.state_version = state.version
        return events

    # ---- section updaters ------------------------------------------------
    def _update_session(self, values: Mapping[str, Any]) -> None:
        s = self._state.session
        st = det.as_int(values, ch.SESSION_STATE)
        if st is not None:
            s.state = SESSION_STATE.get(st, f"value:{st}")
        s.time_remaining = det.as_float(values, ch.SESSION_TIME_REMAIN)
        laps = det.as_int(values, ch.SESSION_LAPS_REMAIN)
        if laps is None:
            laps = det.as_int(values, ch.SESSION_LAPS_REMAIN_FALLBACK)
        s.laps_remaining = laps if laps is not None and 0 <= laps < _LAPS_REMAIN_SENTINEL else None
        total = det.as_int(values, ch.SESSION_LAPS_TOTAL)
        s.laps_total = total if total is not None and 0 < total < _LAPS_REMAIN_SENTINEL else None

    def _update_flags(self, values: Mapping[str, Any], events: list[RaceEvent]) -> None:
        f = self._state.flags
        raw = det.as_int(values, ch.SESSION_FLAGS)
        phase, active = det.flag_phase(raw)
        f.raw = raw
        f.active = active
        now = self._state.session_time
        if phase != f.phase:
            previous = f.phase
            f.phase = phase
            f.since_session_time = now
            f.time_in_state = 0.0
            events.append(
                self._event(
                    EventType.FLAG_CHANGE,
                    severity=Severity.CRITICAL
                    if phase in (FlagPhase.RED, FlagPhase.YELLOW)
                    else Severity.INFO,
                    payload={"from": previous.value, "to": phase.value, "active": active},
                )
            )
        elif f.since_session_time is not None:
            f.time_in_state = max(0.0, now - f.since_session_time)
        if phase is not FlagPhase.GREEN:
            self._lap_all_green = False

    def _update_player(self, values: Mapping[str, Any]) -> None:
        p = self._state.player
        p.lap = det.as_int(values, ch.LAP)
        p.lap_dist_pct = det.as_float(values, ch.LAP_DIST_PCT)
        p.speed = det.as_float(values, ch.SPEED)
        p.gear = det.as_int(values, ch.GEAR)
        p.rpm = det.as_float(values, ch.RPM)
        p.on_pit_road = det.as_bool(values, ch.ON_PIT_ROAD)
        p.position = det.as_int(values, ch.PLAYER_CAR_POSITION)
        p.class_position = det.as_int(values, ch.PLAYER_CAR_CLASS_POSITION)
        p.incidents = det.as_int(values, ch.PLAYER_INCIDENTS)
        surface = det.as_int(values, ch.PLAYER_TRACK_SURFACE)
        p.track_surface = TRK_LOC.get(surface) if surface is not None else None
        last = det.as_float(values, ch.LAP_LAST_LAP_TIME)
        p.last_lap_time = last if (last or 0) > 0 else None
        best = det.as_float(values, ch.LAP_BEST_LAP_TIME)
        p.best_lap_time = best if (best or 0) > 0 else self._player_best

    def _update_fuel_level(self, values: Mapping[str, Any]) -> None:
        fuel = self._state.fuel
        fuel.level = det.as_float(values, ch.FUEL_LEVEL)
        pct = det.as_float(values, ch.FUEL_LEVEL_PCT)
        fuel.level_pct = pct
        if fuel.level is not None and pct is not None and pct > 0.01:
            fuel.capacity = round(fuel.level / pct, 3)
        if self._fuel_at_lap_start is None and fuel.level is not None:
            self._fuel_at_lap_start = fuel.level

    def _update_tyres(self, values: Mapping[str, Any]) -> None:
        t = self._state.tyres
        for corner, name in self._tyre_temp_ch.items():
            v = det.as_float(values, name)
            if v is not None:
                t.temps[corner] = v
                self._tyre_temp_sum[corner] = self._tyre_temp_sum.get(corner, 0.0) + v
        for corner, name in self._tyre_press_ch.items():
            v = det.as_float(values, name)
            if v is not None:
                t.pressures[corner] = v
                self._tyre_press_sum[corner] = self._tyre_press_sum.get(corner, 0.0) + v
        self._tyre_n += 1

    def _update_car_health(self, values: Mapping[str, Any]) -> None:
        h = self._state.car_health
        h.oil_temp = det.as_float(values, ch.OIL_TEMP)
        h.water_temp = det.as_float(values, ch.WATER_TEMP)
        h.oil_pressure = det.as_float(values, ch.OIL_PRESS)
        h.fuel_pressure = det.as_float(values, ch.FUEL_PRESS)
        h.voltage = det.as_float(values, ch.VOLTAGE)
        h.tow_time = det.as_float(values, ch.TOW_TIME)
        warn = det.as_int(values, ch.ENGINE_WARNINGS)
        h.engine_warnings = (
            [k for k, on in decode_bitfield(ENGINE_WARNING_BITS, warn).items() if on]
            if warn is not None
            else []
        )

    def _update_conditions(self, values: Mapping[str, Any]) -> None:
        c = self._state.conditions
        c.air_temp = det.as_float(values, ch.AIR_TEMP)
        track = det.as_float(values, ch.TRACK_TEMP)
        if track is None:
            track = det.as_float(values, ch.TRACK_TEMP_FALLBACK)
        c.track_temp = track
        c.air_density = det.as_float(values, ch.AIR_DENSITY)
        c.air_pressure = det.as_float(values, ch.AIR_PRESSURE)
        c.humidity = det.as_float(values, ch.REL_HUMIDITY)
        c.wind_vel = det.as_float(values, ch.WIND_VEL)
        c.wind_dir = det.as_float(values, ch.WIND_DIR)
        c.track_wetness = det.as_float(values, ch.TRACK_WETNESS)

        now = self._state.session_time
        if now - self._last_cond_sample < 1.0:
            return
        self._last_cond_sample = now
        self._cond_hist.append((now, c.air_temp, c.track_temp))
        while self._cond_hist and now - self._cond_hist[0][0] > self._trend_window_s:
            self._cond_hist.popleft()
        # Slopes are reported per minute -- the units a strategist thinks in.
        air = [(t, v) for t, v, _ in self._cond_hist if v is not None]
        trk = [(t, v) for t, _, v in self._cond_hist if v is not None]
        if len(air) >= 2:
            slope = _lstsq_slope([t for t, _ in air], [v for _, v in air])
            c.air_temp_trend = round(slope * 60.0, 5) if slope is not None else None
        if len(trk) >= 2:
            slope = _lstsq_slope([t for t, _ in trk], [v for _, v in trk])
            c.track_temp_trend = round(slope * 60.0, 5) if slope is not None else None

    # ---- standings + gaps -----------------------------------------------
    def _update_standings(self, values: Mapping[str, Any]) -> None:
        st = self._state.standings
        pcts = values.get(ch.CAR_IDX_LAP_DIST_PCT)
        if not isinstance(pcts, (list, tuple)):
            return

        def arr(name: str):
            v = values.get(name)
            return v if isinstance(v, (list, tuple)) else None

        laps = arr(ch.CAR_IDX_LAP) or arr(ch.CAR_IDX_LAP_COMPLETED)
        positions = arr(ch.CAR_IDX_POSITION)
        class_positions = arr(ch.CAR_IDX_CLASS_POSITION)
        last_times = arr(ch.CAR_IDX_LAST_LAP_TIME)
        best_times = arr(ch.CAR_IDX_BEST_LAP_TIME)
        pit = arr(ch.CAR_IDX_ON_PIT_ROAD)
        surface = arr(ch.CAR_IDX_TRACK_SURFACE)

        player_idx = det.as_int(values, ch.PLAYER_CAR_IDX)
        if player_idx is None:
            player_idx = st.player_idx
        st.player_idx = player_idx

        def at(seq, i, cast):
            if seq is None or i >= len(seq) or seq[i] is None:
                return None
            try:
                out = cast(seq[i])
            except (TypeError, ValueError):
                return None
            return out

        cars: list[CarState] = []
        for idx, raw_pct in enumerate(pcts):
            pct = None if raw_pct is None else float(raw_pct)
            if pct is None or pct < _INACTIVE_PCT:
                continue  # not in the world
            lap = at(laps, idx, int)
            last = at(last_times, idx, float)
            best = at(best_times, idx, float)
            surf = at(surface, idx, int)
            cars.append(
                CarState(
                    idx=idx,
                    is_player=(idx == player_idx),
                    position=at(positions, idx, int),
                    class_position=at(class_positions, idx, int),
                    lap=lap,
                    lap_dist_pct=pct,
                    laps_completed=(lap + pct) if lap is not None else pct,
                    last_lap_time=last if (last or 0) > 0 else None,
                    best_lap_time=best if (best or 0) > 0 else None,
                    on_pit_road=None if at(pit, idx, int) is None else bool(pit[idx]),
                    track_surface=TRK_LOC.get(surf) if surf is not None else None,
                )
            )

        basis, scale = self._gap_scale(cars)
        st.gap_basis = basis
        st.reference_lap_time = self._reference_lap_time(cars)

        # Running order: most race distance covered first.
        order = sorted(
            cars, key=lambda c: (c.laps_completed is None, -(c.laps_completed or 0.0))
        )
        player = next((c for c in order if c.is_player), None)
        if scale is not None:
            for i, car in enumerate(order):
                if car.laps_completed is None:
                    continue
                if i > 0 and order[i - 1].laps_completed is not None:
                    car.gap_ahead = round(
                        (order[i - 1].laps_completed - car.laps_completed) * scale, 3
                    )
                if i + 1 < len(order) and order[i + 1].laps_completed is not None:
                    car.gap_behind = round(
                        (car.laps_completed - order[i + 1].laps_completed) * scale, 3
                    )
                if player is not None and player.lap_dist_pct is not None:
                    delta = (car.lap_dist_pct or 0.0) - player.lap_dist_pct
                    # Wrap to the nearest half-lap: gap is a track-position gap.
                    if delta > 0.5:
                        delta -= 1.0
                    elif delta <= -0.5:
                        delta += 1.0
                    car.gap_to_player = round(delta * scale, 3)
        st.cars = order

    def _reference_lap_time(self, cars: list[CarState]) -> float | None:
        """A lap time we can defend: the player's, else the field's best."""
        p = self._state.player
        for candidate in (p.best_lap_time, self._player_best, p.last_lap_time):
            if candidate and candidate > 0:
                return candidate
        times = [c.best_lap_time for c in cars if c.best_lap_time]
        times += [c.last_lap_time for c in cars if c.last_lap_time]
        return min(times) if times else None

    def _gap_scale(self, cars: list[CarState]) -> tuple[str | None, float | None]:
        """Seconds per lap-fraction, plus the basis used to get there.

        Preference order is stability-first: a known lap time converts a
        lap-distance delta into a steady gap, whereas instantaneous speed makes
        gaps swing wildly through slow corners. If neither is available the gap
        is simply unknown -- we do not invent one.
        """
        ref = self._reference_lap_time(cars)
        if ref and ref > 0:
            return "lap_time_pct", ref
        length = self._state.session.lap_length_m
        speed = self._state.player.speed
        if length and speed and speed > 5.0:
            return "lap_length_speed", length / speed
        return None, None

    # ---- detectors -------------------------------------------------------
    def _run_detectors(self, frame: Frame, events: list[RaceEvent]) -> None:
        caps = self._state.capabilities
        window = self._window

        if caps.lockup:
            self._latching(
                "lockup", det.detect_lockup(window, self.config), events,
                EventType.LOCKUP, Severity.ADVISORY,
            )
        if caps.wheelspin:
            self._latching(
                "wheelspin", det.detect_wheelspin(window, self.config), events,
                EventType.WHEELSPIN, Severity.INFO,
            )
        if caps.offtrack:
            payload = det.detect_offtrack(window)
            if payload:
                events.append(
                    self._event(EventType.OFFTRACK, Severity.ADVISORY, payload)
                )
        if caps.incidents:
            payload = det.detect_incident(window)
            if payload:
                events.append(
                    self._event(EventType.INCIDENT, Severity.CRITICAL, payload)
                )
        if caps.pit:
            self._pit_transitions(events)
        if caps.standings:
            self._latching(
                "blue_flag",
                det.detect_blue_flag(self._state.standings, self.config),
                events,
                EventType.BLUE_FLAG,
                Severity.ADVISORY,
                cooldown=5.0,
            )

    def _latching(
        self,
        name: str,
        payload: dict | None,
        events: list[RaceEvent],
        event_type: EventType,
        severity: Severity,
        cooldown: float | None = None,
    ) -> None:
        """Fire once per episode: re-arm only after the condition clears."""
        if payload is None:
            self._latched.discard(name)
            return
        if name in self._latched:
            return
        gap = cooldown if cooldown is not None else self._event_cooldown_s
        now = self._state.session_time
        if now - self._last_fire.get(name, -1e9) < gap:
            return
        self._latched.add(name)
        self._last_fire[name] = now
        events.append(self._event(event_type, severity, payload))

    def _pit_transitions(self, events: list[RaceEvent]) -> None:
        transition = det.detect_pit_transition(self._window)
        if transition is None:
            return
        p = self._state.player
        if transition == "entry":
            self._lap_had_pit = True
            events.append(
                self._event(
                    EventType.PIT_ENTRY,
                    Severity.INFO,
                    {"lap": p.lap, "stint": p.stint, "fuel": self._state.fuel.level},
                )
            )
        else:
            self._lap_had_pit = True
            p.stint += 1
            p.laps_on_tyres = 0
            p.stint_start_lap = p.lap
            self._stint_temp_laps.clear()
            self._stint_press_laps.clear()
            # Trends describe the set that just came off; on new rubber they are
            # not merely stale, they are about a different tyre.
            self._state.tyres.stint_laps = 0
            self._state.tyres.temp_trend = {}
            self._state.tyres.pressure_trend = {}
            # A refuel re-arms the fuel warnings.
            self._fuel_critical_announced = False
            self._pit_window_announced = False
            self._fuel_at_lap_start = self._state.fuel.level
            events.append(
                self._event(
                    EventType.PIT_EXIT,
                    Severity.INFO,
                    {"lap": p.lap, "stint": p.stint, "fuel": self._state.fuel.level},
                )
            )
            events.append(
                self._event(
                    EventType.STINT_START,
                    Severity.INFO,
                    {"stint": p.stint, "lap": p.lap},
                )
            )

    # ---- lap boundary ----------------------------------------------------
    def _check_lap_boundary(self, frame: Frame, events: list[RaceEvent]) -> None:
        completion = det.detect_lap_completion(self._window)
        if completion is None:
            return
        lap = completion["lap"]
        lap_time = completion["lap_time"]
        p = self._state.player

        events.append(
            self._event(
                EventType.LAP_COMPLETED,
                Severity.INFO,
                {
                    "lap": lap,
                    "lap_time": lap_time,
                    "stint": p.stint,
                    "green": self._lap_all_green and not self._lap_had_pit,
                },
            )
        )

        if lap_time:
            if self._player_best is None or lap_time < self._player_best:
                self._player_best = lap_time
                p.best_lap_time = lap_time
                events.append(
                    self._event(
                        EventType.PERSONAL_BEST,
                        Severity.INFO,
                        {"lap": lap, "lap_time": lap_time},
                    )
                )
            if self._session_best is None or lap_time < self._session_best:
                self._session_best = lap_time
                events.append(
                    self._event(
                        EventType.SESSION_FASTEST_LAP,
                        Severity.INFO,
                        {"lap": lap, "lap_time": lap_time},
                    )
                )

        clean = self._lap_all_green and not self._lap_had_pit
        if clean:
            p.laps_on_tyres += 1
        self._roll_fuel(lap, clean, events)
        self._roll_tyres(lap, clean)

        # Re-arm the per-lap accumulators for the lap now starting.
        self._last_lap = completion["new_lap"]
        self._lap_had_pit = False
        self._lap_all_green = self._state.flags.phase is FlagPhase.GREEN
        self._tyre_temp_sum = {}
        self._tyre_press_sum = {}
        self._tyre_n = 0

    def _roll_fuel(self, lap: int, clean: bool, events: list[RaceEvent]) -> None:
        fuel = self._state.fuel
        if not self._state.capabilities.fuel:
            return
        level = fuel.level
        if level is not None and self._fuel_at_lap_start is not None and clean:
            used = self._fuel_at_lap_start - level
            if used > 0:
                self._fuel_used.append(used)
        if level is not None:
            self._fuel_at_lap_start = level

        samples = list(self._fuel_used)
        fuel.samples = len(samples)
        fuel.per_lap = round(_mean(samples), 4) if samples else None
        std = _stdev(samples)
        fuel.per_lap_std = round(std, 4) if std is not None else None
        self._recompute_fuel_plan(events)

    def _recompute_fuel_plan(self, events: list[RaceEvent]) -> None:
        fuel = self._state.fuel
        state = self._state
        per_lap = fuel.per_lap
        level = fuel.level
        if not per_lap or per_lap <= 0 or level is None:
            return

        fuel.laps_remaining = round(level / per_lap, 3)

        # Laps still to run: prefer the sim's own counter, else time / lap time.
        laps_to_finish: float | None = None
        if state.session.laps_remaining is not None:
            laps_to_finish = float(state.session.laps_remaining)
        elif state.session.time_remaining and state.player.last_lap_time:
            laps_to_finish = math.ceil(
                state.session.time_remaining / state.player.last_lap_time
            )
        fuel.laps_to_finish = laps_to_finish

        if laps_to_finish is not None:
            fuel.fuel_to_finish = round(laps_to_finish * per_lap, 3)
            fuel.margin_l = round(level - fuel.fuel_to_finish, 3)
            fuel.margin_laps = round(fuel.laps_remaining - laps_to_finish, 3)

        current_lap = state.player.lap
        if current_lap is not None:
            fuel.pit_window_latest_lap = current_lap + int(level / per_lap + _LAP_EPS)
            if fuel.capacity and laps_to_finish is not None:
                # Stopping earlier than this leaves more laps than a full tank
                # can cover, so it would force a second stop.
                shortfall = laps_to_finish - (fuel.capacity / per_lap)
                fuel.pit_window_earliest_lap = current_lap + max(
                    0, math.ceil(shortfall - _LAP_EPS)
                )
            else:
                fuel.pit_window_earliest_lap = None

        # "Open" means a stop is actually *needed* and may be taken now. Without
        # the margin test the window would also read open when the car is
        # already carrying enough fuel to see the flag -- true, but useless
        # advice, and it would re-announce after every stop.
        stop_required = fuel.margin_laps is not None and fuel.margin_laps < 0
        was_open = fuel.window_open
        fuel.window_open = bool(
            stop_required
            and fuel.pit_window_earliest_lap is not None
            and fuel.pit_window_latest_lap is not None
            and current_lap is not None
            and fuel.pit_window_earliest_lap <= current_lap <= fuel.pit_window_latest_lap
        )
        if fuel.window_open and not was_open and not self._pit_window_announced:
            self._pit_window_announced = True
            events.append(
                self._event(
                    EventType.PIT_WINDOW_OPEN,
                    Severity.ADVISORY,
                    {
                        "lap": current_lap,
                        "earliest_lap": fuel.pit_window_earliest_lap,
                        "latest_lap": fuel.pit_window_latest_lap,
                        "laps_remaining": fuel.laps_remaining,
                        "per_lap": per_lap,
                    },
                )
            )

        if (
            fuel.laps_remaining is not None
            and fuel.laps_remaining < self.config.fuel_critical_laps
            and not self._fuel_critical_announced
        ):
            self._fuel_critical_announced = True
            events.append(
                self._event(
                    EventType.FUEL_CRITICAL,
                    Severity.CRITICAL,
                    {
                        "lap": current_lap,
                        "level": level,
                        "laps_remaining": fuel.laps_remaining,
                    },
                )
            )

    def _roll_tyres(self, lap: int, clean: bool) -> None:
        t = self._state.tyres
        if not self._state.capabilities.tyres or self._tyre_n == 0:
            return
        n = self._tyre_n
        if self._tyre_temp_sum:
            self._stint_temp_laps.append(
                (lap, {c: v / n for c, v in self._tyre_temp_sum.items()})
            )
        if self._tyre_press_sum:
            self._stint_press_laps.append(
                (lap, {c: v / n for c, v in self._tyre_press_sum.items()})
            )
        t.stint_laps = len(self._stint_temp_laps) or len(self._stint_press_laps)
        t.temp_trend = _corner_trends(self._stint_temp_laps)
        t.pressure_trend = _corner_trends(self._stint_press_laps)

    # ---- helpers ---------------------------------------------------------
    def _event(
        self, event_type: EventType, severity: Severity, payload: dict
    ) -> RaceEvent:
        return RaceEvent(
            event_type=event_type,
            tick=self._state.tick,
            session_time=self._state.session_time,
            lap=self._state.player.lap,
            severity=severity,
            payload=payload,
        )


def _corner_trends(samples: list[tuple[int, dict[str, float]]]) -> dict[str, float]:
    """Least-squares slope per corner across the stint, in units per lap."""
    if len(samples) < 2:
        return {}
    laps = [float(lap) for lap, _ in samples]
    out: dict[str, float] = {}
    for corner in samples[-1][1]:
        ys = [vals[corner] for _, vals in samples if corner in vals]
        if len(ys) != len(laps):
            continue
        slope = _lstsq_slope(laps, ys)
        if slope is not None:
            out[corner] = round(slope, 4)
    return out


def _parse_track_length(raw: Any) -> float | None:
    """``"3.70 km"`` -> ``3700.0``. Returns None for anything unparseable."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    try:
        if text.endswith("km"):
            return float(text[:-2].strip()) * 1000.0
        if text.endswith("mi"):
            return float(text[:-2].strip()) * 1609.344
        if text.endswith("m"):
            return float(text[:-1].strip())
        return float(text) * 1000.0  # iRacing reports kilometres by default
    except ValueError:
        return None
