"""Replay driver: feed a stored or synthetic session through the live engine.

This is the offline development and test path. It deliberately reuses
:meth:`RaceStateEngine.on_frame` -- the *same* entry point the live poller calls
-- so the engine cannot tell replayed frames from live ones. Anything that works
in replay works live, and the determinism test (replay twice, compare the event
log) is therefore a real check on the production code path.

Two sources ship here:

``scenario_source()``
    the scripted synthetic race from :mod:`rtv.racestate.scenario`; needs no
    store, no iRacing and no files -- this is what the tests use.
``store_source()``
    any session already in DuckDB/Parquet, streamed lap by lap so memory stays
    bounded on a full race distance.

Speed is ``1.0`` for real time, ``N`` for N-times real time, and ``0`` (the
default) for as-fast-as-possible.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rtv.catalog.builder import RawHeader, build_catalog
from rtv.catalog.models import Catalog
from rtv.ingest.frame import Frame
from rtv.logging import get_logger
from rtv.racestate.engine import RaceStateEngine
from rtv.racestate.scenario import (
    ScenarioSpec,
    scenario_catalog,
    scenario_frames,
    scenario_session_info,
)
from rtv.store import queries as q

log = get_logger("racestate.replay")

#: Sentinel session id for the built-in scripted race.
SCENARIO_SESSION_ID = "scenario"


@dataclass
class ReplaySource:
    """Everything the engine needs to consume a session, live-shaped."""

    catalog: Catalog
    frames: Iterable[Frame]
    session_id: str | None = None
    session_info: dict[str, Any] | None = None
    total: int | None = None  # frame count when known, for progress reporting


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------
def scenario_source(spec: ScenarioSpec | None = None) -> ReplaySource:
    """The scripted synthetic race -- no store or iRacing required."""
    spec = spec or ScenarioSpec()
    return ReplaySource(
        catalog=scenario_catalog(spec),
        frames=scenario_frames(spec),
        session_id=SCENARIO_SESSION_ID,
        session_info=scenario_session_info(spec),
        total=(spec.laps - 1) * spec.ticks_per_lap
        + int(spec.end_pct * spec.ticks_per_lap),
    )


def catalog_from_store(db, session_id: str, *, flatten_max: int = 6) -> Catalog | None:
    """Rebuild a :class:`Catalog` from the stored ``variable_catalog`` rows.

    The store keeps the header fields verbatim (type, count, unit, desc), so the
    same builder the live/ibt paths use reconstructs an identical catalog.
    """
    rows = db.query_dicts(
        "SELECT name, ir_type, count, unit, descr FROM variable_catalog "
        "WHERE session_id = ? ORDER BY name",
        [session_id],
    )
    if not rows:
        return None
    headers = [
        RawHeader(
            name=r["name"],
            type=int(r["ir_type"]),
            count=int(r["count"] or 1),
            desc=r["descr"] or "",
            unit=r["unit"] or "",
        )
        for r in rows
    ]
    session = db.query_one(
        "SELECT kind, car_id, track_id FROM sessions WHERE session_id = ?", [session_id]
    )
    return build_catalog(
        headers,
        source=(session or {}).get("kind") or "ibt",
        flatten_max=flatten_max,
        car_id=(session or {}).get("car_id"),
        track_id=(session or {}).get("track_id"),
    )


def _column_resolver(catalog: Catalog, row_keys) -> dict[str, str]:
    """Map each catalog column to the key DuckDB actually returned.

    DuckDB identifiers are case-insensitive, so the store's meta ``lap`` column
    and the ``Lap`` telemetry variable collapse into one result key. An exact
    match wins; otherwise we fall back to a case-insensitive match, which is
    what makes ``Lap`` resolve to the ``lap`` column (they carry the same value
    -- the writer populates both from the same frame).
    """
    keys = set(row_keys)
    lower = {k.lower(): k for k in row_keys}
    resolver: dict[str, str] = {}
    for var in catalog.variables.values():
        for col, _ in var.columns:
            if col in keys:
                resolver[col] = col
            elif col.lower() in lower:
                resolver[col] = lower[col.lower()]
    return resolver


def _row_to_values(
    catalog: Catalog, row: dict, resolver: dict[str, str]
) -> dict[str, Any]:
    """Undo the store's column flattening, back to the live frame shape."""
    values: dict[str, Any] = {}
    for var in catalog.variables.values():
        if var.storage.value == "flattened":
            values[var.name] = [
                row.get(resolver.get(col, col)) for col, _ in var.columns
            ]
        else:  # scalar, or a list column that is already a Python list
            col = var.columns[0][0]
            values[var.name] = row.get(resolver.get(col, col))
    return values


def store_source(
    db,
    parquet_dir: Path,
    session_id: str,
    *,
    flatten_max: int = 6,
    session_info: dict | None = None,
) -> ReplaySource:
    """Stream a stored session out of Parquet, one lap partition at a time."""
    catalog = catalog_from_store(db, session_id, flatten_max=flatten_max)
    if catalog is None:
        raise KeyError(f"No stored catalog for session {session_id}")
    session_dir = Path(parquet_dir) / f"session_id={session_id}"
    laps = [
        int(r["lap"])
        for r in db.query_dicts(
            "SELECT lap FROM laps WHERE session_id = ? ORDER BY lap", [session_id]
        )
    ]
    if not laps:
        raise KeyError(f"No laps recorded for session {session_id}")

    def frames() -> Iterator[Frame]:
        resolver: dict[str, str] = {}
        for lap in laps:
            glob = q.lap_glob(session_dir, lap)
            rows = db.query_dicts(
                "SELECT * FROM read_parquet(?, union_by_name=true) ORDER BY tick",
                [glob],
            )
            if rows and not resolver:
                resolver = _column_resolver(catalog, rows[0].keys())
            for row in rows:
                yield Frame(
                    tick=int(row["tick"]),
                    session_time=float(row["session_time"]),
                    lap=int(row["lap"]),
                    values=_row_to_values(catalog, row, resolver),
                    wall_time=None,
                )

    return ReplaySource(
        catalog=catalog,
        frames=frames(),
        session_id=session_id,
        session_info=session_info,
    )


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
@dataclass
class ReplayStatus:
    running: bool = False
    session_id: str | None = None
    speed: float = 0.0
    frames: int = 0
    total: int | None = None
    finished: bool = False
    error: str | None = None

    def to_api(self) -> dict:
        return {
            "running": self.running,
            "session_id": self.session_id,
            "speed": self.speed,
            "frames": self.frames,
            "total": self.total,
            "finished": self.finished,
            "error": self.error,
        }


class ReplayDriver:
    """Pumps a :class:`ReplaySource` into an engine, on this thread or its own."""

    def __init__(self, engine: RaceStateEngine) -> None:
        self._engine = engine
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = ReplayStatus()

    # ---- lifecycle ------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> ReplayStatus:
        with self._lock:
            status = ReplayStatus(**vars(self._status))
            status.running = self.running
            return status

    def start(self, source: ReplaySource, *, speed: float = 0.0) -> None:
        """Begin replaying on a background thread. Idempotent while running."""
        with self._lock:
            if self.running:
                raise RuntimeError("A replay is already running")
            self._stop.clear()
            self._status = ReplayStatus(
                running=True,
                session_id=source.session_id,
                speed=speed,
                total=source.total,
            )
            self._thread = threading.Thread(
                target=self._run_safe,
                args=(source, speed),
                name="rtv-replay",
                daemon=True,
            )
            self._thread.start()
        log.info(
            "Replay started: session=%s speed=%s", source.session_id, speed or "max"
        )

    def stop(self, timeout: float = 5.0) -> None:
        thread = self._thread
        self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._status.running = False
        log.info("Replay stopped.")

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ---- the pump -------------------------------------------------------
    def _run_safe(self, source: ReplaySource, speed: float) -> None:
        try:
            self.run(source, speed=speed, stop_event=self._stop)
        except Exception as exc:  # pragma: no cover - surfaced through status
            log.exception("Replay failed")
            with self._lock:
                self._status.error = str(exc)
        finally:
            with self._lock:
                self._status.running = False
                self._status.finished = True

    def run(
        self,
        source: ReplaySource,
        *,
        speed: float = 0.0,
        stop_event: threading.Event | None = None,
    ) -> int:
        """Drive the engine synchronously. Returns the number of frames fed."""
        engine = self._engine
        engine.bind_catalog(source.catalog, session_id=source.session_id)
        engine.set_session_info(source.session_info)
        engine.state.session.source = "replay"

        n = 0
        first_session_time: float | None = None
        started = time.perf_counter()
        for frame in source.frames:
            if stop_event is not None and stop_event.is_set():
                break
            if speed and speed > 0:
                if first_session_time is None:
                    first_session_time = frame.session_time
                target = (frame.session_time - first_session_time) / speed
                behind = target - (time.perf_counter() - started)
                if behind > 0:
                    if stop_event is not None:
                        stop_event.wait(behind)
                    else:  # pragma: no cover - only used by the threaded path
                        time.sleep(behind)
            engine.on_frame(frame, source.catalog)
            n += 1
            with self._lock:
                self._status.frames = n
        return n


def replay_scenario(
    engine: RaceStateEngine, spec: ScenarioSpec | None = None, *, speed: float = 0.0
) -> int:
    """Convenience: run the scripted race through ``engine`` synchronously."""
    return ReplayDriver(engine).run(scenario_source(spec), speed=speed)
