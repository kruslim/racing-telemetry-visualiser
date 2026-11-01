"""Task A3: fewer refusals for numbers the agents derive, and readable audit values.

On the A1 Spa replay most refusals were not inventions but arithmetic or units: an
off-track speed in m/s said as km/h, "three laps running", a pressure rise since the
stint started, "8 km/h down, 130 versus 139". These tests pin the fixes: the agent's
view of an event (km/h speeds, streak counts), the derived tool fields, the
difference-of-two-facts rule, spelled-out counts, unit conversion within a family,
the `_target_pct` recursion and the rounded audit values. Fully offline.
"""

from __future__ import annotations

import pytest

from rtv.desktop.model.activity import reduce, round_figures
from rtv.desktop.model.review import audit_rows
from rtv.grounding.units import field_unit
from rtv.pitwall.framework import ToolContext
from rtv.pitwall.tools import TOOLS_BY_NAME, _target_pct
from rtv.pitwall.validator import FactSet, validate_output
from rtv.racestate import strategy_math as sm
from rtv.racestate.derived import agent_payload, axle_split, consecutive_run, lap_time_deltas
from rtv.racestate.models import EventType, RaceEvent, RaceState, Severity

# The real Spa A1 replay-2 payloads that were refused.
OFFTRACK = {
    "surface": "off_track",
    "from": "on_track",
    "lap_dist_pct": 0.9794633984565735,
    "speed": 27.74091339111328,
}
RECURRING = {
    "issue": "offtrack",
    "corner": "C09",
    "occurrences": 3,
    "threshold": 3,
    "window_laps": 5,
    "laps": [5, 6, 7],
    "first_lap": 5,
    "last_lap": 7,
    "lap_dist_pct_from": 0.4801,
    "lap_dist_pct_to": 0.4809,
    "mean_speed": 42.894,
}
CORNER = {"telemetry": {"corner": {"min_speed_kmh": 130.81, "ref_min_speed_kmh": 139.02}}}


def _facts(*sources: tuple[str, object]) -> FactSet:
    facts = FactSet(typed=True)
    for name, payload in sources:
        facts.add(name, payload)
    return facts


def _event(event_type: EventType, payload: dict, lap: int = 7) -> RaceEvent:
    return RaceEvent(
        event_type=event_type,
        tick=1,
        session_time=100.0,
        lap=lap,
        severity=Severity.ADVISORY,
        payload=payload,
    )


# --------------------------------------------------------------------------
# 1. speeds and units
# --------------------------------------------------------------------------
def test_the_agent_sees_an_event_speed_in_kmh_only():
    view = agent_payload(OFFTRACK)
    assert "speed" not in view
    assert view["speed_kmh"] == pytest.approx(99.9)
    assert agent_payload(RECURRING)["mean_speed_kmh"] == pytest.approx(154.4)
    assert field_unit("event.speed_kmh") == ("speed", "kmh")
    # The engine's own payload is untouched.
    assert OFFTRACK["speed"] == pytest.approx(27.7409, abs=1e-3)


def test_the_m_s_speed_said_as_kmh_is_no_longer_offered():
    """A1's three refusals: 27.7 (m/s) said as "27.7 km/h". Now the agent has 99.9 km/h."""
    facts = _facts(("event", agent_payload(OFFTRACK)))
    assert validate_output("You went off at 100 km/h.", "", facts).ok
    assert not validate_output("You went off at 27.7 km/h.", "", facts).ok


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        ({"speed": 27.74}, "off at 99.9 km/h"),  # m/s -> km/h
        ({"speed_kmh": 99.9}, "off at 27.7 m/s"),  # km/h -> m/s
        ({"pressures": {"LF": 190.3}}, "left front at 27.6 psi"),  # kPa -> psi
        ({"tyre_pressure_psi": 27.6}, "left front at 190 kPa"),  # psi -> kPa
        ({"oil_temp_c": 100.0}, "oil at 212 °F"),  # °C -> °F
    ],
)
def test_a_figure_converts_within_its_unit_family(payload, text):
    assert validate_output(text, "", _facts(("tool", payload))).ok


def test_a_wrong_unit_explains_the_unit_the_fact_is_in():
    facts = _facts(("race_state", {"player": {"speed": 27.74}}))
    report = validate_output("Off at 27.7 km/h.", "", facts)
    assert not report.ok
    assert "race_state.player.speed in m/s" in report.unsupported_detail[0]
    assert "99.86 km/h" in report.unsupported_detail[0]


# --------------------------------------------------------------------------
# 2. derived tool fields
# --------------------------------------------------------------------------
def test_streak_counts_are_derived_from_the_laps():
    assert consecutive_run([5, 6, 7]) == 3
    assert consecutive_run([2, 5, 6, 7]) == 3
    assert consecutive_run([4, 6]) == 1
    assert consecutive_run([]) == 0
    view = agent_payload(RECURRING)
    assert (view["consecutive_laps"], view["laps_spanned"], view["laps_with_issue"]) == (3, 3, 3)
    assert field_unit("event.consecutive_laps") == ("laps", "laps")
    facts = _facts(("event", view))
    assert validate_output("C09 again, that's three laps in a row off there.", "", facts).ok


def test_the_tyre_tool_returns_changes_and_axle_splits():
    state = RaceState()
    state.capabilities.tyres = True
    t = state.tyres
    t.temps = {"LF": 80.0, "RF": 84.0, "LR": 76.0, "RR": 78.0}
    t.pressures = {"LF": 182.0, "RF": 185.0, "LR": 176.0, "RR": 177.0}
    t.pressure_trend = {"LF": 1.8}
    t.pressure_change_since_stint_start = {"LF": 17.25, "RF": 18.0}
    t.pressure_change_vs_previous_lap = {"LF": 1.5}
    out = sm.tyre_trend(state)
    assert out["since_stint_start"]["pressure_change_kpa"]["LF"] == 17.25
    assert out["vs_previous_lap"]["pressure_change_kpa"]["LF"] == 1.5
    assert out["axles"]["pressures"]["front_minus_rear_kpa"] == 7.0
    assert out["axles"]["temps"]["left_minus_right_c"] == -3.0
    assert out["axles"]["temps"]["spread_c"] == 8.0
    facts = _facts(("get_tyre_trend", out))
    assert validate_output("Left front's up 17 kPa since the stint started.", "", facts).ok
    assert validate_output("Fronts are 7 kPa over the rears.", "", facts).ok
    assert validate_output("Fronts are 7 kPa over the rears.", "", FactSet(typed=True)).ok is False


def test_the_engine_records_tyre_changes_per_lap():
    from rtv.racestate.engine import _corner_change

    laps = [(1, {"LF": 170.0}), (2, {"LF": 173.5}), (3, {"LF": 176.0})]
    assert _corner_change(laps, 0) == {"LF": 6.0}
    assert _corner_change(laps, -2) == {"LF": 2.5}
    assert _corner_change(laps[:1], 0) == {}


def test_axle_split_needs_both_sides():
    assert axle_split({"LF": 80.0}, "c") == {"spread_c": 0.0, "highest": "LF", "lowest": "LF"}
    assert axle_split({}, "c") == {}


def test_lap_time_deltas_compare_the_last_lap():
    laps = [
        {"lap": 1, "lap_time_s": 145.867, "green": True},
        {"lap": 2, "lap_time_s": 143.967, "green": True},
        {"lap": 3, "lap_time_s": 141.75, "green": True},
    ]
    out = lap_time_deltas(laps, best=141.75)
    assert out["delta_vs_previous_lap_s"] == pytest.approx(-2.217)
    assert out["delta_vs_stint_first_lap_s"] == pytest.approx(-4.117)
    assert out["delta_vs_best_lap_s"] == 0.0
    assert out["green_streak_laps"] == 3
    assert lap_time_deltas([], None) == {}


def test_the_strategist_tools_carry_the_race_distance():
    state = RaceState()
    state.player.lap = 17
    state.session.laps_total = 18
    assert sm.fuel_projection(state)["race_total_laps"] == 18
    facts = _facts(("get_fuel_projection", sm.fuel_projection(state)))
    assert validate_output("Lap 17 of 18 laps, bring it home.", "", facts).ok
    assert validate_output("That's 17/18.", "", facts).ok


def test_stint_history_has_lap_deltas():
    events = [
        _event(EventType.LAP_COMPLETED, {"lap": 1, "lap_time": 145.0, "green": True}),
        _event(EventType.LAP_COMPLETED, {"lap": 2, "lap_time": 143.5, "green": True}),
    ]
    out = sm.stint_history(RaceState(), events)
    assert out["current_stint_laps"]["delta_vs_previous_lap_s"] == pytest.approx(-1.5)


def test_the_detector_tool_rows_are_the_agent_view():
    ctx = ToolContext(
        state=RaceState(), event=_event(EventType.OFFTRACK, OFFTRACK), events=[
            _event(EventType.OFFTRACK, OFFTRACK, lap=5)
        ], config={}
    )
    out = TOOLS_BY_NAME["get_recent_detector_events"](ctx, {})
    assert out["events"][0]["payload"]["speed_kmh"] == pytest.approx(99.9)


# --------------------------------------------------------------------------
# 3. the difference of two facts, in the same sentence
# --------------------------------------------------------------------------
def test_a_difference_of_two_facts_said_together_is_grounded():
    """The A1 engineer refusal: 'Minimum speed is 8 km/h down, 130 versus 139.'"""
    facts = _facts(("get_corner_detail", CORNER))
    assert validate_output("Minimum speed is 8 km/h down, 130 versus 139.", "", facts).ok


def test_a_difference_needs_both_terms_in_its_sentence():
    facts = _facts(("get_corner_detail", CORNER))
    assert not validate_output("8 km/h down.", "", facts).ok
    assert not validate_output("Minimum is 130 km/h. You lost 8 km/h.", "", facts).ok


def test_a_difference_must_be_the_right_difference():
    facts = _facts(("get_corner_detail", CORNER))
    assert not validate_output("12 km/h down, 130 versus 139.", "", facts).ok


def test_a_difference_respects_the_unit_family():
    facts = _facts(("t", {"pressures": {"LF": 182.0, "LR": 165.0}, "laps_remaining": 17.0}))
    assert validate_output("Left front 182 against 165 kPa, up 17 kPa.", "", facts).ok
    # 17 laps of fuel is not a pressure difference.
    facts = _facts(("t", {"pressures": {"LF": 182.0, "LR": 165.0}}))
    assert not validate_output("182 against 165 kPa, so 17 laps.", "", facts).ok


def test_decimals_do_not_split_sentences():
    facts = _facts(("get_corner_detail", CORNER))
    assert validate_output("8.2 km/h down at 130.81 versus 139.02.", "", facts).ok


# --------------------------------------------------------------------------
# F3's two false positives: a spelled-out count of occurrences said as laps
# --------------------------------------------------------------------------
def test_three_laps_running_wide_for_three_occurrences():
    """evals/real/spa_run coach-4: the recorded payload, before any derived field."""
    facts = _facts(("event", RECURRING))
    assert validate_output("Speaker's Corner again - three laps running wide there.", "", facts).ok


def test_second_off_in_two_laps_for_a_corner_count_of_two():
    """evals/real/spa_run coach-3: get_corner_detail live.count is 2."""
    facts = _facts(("get_corner_detail", {"live": {"corner": "C09", "count": 2}}))
    assert validate_output("Second off-track in two laps at Speaker's Corner.", "", facts).ok


def test_a_count_never_grounds_a_digit_lap_quantity():
    facts = _facts(("event", RECURRING))
    assert not validate_output("3 laps of fuel left.", "", facts).ok
    assert not validate_output("Four laps running.", "", facts).ok


# --------------------------------------------------------------------------
# 5. _target_pct no longer recurses
# --------------------------------------------------------------------------
def test_an_event_corner_that_matches_no_bucket_does_not_recurse():
    event = _event(EventType.RECURRING_ISSUE, {"issue": "offtrack", "corner": "Z99"})
    ctx = ToolContext(state=RaceState(), event=event, events=[], config={})
    assert _target_pct(ctx, {}) is None
    assert _target_pct(ctx, {"corner": "nowhere"}) is None
    out = TOOLS_BY_NAME["get_corner_detail"](ctx, {})
    assert out["requested_lap_dist_pct"] is None


def test_an_unmatched_place_label_falls_back_to_the_lap_distance():
    event = _event(
        EventType.OFFTRACK,
        {"place": {"label": "T99", "provisional": False}, "lap_dist_pct": 0.25, "corner": "Z1"},
    )
    ctx = ToolContext(state=RaceState(), event=event, events=[], config={})
    assert _target_pct(ctx, {}) == pytest.approx(0.25)


# --------------------------------------------------------------------------
# 4. the audit shows rounded values and the validator's reasons
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("withheld 4.66, 4.6610630306531675", "withheld 4.66, 4.66"),
        ("1:32.418 and 0.9674", "1:32.418 and 0.97"),
        ("2.5 laps, 12", "2.5 laps, 12"),
        ("-0.0001 s", "0 s"),
        (None, ""),
    ],
)
def test_round_figures(text, expected):
    assert round_figures(text) == expected


def _refused_run() -> list[dict]:
    base = {"agent": "coach", "run_id": "coach-1", "event_key": "offtrack", "session_time": 1.0}
    return [
        {**base, "seq": 1, "kind": "run_start", "data": {"model": "kimi-k3"}},
        {
            **base,
            "seq": 2,
            "kind": "validate",
            "data": {
                "ok": False,
                "unsupported": ["4.6610630306531675"],
                "unsupported_detail": ["4.6610630306531675 (no figure in the data matches)"],
            },
        },
        {**base, "seq": 3, "kind": "run_end", "data": {"outcome": "refused"}},
    ]


def test_the_review_audit_rounds_and_keeps_the_reasons():
    (row,) = audit_rows(_refused_run())
    assert row["unsupported"] == ["4.66"]
    assert row["reasons"] == ["4.66 (no figure in the data matches)"]


def test_the_live_audit_rounds_and_keeps_the_reasons():
    model = reduce(_refused_run())
    (validation,) = model.runs["coach-1"].validations
    assert validation["unsupported"] == ["4.66"]
    assert validation["reasons"] == ["4.66 (no figure in the data matches)"]
