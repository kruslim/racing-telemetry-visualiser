"""TelemetryWriter — the concrete :class:`~rtv.ingest.sink.TelemetrySink`.

Writes high-rate telemetry as per-session/per-lap Parquet files and records
metadata in DuckDB. Lap partitioning means chart queries prune to one or two
small files instead of scanning a whole session.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

from rtv.catalog.models import Catalog
from rtv.domain.models import Lap, Session, SessionInfoSnapshot
from rtv.logging import get_logger
from rtv.store.duck import Database

log = get_logger("store.writer")


def _ts(epoch: float | None) -> datetime | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)


class TelemetryWriter:
    def __init__(self, db: Database, parquet_dir: Path) -> None:
        self._db = db
        self._parquet_dir = parquet_dir
        self._parquet_dir.mkdir(parents=True, exist_ok=True)
        self._counters: dict[str, count] = defaultdict(lambda: count())
        self._lock = threading.Lock()

    # ---- sink interface -------------------------------------------------
    def begin_session(self, session: Session, catalog: Catalog) -> None:
        self._db.execute(
            """
            INSERT OR REPLACE INTO sessions
              (session_id, kind, source_path, car_id, track_id, track_name,
               ir_session_id, ir_subsession_id, schema_hash, started_at,
               ended_at, sample_count, poll_hz, status, label)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                session.session_id,
                session.kind.value,
                session.source_path,
                session.car_id,
                session.track_id,
                session.track_name,
                session.ir_session_id,
                session.ir_subsession_id,
                session.schema_hash,
                _ts(session.started_at),
                _ts(session.ended_at),
                session.sample_count,
                session.poll_hz,
                session.status,
                session.label,
            ],
        )
        self._write_catalog(session.session_id, catalog)
        (self._parquet_dir / f"session_id={session.session_id}").mkdir(
            parents=True, exist_ok=True
        )

    def write_telemetry(self, session_id: str, table) -> None:
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq

        if table.num_rows == 0:
            return
        laps = table.column("lap").to_pylist()
        unique_laps = sorted(set(laps))
        for lap in unique_laps:
            mask = pc.equal(table.column("lap"), pa.scalar(lap, type=pa.int32()))
            sub = table.filter(mask)
            lap_dir = (
                self._parquet_dir / f"session_id={session_id}" / f"lap={int(lap)}"
            )
            lap_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                seq = next(self._counters[session_id])
            pq.write_table(
                sub, lap_dir / f"part-{seq:06d}.parquet", compression="zstd"
            )

    def write_session_info(self, snapshot: SessionInfoSnapshot) -> None:
        self._db.execute(
            """
            INSERT OR REPLACE INTO session_info_snapshots
              (session_id, update_seq, captured_tick, captured_at, info_json)
            VALUES (?,?,?,?,?)
            """,
            [
                snapshot.session_id,
                snapshot.update_seq,
                snapshot.captured_tick,
                _ts(snapshot.captured_at),
                json.dumps(snapshot.info, default=str),
            ],
        )

    def write_laps(self, session_id: str, laps: list[Lap]) -> None:
        for lap in laps:
            self._db.execute(
                """
                INSERT OR REPLACE INTO laps
                  (session_id, lap, start_tick, end_tick, start_session_time,
                   lap_time, is_valid, is_out_lap, is_in_lap)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                [
                    session_id,
                    lap.lap,
                    lap.start_tick,
                    lap.end_tick,
                    lap.start_session_time,
                    lap.lap_time,
                    lap.is_valid,
                    lap.is_out_lap,
                    lap.is_in_lap,
                ],
            )

    def end_session(self, session_id: str, *, sample_count: int, ended_at: float) -> None:
        # Only bump sample_count if a positive value is supplied (live passes 0).
        if sample_count > 0:
            self._db.execute(
                "UPDATE sessions SET status='ready', ended_at=?, sample_count=? "
                "WHERE session_id=?",
                [_ts(ended_at), sample_count, session_id],
            )
        else:
            self._db.execute(
                "UPDATE sessions SET status='ready', ended_at=? WHERE session_id=?",
                [_ts(ended_at), session_id],
            )

    # ---- helpers --------------------------------------------------------
    def _write_catalog(self, session_id: str, catalog: Catalog) -> None:
        for var in catalog.variables.values():
            self._db.execute(
                """
                INSERT OR REPLACE INTO variable_catalog
                  (session_id, name, ir_type, type_label, count, unit, descr,
                   storage, decoder, columns)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    session_id,
                    var.name,
                    int(var.ir_type),
                    var.type_label,
                    var.count,
                    var.unit,
                    var.desc,
                    var.storage.value,
                    var.decoder,
                    json.dumps([c for c, _ in var.columns]),
                ],
            )
