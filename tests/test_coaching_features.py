"""Deterministic coaching feature-extraction tests (offline, no iRacing / no LLM).

Builds two synthetic laps that differ on purpose — the 'main' lap is slower through
the single corner and brakes later — then asserts the ported coach.js heuristics
recover those facts. The synthetic catalog has no ``LatAccel``, so it also checks the
grip diagnostic degrades gracefully.
"""

from __future__ import annotations

import math
import time

import pytest

from rtv.domain.models import Session, SessionKind
from rtv.ingest.frame import FrameBuffer
from rtv.ingest.normalize import LapTracker
from rtv.store.duck import Database
from rtv.store.repository import Repository
from rtv.store.writer import TelemetryWriter
from tests.helpers import synthetic_catalog

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("numpy")

from rtv.coaching.features import CoachingService  # noqa: E402

LAP_LEN = 5000.0
TICKS = 300
DDIST = LAP_LEN / TICKS  # metres per tick

V_HI = 70.0  # straight-line speed (m/s)
APEX = 0.75  # corner apex as a fraction of the lap
WIDTH = 0.03  # corner sharpness (Gaussian sigma in lap-fraction)

# (corner_min_speed m/s, brake_onset_pct): lap 1 is the slower 'main' lap that
# also brakes later; laps 2 & 3 are the quicker reference.
LAP_SPECS = [(17.0, 0.72), (20.0, 0.70), (20.0, 0.70)]


def _values(lap: int, i: int, vmin: float, brake_onset: float) -> dict:
    pct = i / TICKS
    # high-speed lap with one sharp braking zone + slow corner at APEX
    speed = V_HI - (V_HI - vmin) * math.exp(-(((pct - APEX) / WIDTH) ** 2))
    brake = 1.0 if brake_onset <= pct < 0.76 else 0.0
    throttle = 1.0 if (pct < brake_onset or pct > 0.80) else 0.0
    return {
        "SessionTime": 0.0,  # overwritten by caller with the integrated time
        "SessionTick": 0,
        "Lap": lap,
        "LapDist": pct * LAP_LEN,
        "LapDistPct": pct,
        "Speed": speed,
        "RPM": 6000.0,
        "Throttle": throttle,
        "Brake": brake,
        "Gear": 3,
        "Lat": 52.0 + 0.01 * math.sin(pct * 2 * math.pi),
        "Lon": -1.0 + 0.01 * math.cos(pct * 2 * math.pi),
        "OnPitRoad": False,
        "LapLastLapTime": 0.0,
        "SessionState": 4,
        "SessionFlags": 0x4,
        "TyrePressure": [165.0, 165.5, 164.0, 164.5],
        "CarIdxLapDistPct": [pct] + [0.0] * 63,
    }


@pytest.fixture()
def service(tmp_path):
    catalog = synthetic_catalog()  # note: no LatAccel channel
    db = Database(tmp_path / "telemetry.duckdb")
    parquet_dir = tmp_path / "parquet"
    writer = TelemetryWriter(db, parquet_dir)
    repo = Repository(db, parquet_dir)

    writer.begin_session(
        Session(session_id="coach-1", kind=SessionKind.IBT, car_id="ir18",
                track_id="spa", track_name="Spa", schema_hash=catalog.schema_hash,
                started_at=time.time()),
        catalog,
    )
    tracker = LapTracker("coach-1")
    buf = FrameBuffer(catalog)
    tick = 0
    t = 0.0  # session time, advanced by Δdist/speed so a slower lap takes longer
    for lap, (vmin, onset) in enumerate(LAP_SPECS, start=1):
        for i in range(TICKS):
            v = _values(lap, i, vmin, onset)
            v["SessionTime"] = t
            v["SessionTick"] = tick
            tracker.update(v, tick=tick, session_time=t)
            buf.append(v, tick=tick, session_time=t, lap=lap)
            t += DDIST / max(v["Speed"], 0.1)
            tick += 1
    tracker.finalize(tick=tick - 1, session_time=t)
    writer.write_telemetry("coach-1", buf.to_arrow())
    writer.write_laps("coach-1", tracker.laps)
    writer.end_session("coach-1", sample_count=tick, ended_at=time.time())
    yield CoachingService(repo)
    db.close()


def test_detects_the_single_corner_near_apex(service):
    f = service.lap_findings("coach-1", main_lap=1, ref_lap=2)
    assert len(f.corners) >= 1
    # The sinusoid's only minimum is at pct 0.75 -> ~3750 m on a 5 km lap.
    apex = min(f.corners, key=lambda c: abs(c.distance - 3750.0))
    assert abs(apex.distance - 3750.0) < 250.0


def test_slower_main_loses_time(service):
    f = service.lap_findings("coach-1", main_lap=1, ref_lap=2)
    assert f.chief.net_lap > 0  # main is slower overall
    assert f.chief.lost > 0
    assert f.chief.losing_n >= 1
    assert f.main_time is not None and f.ref_time is not None
    assert f.main_time > f.ref_time


def test_speed_diagnostic_fires_and_is_bad(service):
    f = service.lap_findings("coach-1", main_lap=1, ref_lap=2)
    apex = min(f.corners, key=lambda c: abs(c.distance - 3750.0))
    speed = [d for d in apex.diags if d.agent == "speed"]
    assert speed, "expected a min-speed diagnostic"
    assert speed[0].good is False  # main is slower through the corner
    assert apex.min_main < apex.min_ref


def test_brake_point_diagnostic_detects_later_braking(service):
    f = service.lap_findings("coach-1", main_lap=1, ref_lap=2)
    apex = min(f.corners, key=lambda c: abs(c.distance - 3750.0))
    brake = [d for d in apex.diags if d.agent == "brake" and not d.lockup]
    assert brake, "expected a brake-point diagnostic"
    assert "late" in brake[0].text  # main brakes ~60-120 m later than reference


def test_grip_skipped_without_lataccel(service):
    f = service.lap_findings("coach-1", main_lap=1, ref_lap=2)
    agents = {d.agent for c in f.corners for d in c.diags}
    assert "grip" not in agents
    assert any("LatAccel" in n for n in f.notes)


def test_identical_laps_are_clean(service):
    f = service.lap_findings("coach-1", main_lap=2, ref_lap=3)
    assert f.chief.losing_n == 0
    assert abs(f.chief.net_lap) < 0.05
    # corner still detected, but it carries no loss diagnostics
    assert f.corners


def test_sectors_sum_to_net_lap(service):
    f = service.lap_findings("coach-1", main_lap=1, ref_lap=2)
    assert [s.name for s in f.sectors] == ["S1", "S2", "S3"]
    total = sum(s.delta for s in f.sectors)
    assert abs(total - f.chief.net_lap) < 0.05


def test_apportioned_time_loss_does_not_exceed_corner_loss(service):
    f = service.lap_findings("coach-1", main_lap=1, ref_lap=2)
    for c in f.corners:
        apportioned = sum(d.time_loss for d in c.diags)
        assert apportioned <= max(0.0, c.net_dt) + 1e-6


def test_unknown_session_and_lap_raise(service):
    with pytest.raises(KeyError):
        service.lap_findings("nope", 1, 2)
    with pytest.raises(KeyError):
        service.lap_findings("coach-1", main_lap=99, ref_lap=2)
