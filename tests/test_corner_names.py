"""Corner names as checkable facts (task A4): the prose check and the pitwall validator.

Two venues: the real Spa map (the replay's live corner map, and the venue names placed on a
real lap in ``tests/fixtures/spa_session_track.json``), and a trace-numbered venue (Fuji,
whose reference maps no corner names), where labels are the only way to name a corner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.pitwall_cases import agent_facts, swapped_name_cases
from evals.pitwall_checks import check_for_agent

from rtv.coaching.agent.prose import check_prose
from rtv.coaching.features import _places_on_lap
from rtv.coaching.models import CornerFinding
from rtv.grounding.corner_names import CornerNames, detect_venue, name_key, venue_names
from rtv.pitwall.agents import AGENT_REGISTRY
from rtv.pitwall.validator import FactSet, validate_output

SPECS = {spec.name: spec for spec in AGENT_REGISTRY}
TRACK = json.loads((Path(__file__).parent / "fixtures" / "spa_session_track.json").read_text())

#: The live corner map of the real Spa replay (evals/real/spa_run).
SPA = {
    "T1": "La Source",
    "T2": "Les Combes",
    "T3": "Bruxelles",
    "T4": "Speaker's Corner",
    "T5": "Double Gauche",
    "T6": "Fagnes",
    "T7": "Campus",
    "T8": "Chicane",
}


def _messages(names: CornerNames, text: str, *about: str) -> list[str]:
    return [issue.message for issue in names.check(text, about)]


@pytest.fixture(scope="module")
def spa() -> CornerNames:
    return CornerNames.build(SPA)


@pytest.fixture(scope="module")
def fuji() -> CornerNames:
    return CornerNames.build({f"T{i}": None for i in range(1, 9)}, venue="fuji")


# --------------------------------------------------------------------------
# the vocabulary
# --------------------------------------------------------------------------
def test_the_spa_map_identifies_the_venue(spa):
    assert spa.venue == "spa"
    assert detect_venue(["La Source", "Campus"]) == "spa"
    assert detect_venue(["The Esses"]) is None  # several circuits have one


def test_names_fold_accents_quotes_and_case():
    assert name_key("Speaker’s Corner") == name_key("speakers corner")
    assert name_key("Courbe Paul Frère") == "courbe paul frere"
    assert "la source" in venue_names()


# --------------------------------------------------------------------------
# Spa: what E1's Kimi run said must pass, swapped names must not
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "about"),
    [
        ("Same corner again - Speaker's Corner. Pick one speed and commit.", "T4"),
        ("Speaker's Corner again - three laps running wide there.", "T4"),
        ("Chicane: brake a touch earlier, then commit to full throttle sooner.", "T8"),
        ("The corner trace maps this to T8 Chicane: min speed down.", ""),
        ("T7 Campus is where the lap goes; T8 Chicane too.", ""),
        ("The corner trace maps this to T8 Hairpin: brake point late.", ""),
        ("T7 Brake earlier. T2: Move the brake point.", ""),
        ("Flat through Eau Rouge and up Raidillon, then brake for Les Combes.", "T2"),
        ("You lose time at the chicane and through the Esses.", ""),
        ("T4 (Speaker's Corner) and T3 (Bruxelles) both cost time.", ""),
    ],
)
def test_spa_right_names_pass(spa, text, about):
    assert _messages(spa, text, about) == []


@pytest.mark.parametrize(
    ("text", "about", "needle"),
    [
        ("Brake earlier into La Source.", "T7", "which is T1, but this is about T7"),
        ("T7 La Source costs you time.", "", "calls T7 'La Source', but T7 is Campus"),
        ("Off at T8 (Fagnes), brake earlier.", "", "but T8 is Chicane"),
        ("T2 (Eau Rouge) is flat.", "", "but T2 is Les Combes"),
        ("At T7 Pouhon you lift.", "", "calls T7 'Pouhon', but T7 is Campus"),
        ("Copse is where it goes wrong.", "", "not a corner of this circuit"),
    ],
)
def test_spa_wrong_names_are_caught(spa, text, about, needle):
    issues = _messages(spa, text, about)
    assert issues and needle in issues[0], issues


def test_a_wheel_or_a_bucket_is_no_corner_to_hold_a_name_to(spa):
    assert _messages(spa, "Speaker's Corner is a line thing.", "RF") == []
    assert _messages(spa, "Speaker's Corner is a line thing.", "C09") == []


# --------------------------------------------------------------------------
# a trace-numbered venue: labels pass, names are invented
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "T3 costs 0.3 s, brake earlier.",
        "Off at T4 again, brake a touch earlier there.",
        "T2 Hairpin entry is where it goes.",
        "Brake earlier into the hairpin.",
    ],
)
def test_numbered_venue_accepts_labels(fuji, text):
    assert _messages(fuji, text) == []


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("Off again at T4 Dunlop, brake earlier.", "T4 has no mapped name"),
        ("T3 (Coca-Cola Corner) costs time.", "T3 has no mapped name"),
        ("T7 — Campus costs time.", "T7 has no mapped name"),
        ("La Source again.", "not a corner of this circuit"),
    ],
)
def test_numbered_venue_rejects_invented_names(fuji, text, needle):
    issues = _messages(fuji, text)
    assert issues and needle in issues[0], issues


# --------------------------------------------------------------------------
# the Layer-1 findings carry the names placed on the lap
# --------------------------------------------------------------------------
def _spa_corner(label: str, pct: float, name: str | None) -> CornerFinding:
    length = TRACK["header"]["length_m"]
    at = pct * length
    return CornerFinding(
        index=int(label[1:]) - 1,
        label=label,
        distance=at,
        sector=1,
        type="Slow corner",
        min_main=100.0,
        min_ref=100.0,
        net_dt=0.1,
        entry_m=at - 120.0,
        exit_m=at + 120.0,
        name=name,
    )


def test_places_on_a_real_spa_lap_belong_to_the_corner_whose_window_holds_them():
    length = TRACK["header"]["length_m"]
    corners = [
        _spa_corner("T1", 0.0557, "La Source"),
        _spa_corner("T2", 0.3597, "Les Combes"),
        _spa_corner("T4", 0.467, "Speaker's Corner"),
    ]
    places = {p.name: p for p in _places_on_lap(TRACK["corners"], corners, length)}
    assert places["La Source"].corner == "T1"
    assert places["Speaker's Corner"].corner == "T4"
    assert places["Eau Rouge"].corner is None  # flat out: no apex detected there
    assert places["Kemmel"].kind == "straight" and places["Kemmel"].corner is None
    assert places["Les Combes"].corner == "T2"
    assert places["Malmedy"].corner is None  # past the end of T2's window
    assert places["La Source"].start_m < places["La Source"].end_m


def _findings() -> dict:
    corners = [
        {"label": "T1", "name": "La Source", "distance": 380.0, "diags": []},
        {"label": "T2", "name": "Les Combes", "distance": 2490.0, "diags": []},
    ]
    places = [
        {"name": "La Source", "kind": "corner", "start_m": 344, "end_m": 437, "corner": "T1"},
        {"name": "Eau Rouge", "kind": "corner", "start_m": 972, "end_m": 1092, "corner": None},
        {"name": "Malmedy", "kind": "corner", "start_m": 2571, "end_m": 2705, "corner": "T2"},
    ]
    return {"corners": corners, "venue": "spa", "places": places}


def test_check_prose_flags_a_swapped_name_as_a_name_issue():
    texts = [("advice[T2].diagnosis", "T2", "You overslow into La Source.")]
    report = check_prose(texts, _findings())
    assert report.kinds == ["name"]
    assert "which is T1, but this is about T2" in report.issues[0]


def test_check_prose_resolves_a_name_without_a_label_through_the_places():
    ok = [
        ("advice[T2].diagnosis", "T2", "Through Malmedy you're already flat."),
        ("advice[T2].cue[0]", "T2", "Carry speed out of Eau Rouge, then brake for T2."),
        ("plan.headline", None, "La Source and Les Combes are where the lap goes."),
    ]
    assert check_prose(ok, _findings()).issues == []
    wrong = [("advice[T1].diagnosis", "T1", "Through Malmedy you're already flat.")]
    assert "which is T2" in check_prose(wrong, _findings()).issues[0]


def test_the_live_prose_mode_leaves_names_to_the_pitwall_check():
    texts = [("spoken_text", "T2", "You overslow into La Source.")]
    assert check_prose(texts, _findings(), mode="live").issues == []


# --------------------------------------------------------------------------
# the pitwall validator: names the agent was shown are its only names
# --------------------------------------------------------------------------
def _pitwall_facts(names: dict[str, str | None], at: str | None = "T4") -> FactSet:
    facts = FactSet()
    track = {
        "provisional": False,
        "corners": [{"label": k, "name": v, "lap_dist_pct": 0.1} for k, v in names.items()],
    }
    facts.add("race_state", {"track": track})
    if at is not None:
        place = {"label": at, "name": names[at], "lap_dist_pct": 0.1, "provisional": False}
        facts.add("event", {"lap_dist_pct": 0.1, "place": place})
    return facts


def test_validate_output_holds_every_agent_to_the_map():
    facts = _pitwall_facts(SPA)
    assert facts.places["T4"] == "Speaker's Corner" and facts.event_corner == "T4"
    assert validate_output("Off again at Speaker's Corner, brake earlier.", "", facts).ok
    wrong = validate_output("Off again at La Source, brake earlier.", "", facts)
    assert not wrong.ok and not wrong.unsupported
    assert "which is T1, but this is about T4" in wrong.contradictions[0]


def test_the_corner_field_binds_the_spoken_text_and_the_detail_only_its_labels():
    facts = _pitwall_facts(SPA, at=None)
    detail = "Speaker's Corner has three standing calls; the uncoached item is T8 Chicane."
    payload = {"theme": "braking", "corner": "T8", "cue": "Brake earlier into the chicane."}
    assert validate_output("Chicane: brake earlier.", detail, facts, payload).ok
    bad = validate_output("Speaker's Corner: brake earlier.", "", facts, payload)
    assert not bad.ok
    assert not validate_output("Brake earlier.", "", facts, {"corner": "T8 Campus"}).ok


def test_no_map_shown_means_no_names_to_check():
    facts = FactSet()
    facts.add("event", {"lap_dist_pct": 0.48})
    assert validate_output("Speaker's Corner again, carry more speed.", "", facts).ok


def test_a_numbered_map_takes_labels_only():
    facts = _pitwall_facts({f"T{i}": None for i in range(1, 9)})
    assert validate_output("Off at T4 again, brake earlier.", "", facts).ok
    assert not validate_output("Off at T4 Dunlop again, brake earlier.", "", facts).ok
    assert not validate_output("Off at La Source again, brake earlier.", "", facts).ok


# --------------------------------------------------------------------------
# the swapped-name golden cases
# --------------------------------------------------------------------------
NAMED = swapped_name_cases()


def test_swapped_name_cases_cover_both_venues_and_both_corner_agents():
    assert {c.venue for c in NAMED} == {"spa", "numbered"}
    assert {c.agent for c in NAMED} == {"coach", "vehicle_engineer"}


def _score(case, answer):
    facts = agent_facts(SPECS[case.agent], case.case)
    return check_for_agent(case.agent, answer, case.case.state, case.case.event, facts=facts)


@pytest.mark.parametrize("case", NAMED, ids=lambda c: c.case.name + ":" + c.venue)
def test_the_right_name_is_grounded(case):
    assert _score(case, case.faithful)["grounding_ok"], case.describe()


@pytest.mark.parametrize("case", NAMED, ids=lambda c: c.case.name + ":" + c.venue)
def test_a_swapped_name_is_caught(case):
    scored = _score(case, case.swapped)
    assert not scored["grounding_ok"], case.describe()
    assert any(issue.startswith("corner name:") for issue in scored["issues"])
