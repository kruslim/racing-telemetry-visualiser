"""Ingestion layer.

Reads telemetry from the live SDK feed and from ``.ibt`` files and normalises
both into the same columnar :class:`~rtv.ingest.frame.FrameBatch`. Framework-
agnostic: knows nothing about HTTP, DuckDB or WebSockets.
"""
