"""The pitwall golden set — built from the scripted race, not hand-written.

The Layer-3 coach's eval set is a JSON file of real session/lap ids, because a lap
of telemetry cannot be written by hand. A *race state* can be: replaying the
scripted synthetic race produces an exact, reproducible sequence of triggering
events with the exact state that was current at each one. So the golden set is
generated, and every case carries its own ground truth.

Degraded cases are appended on purpose. An eval set made only of races where
everything is known cannot tell a grounded agent from a confident one; the cases
with no fuel model, no gaps, no tyre channels and no stored telemetry are where
the difference shows up.

Stage 3 generalised this from one agent to four. The generator takes an
:class:`~rtv.pitwall.framework.AgentSpec` and filters with that agent's own
``match()``, so a case exists exactly when the live path would have woken that
agent -- there is no second, hand-maintained list of triggers to drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from rtv.pitwall.agents.coach import COACH
from rtv.pitwall.agents.spotter import SPOTTER
from rtv.pitwall.agents.strategist import STRATEGIST
from rtv.pitwall.agents.vehicle_engineer import VEHICLE_ENGINEER
from rtv.pitwall.framework import AgentRuntime, AgentSpec
from rtv.racestate import RaceStateEngine, replay_scenario
from rtv.racestate.detectors import DEFAULT_CONFIG
from rtv.racestate.models import EventType, RaceEvent, RaceState, Severity
from rtv.racestate.scenario import ScenarioSpec

#: A milestone every 2 green laps so the scripted 6-lap race actually reaches one.
EVAL_DETECTOR_CONFIG = replace(DEFAULT_CONFIG, stint_milestone_laps=2)

#: The role agents need patterns, not one-offs: lock up at the same corner on
#: laps 1, 2 and 4 (lap 3 is the caution, so it cannot), and lower the tyre-drift
#: threshold under the scenario's scripted 2 C/lap climb. Both are *config*, so
#: the ground-truth scenario and every stage-1/2 test are untouched.
ROLE_DETECTOR_CONFIG = replace(EVAL_DETECTOR_CONFIG, tyre_temp_trend_c_per_lap=1.0)
#: ``corner_pcts`` scripts a real corner just after the braking zone, so the
#: Layer-1 corner detector has something to find when a store is attached.
ROLE_SCENARIO = ScenarioSpec(lockup_laps=(1, 2), corner_pcts=(0.44,))


@dataclass
class EvalCase:
    """One (triggering event, race state) pair an agent must answer."""

    name: str
    event: RaceEvent
    state: RaceState
    #: Recent events, so the stint-history and detector-log tools have something
    #: to rebuild from.
    events: list[RaceEvent] = field(default_factory=list)
    note: str = ""
    #: Which agent this case is for. Drives the checks applied to the answer.
    agent: str = "strategist"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "agent": self.agent,
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


# --------------------------------------------------------------------------
# generated from the scripted race
# --------------------------------------------------------------------------
def scenario_cases(
    spec: ScenarioSpec | None = None,
    *,
    agent: AgentSpec = STRATEGIST,
    config=None,
) -> list[EvalCase]:
    """Replay the scripted race and snapshot the state at each of ``agent``'s triggers.

    The engine is driven frame by frame rather than through
    :func:`replay_scenario` so a *consistent* snapshot can be taken at the exact
    tick each event fired -- taking it afterwards would score the agent against a
    state it never saw.
    """
    from rtv.racestate.scenario import scenario_catalog, scenario_frames, scenario_session_info

    spec = spec or ScenarioSpec()
    catalog = scenario_catalog(spec)
    engine = RaceStateEngine(source="replay", config=config or EVAL_DETECTOR_CONFIG)
    engine.bind_catalog(catalog, session_id="eval-scenario")
    engine.set_session_info(scenario_session_info(spec))

    wanted = {t.event_type for t in agent.triggers}
    runtime = AgentRuntime(agent, provider=None)
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
            trigger = runtime.match(event, state)
            if trigger is None:
                continue  # predicate or cooldown said no; the live path agrees
            runtime.arm(trigger, event)
            seen += 1
            cases.append(
                EvalCase(
                    name=f"scenario-{agent.name}-{seen:02d}-{event.key}",
                    event=event,
                    state=state,
                    events=list(engine.bus.history(200)),
                    note="scripted race",
                    agent=agent.name,
                )
            )
    return cases


def _end_of_scenario_state(config=None, spec: ScenarioSpec | None = None) -> RaceState:
    engine = RaceStateEngine(source="replay", config=config or EVAL_DETECTOR_CONFIG)
    replay_scenario(engine, spec or ScenarioSpec())
    return engine.snapshot()


def _scenario_events(config=None, spec: ScenarioSpec | None = None) -> list[RaceEvent]:
    engine = RaceStateEngine(source="replay", config=config or ROLE_DETECTOR_CONFIG)
    replay_scenario(engine, spec or ROLE_SCENARIO)
    return engine.bus.history(200)


def _synthetic_event(
    state: RaceState,
    event_type: str,
    payload: dict | None = None,
    severity: Severity = Severity.ADVISORY,
) -> RaceEvent:
    return RaceEvent(
        event_type=EventType(event_type),
        tick=state.tick,
        session_time=state.session_time,
        lap=state.player.lap,
        severity=severity,
        payload=payload or {"lap": state.player.lap},
        state_version=state.version,
    )


# --------------------------------------------------------------------------
# the strategist's degraded states (stage 2, unchanged)
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# stage 3: the role agents' degraded states
# --------------------------------------------------------------------------
def engineer_degraded_cases() -> list[EvalCase]:
    """The car is unwell, or the data that would say so is missing."""
    from rtv.racestate.detectors import detect_car_health

    base = _end_of_scenario_state(ROLE_DETECTOR_CONFIG, ROLE_SCENARIO)
    events = _scenario_events()
    out: list[EvalCase] = []

    hot = base.model_copy(deep=True)
    hot.car_health.oil_temp = 138.4
    payload = detect_car_health(hot.car_health, ROLE_DETECTOR_CONFIG)
    out.append(
        EvalCase(
            name="engineer-oil-over-threshold",
            event=_synthetic_event(hot, "car_health_warning", payload),
            state=hot,
            events=events,
            note="Oil is over the configured limit: an advisory, and it is our judgement.",
            agent=VEHICLE_ENGINEER.name,
        )
    )

    warned = base.model_copy(deep=True)
    warned.car_health.water_temp = 108.0
    warned.car_health.engine_warnings = ["water_temp_warning"]
    payload = detect_car_health(warned.car_health, ROLE_DETECTOR_CONFIG)
    out.append(
        EvalCase(
            name="engineer-engine-warning-light",
            event=_synthetic_event(
                warned, "car_health_warning", payload, Severity.CRITICAL
            ),
            state=warned,
            events=events,
            note="The sim's own warning bit: the car is saying it, not us inferring it.",
            agent=VEHICLE_ENGINEER.name,
        )
    )

    blind = base.model_copy(deep=True)
    blind.capabilities.tyres = False
    blind.tyres = blind.tyres.__class__()
    recurrence = next(
        (e for e in events if e.event_type is EventType.RECURRING_ISSUE), None
    )
    out.append(
        EvalCase(
            name="engineer-no-tyre-channels",
            event=(
                recurrence
                or _synthetic_event(
                    blind, "recurring_issue", {"issue": "lockup", "corner": "C08"}
                )
            ),
            state=blind,
            events=events,
            note="Repeated lockups but no tyre channels: describe it, never quote a temperature.",
            agent=VEHICLE_ENGINEER.name,
        )
    )
    return out


def spotter_degraded_cases() -> list[EvalCase]:
    """Traffic, blue flags and an incident -- including one with no gaps at all."""
    base = _end_of_scenario_state(ROLE_DETECTOR_CONFIG, ROLE_SCENARIO)
    events = _scenario_events()
    out: list[EvalCase] = []

    behind = next(
        (
            c
            for c in base.standings.cars
            if not c.is_player and c.gap_to_player is not None and c.gap_to_player < 0
        ),
        None,
    )
    closing = base.model_copy(deep=True)
    out.append(
        EvalCase(
            name="spotter-car-closing",
            event=_synthetic_event(
                closing,
                "traffic_close",
                {
                    "car_idx": behind.idx if behind else 1,
                    "gap": abs(behind.gap_to_player) if behind else 0.3,
                    "side": "behind",
                    "closing_rate_s_per_s": 0.31,
                    "position": behind.position if behind else 6,
                    "threshold_gap_s": ROLE_DETECTOR_CONFIG.traffic_gap_s,
                },
            ),
            state=closing,
            events=events,
            note="A car behind, close and closing: one short call, no lateral claims.",
            agent=SPOTTER.name,
        )
    )

    blue = base.model_copy(deep=True)
    out.append(
        EvalCase(
            name="spotter-blue-flag",
            event=_synthetic_event(
                blue,
                "blue_flag",
                {
                    "car_idx": behind.idx if behind else 1,
                    "gap": 1.2,
                    "position": 1,
                    "laps_ahead": 1.04,
                },
            ),
            state=blue,
            events=events,
            note="Lapped traffic closing: tell the driver it is there, do not order a move.",
            agent=SPOTTER.name,
        )
    )

    no_gaps = base.model_copy(deep=True)
    no_gaps.standings.gap_basis = None
    for car in no_gaps.standings.cars:
        car.gap_ahead = car.gap_behind = car.gap_to_player = None
    out.append(
        EvalCase(
            name="spotter-no-gap-basis",
            event=_synthetic_event(
                no_gaps, "incident", {"incidents": 2, "delta": 1, "lap_dist_pct": 0.31},
                Severity.CRITICAL,
            ),
            state=no_gaps,
            events=events,
            note="An incident with no gap basis: the threat is real, the seconds are not.",
            agent=SPOTTER.name,
        )
    )
    return out


def coach_degraded_cases() -> list[EvalCase]:
    """A repeated mistake with no stored trace, and an off under a caution."""
    base = _end_of_scenario_state(ROLE_DETECTOR_CONFIG, ROLE_SCENARIO)
    events = _scenario_events()
    out: list[EvalCase] = []

    recurrence = next(
        (e for e in events if e.event_type is EventType.RECURRING_ISSUE), None
    )
    if recurrence is not None:
        out.append(
            EvalCase(
                name="coach-recurrence-without-telemetry",
                event=recurrence,
                state=base.model_copy(deep=True),
                events=events,
                note=(
                    "Three lockups at one corner but nothing recorded: coach the "
                    "pattern, and do not claim a brake-point number."
                ),
                agent=COACH.name,
            )
        )

    off = base.model_copy(deep=True)
    off.player.track_surface = "off_track"
    out.append(
        EvalCase(
            name="coach-offtrack",
            event=_synthetic_event(
                off,
                "offtrack",
                {
                    "surface": "off_track",
                    "from": "on_track",
                    "lap_dist_pct": 0.72,
                    "speed": 41.2,
                },
            ),
            state=off,
            events=events,
            note="One off: one cue for that corner, nothing else.",
            agent=COACH.name,
        )
    )
    return out


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------
def build_cases(spec: ScenarioSpec | None = None) -> list[EvalCase]:
    """The strategist's golden set: the scripted race plus the degraded states."""
    return scenario_cases(spec) + degraded_cases()


#: agent name -> the spec, its scenario config, and its degraded-case builder.
ROLE_BUILDERS = {
    VEHICLE_ENGINEER.name: (VEHICLE_ENGINEER, engineer_degraded_cases),
    SPOTTER.name: (SPOTTER, spotter_degraded_cases),
    COACH.name: (COACH, coach_degraded_cases),
}


def build_agent_cases(agent: str) -> list[EvalCase]:
    """The golden set for one agent by name."""
    if agent == STRATEGIST.name:
        return build_cases()
    if agent not in ROLE_BUILDERS:
        raise KeyError(f"No eval cases are defined for agent {agent!r}")
    spec, degraded = ROLE_BUILDERS[agent]
    return (
        scenario_cases(ROLE_SCENARIO, agent=spec, config=ROLE_DETECTOR_CONFIG)
        + degraded()
    )


def build_all_cases() -> dict[str, list[EvalCase]]:
    """Every agent's golden set, keyed by agent name."""
    names = [STRATEGIST.name, *ROLE_BUILDERS]
    return {name: build_agent_cases(name) for name in names}
