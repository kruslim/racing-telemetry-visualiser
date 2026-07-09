"""DuckDB connection management.

A single connection guarded by a lock — DuckDB connections are not safe for
concurrent use across threads, and the live poller thread writes metadata while
the API thread reads. All access goes through :class:`Database`.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb

from rtv.logging import get_logger
from rtv.store.schema import DDL

log = get_logger("store.duck")


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.RLock()
        self._con = duckdb.connect(str(path))
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        with self._lock:
            try:
                self._con.execute("PRAGMA threads=4")
                self._con.execute("SET enable_object_cache=true")
            except Exception as exc:  # pragma: no cover
                log.debug("pragma setup: %s", exc)

    def _migrate(self) -> None:
        with self._lock:
            for stmt in DDL:
                self._con.execute(stmt)

    # ---- execution helpers ---------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        with self._lock:
            self._con.execute(sql, params or [])

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
        with self._lock:
            return self._con.execute(sql, params or []).fetchall()

    def query_dicts(self, sql: str, params: Sequence[Any] | None = None) -> list[dict]:
        with self._lock:
            cur = self._con.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] | None = None) -> dict | None:
        rows = self.query_dicts(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._con.close()
