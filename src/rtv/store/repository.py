"""High-level read API over the store, used by the HTTP layer."""

from __future__ import annotations

import glob as globmod
import json
from pathlib import Path
from typing import Any

from rtv.catalog.enums import BITFIELD_TABLES, ENUM_TABLES
from rtv.ingest.frame import META_COLUMNS
from rtv.logging import get_logger
from rtv.store import queries as q
from rtv.store.duck import Database

log = get_logger("store.repository")


class Repository:
    def __init__(self, db: Database, parquet_dir: Path) -> None:
        self._db = db
        self._parquet_dir = parquet_dir

    def session_dir(self, session_id: str) -> Path:
        return self._parquet_dir / f"session_id={session_id}"

    # ---- sessions -------------------------------------------------------
    def list_sessions(
        self,
        *,
        kind: str | None = None,
        car_id: str | None = None,
        track_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if car_id:
            clauses.append("car_id = ?")
            params.append(car_id)
        if track_id:
            clauses.append("track_id = ?")
            params.append(track_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        return self._db.query_dicts(
            f"SELECT * FROM sessions {where} "
            "ORDER BY started_at DESC NULLS LAST LIMIT ? OFFSET ?",
            params,
        )

    def get_session(self, session_id: str) -> dict | None:
        return self._db.query_one(
            "SELECT * FROM sessions WHERE session_id = ?", [session_id]
        )

    # ---- catalog --------------------------------------------------------
    def get_catalog(self, session_id: str) -> dict | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        rows = self._db.query_dicts(
            "SELECT name, ir_type, type_label, count, unit, descr, storage, "
            "decoder, columns FROM variable_catalog WHERE session_id = ? "
            "ORDER BY name",
            [session_id],
        )
        return {
            "schema_hash": session.get("schema_hash"),
            "source": session.get("kind"),
            "car_id": session.get("car_id"),
            "track_id": session.get("track_id"),
            "count": len(rows),
            "variables": [self._catalog_row_to_api(r) for r in rows],
        }

    @staticmethod
    def _catalog_row_to_api(row: dict) -> dict:
        name = row["name"]
        cols = json.loads(row["columns"]) if row.get("columns") else [name]
        out: dict[str, Any] = {
            "name": name,
            "ir_type": row["type_label"],
            "count": row["count"],
            "unit": row["unit"],
            "desc": row["descr"],
            "storage": row["storage"],
            "columns": cols,
        }
        if row["decoder"] == "bitfield" and name in BITFIELD_TABLES:
            out["decoder"] = "bitfield"
            out["flags"] = list(BITFIELD_TABLES[name].keys())
        elif row["decoder"] == "enum" and name in ENUM_TABLES:
            out["decoder"] = "enum"
            out["enum"] = {str(k): v for k, v in ENUM_TABLES[name].items()}
        return out

    def valid_columns(self, session_id: str) -> set[str]:
        rows = self._db.query_dicts(
            "SELECT columns FROM variable_catalog WHERE session_id = ?",
            [session_id],
        )
        cols: set[str] = set(META_COLUMNS)
        for r in rows:
            cols.update(json.loads(r["columns"]) if r.get("columns") else [])
        return cols

    # ---- laps -----------------------------------------------------------
    def get_laps(self, session_id: str) -> list[dict]:
        return self._db.query_dicts(
            "SELECT lap, start_tick, end_tick, start_session_time, lap_time, "
            "is_valid, is_out_lap, is_in_lap FROM laps WHERE session_id = ? "
            "ORDER BY lap",
            [session_id],
        )

    # ---- session info ---------------------------------------------------
    def get_session_info(self, session_id: str, seq: int | None = None) -> dict | None:
        if seq is None:
            row = self._db.query_one(
                "SELECT info_json FROM session_info_snapshots WHERE session_id = ? "
                "ORDER BY update_seq DESC LIMIT 1",
                [session_id],
            )
        else:
            row = self._db.query_one(
                "SELECT info_json FROM session_info_snapshots "
                "WHERE session_id = ? AND update_seq = ?",
                [session_id, seq],
            )
        if row is None:
            return None
        return json.loads(row["info_json"]) if row["info_json"] else {}

    # ---- telemetry queries ---------------------------------------------
    def get_channels(
        self,
        session_id: str,
        names: list[str],
        *,
        lap: int,
        max_points: int = 2000,
        mode: str = "minmax",
        x: str = "lap_dist_pct",
    ) -> dict:
        self._require_columns(session_id, names + (["LapDistPct"] if x == "lap_dist_pct" else []))
        glob = q.lap_glob(self.session_dir(session_id), lap)
        if not _has_files(glob):
            raise FileNotFoundError(f"No telemetry for session {session_id} lap {lap}")

        total = self._db.query(q.count_rows_sql(), [glob])[0][0]
        bucket = 1 if mode == "raw" else q.bucket_size(total, max_points)
        rows = self._db.query_dicts(q.channels_sql(names, x, bucket), [glob])

        x_values = [r["x"] for r in rows]
        channels: dict[str, Any] = {}
        units = self._units(session_id, names)
        for n in names:
            if bucket <= 1:
                channels[n] = {"unit": units.get(n), "y": [r[n] for r in rows]}
            else:
                channels[n] = {
                    "unit": units.get(n),
                    "y": [r[f"{n}__avg"] for r in rows],
                    "y_min": [r[f"{n}__min"] for r in rows],
                    "y_max": [r[f"{n}__max"] for r in rows],
                }
        return {
            "session_id": session_id,
            "lap": lap,
            "x": x,
            "x_values": x_values,
            "channels": channels,
            "downsample": {
                "mode": mode,
                "bucket": bucket,
                "points": len(rows),
                "source_points": total,
            },
        }

    def compare(
        self, session_id: str, name: str, laps: list[int], *, grid: int = 1000
    ) -> dict:
        self._require_columns(session_id, [name, "LapDistPct"])
        series: dict[str, list] = {}
        for lap in laps:
            glob = q.lap_glob(self.session_dir(session_id), lap)
            if not _has_files(glob):
                continue
            rows = self._db.query_dicts(q.compare_sql(name, grid), [glob])
            buckets = {int(r["bucket"]): r["y"] for r in rows}
            series[str(lap)] = [buckets.get(i) for i in range(grid)]
        x_values = [i / grid for i in range(grid)]
        return {
            "session_id": session_id,
            "channel": name,
            "x": "lap_dist_pct",
            "grid": grid,
            "x_values": x_values,
            "laps": series,
        }

    def get_trackmap(
        self,
        session_id: str,
        *,
        lap: int | None = None,
        color: str | None = None,
        max_points: int = 3000,
    ) -> dict:
        cols = self.valid_columns(session_id)
        if "Lat" not in cols or "Lon" not in cols:
            raise FileNotFoundError("Session has no Lat/Lon channels for a track map")
        if color:
            self._require_columns(session_id, [color])
        glob = (
            q.lap_glob(self.session_dir(session_id), lap)
            if lap is not None
            else q.session_glob(self.session_dir(session_id))
        )
        if not _has_files(glob):
            raise FileNotFoundError("No telemetry for track map")
        total = self._db.query(q.count_rows_sql(), [glob])[0][0]
        bucket = q.bucket_size(total, max_points)
        rows = self._db.query_dicts(q.trackmap_sql(color, bucket), [glob])
        return {
            "session_id": session_id,
            "lap": lap,
            "color": color,
            "points": rows,
            "count": len(rows),
        }

    # ---- helpers --------------------------------------------------------
    def _require_columns(self, session_id: str, names: list[str]) -> None:
        valid = self.valid_columns(session_id)
        missing = [n for n in names if n not in valid]
        if missing:
            raise KeyError(f"Unknown channels: {missing}")

    def _units(self, session_id: str, names: list[str]) -> dict[str, str | None]:
        # Map a (possibly flattened) column name back to its variable's unit.
        rows = self._db.query_dicts(
            "SELECT name, unit, columns FROM variable_catalog WHERE session_id = ?",
            [session_id],
        )
        col_unit: dict[str, str | None] = {}
        for r in rows:
            for c in (json.loads(r["columns"]) if r.get("columns") else [r["name"]]):
                col_unit[c] = r["unit"]
        return {n: col_unit.get(n) for n in names}


def _has_files(glob_pattern: str) -> bool:
    return bool(globmod.glob(glob_pattern))
