"""The race-state schema: one versioned :class:`RaceState` plus the event types.

These Pydantic models are the whole public contract of the pitwall's hot loop.
Downstream stages (strategist / vehicle engineer / spotter / coach agents) read
``RaceState`` snapshots and subscribe to ``RaceEvent``s -- they never touch raw
60 Hz frames, exactly as the Layer-1 ``LapFindings`` shield the coach from them.

Grounding rule enforced throughout: a field is ``None`` unless a channel present
in this session's runtime catalog produced it, and :class:`Capabilities` records
which groups were backed by real channels. Nothing is estimated into existence.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Severity(StrEnum):
    INFO = "info"
    ADVISORY = "advisory"
    CRITICAL = "critical"


class EventType(StrEnum):
    """Every event the deterministic detectors can emit (the event catalog)."""

    FLAG_CHANGE = "flag_change"
    SESSION_STATE_CHANGE = "session_state_change"
    LAP_COMPLETED = "lap_completed"
    PERSONAL_BEST = "personal_best"
    SESSION_FASTEST_LAP = "session_fastest_lap"
    LOCKUP = "lockup"
    WHEELSPIN = "wheelspin"
    OFFTRACK = "offtrack"
    INCIDENT = "incident"
    PIT_ENTRY = "pit_entry"
    PIT_EXIT = "pit_exit"
    STINT_START = "stint_start"
    PIT_WINDOW_OPEN = "pit_window_open"
    FUEL_CRITICAL = "fuel_critical"
    BLUE_FLAG = "blue_flag"


class FlagPhase(StrEnum):
    """Coarse race phase distilled from the SessionFlags bitfield."""

    UNKNOWN = "unknown"
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    WHITE = "white"
    CHECKERED = "checkered"


class RaceEvent(BaseModel):
    """A single deterministic observation, timestamped in session time.

    ``state_version`` pins the event to the :class:`RaceState` version that was
    current when it fired, so a consumer can always fetch the exact context.
    Deliberately contains no wall-clock field: replaying the same session twice
    must produce a byte-identical event sequence.
    """

    event_type: EventType
    tick: int
    session_time: float
    lap: int | None = None
    severity: Severity = Severity.INFO
    payload: dict[str, Any] = Field(default_factory=dict)
    state_version: int = 0

    @property
    def key(self) -> str:
        """Stable discriminator used by tests/smoke: ``flag_change:yellow``."""
        if self.event_type is EventType.FLAG_CHANGE:
            return f"{self.event_type.value}:{self.payload.get('to')}"
        return self.event_type.value

    def to_api(self) -> dict[str, Any]:
        return {**self.model_dump(mode="json"), "key": self.key}


class Capabilities(BaseModel):
    """Which state groups this session's catalog can actually support."""

    session: bool = False
    flags: bool = False
    standings: bool = False
    player: bool = False
    fuel: bool = False
    tyres: bool = False
    car_health: bool = False
    conditions: bool = False
    pit: bool = False
    # detector-level capabilities
    lockup: bool = False
    wheelspin: bool = False
    offtrack: bool = False
    incidents: bool = False
    #: Channels the engine looked for and did not find, for operator diagnosis.
    missing: list[str] = Field(default_factory=list)


class SessionSummary(BaseModel):
    session_id: str | None = None
    source: str = "live"  # "live" | "replay"
    state: str | None = None  # decoded SessionState label
    time_remaining: float | None = None
    laps_remaining: int | None = None
    laps_total: int | None = None
    car_id: str | None = None
    track_id: str | None = None
    track_name: str | None = None
    lap_length_m: float | None = None


class FlagState(BaseModel):
    raw: int | None = None
    phase: FlagPhase = FlagPhase.UNKNOWN
    active: list[str] = Field(default_factory=list)
    #: Session time at which the current phase began.
    since_session_time: float | None = None
    time_in_state: float = 0.0


class CarState(BaseModel):
    """One entry in the standings, derived from the per-car-index arrays."""

    idx: int
    is_player: bool = False
    position: int | None = None
    class_position: int | None = None
    lap: int | None = None
    lap_dist_pct: float | None = None
    #: lap + lap_dist_pct -- the single number the running order sorts on.
    laps_completed: float | None = None
    last_lap_time: float | None = None
    best_lap_time: float | None = None
    on_pit_road: bool | None = None
    track_surface: str | None = None
    #: Seconds to the car ahead / behind in the running order (None if the gap
    #: basis is unknown -- see :attr:`StandingsState.gap_basis`).
    gap_ahead: float | None = None
    gap_behind: float | None = None
    #: Signed seconds relative to the player (+ = ahead of the player).
    gap_to_player: float | None = None


class StandingsState(BaseModel):
    cars: list[CarState] = Field(default_factory=list)
    player_idx: int | None = None
    #: How gaps were converted from lap-distance deltas to seconds:
    #: ``"lap_length_speed"`` (delta_pct * lap_length / speed) or
    #: ``"lap_time_pct"`` (delta_pct * a known lap time). ``None`` => gaps unknown.
    gap_basis: str | None = None
    lap_length_m: float | None = None
    reference_lap_time: float | None = None


class PlayerState(BaseModel):
    lap: int | None = None
    position: int | None = None
    class_position: int | None = None
    lap_dist_pct: float | None = None
    speed: float | None = None
    gear: int | None = None
    rpm: float | None = None
    last_lap_time: float | None = None
    best_lap_time: float | None = None
    on_pit_road: bool | None = None
    track_surface: str | None = None
    incidents: int | None = None
    #: 1-based; incremented on every pit exit.
    stint: int = 1
    #: Green-flag laps completed since the current stint began.
    laps_on_tyres: int = 0
    stint_start_lap: int | None = None


class FuelState(BaseModel):
    level: float | None = None
    level_pct: float | None = None
    capacity: float | None = None  # derived: level / level_pct
    #: Rolling mean/std of consumption over the last N *green* laps.
    per_lap: float | None = None
    per_lap_std: float | None = None
    samples: int = 0
    laps_remaining: float | None = None  # level / per_lap
    laps_to_finish: float | None = None  # race laps still to run
    fuel_to_finish: float | None = None
    margin_l: float | None = None  # level - fuel_to_finish
    margin_laps: float | None = None  # laps_remaining - laps_to_finish
    pit_window_earliest_lap: int | None = None
    pit_window_latest_lap: int | None = None
    window_open: bool = False


class TyreState(BaseModel):
    #: Per corner (LF/RF/LR/RR); missing corners are simply absent from the dict.
    temps: dict[str, float] = Field(default_factory=dict)
    pressures: dict[str, float] = Field(default_factory=dict)
    #: Per-corner least-squares slope of the per-lap mean temperature across the
    #: current stint, in degrees per lap. Needs >= 2 completed stint laps.
    temp_trend: dict[str, float] = Field(default_factory=dict)
    pressure_trend: dict[str, float] = Field(default_factory=dict)
    stint_laps: int = 0


class CarHealthState(BaseModel):
    oil_temp: float | None = None
    water_temp: float | None = None
    oil_pressure: float | None = None
    fuel_pressure: float | None = None
    voltage: float | None = None
    tow_time: float | None = None
    engine_warnings: list[str] = Field(default_factory=list)


class ConditionsState(BaseModel):
    air_temp: float | None = None
    track_temp: float | None = None
    air_density: float | None = None
    air_pressure: float | None = None
    humidity: float | None = None
    wind_vel: float | None = None
    wind_dir: float | None = None
    track_wetness: float | None = None
    #: Degrees per minute, least-squares over the trend window (None until enough
    #: samples have accumulated).
    air_temp_trend: float | None = None
    track_temp_trend: float | None = None


class EngineMetrics(BaseModel):
    """Self-timing so the hot loop's cost is observable, not assumed."""

    frames: int = 0
    events: int = 0
    last_update_ms: float = 0.0
    avg_update_ms: float = 0.0
    max_update_ms: float = 0.0
    dropped_frames: int = 0


class RaceState(BaseModel):
    """The single continuously-updated object the pitwall reasons over."""

    version: int = 0
    tick: int = 0
    session_time: float = 0.0
    wall_time: float | None = None
    session: SessionSummary = Field(default_factory=SessionSummary)
    flags: FlagState = Field(default_factory=FlagState)
    standings: StandingsState = Field(default_factory=StandingsState)
    player: PlayerState = Field(default_factory=PlayerState)
    fuel: FuelState = Field(default_factory=FuelState)
    tyres: TyreState = Field(default_factory=TyreState)
    car_health: CarHealthState = Field(default_factory=CarHealthState)
    conditions: ConditionsState = Field(default_factory=ConditionsState)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    metrics: EngineMetrics = Field(default_factory=EngineMetrics)

    def to_api(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
