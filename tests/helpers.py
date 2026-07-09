"""Shared test helpers: synthetic headers and frame generation."""

from __future__ import annotations

import math

from rtv.catalog.builder import RawHeader, build_catalog
from rtv.catalog.types import IRType


def synthetic_headers() -> list[RawHeader]:
    """A representative header set covering every type and array strategy."""
    return [
        RawHeader("SessionTime", IRType.DOUBLE, unit="s"),
        RawHeader("SessionTick", IRType.INT),
        RawHeader("Lap", IRType.INT),
        RawHeader("LapDist", IRType.FLOAT, unit="m"),
        RawHeader("LapDistPct", IRType.FLOAT, unit="%"),
        RawHeader("Speed", IRType.FLOAT, unit="m/s"),
        RawHeader("RPM", IRType.FLOAT, unit="rev/min"),
        RawHeader("Throttle", IRType.FLOAT, unit="%"),
        RawHeader("Brake", IRType.FLOAT, unit="%"),
        RawHeader("Gear", IRType.INT),
        RawHeader("Lat", IRType.DOUBLE, unit="deg"),
        RawHeader("Lon", IRType.DOUBLE, unit="deg"),
        RawHeader("OnPitRoad", IRType.BOOL),
        RawHeader("LapLastLapTime", IRType.FLOAT, unit="s"),
        RawHeader("SessionState", IRType.INT),  # enum by name
        RawHeader("SessionFlags", IRType.BITFIELD),
        RawHeader("TyrePressure", IRType.FLOAT, count=4, unit="kPa"),  # flattened
        RawHeader("CarIdxLapDistPct", IRType.FLOAT, count=64, unit="%"),  # list
    ]


def synthetic_catalog(flatten_max: int = 6):
    return build_catalog(
        synthetic_headers(), source="ibt", flatten_max=flatten_max
    )


def generate_frames(laps: int = 3, ticks_per_lap: int = 200):
    """Yield (values, tick, session_time, lap) tuples for a few synthetic laps."""
    tick = 0
    t = 0.0
    for lap in range(1, laps + 1):
        for i in range(ticks_per_lap):
            pct = i / ticks_per_lap
            angle = pct * 2 * math.pi
            values = {
                "SessionTime": t,
                "SessionTick": tick,
                "Lap": lap,
                "LapDist": pct * 5000.0,
                "LapDistPct": pct,
                "Speed": 40.0 + 20.0 * math.sin(angle),
                "RPM": 6000.0 + 1500.0 * math.sin(angle),
                "Throttle": max(0.0, math.sin(angle)),
                "Brake": max(0.0, -math.sin(angle)),
                "Gear": 3,
                "Lat": 52.0 + 0.01 * math.sin(angle),
                "Lon": -1.0 + 0.01 * math.cos(angle),
                "OnPitRoad": False,
                "LapLastLapTime": 90.0 if i == 0 and lap > 1 else 0.0,
                "SessionState": 4,
                "SessionFlags": 0x00000004,  # green
                "TyrePressure": [165.0, 165.5, 164.0, 164.5],
                "CarIdxLapDistPct": [pct] + [0.0] * 63,
            }
            yield values, tick, t, lap
            tick += 1
            t += 1.0 / 60.0
