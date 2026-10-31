"""Real telemetry end to end: a 42-minute iRacing GT3 race at Spa.

Import, lap detection, coaching findings and replay-as-live, all on a real
``.ibt`` rather than generated data. The file is too large to commit; fetch it
with ``python scripts/fetch_fixtures.py`` (CI caches it). Without it, this module
skips, so the default suite stays offline.

Expected values come from the file itself (iRacing's own ``LapLastLapTime`` and
``OnPitRoad``), not from anything RTV computed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("irsdk")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

FIXTURE = Path(
    os.environ.get(
        "RTV_REAL_IBT",
        Path(__file__).parent / "fixtures" / "real" / "audir8lmsevo2gt3_spa_race.ibt",
    )
)
pytestmark = pytest.mark.skipif(
    not FIXTURE.is_file(), reason="real .ibt fixture absent; run scripts/fetch_fixtures.py"
)

#: iRacing's official lap times for a few laps, read straight from the file.
OFFICIAL = {2: 143.965, 6: 140.618, 11: 141.030, 17: 140.722}
IN_LAP, OUT_LAP = 12, 13


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path_factory.mktemp("real")))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_PITWALL", "true")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.ingest.ibt import import_ibt
    from rtv.main import create_app

    try:
        with TestClient(create_app()) as c:
            services = c.app.state.services
            c.session_id = import_ibt(  # type: ignore[attr-defined]
                str(FIXTURE), services.writer, flatten_max=services.settings.array_flatten_max
            )
            yield c
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _laps(client) -> dict[int, dict]:
    rows = client.get(f"/api/v1/sessions/{client.session_id}/laps").json()["laps"]
    return {row["lap"]: row for row in rows}


def test_laps_carry_iracings_official_times(client):
    laps = _laps(client)
    assert len(laps) == 19
    for lap, official in OFFICIAL.items():
        assert laps[lap]["lap_time"] == pytest.approx(official, abs=1e-3), lap


def test_pit_laps_and_partial_segments_are_flagged(client):
    laps = _laps(client)
    assert laps[IN_LAP]["is_in_lap"] and not laps[IN_LAP]["is_out_lap"]
    assert laps[OUT_LAP]["is_out_lap"] and not laps[OUT_LAP]["is_in_lap"]
    assert not any(laps[n]["is_in_lap"] or laps[n]["is_out_lap"]
                   for n in laps if n not in (IN_LAP, OUT_LAP))
    # Lap 0 is the grid; the last segment never crossed the line.
    assert laps[0]["is_valid"] is False
    assert laps[max(laps)]["is_valid"] is False
    assert all(laps[n]["is_valid"] for n in range(1, max(laps)))


def test_coaching_findings_on_real_laps(client):
    r = client.get(
        "/api/v1/coaching/lap-findings",
        params={"session_id": client.session_id, "main_lap": 8, "ref_lap": 6},
    )
    assert r.status_code == 200, r.text
    f = r.json()
    assert 6900 < f["lap_length_m"] < 7100  # Spa GP is 7.004 km
    assert len(f["corners"]) >= 5
    la_source = f["corners"][0]
    assert la_source["distance"] < 600 and la_source["min_main"] < 80  # the hairpin
    assert f["main_time"] - f["ref_time"] == pytest.approx(2.818, abs=1e-2)
    # Every KPI is computable: this file logs steering, pedals and gear.
    for side in ("main", "ref"):
        assert all(v is not None for v in f["kpis"][side].values())
    # Lap-time spread is over clean laps, not the grid segment or the pit stop.
    assert f["chief"]["time_spread"] < 10


def test_session_track_names_spas_corners_in_lap_order(client):
    r = client.get(f"/api/v1/sessions/{client.session_id}/track")
    assert r.status_code == 200, r.text
    t = r.json()
    header = t["header"]
    assert header["config_name"] == "Grand Prix Pit"
    assert header["num_turns"] == 21 and header["pit_speed_limit_kph"] == 80.0
    assert header["sector_starts_pct"][0] == 0.0 and len(header["sector_starts_pct"]) == 5
    assert t["reference"]["key"] == "spa"
    names = [c["name"] for c in t["corners"]]
    # The circuit's real order, read off its own Lat/Lon trace.
    expected = ["La Source", "Eau Rouge", "Raidillon", "Kemmel", "Les Combes", "Bruxelles",
                "Double Gauche", "Fagnes", "Campus", "Blanchimont", "Chicane"]
    assert [n for n in names if n in expected] == expected


def test_layer1_corners_carry_the_circuits_names(client):
    r = client.get(
        "/api/v1/coaching/lap-findings",
        params={"session_id": client.session_id, "main_lap": 6, "ref_lap": 11},
    )
    corners = r.json()["corners"]
    assert corners[0]["name"] == "La Source"
    assert corners[-1]["name"] == "Chicane"  # the Bus Stop
    assert sum(1 for c in corners if c["name"]) >= len(corners) - 1


def test_replay_runs_the_real_race_through_the_live_path(client):
    seen_speeds: list[float] = []
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"op": "subscribe", "channels": ["Speed"], "rate_hz": 60})
        r = client.post(
            "/api/v1/replay/start", json={"session_id": client.session_id, "speed": 0}
        )
        assert r.status_code == 200, r.text
        for _ in range(500):
            msg = ws.receive_json()
            if msg["type"] == "data" and msg["v"].get("Speed") is not None:
                seen_speeds.append(msg["v"]["Speed"])
                if len(seen_speeds) >= 3:
                    break
    assert seen_speeds  # /ws/live streams the replay like a live session

    deadline = time.time() + 300
    status = {}
    while time.time() < deadline:
        status = client.get("/api/v1/replay/status").json()
        if status["finished"]:
            break
        time.sleep(0.2)
    assert status["finished"] and status["error"] is None
    assert status["frames"] == status["total"] == 151_892

    state = client.get("/api/v1/racestate").json()
    caps = state["capabilities"]
    assert caps["fuel"] and caps["tyres"] and caps["pit"]
    # The .ibt does not record every car's track position, so there are no
    # standings: reported as missing rather than invented.
    assert caps["standings"] is False and "CarIdxLapDistPct" in caps["missing"]
    assert 3.5 < state["fuel"]["per_lap"] < 4.5  # litres per green lap
    assert state["player"]["best_lap_time"] == pytest.approx(OFFICIAL[6], abs=1e-3)

    events = client.get("/api/v1/racestate/events", params={"limit": 500}).json()["events"]
    by_key: dict[str, list[dict]] = {}
    for event in events:
        by_key.setdefault(event["key"].split(":")[0], []).append(event)
    assert [e["lap"] for e in by_key["pit_entry"]] == [IN_LAP]
    assert [e["lap"] for e in by_key["pit_exit"]] == [OUT_LAP]
    assert by_key.get("stint_lap_milestone")  # the strategist's wake-up fires
    laps = {e["payload"]["lap"]: e["payload"]["lap_time"] for e in by_key["lap_completed"]}
    assert laps[2] == pytest.approx(OFFICIAL[2], abs=0.05)


def _replayed(client) -> None:
    """The replay above has run; run it here when this test is selected on its own."""
    if client.get("/api/v1/replay/status").json().get("finished"):
        return
    r = client.post("/api/v1/replay/start", json={"session_id": client.session_id, "speed": 0})
    assert r.status_code == 200, r.text
    deadline = time.time() + 300
    while time.time() < deadline and not client.get("/api/v1/replay/status").json()["finished"]:
        time.sleep(0.2)


def test_live_corner_labels_match_the_review(client):
    """E1: the replayed race names places with the review's own corner labels."""
    from rtv.desktop.model.review import default_laps

    _replayed(client)
    track = client.get("/api/v1/racestate").json()["track"]
    assert track["provisional"] is False
    # Lap 1 (the start) has an extra apex at Raidillon and laps 3-4 ran under yellow, so the
    # first two clean laps that agree are 2 and 5.
    assert track["corner_map_lap"] == 5

    laps = client.get(f"/api/v1/sessions/{client.session_id}/laps").json()["laps"]
    picks = default_laps(laps)  # the laps the desktop review opens on
    f = client.get(
        "/api/v1/coaching/lap-findings",
        params={
            "session_id": client.session_id, "main_lap": picks["main"], "ref_lap": picks["ref"],
        },
    ).json()
    review = [(c["label"], c["name"]) for c in f["corners"]]
    live = [(c["label"], c["name"]) for c in track["corners"]]
    assert live == review
    assert live[0] == ("T1", "La Source") and live[-1] == ("T8", "Chicane")

    events = client.get("/api/v1/racestate/events", params={"limit": 500}).json()["events"]
    placed = [e for e in events if "place" in (e.get("payload") or {})]
    assert placed
    labels = {c["label"] for c in track["corners"]}
    for e in placed:
        place = e["payload"]["place"]
        if place["provisional"]:
            assert place["label"].startswith("C") and place["name"] is None
        else:
            assert place["label"] in labels
    recurring = [e for e in placed if e["key"].startswith("recurring_issue")]
    assert recurring and all(not e["payload"]["place"]["provisional"] for e in recurring)
    assert recurring[0]["payload"]["place"]["name"] == "Speaker's Corner"


@pytest.mark.parametrize(("main_lap", "ref_lap"), [(12, 6), (8, 6), (5, 3), (10, 6), (3, 6)])
def test_prose_check_accepts_the_findings_own_words(client, main_lap, ref_lap):
    """No false alarms on real data: a plan written in the findings' own words is fully grounded."""
    from evals.checks import cross_check

    f = client.get(
        "/api/v1/coaching/lap-findings",
        params={"session_id": client.session_id, "main_lap": main_lap, "ref_lap": ref_lap},
    ).json()
    priorities = [
        {"corner": p["label"], "why": p["why"], "gain_s": p["gain"]} for p in f["chief"]["top3"]
    ]
    advices = [
        {
            "corner": c["label"],
            "diagnosis": ". ".join(d["text"] for d in c["diags"]),
            "cues": [f"{d['text']} at {d['distance']:.0f} m" for d in c["diags"]],
            "est_gain_s": c["net_dt"],
        }
        for c in f["corners"]
        if c["label"] in {p["corner"] for p in priorities}
    ]
    result = {
        "clean": False,
        "plan": {"headline": f"{f['chief']['lost']:.2f} s available.", "one_lap_focus": "",
                 "priorities": priorities},
        "advices": advices,
    }
    score = cross_check(result, f)
    assert score["issues"] == []
    assert score["prose_claims"] > len(priorities)


def test_prose_check_catches_the_kimi_t7_brake_advice(client):
    """The orchestrator's real lap-12 plan moved the T7 brake point the wrong way."""
    from evals.checks import cross_check

    f = client.get(
        "/api/v1/coaching/lap-findings",
        params={"session_id": client.session_id, "main_lap": IN_LAP, "ref_lap": 6},
    ).json()
    t7 = next(c for c in f["corners"] if c["label"] == "T7")
    plan = {
        "headline": "",
        "one_lap_focus": "T7: move your brake point from 6540 m to ~6570 m.",
        "priorities": [{"corner": "T7", "why": "", "gain_s": t7["net_dt"]}],
    }
    result = {"clean": False, "plan": plan, "advices": []}
    score = cross_check(result, f)
    assert score["claim_factuality"] < 1.0
    assert any("brake later at T7" in i and "6508 m" in i for i in score["prose_issues"])
