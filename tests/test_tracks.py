"""Pre-loaded track reference data: the data files, venue matching and corner naming.

The header values below are copied from real iRacing ``.ibt`` files (Spa, Road
Atlanta, Mount Panorama, Phillip Island); the corner geometry is the shipped
OpenStreetMap data itself, so nothing here is generated.
"""

from __future__ import annotations

import numpy as np
import pytest

from rtv.tracks import corner_map, get, header_facts, load_all, name_at_pct, resolve

#: The venues the user races GT3 at most; each must ship with reference data.
GT3_VENUES = ("spa", "fuji", "lemans", "nurburgring", "monza", "silverstone", "suzuka",
              "daytona", "watkinsglen", "roadatlanta", "sebring", "redbullring",
              "barcelona", "imola", "zandvoort", "bathurst")

#: WeekendInfo fields as the real files write them.
SPA = {"TrackName": "spa up", "TrackID": 163, "TrackLatitude": "50.444061 m",
       "TrackLongitude": "5.965178 m"}
ROAD_ATLANTA = {"TrackName": "roadatlanta full", "TrackID": 127,
                "TrackLatitude": "34.150393 m", "TrackLongitude": "-83.814226 m"}


def test_every_data_file_loads_with_attribution():
    refs = load_all()
    assert set(GT3_VENUES) <= set(refs)
    for ref in refs.values():
        assert "OpenStreetMap" in ref.attribution
        s, w, n, e = ref.bbox
        assert s < ref.center[0] < n and w < ref.center[1] < e
        for seg in ref.segments:
            assert seg.kind in ("corner", "straight")
            assert seg.osm_ways or seg.osm_nodes  # every name traces back to OSM
            assert all(len(line) >= 1 for line in seg.polylines)


def test_resolve_matches_by_header_position():
    assert resolve(SPA).key == "spa"
    assert resolve(ROAD_ATLANTA).key == "roadatlanta"


def test_resolve_falls_back_to_track_name_and_refuses_unknowns():
    assert resolve({"TrackName": "monza full"}).key == "monza"
    assert resolve({"TrackName": "spielberg gp"}).key == "redbullring"
    assert resolve({"TrackName": "synthetic-circuit"}) is None
    # A position far from every venue does not borrow the nearest one's names.
    assert resolve({"TrackName": "x", "TrackLatitude": "0.5 m", "TrackLongitude": "0.5 m"}) is None
    assert resolve(None) is None


def test_name_at_finds_a_mapped_corner_and_nothing_off_track():
    spa = get("spa")
    raidillon = next(s for s in spa.segments if s.name == "Raidillon")
    lat, lon = raidillon.polylines[0][len(raidillon.polylines[0]) // 2]
    assert spa.name_at(lat, lon).name == "Raidillon"
    assert spa.name_at(lat + 0.01, lon) is None  # ~1 km north, in the forest
    # Named straights never name a corner.
    kemmel = next(s for s in spa.segments if s.name == "Kemmel")
    k_lat, k_lon = kemmel.polylines[0][len(kemmel.polylines[0]) // 2]
    assert spa.name_at(k_lat, k_lon) is None


def test_corner_map_orders_segments_along_a_trace_and_wraps_the_line():
    spa = get("spa")
    order = ["Blanchimont", "Chicane", "La Source", "Eau Rouge", "Raidillon"]
    pts = []
    for name in order:
        seg = next(s for s in spa.segments if s.name == name)
        line = max(seg.polylines, key=len)
        pts.extend(line)
    lat = np.array([p[0] for p in pts])
    lon = np.array([p[1] for p in pts])
    # start the "lap" mid-way so La Source sits across the wrap
    pct = (np.arange(len(pts)) / len(pts) + 0.6) % 1.0
    mapped = corner_map(spa, lat, lon, pct)
    assert [c["name"] for c in sorted(mapped, key=lambda c: (c["start_pct"] - 0.6) % 1.0)] \
        == order
    la_source = next(c for c in mapped if c["name"] == "La Source")
    assert name_at_pct(mapped, la_source["mid_pct"]) == "La Source"
    assert name_at_pct(mapped, (la_source["end_pct"] + 0.5) % 1.0) != "La Source"


def test_point_mapped_corners_get_label_slack():
    pi = get("phillipisland")
    doohan = next(s for s in pi.corners if s.name == "Doohan Corner")
    assert doohan.radius_m and not doohan.osm_ways
    lat, lon = doohan.polylines[0][0]
    # ~45 m from the label still counts; ~150 m does not
    assert pi.name_at(lat + 0.0004, lon).name == "Doohan Corner"
    assert pi.name_at(lat + 0.00135, lon) is None


def test_header_facts_parse_iracing_units():
    info = {
        "WeekendInfo": {**SPA, "TrackLength": "6.93 km", "TrackLengthOfficial": "7.00 km",
                        "TrackConfigName": "Grand Prix Pit", "TrackNumTurns": 21,
                        "TrackPitSpeedLimit": "80.00 kph"},
        "SplitTimeInfo": {"Sectors": [{"SectorNum": 0, "SectorStartPct": 0.0},
                                      {"SectorNum": 1, "SectorStartPct": 0.310613}]},
    }
    facts = header_facts(info)
    assert facts["length_m"] == 6930.0 and facts["official_length_m"] == 7000.0
    assert facts["pit_speed_limit_kph"] == 80.0 and facts["num_turns"] == 21
    assert facts["sector_starts_pct"] == [0.0, 0.310613]
    assert facts["latitude"] == pytest.approx(50.444061)
    assert header_facts(None)["sector_starts_pct"] == []


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_track_endpoints(client):
    listing = client.get("/api/v1/tracks").json()
    assert listing["count"] >= len(GT3_VENUES)
    spa = next(t for t in listing["tracks"] if t["key"] == "spa")
    assert spa["named_corners"] >= 10
    detail = client.get("/api/v1/tracks/spa").json()
    assert any(s["name"] == "Eau Rouge" for s in detail["segments"])
    assert client.get("/api/v1/tracks/nowhere").status_code == 404
    assert client.get("/api/v1/sessions/missing/track").status_code == 404


def test_truncated_ibt_reads_only_the_records_it_holds():
    from types import SimpleNamespace

    from rtv.ingest.ibt import _complete_records

    def fake(declared: int, size: int):
        header = SimpleNamespace(buf_len=100, var_buf=[SimpleNamespace(buf_offset=1000)])
        return SimpleNamespace(_disk_header=SimpleNamespace(session_record_count=declared),
                               _header=header, _shared_mem=b"\0" * size)

    assert _complete_records(fake(50, 1000 + 100 * 50)) == 50
    assert _complete_records(fake(1_852_796_192, 1000 + 100 * 9 + 40)) == 9
