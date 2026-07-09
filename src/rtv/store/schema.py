"""DuckDB metadata DDL."""

from __future__ import annotations

DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id       TEXT PRIMARY KEY,
        kind             TEXT NOT NULL,
        source_path      TEXT,
        car_id           TEXT,
        track_id         TEXT,
        track_name       TEXT,
        ir_session_id    BIGINT,
        ir_subsession_id BIGINT,
        schema_hash      TEXT,
        started_at       TIMESTAMP,
        ended_at         TIMESTAMP,
        sample_count     BIGINT DEFAULT 0,
        poll_hz          INTEGER,
        status           TEXT DEFAULT 'open',
        label            TEXT
    )
    """,
    # Migration for DBs created before `label` existed (idempotent).
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS label TEXT",
    """
    CREATE TABLE IF NOT EXISTS laps (
        session_id         TEXT NOT NULL,
        lap                INTEGER NOT NULL,
        start_tick         BIGINT,
        end_tick           BIGINT,
        start_session_time DOUBLE,
        lap_time           DOUBLE,
        is_valid           BOOLEAN DEFAULT TRUE,
        is_out_lap         BOOLEAN DEFAULT FALSE,
        is_in_lap          BOOLEAN DEFAULT FALSE,
        PRIMARY KEY (session_id, lap)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_info_snapshots (
        session_id   TEXT NOT NULL,
        update_seq   INTEGER NOT NULL,
        captured_tick BIGINT,
        captured_at  TIMESTAMP,
        info_json    JSON,
        PRIMARY KEY (session_id, update_seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS variable_catalog (
        session_id TEXT NOT NULL,
        name       TEXT NOT NULL,
        ir_type    SMALLINT,
        type_label TEXT,
        count      INTEGER,
        unit       TEXT,
        descr      TEXT,
        storage    TEXT,
        decoder    TEXT,
        columns    JSON,
        PRIMARY KEY (session_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_jobs (
        job_id     TEXT PRIMARY KEY,
        session_id TEXT,
        source_path TEXT,
        status     TEXT DEFAULT 'pending',
        progress   REAL DEFAULT 0.0,
        error      TEXT,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    )
    """,
)
