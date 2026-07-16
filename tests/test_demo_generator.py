"""The simulated demo session generates valid, coachable data through the real store."""

from __future__ import annotations

import pytest

from rtv.coaching.features import CoachingService
from rtv.demo.generator import DEMO_SESSION_ID, generate_demo_session
from rtv.demo.seed import seed_demo_session
from rtv.store.duck import Database
from rtv.store.repository import Repository
from rtv.store.writer import TelemetryWriter


def test_generate_demo_session_shape():
    demo = generate_demo_session()
    # Several laps with distinct, sane lap times.
    assert len(demo.laps) >= 6
    times = [lap.lap_time for lap in demo.laps]
    assert all(60 < t < 200 for t in times)
    assert len(set(times)) == len(times)  # every lap differs
    # The reference (lap 1) is the fastest — mistakes make the rest slower.
    assert times[0] == min(times)

    # The Arrow table carries the columns the queries + coaching need.
    cols = set(demo.table.column_names)
    for required in ("tick", "session_time", "lap", "Speed", "Throttle", "Brake",
                     "Gear", "LapDist", "LapDistPct", "Lat", "Lon"):
        assert required in cols, required
    assert demo.table.num_rows > 1000


@pytest.fixture()
def seeded(tmp_path):
    db = Database(tmp_path / "telemetry.duckdb")
    parquet = tmp_path / "parquet"
    repo = Repository(db, parquet)
    writer = TelemetryWriter(db, parquet)
    seed_demo_session(repo, writer, force=True)
    yield repo
    db.close()


def test_seed_is_idempotent(tmp_path):
    db = Database(tmp_path / "telemetry.duckdb")
    parquet = tmp_path / "parquet"
    repo = Repository(db, parquet)
    writer = TelemetryWriter(db, parquet)
    assert seed_demo_session(repo, writer) == DEMO_SESSION_ID  # first time creates
    assert seed_demo_session(repo, writer) is None             # second time skips
    db.close()


def test_seeded_endpoints_and_coaching(seeded):
    repo = seeded
    sessions = repo.list_sessions()
    assert any(s["session_id"] == DEMO_SESSION_ID for s in sessions)

    laps = repo.get_laps(DEMO_SESSION_ID)
    assert len(laps) >= 6
    lap_nums = [l["lap"] for l in laps]

    # Chart data comes back non-empty for a real lap.
    ch = repo.get_channels(DEMO_SESSION_ID, ["Speed", "Throttle", "Brake"],
                           lap=lap_nums[1], mode="raw", x="lap_dist_pct")
    speeds = [v for v in ch["channels"]["Speed"]["y"] if v is not None]
    assert speeds and max(speeds) > 30  # m/s

    # A GPS track map is available (Lat/Lon present).
    tm = repo.get_trackmap(DEMO_SESSION_ID, lap=lap_nums[0])
    assert tm["count"] > 50

    # The deterministic coach finds corners with distances and real losses.
    findings = CoachingService(repo).lap_findings(DEMO_SESSION_ID, lap_nums[4], lap_nums[0]).to_dict()
    assert findings["lap_length_m"] > 1000
    assert len(findings["corners"]) >= 8
    assert all("distance" in c and "label" in c for c in findings["corners"])
    # The slower main lap loses net time to the reference.
    assert findings["chief"]["net_lap"] > 0
