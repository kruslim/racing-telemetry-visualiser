"""A scripted synthetic race -- the deterministic ground truth for the engine.

This extends the demo-session seeding pattern used by ``tests/helpers.py`` and
``scripts/smoke_offline.py`` into a full multi-car race with events placed at
known laps, so tests can assert an exact event sequence rather than "something
fired". The script produces, in order:

======================  ==================================================
lap 3, 30%-80%          full-course yellow, then back to green
lap 4, ~40%             a front-axle lock-up under braking
lap 4 -> 5 boundary     the fuel pit window opens
lap 5, 85%              pit entry
lap 6, 15%              pit exit (refuel to a full tank) + new stint
======================  ==================================================

Fuel is scripted so the window arithmetic lands on lap 5 exactly: a 4.0 L tank
burning 0.5 L/lap covers 8 laps, the race is 12 laps, and the car starts on 3.0 L
-- so from lap 5 a full tank is both *sufficient* to reach the flag and *needed*
to avoid running dry. See ``docs/PITWALL.md`` for the derivation.

Everything here is a pure function of the spec: no clocks, no randomness, so two
runs produce byte-identical frames (which is what the determinism test relies on).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field

from rtv.catalog.builder import RawHeader, build_catalog
from rtv.catalog.models import Catalog
from rtv.catalog.types import IRType
from rtv.ingest.frame import Frame

#: Grid offsets in laps relative to the player (index 0), constant through the run.
DEFAULT_GRID = (0.0, 0.03, 0.06, -0.03, -0.06, 0.12, -0.12, 0.20)

GREEN = 0x00000004
YELLOW = 0x00000008 | 0x00004000  # yellow + caution, as iRacing sets under FCY


@dataclass(frozen=True)
class ScenarioSpec:
    """Every knob of the scripted race. Defaults are the ground-truth scenario."""

    hz: int = 60
    ticks_per_lap: int = 600  # -> a 10.0 s lap, keeping test runs quick
    laps: int = 6  # laps of telemetry actually generated
    end_pct: float = 0.60  # stop part-way through the final lap
    race_laps: int = 12  # scheduled race distance (drives laps-remaining)
    cars: int = 8
    grid: tuple[float, ...] = DEFAULT_GRID

    fuel_start: float = 3.0
    fuel_capacity: float = 4.0
    fuel_per_lap: float = 0.5

    yellow_lap: int = 3
    yellow_from_pct: float = 0.30
    yellow_to_pct: float = 0.80

    lockup_lap: int = 4
    #: Extra laps that lock up at the *same* corner, for the recurrence tests.
    #: Empty by default so the ground-truth scenario stays byte-identical.
    lockup_laps: tuple[int, ...] = ()
    lockup_from_pct: float = 0.40
    lockup_to_pct: float = 0.42
    lockup_wheel_ratio: float = 0.60  # front wheel speed as a fraction of car speed

    pit_entry_lap: int = 5
    pit_entry_pct: float = 0.85
    pit_exit_lap: int = 6
    pit_exit_pct: float = 0.15

    base_speed: float = 40.0  # m/s
    #: Lap fractions carrying a scripted corner (a speed dip deep enough for the
    #: Layer-1 corner detector to find). Empty by default: the ground-truth
    #: scenario is a smooth sinusoid and must stay byte-identical.
    corner_pcts: tuple[float, ...] = ()
    corner_depth: float = 22.0  # m/s scrubbed off at the apex
    corner_half_width_pct: float = 0.04
    lap_length_m: float = 4000.0
    player_idx: int = 0
    lap_time_spread: float = 0.05  # per-car last-lap-time increment

    #: Channels to omit from the catalog, for degradation tests.
    exclude: tuple[str, ...] = field(default=())

    @property
    def lap_time(self) -> float:
        return self.ticks_per_lap / self.hz


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------
def _headers() -> list[RawHeader]:
    f, d, i, b, bf = IRType.FLOAT, IRType.DOUBLE, IRType.INT, IRType.BOOL, IRType.BITFIELD
    heads = [
        RawHeader("SessionTime", d, unit="s"),
        RawHeader("SessionTick", i),
        RawHeader("SessionState", i),
        RawHeader("SessionTimeRemain", d, unit="s"),
        RawHeader("SessionLapsRemainEx", i),
        RawHeader("SessionLapsTotal", i),
        RawHeader("SessionFlags", bf),
        RawHeader("Lap", i),
        RawHeader("LapDist", f, unit="m"),
        RawHeader("LapDistPct", f, unit="%"),
        RawHeader("LapLastLapTime", f, unit="s"),
        RawHeader("Speed", f, unit="m/s"),
        RawHeader("Throttle", f, unit="%"),
        RawHeader("Brake", f, unit="%"),
        RawHeader("Gear", i),
        RawHeader("RPM", f, unit="rev/min"),
        RawHeader("OnPitRoad", b),
        RawHeader("PlayerCarIdx", i),
        RawHeader("PlayerCarPosition", i),
        RawHeader("PlayerCarClassPosition", i),
        RawHeader("PlayerTrackSurface", i),
        RawHeader("PlayerCarMyIncidentCount", i),
        RawHeader("FuelLevel", f, unit="l"),
        RawHeader("FuelLevelPct", f, unit="%"),
        RawHeader("OilTemp", f, unit="C"),
        RawHeader("WaterTemp", f, unit="C"),
        RawHeader("Voltage", f, unit="V"),
        RawHeader("EngineWarnings", bf),
        RawHeader("AirTemp", f, unit="C"),
        RawHeader("TrackTempCrew", f, unit="C"),
        RawHeader("Lat", d, unit="deg"),
        RawHeader("Lon", d, unit="deg"),
    ]
    for corner in ("LF", "RF", "LR", "RR"):
        heads.append(RawHeader(f"{corner}speed", f, unit="m/s"))
        heads.append(RawHeader(f"{corner}tempCM", f, unit="C"))
        heads.append(RawHeader(f"{corner}pressure", f, unit="kPa"))
    for name, typ in (
        ("CarIdxLapDistPct", f),
        ("CarIdxLap", i),
        ("CarIdxPosition", i),
        ("CarIdxLastLapTime", f),
        ("CarIdxOnPitRoad", b),
        ("CarIdxTrackSurface", i),
    ):
        heads.append(RawHeader(name, typ, count=64))
    return heads


def scenario_catalog(spec: ScenarioSpec | None = None) -> Catalog:
    """Build the scenario's runtime catalog, minus any ``spec.exclude`` channels."""
    spec = spec or ScenarioSpec()
    excluded = set(spec.exclude)
    heads = [h for h in _headers() if h.name not in excluded]
    cat = build_catalog(heads, source="ibt", flatten_max=6)
    cat.car_id = "synthetic-gt3"
    cat.track_id = "synthetic-circuit"
    return cat


def scenario_session_info(spec: ScenarioSpec | None = None) -> dict:
    """The session-info document the replay driver feeds to the engine.

    Shaped like a real iRacing document: ``WeekendInfo`` for the track,
    ``DriverInfo`` for the car scalars and ``CarSetup`` for the setup sheet the
    vehicle engineer's ``get_setup_snapshot`` reads.
    """
    spec = spec or ScenarioSpec()
    return {
        "WeekendInfo": {
            "TrackName": "synthetic-circuit",
            "TrackDisplayName": "Synthetic Circuit",
            "TrackLength": f"{spec.lap_length_m / 1000.0:.2f} km",
        },
        "DriverInfo": {
            "DriverCarIdx": spec.player_idx,
            "DriverCarFuelMaxLtr": spec.fuel_capacity,
            "DriverCarMaxFuelPct": 1.0,
            "DriverCarRedLine": 7500.0,
        },
        "CarSetupUpdateCount": 1,
        "CarSetup": {
            "Chassis": {
                "Front": {"BrakePressureBias": "54.0%", "ArbSize": "Medium"},
                "LeftFront": {"ColdPressure": "165 kPa", "Camber": "-3.4 deg"},
                "RightFront": {"ColdPressure": "165 kPa", "Camber": "-3.2 deg"},
                "LeftRear": {"ColdPressure": "160 kPa", "Camber": "-2.1 deg"},
                "RightRear": {"ColdPressure": "160 kPa", "Camber": "-2.0 deg"},
            },
            "TiresAero": {"AeroSettings": {"RearWingSetting": "7"}},
        },
    }


# --------------------------------------------------------------------------
# the scripted race
# --------------------------------------------------------------------------
def _in_span(lap: int, pct: float, at_lap: int, lo: float, hi: float) -> bool:
    return lap == at_lap and lo <= pct < hi


def _after(lap: int, pct: float, at_lap: int, at_pct: float) -> bool:
    return (lap, pct) >= (at_lap, at_pct)


def _corner_dip(pct: float, spec: ScenarioSpec) -> float:
    """Speed scrubbed off by the nearest scripted corner, as a raised cosine.

    A smooth sinusoidal lap has no local minimum prominent enough for the Layer-1
    corner detector, so a scenario that needs a *corner* has to script one. Opt-in
    via ``ScenarioSpec.corner_pcts``; wrapped at the start/finish line so a corner
    can sit anywhere on the lap.
    """
    if not spec.corner_pcts:
        return 0.0
    half = spec.corner_half_width_pct
    worst = 0.0
    for apex in spec.corner_pcts:
        delta = abs(pct - apex)
        delta = min(delta, 1.0 - delta)  # the lap is a loop
        if delta >= half:
            continue
        worst = max(worst, spec.corner_depth * 0.5 * (1.0 + math.cos(math.pi * delta / half)))
    return worst


def scenario_frames(spec: ScenarioSpec | None = None) -> Iterator[Frame]:
    """Yield the scripted race as :class:`Frame`s, exactly as a poller would."""
    spec = spec or ScenarioSpec()
    excluded = set(spec.exclude)
    n_cars = min(spec.cars, len(spec.grid))
    dt = 1.0 / spec.hz
    tick = 0
    session_time = 0.0

    # Positions are fixed by the constant grid offsets: furthest along leads.
    order = sorted(range(n_cars), key=lambda c: -spec.grid[c])
    position = {car: i + 1 for i, car in enumerate(order)}
    player_position = position[spec.player_idx]

    for lap in range(1, spec.laps + 1):
        last_i = (
            int(spec.end_pct * spec.ticks_per_lap)
            if lap == spec.laps
            else spec.ticks_per_lap
        )
        for i in range(last_i):
            pct = i / spec.ticks_per_lap
            progress = (lap - 1) + pct

            # --- scripted phases ---------------------------------------
            yellow = _in_span(lap, pct, spec.yellow_lap, spec.yellow_from_pct,
                              spec.yellow_to_pct)
            locking = any(
                _in_span(lap, pct, at, spec.lockup_from_pct, spec.lockup_to_pct)
                for at in (spec.lockup_lap, *spec.lockup_laps)
            )
            in_pits = _after(lap, pct, spec.pit_entry_lap, spec.pit_entry_pct) and not (
                _after(lap, pct, spec.pit_exit_lap, spec.pit_exit_pct)
            )
            refuelled = _after(lap, pct, spec.pit_exit_lap, spec.pit_exit_pct)

            # --- fuel ---------------------------------------------------
            if refuelled:
                exit_progress = (spec.pit_exit_lap - 1) + spec.pit_exit_pct
                level = spec.fuel_capacity - spec.fuel_per_lap * (
                    progress - exit_progress
                )
            else:
                level = spec.fuel_start - spec.fuel_per_lap * progress
            level = max(0.0, level)

            # --- driving ------------------------------------------------
            angle = pct * 2 * math.pi
            if in_pits:
                speed = 22.0
                throttle, brake = 0.2, 0.0
            elif yellow:
                speed = 25.0
                throttle, brake = 0.3, 0.0
            elif locking:
                speed = spec.base_speed
                throttle, brake = 0.0, 0.9
            else:
                dip = _corner_dip(pct, spec)
                speed = max(8.0, spec.base_speed + 8.0 * math.sin(angle) - dip)
                throttle = max(0.0, math.sin(angle))
                brake = max(0.0, -math.sin(angle)) * 0.5

            front_wheel = speed * (spec.lockup_wheel_ratio if locking else 1.0)

            values: dict = {
                "SessionTime": session_time,
                "SessionTick": tick,
                "SessionState": 4,  # racing
                "SessionTimeRemain": max(
                    0.0, (spec.race_laps - progress) * spec.lap_time
                ),
                "SessionLapsRemainEx": max(0, spec.race_laps - lap + 1),
                "SessionLapsTotal": spec.race_laps,
                "SessionFlags": YELLOW if yellow else GREEN,
                "Lap": lap,
                "LapDist": pct * spec.lap_length_m,
                "LapDistPct": pct,
                "LapLastLapTime": spec.lap_time if lap > 1 else 0.0,
                "Speed": speed,
                "Throttle": throttle,
                "Brake": brake,
                "Gear": 2 if in_pits else 4,
                "RPM": 3000.0 + 400.0 * speed,
                "OnPitRoad": in_pits,
                "PlayerCarIdx": spec.player_idx,
                "PlayerCarPosition": player_position,
                "PlayerCarClassPosition": player_position,
                "PlayerTrackSurface": 1 if in_pits else 3,  # in_pit_stall / on_track
                "PlayerCarMyIncidentCount": 0,
                "FuelLevel": level,
                "FuelLevelPct": level / spec.fuel_capacity,
                "OilTemp": 95.0 + 0.2 * progress,
                "WaterTemp": 88.0 + 0.15 * progress,
                "Voltage": 12.6,
                "EngineWarnings": 0,
                "AirTemp": 22.0,
                "TrackTempCrew": 31.0 + 0.1 * progress,
                "Lat": 52.0 + 0.01 * math.sin(angle),
                "Lon": -1.0 + 0.01 * math.cos(angle),
            }

            # Tyres: temperature climbs through a stint and resets on new rubber.
            stint_laps = progress - (
                ((spec.pit_exit_lap - 1) + spec.pit_exit_pct) if refuelled else 0.0
            )
            for c_i, corner in enumerate(("LF", "RF", "LR", "RR")):
                values[f"{corner}speed"] = (
                    front_wheel if corner in ("LF", "RF") else speed
                )
                values[f"{corner}tempCM"] = 80.0 + 2.0 * stint_laps + c_i
                values[f"{corner}pressure"] = 165.0 + 0.5 * stint_laps + c_i * 0.5

            # Per-car-index arrays (64 wide, -1 for cars not in the world).
            pcts = [-1.0] * 64
            laps_arr = [0] * 64
            pos_arr = [0] * 64
            last_arr = [0.0] * 64
            pit_arr = [False] * 64
            surf_arr = [-1] * 64
            for car in range(n_cars):
                car_progress = max(0.0, progress + spec.grid[car])
                car_lap = int(car_progress) + 1
                pcts[car] = car_progress - int(car_progress)
                laps_arr[car] = car_lap
                pos_arr[car] = position[car]
                last_arr[car] = (
                    spec.lap_time + car * spec.lap_time_spread if car_lap > 1 else 0.0
                )
                pit_arr[car] = in_pits if car == spec.player_idx else False
                surf_arr[car] = 3
            values["CarIdxLapDistPct"] = pcts
            values["CarIdxLap"] = laps_arr
            values["CarIdxPosition"] = pos_arr
            values["CarIdxLastLapTime"] = last_arr
            values["CarIdxOnPitRoad"] = pit_arr
            values["CarIdxTrackSurface"] = surf_arr

            for name in excluded:
                values.pop(name, None)

            yield Frame(
                tick=tick,
                session_time=session_time,
                lap=lap,
                values=values,
                wall_time=None,  # replay must stay clock-free to be deterministic
            )
            tick += 1
            session_time += dt


# --------------------------------------------------------------------------
# store seeding (so the scenario can also be replayed from DuckDB/Parquet)
# --------------------------------------------------------------------------
def seed_scenario_session(
    writer, session_id: str, spec: ScenarioSpec | None = None
) -> str:
    """Write the scripted race into the real store, exactly as an import would."""
    from rtv.domain.models import Session, SessionInfoSnapshot, SessionKind
    from rtv.ingest.frame import FrameBuffer
    from rtv.ingest.normalize import LapTracker

    spec = spec or ScenarioSpec()
    catalog = scenario_catalog(spec)
    writer.begin_session(
        Session(
            session_id=session_id,
            kind=SessionKind.IBT,
            car_id=catalog.car_id,
            track_id=catalog.track_id,
            track_name="Synthetic Circuit",
            schema_hash=catalog.schema_hash,
            started_at=0.0,
            label="Pitwall scenario",
        ),
        catalog,
    )
    writer.write_session_info(
        SessionInfoSnapshot(
            session_id=session_id,
            update_seq=1,
            captured_tick=0,
            captured_at=0.0,
            info=scenario_session_info(spec),
        )
    )

    tracker = LapTracker(session_id)
    buf = FrameBuffer(catalog)
    n = 0
    last_tick = 0
    for frame in scenario_frames(spec):
        tracker.update(frame.values, tick=frame.tick, session_time=frame.session_time)
        buf.append(
            frame.values,
            tick=frame.tick,
            session_time=frame.session_time,
            lap=frame.lap,
        )
        n += 1
        last_tick = frame.tick
    tracker.finalize(tick=last_tick, session_time=last_tick / spec.hz)
    writer.write_telemetry(session_id, buf.to_arrow())
    writer.write_laps(session_id, tracker.laps)
    writer.end_session(session_id, sample_count=n, ended_at=0.0)
    return session_id
