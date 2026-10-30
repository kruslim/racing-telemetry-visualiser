"""Hermetic fixtures — the foundation of an offline, deterministic eval suite.

Each fixture resolves to a :class:`~rtv.coaching.agent.provider.FindingsProvider` the coach
graph can run against with **no DuckDB, no store, no disk, no network** — the same role
canopy's ``readers.fixtures.build_fixture`` plays. A synthetic provider returns hand-authored
``LapFindings``-shaped dicts (the style already used by ``tests/test_evals.py``), so a seeded
case produces byte-identical findings every run and "the coach must pick T4" has ground truth
to check against.

``build_store_fixture`` is the one belt-and-braces integration path: it pushes synthetic laps
through the *real* ``features.py`` pipeline via a temp store, proving the graph flows end to
end. The synthetic-findings fixtures are what the regression suite actually runs on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SyntheticFindingsProvider:
    """A ``FindingsProvider`` backed by a canned findings dict — fully offline.

    ``findings`` is a ``LapFindings.to_dict()``-shaped payload. ``channels`` is the captured
    channel list a refusal cites. Set ``session_exists=False`` to model a missing session
    (every lookup raises ``KeyError``, as the live provider would).
    """

    findings: dict[str, Any]
    channels: list[str] = field(default_factory=list)
    session_exists: bool = True
    session_id: str = "synthetic"

    def list_sessions(self, limit: int = 20) -> list[dict]:
        if not self.session_exists:
            return []
        return [{"session_id": self.session_id, "track_name": "Synthetic Circuit"}]

    def list_laps(self, session_id: str) -> list[dict]:
        if not self.session_exists:
            raise KeyError(f"Unknown session {session_id}")
        return [
            {"lap": self.findings.get("ref_lap", 3), "lap_time": 90.0, "is_valid": True},
            {"lap": self.findings.get("main_lap", 5), "lap_time": 90.4, "is_valid": True},
        ]

    def lap_findings(self, session_id: str, main_lap: int, ref_lap: int) -> dict:
        if not self.session_exists:
            raise KeyError(f"Unknown session {session_id}")
        return self.findings

    def available_channels(self, session_id: str) -> list[str]:
        if not self.session_exists:
            raise KeyError(f"Unknown session {session_id}")
        return list(self.channels)

    def compare_channel(
        self, session_id: str, name: str, laps: list[int], grid: int = 200
    ) -> dict:
        if not self.session_exists:
            raise KeyError(f"Unknown session {session_id}")
        if name not in self.channels:
            raise KeyError(f"Unknown channels: [{name!r}]")
        return {"session_id": session_id, "channel": name, "grid": grid, "laps": {}}


# The channels a normal road-course session captures — deliberately WITHOUT tyre temperatures,
# so a "what were my tyre temps?" question has a real absence to refuse against.
_ROAD_CHANNELS = ["Speed", "Throttle", "Brake", "Gear", "RPM", "LatAccel", "LapDist"]


def _corner(index: int, label: str, net_dt: float, *, diags: list[dict] | None = None) -> dict:
    """A CornerFinding-shaped dict. Min speeds are derived so the gap tracks net_dt."""
    return {
        "index": index,
        "label": label,
        "distance": 200.0 * (index + 1),
        "sector": 1 + index % 3,
        "type": "Slow corner",
        "min_main": 100.0 - 10.0 * net_dt,
        "min_ref": 100.0,
        "net_dt": round(net_dt, 3),
        "diags": diags or [],
    }


def _chief(corners: list[dict]) -> dict:
    losing = sorted(
        (c for c in corners if c["net_dt"] > 0.02), key=lambda c: c["net_dt"], reverse=True
    )
    top3 = [
        {"index": c["index"], "label": c["label"], "gain": c["net_dt"], "why": "time available"}
        for c in losing[:3]
    ]
    return {
        "lost": round(sum(c["net_dt"] for c in losing), 3),
        "net_lap": round(sum(c["net_dt"] for c in corners), 3),
        "losing_n": len(losing),
        "total": len(corners),
        "top3": top3,
        "time_spread": None,
    }


def _findings(corners: list[dict], *, session_id: str = "synthetic") -> dict:
    return {
        "session_id": session_id,
        "car_id": "synthetic-car",
        "track_id": "synthetic-track",
        "track_name": "Synthetic Circuit",
        "main_lap": 5,
        "ref_lap": 3,
        "main_time": 90.4,
        "ref_time": 90.0,
        "lap_length_m": 4000.0,
        "corners": corners,
        "sectors": [],
        "chief": _chief(corners),
        "notes": [],
    }


_BRAKE_DIAG = {
    "agent": "brake",
    "text": "Brake point 9 m early",
    "distance": 210.0,
    "magnitude": 1.0,
    "good": False,
    "time_loss": 0.18,
    "lockup": False,
    "variance": False,
}


def _build(name: str) -> SyntheticFindingsProvider:
    if name == "clean_lap":
        # A healthy lap: nothing meaningfully lost — the coach must NOT invent priorities.
        corners = [_corner(0, "T1", 0.0), _corner(1, "T2", -0.01), _corner(2, "T3", 0.01)]
        return SyntheticFindingsProvider(_findings(corners), channels=_ROAD_CHANNELS)

    if name == "t4_brake_loss":
        # One dominant loss with a brake diagnostic — the known-anomaly analog. The coach must
        # pick T4 and cite its net_dt.
        corners = [
            _corner(0, "T1", 0.05),
            _corner(1, "T4", 0.30, diags=[_BRAKE_DIAG]),
            _corner(2, "T7", 0.08),
        ]
        return SyntheticFindingsProvider(_findings(corners), channels=_ROAD_CHANNELS)

    if name == "multi_corner":
        corners = [
            _corner(0, "T1", 0.12),
            _corner(1, "T2", 0.25),
            _corner(2, "T3", 0.40),
            _corner(3, "T4", 0.07),
        ]
        return SyntheticFindingsProvider(_findings(corners), channels=_ROAD_CHANNELS)

    if name == "no_tyre_temp":
        # Findings exist, but tyre temperatures were never captured — a tyre-temp question must
        # refuse (channel_not_captured), not invent a number.
        corners = [_corner(0, "T4", 0.30, diags=[_BRAKE_DIAG])]
        return SyntheticFindingsProvider(_findings(corners), channels=_ROAD_CHANNELS)

    if name == "missing_session":
        # The session does not exist: every lookup raises. A review request must refuse.
        return SyntheticFindingsProvider(
            _findings([_corner(0, "T1", 0.1)]), channels=[], session_exists=False
        )

    raise KeyError(f"Unknown fixture {name!r}. Known: {', '.join(FIXTURES)}.")


FIXTURES: tuple[str, ...] = (
    "clean_lap",
    "t4_brake_loss",
    "multi_corner",
    "no_tyre_temp",
    "missing_session",
)


def build_fixture(name: str) -> SyntheticFindingsProvider:
    """Resolve a named synthetic fixture to an offline ``FindingsProvider``."""
    return _build(name)


def build_store_fixture(tmp_path: Any) -> Any:
    """A store-backed provider over synthetic laps pushed through the REAL feature pipeline.

    Mirrors the store-round-trip fixture in ``tests/test_coaching_features.py``: a slower
    'main' lap (lap 1) and quicker reference laps are written through the actual
    ``TelemetryWriter``/``LapTracker``, then read back via ``Repository`` and wrapped in
    ``RepositoryFindingsProvider``. Used by a single integration smoke test — the
    synthetic-findings fixtures above are what the regression suite runs on. Returns
    ``(provider, session_id, main_lap, ref_lap)``.
    """
    import math
    import time

    from tests.helpers import synthetic_catalog

    from rtv.coaching.agent.provider import RepositoryFindingsProvider
    from rtv.domain.models import Session, SessionKind
    from rtv.ingest.frame import FrameBuffer
    from rtv.ingest.normalize import LapTracker
    from rtv.store.duck import Database
    from rtv.store.repository import Repository
    from rtv.store.writer import TelemetryWriter

    lap_len, ticks_per_lap = 5000.0, 300
    ddist = lap_len / ticks_per_lap
    v_hi, apex, width = 70.0, 0.75, 0.03
    # (corner_min_speed m/s, brake_onset_pct): lap 1 is the slower 'main' lap that brakes later.
    lap_specs = [(17.0, 0.72), (20.0, 0.70), (20.0, 0.70)]

    def values(lap: int, i: int, vmin: float, brake_onset: float) -> dict:
        pct = i / ticks_per_lap
        speed = v_hi - (v_hi - vmin) * math.exp(-(((pct - apex) / width) ** 2))
        return {
            "SessionTime": 0.0,
            "SessionTick": 0,
            "Lap": lap,
            "LapDist": pct * lap_len,
            "LapDistPct": pct,
            "Speed": speed,
            "RPM": 6000.0,
            "Throttle": 1.0 if (pct < brake_onset or pct > 0.80) else 0.0,
            "Brake": 1.0 if brake_onset <= pct < 0.76 else 0.0,
            "Gear": 3,
            "Lat": 52.0 + 0.01 * math.sin(pct * 2 * math.pi),
            "Lon": -1.0 + 0.01 * math.cos(pct * 2 * math.pi),
            "OnPitRoad": False,
            "LapLastLapTime": 0.0,
            "SessionState": 4,
            "SessionFlags": 0x4,
            "TyrePressure": [165.0, 165.5, 164.0, 164.5],
            "CarIdxLapDistPct": [pct] + [0.0] * 63,
        }

    catalog = synthetic_catalog()
    db = Database(tmp_path / "telemetry.duckdb")
    parquet_dir = tmp_path / "parquet"
    writer = TelemetryWriter(db, parquet_dir)
    session_id = "smoke-session"
    writer.begin_session(
        Session(
            session_id=session_id, kind=SessionKind.IBT, car_id="ir18", track_id="spa",
            track_name="Spa", schema_hash=catalog.schema_hash, started_at=time.time(),
        ),
        catalog,
    )
    tracker = LapTracker(session_id)
    buf = FrameBuffer(catalog)
    tick = 0
    t = 0.0
    for lap, (vmin, onset) in enumerate(lap_specs, start=1):
        for i in range(ticks_per_lap):
            v = values(lap, i, vmin, onset)
            v["SessionTime"], v["SessionTick"] = t, tick
            tracker.update(v, tick=tick, session_time=t)
            buf.append(v, tick=tick, session_time=t, lap=lap)
            t += ddist / max(v["Speed"], 0.1)
            tick += 1
    tracker.finalize(tick=tick - 1, session_time=t)
    writer.write_telemetry(session_id, buf.to_arrow())
    writer.write_laps(session_id, tracker.laps)
    writer.end_session(session_id, sample_count=tick, ended_at=time.time())

    repo = Repository(db, parquet_dir)
    return RepositoryFindingsProvider(repo), session_id, 1, 2
