"""The strategist's golden set — built from the scripted race, not hand-written.

The Layer-3 coach's eval set is a JSON file of real session/lap ids, because a lap
of telemetry cannot be written by hand. A *race state* can be: replaying the
scripted synthetic race produces an exact, reproducible sequence of triggering
events with the exact state that was current at each one. So the golden set is
generated, and every case carries its own ground truth.

Three degraded cases are appended on purpose. An eval set made only of races where
everything is known cannot tell a grounded strategist from a confident one; the
cases with no fuel model, no gaps and a caution running are where the difference
shows up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rtv.pitwall.agents.strategist import STRATEGIST
from rtv.pitwall.framework import AgentRuntime
from rtv.racestate import RaceStateEngine, replay_scenario
from rtv.racestate.detectors import DEFAULT_CONFIG
from rtv.racestate.models import RaceEvent, RaceState
from rtv.racestate.scenario import ScenarioSpec

#: A milestone every 2 green laps so the scripted 6-lap race actually reaches one.
EVAL_DETECTOR_CONFIG = DEFAULT_CONFIG.__class__(
    **{**DEFAULT_CONFIG.__dict__, "stint_milestone_laps": 2}
)


@dataclass
class EvalCase:
    """One (triggering event, race state) pair the strategist must answer."""

    name: str
    event: RaceEvent
    state: RaceState
    #: Recent events, so the stint-history tool has something to rebuild from.
    events: list[RaceEvent] = field(default_factory=list)
    note: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trigger": self.event.key,
            "lap": self.event.lap,
            "note": self.note,
            "fuel_per_lap": self.state.fuel.per_lap,
            "margin_laps": self.state.fuel.margin_laps,
            "window": [
                self.state.fuel.pit_window_earliest_lap,
                self.state.fuel.pit_window_latest_lap,
            ],
            "flags": self.state.flags.phase.value,
            "gap_basis": self.state.standings.gap_basis,
        }


def _trigger_types() -> set[str]:
    return {t.event_type for t in STRATEGIST.triggers}


def scenario_cases(spec: ScenarioSpec | None = None) -> list[EvalCase]:
    """Replay the scripted race and snapshot the state at each strategist trigger.

    The engine is driven frame by frame rather than through
    :func:`replay_scenario` so a *consistent* snapshot can be taken at the exact
    tick each event fired -- taking it afterwards would score the agent against a
    state it never saw.
    """
    from rtv.racestate.scenario import scenario_catalog, scenario_frames, scenario_session_info

    spec = spec or ScenarioSpec()
    catalog = scenario_catalog(spec)
    engine = RaceStateEngine(source="replay", config=EVAL_DETECTOR_CONFIG)
    engine.bind_catalog(catalog, session_id="eval-scenario")
    engine.set_session_info(scenario_session_info(spec))

    wanted = _trigger_types()
    runtime = AgentRuntime(STRATEGIST, provider=None)
    cases: list[EvalCase] = []
    seen = 0

    for frame in scenario_frames(spec):
        before = len(engine.bus.history())
        engine.on_frame(frame, catalog)
        new = engine.bus.history()[before:]
        if not new:
            continue
        state = engine.snapshot()
        for event in new:
            if event.event_type.value not in wanted:
                continue
            if runtime.match(event, state) is None:
                continue  # predicate or cooldown said no; the live path agrees
            runtime.arm(runtime.match(event, state) or STRATEGIST.triggers[0], event)
            seen += 1
            cases.append(
                EvalCase(
                    name=f"scenario-{seen:02d}-{event.key}",
                    event=event,
                    state=state,
                    events=list(engine.bus.history(200)),
                    note="scripted race",
                )
            )
    return cases


def degraded_cases() -> list[EvalCase]:
    """States where the honest answer is 'I don't have the numbers'."""
    base = _end_of_scenario_state()
    out: list[EvalCase] = []

    # 1) No fuel channels at all -> no pit call is possible.
    no_fuel = base.model_copy(deep=True)
    no_fuel.capabilities.fuel = False
    no_fuel.fuel = no_fuel.fuel.__class__()
    out.append(
        EvalCase(
            name="degraded-no-fuel-model",
            event=_synthetic_event(no_fuel, "stint_lap_milestone"),
            state=no_fuel,
            note="No fuel channels: the only honest recommendation is 'hold'.",
        )
    )

    # 2) Fuel known, gaps unknown -> a stop can be called, a rejoin cannot.
    no_gaps = base.model_copy(deep=True)
    no_gaps.standings.gap_basis = None
    for car in no_gaps.standings.cars:
        car.gap_ahead = car.gap_behind = car.gap_to_player = None
    no_gaps.fuel.window_open = True
    no_gaps.fuel.margin_laps = -1.5
    out.append(
        EvalCase(
            name="degraded-no-gap-basis",
            event=_synthetic_event(no_gaps, "pit_window_open"),
            state=no_gaps,
            note="No gap basis: a stop lap is defensible, a rejoin position is not.",
        )
    )

    # 3) A caution while a stop is still owed -> the cheap stop is now.
    yellow = base.model_copy(deep=True)
    yellow.flags.phase = yellow.flags.phase.__class__.YELLOW
    yellow.flags.active = ["yellow", "caution"]
    yellow.fuel.margin_laps = -2.0
    yellow.fuel.window_open = True
    yellow.fuel.pit_window_earliest_lap = yellow.player.lap
    yellow.fuel.pit_window_latest_lap = (yellow.player.lap or 0) + 2
    out.append(
        EvalCase(
            name="degraded-yellow-opportunity",
            event=_synthetic_event(yellow, "flag_change", {"from": "green", "to": "yellow"}),
            state=yellow,
            note="Caution out with a stop still owed: an opportunistic stop is right.",
        )
    )

    # 4) The car we are racing stops while we are comfortably fuelled. Nobody
    #    else pits in the scripted race, so this trigger only exists here.
    rival = base.model_copy(deep=True)
    rival.fuel.margin_laps = 2.5
    rival.fuel.window_open = False
    ahead = next((c for c in rival.standings.cars if not c.is_player), None)
    out.append(
        EvalCase(
            name="degraded-rival-pitted-no-need",
            event=_synthetic_event(
                rival,
                "rival_pitted",
                {
                    "car_idx": ahead.idx if ahead else 1,
                    "position": ahead.position if ahead else 1,
                    "player_position": rival.player.position,
                    "gap_to_player": ahead.gap_to_player if ahead else None,
                    "lap": rival.player.lap,
                },
            ),
            state=rival,
            note="A rival stops but we do not need fuel: covering them is the wrong reflex.",
        )
    )
    return out


def _end_of_scenario_state() -> RaceState:
    engine = RaceStateEngine(source="replay", config=EVAL_DETECTOR_CONFIG)
    replay_scenario(engine, ScenarioSpec())
    return engine.snapshot()


def _synthetic_event(
    state: RaceState, event_type: str, payload: dict | None = None
) -> RaceEvent:
    from rtv.racestate.models import EventType, Severity

    return RaceEvent(
        event_type=EventType(event_type),
        tick=state.tick,
        session_time=state.session_time,
        lap=state.player.lap,
        severity=Severity.ADVISORY,
        payload=payload or {"lap": state.player.lap},
        state_version=state.version,
    )


def build_cases(spec: ScenarioSpec | None = None) -> list[EvalCase]:
    """The full golden set: the scripted race plus the degraded states."""
    return scenario_cases(spec) + degraded_cases()
