"""Deterministic race-state engine: the LLM-free hot loop of the v2 pitwall.

Consumes the same 60 Hz frame stream the live poller already produces (or a
replayed session) and maintains one continuously-updated :class:`RaceState`,
publishing typed :class:`RaceEvent`s on an :class:`EventBus` that the later
agent stages subscribe to. There are no LLM calls anywhere in this package --
every number it reports is traceable to a channel in the runtime catalog.
"""

from rtv.racestate.bus import EventBus, Subscription
from rtv.racestate.detectors import DetectorConfig
from rtv.racestate.engine import RaceStateEngine
from rtv.racestate.models import (
    Capabilities,
    CarState,
    EventType,
    FlagPhase,
    FuelState,
    PlayerState,
    RaceEvent,
    RaceState,
    Severity,
    StandingsState,
    TyreState,
)
from rtv.racestate.replay import (
    ReplayDriver,
    ReplaySource,
    replay_scenario,
    scenario_source,
    store_source,
)
from rtv.racestate.scenario import ScenarioSpec, scenario_catalog, scenario_frames
from rtv.racestate.strategy_math import (
    DEFAULT_PIT_LANE_LOSS_S,
    PitOutcome,
    fuel_projection,
    simulate_pit_outcome,
    standings_around_player,
    stint_history,
    tyre_trend,
)

__all__ = [
    "DEFAULT_PIT_LANE_LOSS_S",
    "Capabilities",
    "CarState",
    "DetectorConfig",
    "EventBus",
    "EventType",
    "FlagPhase",
    "FuelState",
    "PitOutcome",
    "PlayerState",
    "RaceEvent",
    "RaceState",
    "RaceStateEngine",
    "ReplayDriver",
    "ReplaySource",
    "ScenarioSpec",
    "Severity",
    "StandingsState",
    "Subscription",
    "TyreState",
    "fuel_projection",
    "replay_scenario",
    "scenario_catalog",
    "scenario_frames",
    "scenario_source",
    "simulate_pit_outcome",
    "standings_around_player",
    "stint_history",
    "store_source",
    "tyre_trend",
]
