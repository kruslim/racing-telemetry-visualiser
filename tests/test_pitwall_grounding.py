"""Typed grounding (task A1): a figure said with a unit must match a fact of that unit.

The old check accepted any spoken number that was near *any* number the agent had been
shown, so a tyre pressure could ground "laps of fuel". These tests pin the typed check:
units read from prose, units inferred from engine field paths, the bare-number fallback,
the rollback flag, and one swapped-fact golden case per agent that the old check let
through. Fully offline.
"""

from __future__ import annotations

import pytest
from evals.pitwall_cases import agent_facts, swapped_fact_cases
from evals.pitwall_checks import check_for_agent

from rtv.grounding.units import convert, field_unit, read_figures
from rtv.pitwall.agents import AGENT_REGISTRY
from rtv.pitwall.validator import FactSet, validate_output

SPECS = {spec.name: spec for spec in AGENT_REGISTRY}


def _read(text: str) -> list[tuple[float, str | None]]:
    return [(f.value, f.family) for f in read_figures(text)]


# --------------------------------------------------------------------------
# reading figures and their units
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("6 laps of fuel", [(6.0, "laps")]),
        ("Box on lap 12", [(12.0, "lap")]),
        ("L12 then 6 more laps", [(12.0, "lap"), (6.0, "laps")]),
        ("Rejoin P4, 2.4s behind", [(4.0, "position"), (2.4, "s")]),
        ("Best is a 1:32.4", [(92.4, "s")]),
        ("Fronts at 28 psi and 85°C", [(28.0, "pressure"), (85.0, "temp")]),
        ("Off at 95 percent", [(95.0, "percent")]),
        ("Down 10 km/h at the apex", [(10.0, "speed")]),
        ("41.7 litres in the tank", [(41.7, "litres")]),
        ("Lose 2 places", [(2.0, "position")]),
        ("Fourth place, third gear", [(4.0, "position"), (3.0, "gear")]),
        ("2.5 a lap", [(2.5, None)]),
        ("T7 and C08", []),
    ],
)
def test_figures_are_read_with_the_unit_next_to_them(text, expected):
    assert _read(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Three laps left", [(3.0, "laps")]),
        ("two seconds behind", [(2.0, "s")]),
        ("twenty laps to go", [(20.0, "laps")]),
        ("twenty-one laps", [(21.0, "laps")]),
        ("two point four seconds", [(2.4, "s")]),
        ("a lap and a half of fuel", [(1.5, "laps")]),
        ("two and a half laps", [(2.5, "laps")]),
        ("half a second", [(0.5, "s")]),
        ("three tenths up", [(3.0, "s")]),
        ("a couple of laps", []),
        ("nothing to report", []),
    ],
)
def test_spelled_out_numbers_are_figures(text, expected):
    assert _read(text) == expected


def test_tenths_are_seconds_in_their_own_unit():
    (fig,) = read_figures("three tenths")
    assert (fig.family, fig.unit) == ("s", "tenth")
    assert convert(0.3, "s", "s", "tenth") == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("race_state.fuel.laps_remaining", ("laps", "laps")),
        ("race_state.fuel.per_lap", ("litres", "l")),
        ("race_state.fuel.pit_window_earliest_lap", ("lap", "lap")),
        ("race_state.fuel.level_pct", ("percent", "fraction")),
        ("race_state.tyres.pressures.LF", ("pressure", "kpa")),
        ("race_state.tyres.temps.RR", ("temp", "c")),
        ("race_state.player.speed", ("speed", "ms")),
        ("race_state.player.position", ("position", "pos")),
        ("race_state.player.best_lap_time", ("s", "s")),
        ("race_state.car_health.oil_pressure", ("pressure", "bar")),
        ("get_standings_around_player.cars[2].gap_to_player_s", ("s", "s")),
        ("simulate_pit_outcome.rejoin_position", ("position", "pos")),
        ("simulate_pit_outcome.fuel_to_add_l", ("litres", "l")),
        ("event.laps[1]", ("lap", "lap")),
        ("race_state.player.stint", None),
        ("race_state.version", None),
    ],
)
def test_engine_fields_know_their_unit(path, expected):
    assert field_unit(path) == expected


# --------------------------------------------------------------------------
# the typed check
# --------------------------------------------------------------------------
def _facts(**sources) -> FactSet:
    facts = FactSet(typed=True)
    for name, payload in sources.items():
        facts.add(name, payload)
    return facts


def test_a_tyre_pressure_cannot_ground_laps_of_fuel():
    facts = _facts(
        race_state={"fuel": {"laps_remaining": 6.2}, "tyres": {"pressures": {"LF": 18.9}}}
    )
    assert validate_output("About 6 laps of fuel.", "", facts).ok
    report = validate_output("About 19 laps of fuel.", "", facts)
    assert not report.ok
    assert report.unsupported == ["19"]
    assert "laps" in report.unsupported_detail[0]
    assert "pressures" in report.unsupported_detail[0]


def test_a_lap_number_is_not_a_count_of_laps():
    facts = _facts(race_state={"fuel": {"pit_window_earliest_lap": 14, "laps_remaining": 6.2}})
    assert validate_output("Window opens lap 14.", "", facts).ok
    assert not validate_output("14 laps of fuel left.", "", facts).ok


def test_a_unit_is_converted_before_it_is_compared():
    facts = _facts(race_state={"tyres": {"pressures": {"LF": 190.3}}, "player": {"speed": 41.2}})
    assert validate_output("Left front 27.6 psi.", "", facts).ok
    assert validate_output("Left front 28 psi.", "", facts).ok
    assert validate_output("You were doing 148 km/h.", "", facts).ok
    assert not validate_output("You were doing 41 km/h.", "", facts).ok


def test_small_integers_are_only_free_when_bare():
    facts = _facts(race_state={"fuel": {"laps_remaining": 6.2}})
    assert validate_output("Two things: hold it.", "", facts).ok
    assert validate_output("Box in 2.", "", facts).ok
    assert not validate_output("Box in 2 laps.", "", facts).ok
    assert not validate_output("Two laps of fuel.", "", facts).ok


def test_a_bare_number_keeps_the_looser_any_fact_match():
    facts = _facts(race_state={"tyres": {"pressures": {"LF": 18.9}}})
    assert validate_output("We're at 18.9 now.", "", facts).ok
    assert not validate_output("We're at 44 now.", "", facts).ok


def test_spelled_out_figures_are_checked():
    facts = _facts(race_state={"fuel": {"laps_remaining": 6.2}, "player": {"position": 9}})
    assert validate_output("Six laps of fuel.", "", facts).ok
    assert not validate_output("Nine laps of fuel.", "", facts).ok
    assert validate_output("A couple of laps, then box.", "", facts).ok


def test_a_detector_finding_takes_its_unit_from_the_finding():
    facts = _facts(event={"measure": "oil_temp", "value": 138.4, "threshold": 130.0, "unit": "C"})
    assert validate_output("Oil is 138 degrees, limit 130.", "", facts).ok
    assert not validate_output("Oil is 138 laps.", "", facts).ok


def test_strings_in_tool_results_carry_their_units():
    facts = _facts(sim={"assumptions": ["Pit-lane loss 25.0s."]})
    assert validate_output("Stop costs us 25 seconds.", "", facts).ok
    assert not validate_output("Stop costs us 25 laps.", "", facts).ok


def test_structured_fields_are_still_matched_exactly():
    facts = _facts(outcome={"rejoin_position": 8, "tyre_pressure": 18.9})
    assert not validate_output("", "", facts, payload={"rejoin_position": 19}).ok
    assert validate_output("", "", facts, payload={"rejoin_position": 8}).ok


def test_the_flag_off_restores_the_any_number_check():
    facts = FactSet(typed=False)
    facts.add("race_state", {"fuel": {"laps_remaining": 6.2}, "tyres": {"pressures": {"LF": 18.9}}})
    report = validate_output("About 19 laps of fuel.", "", facts)
    assert report.ok
    assert report.typed is False


def test_the_flag_defaults_on_and_reads_the_setting(monkeypatch):
    from rtv.config import get_settings

    assert FactSet().typed is True
    monkeypatch.setenv("RTV_PITWALL_TYPED_GROUNDING", "false")
    get_settings.cache_clear()
    try:
        assert FactSet().typed is False
    finally:
        monkeypatch.delenv("RTV_PITWALL_TYPED_GROUNDING")
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# the swapped-fact golden cases, one per agent
# --------------------------------------------------------------------------
SWAPPED = swapped_fact_cases()


def test_every_agent_has_a_swapped_fact_case():
    assert {c.agent for c in SWAPPED} == set(SPECS)


def _score(case, answer, *, typed):
    facts = agent_facts(SPECS[case.agent], case.case, typed=typed)
    return check_for_agent(case.agent, answer, case.case.state, case.case.event, facts=facts)


@pytest.mark.parametrize("case", SWAPPED, ids=lambda c: c.agent)
def test_the_right_fact_is_grounded(case):
    assert _score(case, case.faithful, typed=True)["grounding_ok"], case.faithful


@pytest.mark.parametrize("case", SWAPPED, ids=lambda c: c.agent)
def test_a_swapped_fact_is_caught(case):
    scored = _score(case, case.swapped, typed=True)
    assert not scored["grounding_ok"], case.describe()


@pytest.mark.parametrize("case", SWAPPED, ids=lambda c: c.agent)
def test_the_any_number_check_let_the_swap_through(case):
    """Why A1 exists: the old check grounds the swapped number on the wrong fact."""
    assert _score(case, case.swapped, typed=False)["grounding_ok"], case.describe()
