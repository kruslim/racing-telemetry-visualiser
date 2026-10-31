"""A live recording keeps the whole field: ``CarIdx*`` arrays survive store and replay.

Real ``.ibt`` files carry no ``CarIdx*`` channels, so standings, gaps and the
spotter detectors have only ever run on live frames. This proves the other half
of the rig-session plan: a session captured by the *real* :class:`LivePoller`
(driven by a fake ``irsdk`` that serves the scripted multi-car race) is stored
with all 64 entries of every per-car array, and replaying it from DuckDB/Parquet
gives the engine exactly the field the in-memory script did.

Test-only synthetic data (``evals/synthetic_race.py``); nothing here is served.
"""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from evals.synthetic_race import scenario_frames, scenario_source  # noqa: E402

from rtv.racestate import RaceStateEngine  # noqa: E402
from rtv.racestate.models import EventType  # noqa: E402
from rtv.racestate.replay import ReplayDriver, store_source  # noqa: E402
from rtv.store.repository import Repository  # noqa: E402
from tests.helpers import field_spec, record_live_session  # noqa: E402

FIELD_SPEC = field_spec()

CAR_IDX = (
    "CarIdxLapDistPct",
    "CarIdxLap",
    "CarIdxPosition",
    "CarIdxLastLapTime",
    "CarIdxOnPitRoad",
    "CarIdxTrackSurface",
)


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("live-field")
    try:
        db, session_id = record_live_session(tmp, mp, FIELD_SPEC)
    finally:
        mp.undo()
    yield db, tmp / "parquet", session_id
    db.close()


def _replay(source) -> RaceStateEngine:
    engine = RaceStateEngine(source="replay")
    ReplayDriver(engine).run(source)
    return engine


def _event_keys(engine: RaceStateEngine) -> list[str]:
    return [e.key for e in engine.bus.history()]


# --------------------------------------------------------------------------
# the store keeps every entry
# --------------------------------------------------------------------------
def test_live_catalog_stores_carIdx_arrays_as_64_wide_list_columns(recorded):
    db, parquet, session_id = recorded
    repo = Repository(db, parquet)
    catalog = {v["name"]: v for v in repo.get_catalog(session_id)["variables"]}
    for name in CAR_IDX:
        assert catalog[name]["count"] == 64
        assert catalog[name]["storage"] == "list"
        assert catalog[name]["columns"] == [name]
    session = repo.get_session(session_id)
    assert session["kind"] == "live"


def test_every_stored_frame_has_the_full_field(recorded):
    db, parquet, session_id = recorded
    glob = str(parquet / f"session_id={session_id}" / "lap=*" / "*.parquet")
    expected = list(scenario_frames(FIELD_SPEC))
    rows = db.query_dicts(
        'SELECT tick, "CarIdxLapDistPct", "CarIdxPosition", "CarIdxOnPitRoad" '
        "FROM read_parquet(?, union_by_name=true) ORDER BY tick",
        [glob],
    )
    assert len(rows) == len(expected)
    for row, frame in zip(rows, expected, strict=True):
        assert row["tick"] == frame.tick
        assert len(row["CarIdxLapDistPct"]) == 64
        assert row["CarIdxLapDistPct"] == pytest.approx(frame.values["CarIdxLapDistPct"])
        assert row["CarIdxPosition"] == frame.values["CarIdxPosition"]
        assert row["CarIdxOnPitRoad"] == frame.values["CarIdxOnPitRoad"]


def test_raw_channel_query_returns_the_whole_array(recorded):
    db, parquet, session_id = recorded
    out = Repository(db, parquet).get_channels(session_id, ["CarIdxPosition"], lap=2, mode="raw")
    ys = out["channels"]["CarIdxPosition"]["y"]
    assert ys and all(len(y) == 64 for y in ys)
    assert sorted(p for p in ys[0] if p) == list(range(1, 9))


def test_downsampling_an_array_is_refused_not_a_500(recorded):
    from rtv.store.queries import QueryError

    db, parquet, session_id = recorded
    repo = Repository(db, parquet)
    with pytest.raises(QueryError, match="per-car array"):
        repo.get_channels(session_id, ["CarIdxPosition"], lap=2, max_points=50)
    with pytest.raises(QueryError, match="per-car array"):
        repo.compare(session_id, "CarIdxLapDistPct", [2, 3])
    # Scalars in the same session still downsample.
    out = repo.get_channels(session_id, ["Speed"], lap=2, max_points=50)
    assert out["downsample"]["bucket"] > 1


# --------------------------------------------------------------------------
# replay gives the engine the same field the script did
# --------------------------------------------------------------------------
def test_stored_live_replay_matches_the_in_memory_field(recorded):
    db, parquet, session_id = recorded
    stored = _replay(
        store_source(
            db,
            parquet,
            session_id,
            session_info=Repository(db, parquet).get_session_info(session_id),
        )
    )
    direct = _replay(scenario_source(FIELD_SPEC))

    assert _event_keys(stored) == _event_keys(direct)

    st, dt = stored.state.standings, direct.state.standings
    assert len(st.cars) == len(dt.cars) == 8
    assert [c.idx for c in st.cars] == [c.idx for c in dt.cars]
    assert [c.position for c in st.cars] == [c.position for c in dt.cars]
    assert [c.gap_to_player for c in st.cars] == pytest.approx([c.gap_to_player for c in dt.cars])
    assert st.gap_basis == dt.gap_basis is not None


def test_spotter_detectors_see_the_field_in_the_stored_replay(recorded):
    db, parquet, session_id = recorded
    engine = _replay(store_source(db, parquet, session_id))
    types_seen = {e.event_type for e in engine.bus.history()}
    assert EventType.BLUE_FLAG in types_seen
    assert EventType.TRAFFIC_CLOSE in types_seen
    blue = next(e for e in engine.bus.history() if e.event_type is EventType.BLUE_FLAG)
    assert blue.payload["car_idx"] == 7
