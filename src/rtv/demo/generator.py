"""Generate a plausible sim-racing session as PyArrow tables + metadata.

The model is deliberately simple but physically grounded, so the *shape* of the
data matches a real ``.ibt`` import (iRacing channel names, 60 Hz time sampling,
monotonic ``LapDist``/``LapDistPct``, a GPS track from ``Lat``/``Lon``):

1. a closed circuit is described by a handful of signed-curvature corners;
2. a grip-limited speed profile is solved with backward (braking) and forward
   (traction) passes over distance;
3. each lap is re-simulated at 60 Hz with a per-lap skill factor and a couple of
   scripted mistakes (an early lift, a brake lock-up), so lap-vs-lap deltas are
   real and the coach has something true to find.

Everything is seeded, so re-generating is byte-stable.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from rtv.catalog.builder import RawHeader, build_catalog
from rtv.catalog.models import Catalog
from rtv.catalog.types import IRType
from rtv.domain.models import Lap, Session, SessionKind

DEMO_SESSION_ID = "demo-sunset-ridge"
_TRACK_NAME = "Sunset Ridge Circuit"
_CAR_ID = "demo-gt3"
_TRACK_ID = "demo-sunset-ridge"
# A fixed base epoch keeps re-seeds deterministic (no wall-clock in the data).
_BASE_EPOCH = 1_700_000_000.0

# --- vehicle envelope (a grippy GT car) ------------------------------------
V_MAX = 79.0  # m/s  (~284 km/h)
A_LAT = 26.0  # m/s^2 peak lateral (~2.65 g)
A_BRAKE = 34.0  # m/s^2 peak braking (~3.5 g with aero)
A_ACCEL = 12.5  # m/s^2 peak traction
DT = 1.0 / 60.0  # 60 Hz sampling, like the live SDK
_DS = 1.0  # metres between distance-grid nodes

# --- output channels: (name, ir_type, unit, description) -------------------
# All scalar (count == 1) so each maps to one column named exactly like this.
_CHANNEL_SPEC: tuple[tuple[str, IRType, str, str], ...] = (
    ("Speed", IRType.FLOAT, "m/s", "GPS vehicle speed"),
    ("Throttle", IRType.FLOAT, "%", "0=off 1=full throttle"),
    ("Brake", IRType.FLOAT, "%", "0=off 1=full brake"),
    ("Gear", IRType.INT, "", "-1=R 0=N 1..n"),
    ("RPM", IRType.FLOAT, "revs/min", "Engine speed"),
    ("SteeringWheelAngle", IRType.FLOAT, "rad", "Steering wheel angle"),
    ("LatAccel", IRType.FLOAT, "m/s^2", "Lateral acceleration"),
    ("LongAccel", IRType.FLOAT, "m/s^2", "Longitudinal acceleration"),
    ("LapDist", IRType.FLOAT, "m", "Distance travelled this lap"),
    ("LapDistPct", IRType.FLOAT, "%", "Fraction of lap completed (0..1)"),
    ("Lat", IRType.DOUBLE, "deg", "Latitude"),
    ("Lon", IRType.DOUBLE, "deg", "Longitude"),
)
_ARROW_TYPE = {
    IRType.FLOAT: pa.float32(),
    IRType.INT: pa.int32(),
    IRType.DOUBLE: pa.float64(),
}


@dataclass(frozen=True)
class _Corner:
    frac: float  # position as a fraction of the lap (0..1)
    v_apex: float  # target apex speed, km/h
    sign: float  # +1 left, -1 right
    width: float  # metres (Gaussian sigma of the curvature bump)


# A 14-corner lap: a mix of hairpins, medium corners and a couple of fast sweepers.
_CORNERS: tuple[_Corner, ...] = (
    _Corner(0.06, 105, -1, 55),  # T1  medium right
    _Corner(0.12, 78, +1, 45),   # T2  slow left
    _Corner(0.19, 62, -1, 42),   # T3  hairpin right
    _Corner(0.27, 175, +1, 70),  # T4  fast left sweeper
    _Corner(0.34, 120, -1, 52),  # T5  medium right
    _Corner(0.41, 95, +1, 48),   # T6  slow-medium left
    _Corner(0.48, 150, -1, 60),  # T7  fast right
    _Corner(0.55, 70, +1, 44),   # T8  hairpin left
    _Corner(0.62, 200, -1, 75),  # T9  fast right kink
    _Corner(0.70, 110, +1, 50),  # T10 medium left
    _Corner(0.77, 85, -1, 46),   # T11 slow right
    _Corner(0.84, 165, +1, 66),  # T12 fast left
    _Corner(0.90, 100, -1, 50),  # T13 medium right
    _Corner(0.96, 68, +1, 44),   # T14 final hairpin left
)

_LAP_LENGTH = 4000.0  # metres (nominal; the geometry is scaled to this)


def _kmh(v: float) -> float:
    return v / 3.6


@dataclass
class _Track:
    length: float
    ds: float
    dist: np.ndarray  # (N,) distance grid
    kappa: np.ndarray  # (N,) signed curvature (1/m)
    v_base: np.ndarray  # (N,) feasible speed profile (m/s)
    lat: np.ndarray  # (N,) degrees
    lon: np.ndarray  # (N,) degrees


def _build_track() -> _Track:
    length = _LAP_LENGTH
    n = int(round(length / _DS))
    dist = np.linspace(0.0, length, n, endpoint=False)

    # Signed curvature = sum of Gaussian bumps, one per corner. Each bump's
    # amplitude is chosen so the apex speed sqrt(A_LAT/|kappa|) hits the target.
    kappa = np.zeros(n)
    for c in _CORNERS:
        centre = c.frac * length
        v = _kmh(c.v_apex)
        amp = c.sign * (A_LAT / (v * v))
        # wrap-aware distance to the corner centre
        dd = dist - centre
        dd = (dd + length / 2) % length - length / 2
        kappa += amp * np.exp(-(dd * dd) / (2.0 * c.width * c.width))

    # Grip-limited cornering speed, then enforce braking/traction along distance.
    v_curve = np.sqrt(A_LAT / np.maximum(np.abs(kappa), 1e-6))
    v = np.minimum(v_curve, V_MAX)
    # Two wrap-around passes settle the closed loop.
    for _ in range(2):
        for i in range(n):  # backward (braking) pass, walking against travel
            j = (n - 1 - i)
            nxt = (j + 1) % n
            v[j] = min(v[j], np.sqrt(v[nxt] ** 2 + 2 * A_BRAKE * _DS))
        for i in range(n):  # forward (traction) pass
            prv = (i - 1) % n
            v[i] = min(v[i], np.sqrt(v[prv] ** 2 + 2 * A_ACCEL * _DS))
    v_base = np.clip(v, 12.0, V_MAX)

    lat, lon = _geometry(dist, kappa, length)
    return _Track(length=length, ds=_DS, dist=dist, kappa=kappa, v_base=v_base, lat=lat, lon=lon)


def _geometry(dist: np.ndarray, kappa: np.ndarray, length: float) -> tuple[np.ndarray, np.ndarray]:
    """Integrate curvature into a heading, then into an (x, y) loop, then to GPS."""
    n = dist.size
    # Force the heading to close (net turn = -2*pi = one clockwise loop) by adding
    # a tiny uniform curvature — negligible next to the corners.
    theta = np.cumsum(kappa) * _DS
    theta -= np.linspace(0.0, theta[-1] + 2 * np.pi, n)  # drift correction to -2pi
    x = np.cumsum(np.cos(theta)) * _DS
    y = np.cumsum(np.sin(theta)) * _DS
    x -= x.mean()
    y -= y.mean()

    # Map local metres to GPS around a plausible anchor.
    lat0, lon0 = 36.7783, -119.4179  # somewhere sunny
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * np.cos(np.radians(lat0))
    lat = lat0 + y / m_per_deg_lat
    lon = lon0 + x / m_per_deg_lon
    return lat, lon


@dataclass(frozen=True)
class _Mistake:
    """A localised driver error injected into one lap."""

    frac: float  # where on the lap
    kind: str  # "lift" | "lockup"
    width: float  # metres
    severity: float  # 0..1


@dataclass(frozen=True)
class _LapDef:
    label: str
    skill: float  # global speed scale vs the ideal profile (<= 1.0)
    mistakes: tuple[_Mistake, ...] = ()
    valid: bool = True


# Lap 1 is the reference (cleanest). The rest carry small, specific errors so the
# coach finds genuine, distinct losses corner-to-corner.
_LAP_DEFS: tuple[_LapDef, ...] = (
    _LapDef("Reference", 0.995),
    _LapDef("Lap 2", 0.983, (_Mistake(0.19, "lockup", 45, 0.8),)),        # locks into T3
    _LapDef("Lap 3", 0.980, (_Mistake(0.55, "lift", 60, 0.6),)),         # early lift into T8
    _LapDef("Lap 4", 0.986, (_Mistake(0.90, "lockup", 40, 0.5),)),       # small lock T13
    _LapDef("Lap 5", 0.978, (_Mistake(0.34, "lift", 55, 0.7),
                              _Mistake(0.77, "lockup", 40, 0.6))),
    _LapDef("Lap 6", 0.989),
    _LapDef("Lap 7", 0.982, (_Mistake(0.62, "lift", 70, 0.5),)),         # lifts in the fast T9
    _LapDef("Lap 8", 0.991, (_Mistake(0.96, "lockup", 35, 0.4),)),
)


def _lap_speed_profile(track: _Track, ld: _LapDef) -> np.ndarray:
    """The target speed a lap aims for: the ideal, scaled by skill, dented by mistakes."""
    v = track.v_base * ld.skill
    for m in ld.mistakes:
        centre = m.frac * track.length
        dd = track.dist - centre
        dd = (dd + track.length / 2) % track.length - track.length / 2
        well = np.exp(-(dd * dd) / (2.0 * m.width * m.width))
        if m.kind == "lift":
            v = v * (1.0 - 0.14 * m.severity * well)  # backs out of the throttle
        else:  # lockup: overslows just past the brake point, then recovers
            v = v * (1.0 - 0.18 * m.severity * well)
    return np.clip(v, 10.0, V_MAX)


def _gear_for(speed: float) -> int:
    for g, lo in enumerate([0, 16, 27, 40, 54, 68], start=1):
        if speed < lo:
            return max(1, g - 1)
    return 6


_GEAR_BANDS = [0.0, 16.0, 27.0, 40.0, 54.0, 68.0, V_MAX + 1]


def _rpm_for(speed: float, gear: int) -> float:
    lo = _GEAR_BANDS[gear - 1]
    hi = _GEAR_BANDS[gear]
    frac = 0.0 if hi <= lo else (speed - lo) / (hi - lo)
    return float(4800.0 + np.clip(frac, 0.0, 1.0) * 3400.0)


@dataclass
class _SimLap:
    columns: dict[str, np.ndarray]
    lap_time: float
    n: int


def _simulate_lap(track: _Track, ld: _LapDef, rng: np.random.Generator) -> _SimLap:
    target = _lap_speed_profile(track, ld)
    length = track.length
    n = track.dist.size
    step = length / n  # the grid is uniform, so interpolation is O(1) index math

    def _interp(arr: np.ndarray, d: float) -> float:
        pos = (d % length) / step
        i0 = int(pos)
        frac = pos - i0
        i1 = i0 + 1 if i0 + 1 < n else 0
        return float(arr[i0] * (1.0 - frac) + arr[i1] * frac)

    def v_at(d: float) -> float:
        return _interp(target, d)

    def kappa_at(d: float) -> float:
        return _interp(track.kappa, d)

    def latlon_at(d: float) -> tuple[float, float]:
        return _interp(track.lat, d), _interp(track.lon, d)

    speed_l, thr_l, brk_l, gear_l = [], [], [], []
    rpm_l, steer_l, latg_l, long_l = [], [], [], []
    dist_l, pct_l, lat_l, lon_l, st_l = [], [], [], [], []

    d = 0.0
    t = 0.0
    v = v_at(0.0)
    max_steps = int((length / 10.0) * 60 * 3) + 5000  # generous guard
    steps = 0
    while d < length and steps < max_steps:
        v_next = v_at(d + max(v, 1.0) * DT)
        a = (v_next - v) / DT
        k = kappa_at(d)
        # tiny sensor-like noise so traces aren't glassy
        jitter = rng.normal(0.0, 0.15)

        if a < -0.6:
            brake = float(np.clip(-a / A_BRAKE, 0.0, 1.0))
            throttle = 0.0
        elif a > 0.4:
            throttle = float(np.clip(0.35 + a / A_ACCEL, 0.0, 1.0))
            brake = 0.0
        else:  # maintenance throttle
            throttle = float(np.clip(0.25 + 0.75 * (v / V_MAX), 0.0, 1.0))
            brake = 0.0

        # Scripted lock-up: a spike of brake right at the mistake, over-slowing.
        for m in ld.mistakes:
            if m.kind == "lockup":
                dd = ((d - m.frac * length) + length / 2) % length - length / 2
                if abs(dd) < m.width * 0.6:
                    brake = max(brake, 0.9 * m.severity + 0.1)
                    throttle = 0.0

        gear = _gear_for(v)
        speed_l.append(v + jitter)
        thr_l.append(throttle)
        brk_l.append(brake)
        gear_l.append(gear)
        rpm_l.append(_rpm_for(v, gear) + jitter * 10)
        steer_l.append(float(np.clip(k * 130.0, -3.2, 3.2)))
        latg_l.append(v * v * k)
        long_l.append(a)
        dist_l.append(d)
        pct_l.append(d / length)
        la, lo = latlon_at(d)
        lat_l.append(la)
        lon_l.append(lo)
        st_l.append(t)

        d += v * DT
        v = v_at(d)
        t += DT
        steps += 1

    cols = {
        "Speed": np.asarray(speed_l, dtype=np.float32),
        "Throttle": np.asarray(thr_l, dtype=np.float32),
        "Brake": np.asarray(brk_l, dtype=np.float32),
        "Gear": np.asarray(gear_l, dtype=np.int32),
        "RPM": np.asarray(rpm_l, dtype=np.float32),
        "SteeringWheelAngle": np.asarray(steer_l, dtype=np.float32),
        "LatAccel": np.asarray(latg_l, dtype=np.float32),
        "LongAccel": np.asarray(long_l, dtype=np.float32),
        "LapDist": np.asarray(dist_l, dtype=np.float32),
        "LapDistPct": np.asarray(pct_l, dtype=np.float32),
        "Lat": np.asarray(lat_l, dtype=np.float64),
        "Lon": np.asarray(lon_l, dtype=np.float64),
        "_session_time": np.asarray(st_l, dtype=np.float64),
    }
    return _SimLap(columns=cols, lap_time=t, n=len(speed_l))


def build_catalog_for_demo() -> Catalog:
    headers = [
        RawHeader(name=name, type=int(ir), count=1, unit=unit, desc=desc)
        for (name, ir, unit, desc) in _CHANNEL_SPEC
    ]
    return build_catalog(
        headers, source="ibt", flatten_max=6, car_id=_CAR_ID, track_id=_TRACK_ID
    )


@dataclass
class DemoSession:
    session: Session
    catalog: Catalog
    table: pa.Table
    laps: list[Lap]
    sample_count: int


def generate_demo_session(
    *, session_id: str = DEMO_SESSION_ID, seed: int = 20240517
) -> DemoSession:
    """Build the full demo session in memory (metadata + one combined Arrow table)."""
    rng = np.random.default_rng(seed)
    track = _build_track()
    catalog = build_catalog_for_demo()

    per_lap: list[tuple[int, _SimLap]] = []
    for idx, ld in enumerate(_LAP_DEFS, start=1):
        per_lap.append((idx, _simulate_lap(track, ld, rng)))

    # Assemble one Arrow table with a global tick + session_time and a lap column;
    # TelemetryWriter partitions it into per-lap Parquet just like a real import.
    channel_names = [name for (name, *_rest) in _CHANNEL_SPEC]
    accum: dict[str, list[np.ndarray]] = {n: [] for n in channel_names}
    tick_chunks: list[np.ndarray] = []
    stime_chunks: list[np.ndarray] = []
    lap_chunks: list[np.ndarray] = []
    laps: list[Lap] = []

    tick = 0
    session_time = 0.0
    for lap_no, sim in per_lap:
        n = sim.n
        start_tick = tick
        ticks = np.arange(tick, tick + n, dtype=np.int64)
        stimes = session_time + sim.columns["_session_time"]
        for name in channel_names:
            accum[name].append(sim.columns[name])
        tick_chunks.append(ticks)
        stime_chunks.append(stimes.astype(np.float64))
        lap_chunks.append(np.full(n, lap_no, dtype=np.int32))
        laps.append(
            Lap(
                session_id=session_id,
                lap=lap_no,
                start_tick=start_tick,
                end_tick=tick + n - 1,
                start_session_time=float(session_time),
                lap_time=round(sim.lap_time, 3),
                is_valid=_LAP_DEFS[lap_no - 1].valid,
                is_out_lap=(lap_no == 1),
                is_in_lap=(lap_no == len(per_lap)),
            )
        )
        tick += n
        session_time = float(stimes[-1] + DT)

    arrays: list[pa.Array] = [
        pa.array(np.concatenate(tick_chunks)),
        pa.array(np.concatenate(stime_chunks)),
        pa.array(np.concatenate(lap_chunks)),
    ]
    names: list[str] = ["tick", "session_time", "lap"]
    for name, ir, *_rest in _CHANNEL_SPEC:
        arrays.append(pa.array(np.concatenate(accum[name]), type=_ARROW_TYPE[ir]))
        names.append(name)
    table = pa.Table.from_arrays(arrays, names=names)

    session = Session(
        session_id=session_id,
        kind=SessionKind.IBT,
        source_path="<generated: rtv.demo>",
        car_id=_CAR_ID,
        track_id=_TRACK_ID,
        track_name=_TRACK_NAME,
        schema_hash=catalog.schema_hash,
        started_at=_BASE_EPOCH,
        ended_at=_BASE_EPOCH + session_time,
        sample_count=table.num_rows,
        poll_hz=60,
        status="open",
        label="Demo — Simulated Lap (Sunset Ridge)",
    )
    return DemoSession(
        session=session,
        catalog=catalog,
        table=table,
        laps=laps,
        sample_count=table.num_rows,
    )
