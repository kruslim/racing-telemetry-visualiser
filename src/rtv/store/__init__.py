"""Persistence layer: DuckDB metadata + Parquet telemetry.

High-rate telemetry is written as per-session, per-lap Parquet files and queried
through DuckDB; small mutable metadata (sessions, laps, catalog, session-info
snapshots, import jobs) lives in DuckDB native tables. Knows nothing about HTTP.
"""
