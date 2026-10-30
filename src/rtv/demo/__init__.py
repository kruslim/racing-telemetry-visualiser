"""Synthetic (simulated) telemetry that flows through the *real* store.

No ``.ibt`` file ships with the repo, so :mod:`rtv.demo` generates a physically
plausible multi-lap session and writes it via the ordinary ``TelemetryWriter``
path — DuckDB metadata + per-lap Parquet. Every real endpoint (charts, compare,
track map, deterministic lap-findings and the LLM chat) then serves this data
unchanged; swap the generator for a real import and nothing else moves.
"""

from __future__ import annotations

from rtv.demo.generator import DEMO_SESSION_ID, generate_demo_session
from rtv.demo.seed import seed_demo_session

__all__ = ["DEMO_SESSION_ID", "generate_demo_session", "seed_demo_session"]
