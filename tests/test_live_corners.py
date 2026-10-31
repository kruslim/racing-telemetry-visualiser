"""Live corner labels match the post-race review (task E1).

The review labels corners ``T1``... from Layer 1's apex detection and names them from the venue
reference. Live, the engine builds the same map from its first clean lap and puts a
``place = {label, name, lap_dist_pct, provisional}`` on every event that happened somewhere.
Before that lap the label is the lap-distance bucket, marked provisional.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from evals.checks import cross_check
from evals.synthetic_race import (
    ScenarioSpec,
    scenario_catalog,
    scenario_frames,
    scenario_session_info,
)

from rtv import tracks
from rtv.coaching.agent.validation import check_figures, resolve_corner, split_corner
from rtv.coaching.features import detect_apexes
from rtv.racestate import RaceStateEngine
from rtv.tracks.live import LiveCornerMap, bucket_label, place_of

#: A real ``/sessions/{id}/track`` response for the Spa race (reference lap 6).
SPA_TRACK = json.loads(
    (Path(__file__).parent / "fixtures" / "spa_session_track.json").read_text(encoding="utf-8")
)
SPA_CORNERS = [c for c in SPA_TRACK["corners"] if c["kind"] == "corner"]
GRID = 1500


# --------------------------------------------------------------------------
# a lap built from the Spa corner map: a speed dip and the venue's own geometry at each
# --------------------------------------------------------------------------
def _spa_lap():
    """Speed, distance, lat and lon on the review's grid, with an apex at each named corner.

    Lat/Lon are the OSM centreline point of each corner around its apex (NaN elsewhere), so the
    name lookup is the real one against the real Spa reference.
    """
    ref = tracks.get("spa")
    assert ref is not None
    pct = (np.arange(GRID) + 0.5) / GRID
    length = 7004.0
    speed = np.full(GRID, 70.0)
    lat = np.full(GRID, np.nan)
    lon = np.full(GRID, np.nan)
    by_name = {s.name: s for s in ref.segments if s.kind == "corner"}
    for c in SPA_CORNERS:
        d = np.abs(pct - c["mid_pct"])
        d = np.minimum(d, 1 - d) * length  # metres from the apex
        speed -= 30.0 * np.clip(1 - d / 120.0, 0, None) ** 2
        line = by_name[c["name"]].polylines[0]
        mid_lat, mid_lon = line[len(line) // 2]
        near = d < 15.0
        lat[near], lon[near] = mid_lat, mid_lon
    return ref, speed, pct * length, lat, lon


def test_the_live_map_numbers_and_names_spa_like_the_review():
    ref, speed, dist, lat, lon = _spa_lap()
    m = LiveCornerMap(ref)
    assert m.build(6, speed=speed, dist=dist, lat=lat, lon=lon)
    # Same detector as Layer 1: same apexes, so same T-numbers.
    assert len(m.corners) == len(detect_apexes(speed, dist)) == len(SPA_CORNERS)
    assert [c["label"] for c in m.corners[:3]] == ["T1", "T2", "T3"]
    assert [c["name"] for c in m.corners] == [c["name"] for c in SPA_CORNERS]
    assert m.corners[0]["name"] == "La Source"


def test_places_take_the_nearest_apex_and_wrap_at_the_line():
    ref, speed, dist, lat, lon = _spa_lap()
    m = LiveCornerMap(ref)
    m.build(6, speed=speed, dist=dist, lat=lat, lon=lon)
    # A lockup in the braking zone before La Source belongs to La Source.
    assert m.place(0.045) == {
        "label": "T1", "name": "La Source", "lap_dist_pct": 0.045, "provisional": False,
    }
    last = m.corners[-1]
    assert last["name"] == "Chicane"
    # Before the line is the Bus Stop chicane; a little past it, La Source.
    assert m.place(0.99)["label"] == last["label"]
    assert m.place(1.03)["label"] == "T1"  # a fraction past 1.0 wraps
    assert m.find("T1 La Source")["name"] == "La Source"
    assert m.find("T99") is None


def test_before_a_clean_lap_the_place_is_a_provisional_bucket():
    m = LiveCornerMap(tracks.get("spa"))
    assert m.place(0.4033) == {
        "label": "C08", "name": None, "lap_dist_pct": 0.4033, "provisional": True,
    }
    assert bucket_label(0.4033, 20) == "C08"
    assert m.place(None) is None


def test_without_lat_lon_the_labels_still_match_and_names_stay_empty():
    _, speed, dist, _, _ = _spa_lap()
    m = LiveCornerMap(tracks.get("spa"))
    m.build(3, speed=speed, dist=dist)
    assert len(m.corners) == len(SPA_CORNERS)
    assert all(c["name"] is None for c in m.corners)


def _feed(m: LiveCornerMap, lap: int, speed, dist, *, upto: float = 1.0) -> None:
    for i in range(GRID):
        p = (i + 0.5) / GRID
        if p > upto:
            break
        m.observe({"Lap": lap, "LapDistPct": p, "Speed": speed[i], "LapDist": dist[i]})


def test_frames_resample_into_the_same_map_and_partial_or_dirty_laps_are_refused():
    _, speed, dist, _, _ = _spa_lap()
    m = LiveCornerMap()
    _feed(m, 1, speed, dist, upto=0.5)  # joined mid-lap
    assert not m.lap_done(1, clean=True)
    _feed(m, 2, speed, dist)
    assert not m.lap_done(2, clean=False)  # a yellow or a pit stop
    _feed(m, 3, speed, dist)
    assert not m.lap_done(3, clean=True)  # one clean lap: nothing to agree with yet
    _feed(m, 4, speed, dist)
    assert m.lap_done(4, clean=True)
    assert m.lap == 4 and len(m.corners) == len(SPA_CORNERS)
    _feed(m, 5, speed, dist)
    assert not m.lap_done(5, clean=True)  # adopted once; later laps never renumber it


def test_an_atypical_lap_does_not_set_the_numbering():
    """The Spa start lap has an extra apex (Raidillon); the typical laps do not."""
    _, speed, dist, _, _ = _spa_lap()
    pct = (np.arange(GRID) + 0.5) / GRID
    d = np.minimum(np.abs(pct - 0.25), 1 - np.abs(pct - 0.25)) * 7004.0
    start = speed - 30.0 * np.clip(1 - d / 120.0, 0, None) ** 2  # a lift on Kemmel
    m = LiveCornerMap()
    assert not m.offer(1, speed=start, dist=dist)
    assert not m.offer(2, speed=speed, dist=dist)  # disagrees with lap 1
    assert m.offer(3, speed=speed, dist=dist)  # agrees with lap 2
    assert len(m.corners) == len(SPA_CORNERS) and m.lap == 3


def test_a_recurring_issue_is_placed_at_the_middle_of_its_hits():
    assert place_of({"lap_dist_pct_from": 0.40, "lap_dist_pct_to": 0.42}) == pytest.approx(0.41)
    assert place_of({"lap_dist_pct_from": 0.01, "lap_dist_pct_to": 0.99}) == pytest.approx(0.0)
    assert place_of({"lap_dist_pct": 0.3}) == 0.3
    assert place_of({"lap": 3}) is None


# --------------------------------------------------------------------------
# the engine: provisional first, then the review's labels
# --------------------------------------------------------------------------
def test_engine_labels_events_with_the_review_corner_once_a_clean_lap_is_mapped():
    spec = ScenarioSpec(corner_pcts=(0.15, 0.41, 0.72), lockup_laps=(1, 2))
    catalog = scenario_catalog(spec)
    engine = RaceStateEngine(source="replay")
    engine.bind_catalog(catalog, session_id="e1")
    engine.set_session_info(scenario_session_info(spec))
    for frame in scenario_frames(spec):
        engine.on_frame(frame, catalog)

    track = engine.state.track
    assert not track.provisional and track.corner_map_lap is not None
    assert [c.label for c in track.corners] == ["T1", "T2", "T3"]
    assert [c.lap_dist_pct for c in track.corners] == pytest.approx([0.15, 0.41, 0.72], abs=0.02)

    history = engine.bus.history()
    lockups = [e for e in history if e.key == "lockup"]
    assert lockups and all("place" in e.payload for e in lockups)
    early = [e for e in lockups if e.lap is not None and e.lap <= track.corner_map_lap]
    late = [e for e in lockups if e.lap is not None and e.lap > track.corner_map_lap]
    assert early and all(e.payload["place"]["provisional"] for e in early)
    assert all(e.payload["place"]["label"].startswith("C") for e in early)
    assert late and all(e.payload["place"]["label"] == "T2" for e in late)

    recurring = next(e for e in history if e.key == "recurring_issue")
    assert recurring.payload["corner"].startswith("C")  # the counting bucket is kept
    assert recurring.payload["place"]["label"] == "T2"
    assert recurring.payload["place"]["provisional"] is False

    # Events that name no place get no place.
    assert all("place" not in e.payload for e in history if e.key == "lap_completed")


def test_corner_detail_resolves_a_review_label_through_the_live_map():
    from rtv.pitwall.framework import ToolContext
    from rtv.pitwall.tools import GET_CORNER_DETAIL

    spec = ScenarioSpec(corner_pcts=(0.15, 0.41, 0.72), lockup_laps=(1, 2))
    catalog = scenario_catalog(spec)
    engine = RaceStateEngine(source="replay")
    engine.bind_catalog(catalog, session_id="e1")
    for frame in scenario_frames(spec):
        engine.on_frame(frame, catalog)
    history = engine.bus.history()
    trigger = next(e for e in history if e.key == "recurring_issue")
    ctx = ToolContext(state=engine.state, event=trigger, events=list(history))
    out = GET_CORNER_DETAIL.fn(ctx, {"corner": "T2 whatever name"})
    assert out["requested_lap_dist_pct"] == pytest.approx(0.41, abs=0.02)
    assert out["live"]["place"]["label"] == "T2"
    assert out["live"]["corner"].startswith("C")  # the bucket, as before
    assert out["live"]["count"] >= 3
    # Each row keeps the place it was given when it fired: a bucket before the map existed.
    assert {r["payload"]["place"]["label"] for r in out["live"]["events"]} <= {"T2", "C08"}


# --------------------------------------------------------------------------
# the coaching checks: "T7 — Campus" is T7, if Campus is T7's name
# --------------------------------------------------------------------------
FINDINGS = {
    "corners": [
        {"label": "T1", "name": "La Source", "net_dt": 0.30, "min_main": 70.0, "min_ref": 72.0},
        {"label": "T7", "name": "Campus", "net_dt": 0.21, "min_main": 110.0, "min_ref": 112.0},
        {"label": "T9", "name": None, "net_dt": 0.05, "min_main": 150.0, "min_ref": 150.5},
    ],
    "chief": {"top3": [{"label": "T1"}]},
}


@pytest.mark.parametrize(
    ("cited", "expected"),
    [
        ("T7", ("T7", None)),
        ("T7 Campus", ("T7", "Campus")),
        ("T7 — Campus", ("T7", "Campus")),
        ("T7: Campus", ("T7", "Campus")),
        ("Turn 7 (Campus)", ("T7", "Campus")),
        ("C09", ("C09", None)),
    ],
)
def test_split_corner(cited, expected):
    assert split_corner(cited) == expected


def test_a_named_label_resolves_only_with_its_own_name():
    corners = {c["label"]: c for c in FINDINGS["corners"]}
    assert resolve_corner(corners, "T7 — Campus", "p")[0]["label"] == "T7"
    assert resolve_corner(corners, "t1 la source", "p")[0]["label"] == "T1"
    corner, issue = resolve_corner(corners, "T7 Pouhon", "p")
    assert corner is None and "Campus" in issue
    corner, issue = resolve_corner(corners, "T9 Blanchimont", "p")
    assert corner is None and "no mapped name" in issue
    assert resolve_corner(corners, "T4", "p")[0] is None


def test_cross_check_scores_named_corners_as_the_corner():
    result = {
        "plan": {
            "headline": "",
            "one_lap_focus": "",
            "priorities": [
                {"corner": "T1 — La Source", "gain_s": 0.30, "why": ""},
                {"corner": "T7 — Campus", "gain_s": 0.21, "why": ""},
            ],
        },
        "advices": [],
    }
    score = cross_check(result, FINDINGS)
    assert score["outcome_accuracy"] is True
    assert score["claim_factuality"] == 1.0, score["issues"]
    assert not any("hallucinated" in i for i in score["issues"])

    result["plan"]["priorities"][1]["corner"] = "T7 Pouhon"
    score = cross_check(result, FINDINGS)
    assert score["claim_factuality"] < 1.0
    assert any("Pouhon" in i for i in score["issues"])


def test_check_figures_accepts_named_citations():
    claims = [{"citations": [{"corner": "T7 Campus", "figure": "net_dt", "value": 0.21}]}]
    assert check_figures([{"corner": "T1 La Source", "gain_s": 0.30}], claims, FINDINGS) == []
