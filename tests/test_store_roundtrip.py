"""End-to-end offline pipeline: FrameBuffer -> Parquet writer -> DuckDB queries.

Exercises the whole persistence + query path without needing iRacing or an .ibt
file (pyarrow + duckdb only).
"""

from __future__ import annotations

import time

import pytest

from rtv.domain.models import Session, SessionKind
from rtv.ingest.frame import FrameBuffer
from rtv.ingest.normalize import LapTracker
from rtv.store.duck import Database
from rtv.store.repository import Repository
from rtv.store.writer import TelemetryWriter
from tests.helpers import generate_frames, synthetic_catalog

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")


@pytest.fixture()
def populated(tmp_path):
    catalog = synthetic_catalog()
    db = Database(tmp_path / "telemetry.duckdb")
    parquet_dir = tmp_path / "parquet"
    writer = TelemetryWriter(db, parquet_dir)
    repo = Repository(db, parquet_dir)

    session = Session(
        session_id="sess-1",
        kind=SessionKind.IBT,
        schema_hash=catalog.schema_hash,
        started_at=time.time(),
    )
    writer.begin_session(session, catalog)

    tracker = LapTracker("sess-1")
    buf = FrameBuffer(catalog)
    last_tick = 0
    for values, tick, t, lap in generate_frames(laps=3, ticks_per_lap=300):
        tracker.update(values, tick=tick, session_time=t)
        buf.append(values, tick=tick, session_time=t, lap=lap)
        last_tick = tick
    tracker.finalize(tick=last_tick, session_time=float(last_tick))
    writer.write_telemetry("sess-1", buf.to_arrow())
    writer.write_laps("sess-1", tracker.laps)
    writer.end_session("sess-1", sample_count=last_tick + 1, ended_at=time.time())
    yield repo
    db.close()


def test_session_and_catalog_persisted(populated):
    repo = populated
    sessions = repo.list_sessions()
    assert len(sessions) == 1
    cat = repo.get_catalog("sess-1")
    assert cat["count"] == len(synthetic_catalog())
    names = {v["name"] for v in cat["variables"]}
    assert "TyrePressure" in names and "CarIdxLapDistPct" in names


def test_laps_persisted(populated):
    laps = populated.get_laps("sess-1")
    assert [row["lap"] for row in laps] == [1, 2, 3]


def test_channels_downsampled(populated):
    res = populated.get_channels(
        "sess-1", ["Speed", "Throttle"], lap=2, max_points=100, mode="minmax"
    )
    assert res["downsample"]["points"] <= 100
    assert len(res["x_values"]) == res["downsample"]["points"]
    assert "y" in res["channels"]["Speed"]
    assert "y_min" in res["channels"]["Speed"]
    # x axis defaults to lap_dist_pct, which is monotonic 0..1 within a lap
    assert res["x_values"][0] < res["x_values"][-1]


def test_channels_raw_mode(populated):
    res = populated.get_channels(
        "sess-1", ["Speed"], lap=1, max_points=100_000, mode="raw", x="tick"
    )
    assert res["downsample"]["bucket"] == 1
    assert len(res["channels"]["Speed"]["y"]) == 300


def test_flattened_array_column_queryable(populated):
    res = populated.get_channels(
        "sess-1", ["TyrePressure_0"], lap=1, max_points=50
    )
    assert res["channels"]["TyrePressure_0"]["y"]


def test_unknown_channel_raises(populated):
    with pytest.raises(KeyError):
        populated.get_channels("sess-1", ["NotARealChannel"], lap=1)


def test_compare_aligns_on_lap_distance(populated):
    res = populated.compare("sess-1", "Speed", [1, 2], grid=200)
    assert res["grid"] == 200
    assert set(res["laps"].keys()) == {"1", "2"}
    assert len(res["x_values"]) == 200
    assert len(res["laps"]["1"]) == 200


def test_trackmap_polyline(populated):
    res = populated.get_trackmap("sess-1", color="Speed", max_points=500)
    assert res["count"] <= 500
    first = res["points"][0]
    assert "lat" in first and "lon" in first and "color" in first


def test_session_label_round_trips(tmp_path):
    catalog = synthetic_catalog()
    db = Database(tmp_path / "telemetry.duckdb")
    parquet_dir = tmp_path / "parquet"
    writer = TelemetryWriter(db, parquet_dir)
    repo = Repository(db, parquet_dir)
    session = Session(
        session_id="alien-1",
        kind=SessionKind.IBT,
        track_id="18",
        track_name="Spa",
        schema_hash=catalog.schema_hash,
        started_at=time.time(),
        label="Alien – Verstappen",
    )
    writer.begin_session(session, catalog)

    assert repo.get_session("alien-1")["label"] == "Alien – Verstappen"
    assert repo.list_sessions()[0]["label"] == "Alien – Verstappen"
    db.close()
