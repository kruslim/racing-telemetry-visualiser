"""``scripts/trim_fixture.py`` / ``rtv.store.fixture``: a few laps of a session as a fixture.

The source is a live recording made by the real poller (``tests.helpers``), with a
race journal written by the real :class:`SessionRecorder` while the engine replays
it. The fixture must keep every ``CarIdx*`` entry, the journal records from the
same stretch of session time, and seed back into a store that replays the field.
Offline; the frames are the test-only scripted race from ``evals/``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from rtv.racestate import RaceStateEngine  # noqa: E402
from rtv.racestate.models import EventType  # noqa: E402
from rtv.racestate.replay import ReplayDriver, store_source  # noqa: E402
from rtv.store import journal  # noqa: E402
from rtv.store.duck import Database  # noqa: E402
from rtv.store.fixture import (  # noqa: E402
    FIXTURE_FILE,
    FRAMES_FILE,
    parse_laps,
    read_fixture,
    seed_fixture,
    trim_session,
)
from rtv.store.repository import Repository  # noqa: E402
from rtv.store.writer import TelemetryWriter  # noqa: E402
from tests.helpers import field_spec, record_live_session  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """A data dir laid out like ``./data``: DuckDB, Parquet and a journal run."""
    mp = pytest.MonkeyPatch()
    data = tmp_path_factory.mktemp("data")
    try:
        db, session_id = record_live_session(data, mp, field_spec())
    finally:
        mp.undo()

    engine = RaceStateEngine(source="replay")
    recorder = journal.SessionRecorder(data / "sessions", snapshot=engine.snapshot)
    engine.bus.subscribe_callback(recorder.on_race_event)
    recorder.begin(session_id, "replay")

    class _Sink:
        def __call__(self, frame, catalog):
            st = engine.state
            recorder.on_frame(st.session_time, st.tick, st.player.lap)

    ReplayDriver(engine, publish=_Sink()).run(store_source(db, data / "parquet", session_id))
    recorder.end(reason="replay_finished")
    yield data, db, session_id
    db.close()


def _laps_of(table) -> set[int]:
    return set(table.column("lap").to_pylist())


def test_parse_laps():
    assert parse_laps("4") == (4, 4)
    assert parse_laps(" 2-3 ") == (2, 3)
    for bad in ("", "x", "3-2", "-1"):
        with pytest.raises(ValueError):
            parse_laps(bad)


def test_trim_keeps_the_lap_range_and_every_car(store, tmp_path):
    import pyarrow.parquet as pq

    data, db, sid = store
    result = trim_session(
        db, data / "parquet", sid, (2, 3), tmp_path / "fx", journal_root=data / "sessions"
    )
    table = pq.read_table(tmp_path / "fx" / FRAMES_FILE)
    assert _laps_of(table) == {2, 3}
    assert result.frames == table.num_rows == 2 * field_spec().ticks_per_lap
    ticks = table.column("tick").to_pylist()
    assert ticks == sorted(ticks)
    # Both spellings survive: the meta ``lap`` axis and the ``Lap`` channel.
    assert {"lap", "Lap"} <= set(table.column_names)
    row = table.slice(0, 1).to_pylist()[0]
    assert len(row["CarIdxLapDistPct"]) == 64
    assert sum(1 for p in row["CarIdxLapDistPct"] if p >= 0) == 8

    meta = read_fixture(tmp_path / "fx")
    assert meta["source_session_id"] == sid
    assert [r["lap"] for r in meta["lap_rows"]] == [2, 3]
    assert meta["session"]["kind"] == "live"
    assert meta["session_info"]["info"]["WeekendInfo"]["SubSessionID"] == 4242


def test_trim_copies_only_journal_records_inside_the_span(store, tmp_path):
    data, db, sid = store
    result = trim_session(
        db, data / "parquet", sid, (3, 3), tmp_path / "fx", journal_root=data / "sessions"
    )
    t0, t1 = result.session_time
    (run_file,) = (tmp_path / "fx" / "journal").glob("*.jsonl")
    records = list(journal.iter_records(run_file))
    kinds = {r["kind"] for r in records}
    assert {"lifecycle", "race_event", "state"} <= kinds
    phases = [r["data"]["phase"] for r in records if r["kind"] == "lifecycle"]
    assert phases == ["start", "end"]
    for r in records:
        if r["kind"] != "lifecycle":
            assert t0 <= r["session_time"] <= t1
    # Lap 3 holds a known milestone of the scripted race: the yellow.
    assert any(r.get("event_key") == "flag_change:yellow" for r in records)
    assert result.journal_records == {run_file.stem: len(records)}

    original = list(journal.iter_records(journal.run_path(data / "sessions", sid, run_file.stem)))
    assert len(records) < len(original)


def test_channel_filter_keeps_what_the_replay_needs(store, tmp_path):
    import pyarrow.parquet as pq

    data, db, sid = store
    trim_session(db, data / "parquet", sid, (2, 2), tmp_path / "fx", channels=["CarIdx*"])
    names = {r["name"] for r in read_fixture(tmp_path / "fx")["catalog"]}
    assert {"CarIdxLapDistPct", "CarIdxPosition", "Lap", "LapDistPct", "SessionTime"} <= names
    assert "Speed" not in names and "FuelLevel" not in names
    assert "Speed" not in pq.read_table(tmp_path / "fx" / FRAMES_FILE).column_names


def test_refuses_a_non_empty_out_dir_and_unknown_input(store, tmp_path):
    data, db, sid = store
    out = tmp_path / "fx"
    out.mkdir()
    (out / "keep.txt").write_text("x")
    with pytest.raises(FileExistsError):
        trim_session(db, data / "parquet", sid, (2, 2), out)
    assert (out / "keep.txt").exists()
    with pytest.raises(KeyError):
        trim_session(db, data / "parquet", "nope", (2, 2), tmp_path / "a")
    with pytest.raises(KeyError):
        trim_session(db, data / "parquet", sid, (40, 41), tmp_path / "b")


def test_a_seeded_fixture_replays_the_field(store, tmp_path):
    data, db, sid = store
    fx = tmp_path / "fx"
    trim_session(db, data / "parquet", sid, (2, 4), fx, journal_root=data / "sessions")

    target = tmp_path / "store"
    db2 = Database(target / "telemetry.duckdb")
    try:
        writer = TelemetryWriter(db2, target / "parquet")
        new_sid = seed_fixture(
            writer, fx, session_id="rig-fixture", journal_root=target / "sessions"
        )
        assert new_sid == "rig-fixture"
        repo = Repository(db2, target / "parquet")
        assert [lap["lap"] for lap in repo.get_laps(new_sid)] == [2, 3, 4]
        assert repo.get_session(new_sid)["kind"] == "live"

        engine = RaceStateEngine(source="replay")
        ReplayDriver(engine).run(
            store_source(
                db2, target / "parquet", new_sid, session_info=repo.get_session_info(new_sid)
            )
        )
        assert len(engine.state.standings.cars) == 8
        assert EventType.BLUE_FLAG in {e.event_type for e in engine.bus.history()}

        runs = journal.list_runs(target / "sessions", new_sid)
        assert len(runs) == 1 and runs[0]["complete"]
    finally:
        db2.close()


def test_cli_lists_and_trims(store, tmp_path):
    data, _db, sid = store
    # The CLI opens the store itself; DuckDB allows one writer per file, so copy it.
    import shutil

    _db.execute("CHECKPOINT")
    copy = tmp_path / "data"
    shutil.copytree(data, copy, ignore=shutil.ignore_patterns("*.wal"))

    script = str(ROOT / "scripts" / "trim_fixture.py")
    listed = subprocess.run(
        [sys.executable, script, "--data-dir", str(copy), "--list"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert sid in listed.stdout

    out = tmp_path / "cli-fx"
    done = subprocess.run(
        [
            sys.executable,
            script,
            "--data-dir",
            str(copy),
            "--session",
            sid,
            "--laps",
            "2-3",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = json.loads(done.stdout)
    assert summary["laps"] == [2, 3]
    assert (out / FIXTURE_FILE).exists() and summary["journal_records"]

    bad = subprocess.run(
        [
            sys.executable,
            script,
            "--data-dir",
            str(copy),
            "--session",
            sid,
            "--laps",
            "x",
            "--out",
            str(tmp_path / "x"),
        ],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 2
