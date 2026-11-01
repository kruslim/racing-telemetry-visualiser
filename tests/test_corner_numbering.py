"""R3: Layer-1 corners take the session corner map's labels, the same on every lap pair."""

from __future__ import annotations

from rtv.coaching.features import CoachingService
from rtv.coaching.models import CornerFinding

LAP_M = 7000.0
MAP = {
    "lap": 3,
    "agreed": True,
    "corners": [
        {"label": "T1", "name": "La Source", "lap_dist_pct": 0.056},
        {"label": "T2", "name": "Les Combes", "lap_dist_pct": 0.358},
        {"label": "T3", "name": "Bruxelles", "lap_dist_pct": 0.438},
        {"label": "T4", "name": "Speaker's Corner", "lap_dist_pct": 0.472},
        {"label": "T5", "name": None, "lap_dist_pct": 0.973},
    ],
}


def _service(smap=MAP) -> CoachingService:
    svc = CoachingService.__new__(CoachingService)
    svc.corner_map = lambda _sid: smap  # type: ignore[method-assign]
    return svc


def _corners(*pcts: float) -> list[CornerFinding]:
    return [
        CornerFinding(
            index=i, label=f"T{i + 1}", distance=p * LAP_M, sector=1, type="Slow corner",
            min_main=80.0, min_ref=80.0, net_dt=0.0, name="own lookup",
        )
        for i, p in enumerate(pcts)
    ]


def test_a_missing_corner_keeps_the_others_labels():
    corners = _corners(0.056, 0.351, 0.437, 0.975)  # no Speaker's Corner on this lap
    notes: list[str] = []
    on_map, lap = _service()._session_labels("s", 8, corners, LAP_M, notes)
    assert [(c.label, c.name) for c in corners] == [
        ("T1", "La Source"), ("T2", "Les Combes"), ("T3", "Bruxelles"), ("T5", None),
    ]
    assert on_map == {0, 1, 2, 3} and lap == 3
    assert notes == [
        "T4 (Speaker's Corner) of the session corner map (lap 3) was not detected on lap 8."
    ]


def test_an_extra_apex_is_numbered_after_the_map():
    corners = _corners(0.056, 0.17, 0.358, 0.438, 0.472, 0.973)
    notes: list[str] = []
    on_map, _lap = _service()._session_labels("s", 1, corners, LAP_M, notes)
    assert [c.label for c in corners] == ["T1", "T6", "T2", "T3", "T4", "T5"]
    assert 1 not in on_map and corners[1].off_map and not corners[0].off_map
    assert corners[1].name == "own lookup"  # named by its own lap later
    assert len(notes) == 1 and notes[0].startswith("T6 (apex 1190 m) is not on")


def test_two_apexes_on_one_map_corner_give_it_to_the_nearer():
    corners = _corners(0.432, 0.440)
    _service()._session_labels("s", 2, corners, LAP_M, [])
    assert [c.label for c in corners] == ["T6", "T3"]


def test_a_corner_across_the_line_still_matches():
    corners = _corners(0.999)
    smap = {"lap": 2, "agreed": True,
            "corners": [{"label": "T1", "name": "Turn One", "lap_dist_pct": 0.004}]}
    _service(smap)._session_labels("s", 2, corners, LAP_M, [])
    assert corners[0].label == "T1" and corners[0].name == "Turn One"


def test_without_a_map_the_lap_keeps_its_own_numbering():
    corners = _corners(0.1, 0.5)
    on_map, lap = _service({"lap": None, "agreed": False, "corners": []})._session_labels(
        "s", 2, corners, LAP_M, []
    )
    assert [c.label for c in corners] == ["T1", "T2"] and on_map == set() and lap is None
