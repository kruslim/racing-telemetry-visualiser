"""Structured findings produced by the deterministic feature extractor.

These dataclasses are the contract every coaching surface consumes. They are small
(~20 corner findings, a few KB serialized) — deliberately LLM-sized — and they double
as eval ground truth: an LLM claim like "brake 7 m earlier into T4" can be checked
against the matching :class:`Diagnostic` here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# The deterministic analysts that attribute a Diagnostic (no LLM in any of them).
AGENTS = ("speed", "brake", "throttle", "gear", "grip", "consistency")

#: Display identity for every analyst a UI shows, in roster order. ``txt`` (the
#: turn-by-turn walkthrough) and ``sector`` summarise rather than diagnose, so
#: they are in the roster but never on a Diagnostic.
AGENT_ROSTER: dict[str, dict[str, str]] = {
    "txt": {"name": "Turn-by-Turn", "code": "TXT", "watch": "Corner-by-corner walkthrough"},
    "sector": {"name": "Sector", "code": "SEC", "watch": "S1 / S2 / S3 split times"},
    "brake": {"name": "Brake", "code": "BRK", "watch": "Brake point, trail, lock-up"},
    "speed": {"name": "Speed / Line", "code": "SPD", "watch": "Min corner speed, apex"},
    "throttle": {"name": "Throttle", "code": "THR", "watch": "Application, full-throttle %"},
    "gear": {"name": "Gearing", "code": "GER", "watch": "Gear selection at apex"},
    "grip": {"name": "Grip / Tyres", "code": "GRP", "watch": "Lateral grip held"},
    "consistency": {"name": "Consistency", "code": "CNS", "watch": "Lap-to-lap spread"},
}


@dataclass
class Diagnostic:
    """A single observation pinned to a distance, attributed to one analyst 'agent'."""

    agent: str  # one of AGENTS
    text: str  # human-readable, e.g. "Brake point 9 m early"
    distance: float  # metres into the lap where it is pinned
    magnitude: float  # severity weight used to apportion lost time
    good: bool = False  # True when the main lap is *better* than reference here
    time_loss: float = 0.0  # seconds apportioned to this diagnostic (0 when good/variance)
    lockup: bool = False  # brake lock-up flag
    variance: bool = False  # stint-consistency finding (not a single-lap loss)


@dataclass
class CornerFinding:
    index: int
    label: str  # "T1", "T2", ...
    distance: float  # apex distance (m)
    sector: int  # 1, 2 or 3
    type: str  # Hairpin / Slow corner / Medium corner / Fast corner
    min_main: float  # minimum speed through the corner, km/h
    min_ref: float  # reference minimum speed, km/h
    net_dt: float  # seconds; + = main lost time across this corner
    diags: list[Diagnostic] = field(default_factory=list)
    entry_m: float = 0.0  # start of the window net_dt is measured over (m)
    exit_m: float = 0.0  # end of that window (m)


@dataclass
class SectorDelta:
    index: int
    name: str  # "S1" / "S2" / "S3"
    main: float | None  # main sector time (s)
    ref: float | None  # reference sector time (s)
    delta: float | None  # main - ref (s); + = slower
    start_m: float = 0.0  # sector start distance (m)
    end_m: float = 0.0  # sector end distance (m)


@dataclass
class Priority:
    index: int  # corner index
    label: str
    gain: float  # seconds available if this corner is fixed
    why: str  # the dominant reason (top diagnostic text)


@dataclass
class ChiefSummary:
    lost: float  # total time available across losing corners (s)
    net_lap: float  # net lap delta main - ref (s)
    losing_n: int  # how many corners lose time
    total: int  # total corners analysed
    top3: list[Priority]
    time_spread: float | None  # lap-time spread across the stint (s), if available


@dataclass
class DriverKPIs:
    """Whole-lap driving-style scores for one lap. ``None`` = channel not captured."""

    trail_brake_pct: float | None  # share of braking samples taken while turning
    avg_throttle_pct: float | None  # mean throttle application over the lap
    throttle_smoothness: float | None  # 0-100, inverse of pedal jitter per km
    steer_corrections: int | None  # steering sign reversals (mid-corner corrections)
    coasting_pct: float | None  # share of the lap with neither pedal applied


@dataclass
class AgentStat:
    count: int  # findings this analyst produced
    time: float  # seconds of loss attributed to them


@dataclass
class LapFindings:
    session_id: str
    car_id: str | None
    track_id: str | None
    track_name: str | None
    main_lap: int
    ref_lap: int
    main_time: float | None
    ref_time: float | None
    lap_length_m: float
    corners: list[CornerFinding]
    sectors: list[SectorDelta]
    chief: ChiefSummary
    notes: list[str] = field(default_factory=list)  # caveats, e.g. skipped diagnostics
    kpis: dict[str, DriverKPIs] = field(default_factory=dict)  # "main" / "ref"
    agent_stats: dict[str, AgentStat] = field(default_factory=dict)  # keyed by roster id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
