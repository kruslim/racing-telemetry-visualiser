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


# --------------------------------------------------------------------------
# a live recording through the real poller, without iRacing
# --------------------------------------------------------------------------
class FakeIRSDK:
    """Just enough of pyirsdk's ``IRSDK`` for :class:`LivePoller` to record from.

    Serves one scripted frame per ``freeze_var_buffer_latest`` and sets ``done``
    when they run out; after that every read fails, so the poller records nothing
    more until it is stopped.
    """

    def __init__(self, headers, frames, session_info: dict, done) -> None:
        self._frames = iter(frames)
        self._info = session_info
        self._done = done
        self._current: dict | None = None
        self.is_initialized = True
        self.is_connected = True
        self.session_info_update = 1
        self._var_headers = list(headers)

    def startup(self) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def freeze_var_buffer_latest(self) -> None:
        try:
            self._current = next(self._frames).values
        except StopIteration:
            self._done.set()
            raise RuntimeError("no new telemetry") from None

    def unfreeze_var_buffer_latest(self) -> None:
        pass

    def __getitem__(self, key: str):
        if key in self._info:
            return self._info[key]
        if self._current is None:
            return None
        return self._current.get(key)


def field_spec():
    """Eight cars; car 7 starts most of a lap up (just behind us on track) and gains
    0.2 laps a lap, so it closes, laps us and drives off: blue flag and traffic."""
    from evals.synthetic_race import ScenarioSpec

    return ScenarioSpec(
        ticks_per_lap=300,
        laps=5,
        grid=(0.0, 0.03, 0.06, -0.03, -0.06, 0.12, -0.12, 0.80),
        closing=((7, 0.2),),
    )


def record_live_session(tmp_path, monkeypatch, spec):
    """Record the scripted race ``spec`` with the real live poller into a fresh store.

    Returns ``(db, session_id)``; the Parquet lives under ``tmp_path / "parquet"``.
    Many short flushes, so each lap lands as several Parquet parts, as it does live.
    """
    import sys
    import threading
    import types

    from evals.synthetic_race import _headers, scenario_frames, scenario_session_info

    from rtv.ingest.live import LivePoller
    from rtv.store.duck import Database
    from rtv.store.writer import TelemetryWriter

    info = scenario_session_info(spec)
    info["WeekendInfo"] = {**info["WeekendInfo"], "SubSessionID": 4242}
    done = threading.Event()
    fake = types.ModuleType("irsdk")
    fake.IRSDK = lambda: FakeIRSDK(_headers(), scenario_frames(spec), info, done)
    monkeypatch.setitem(sys.modules, "irsdk", fake)

    db = Database(tmp_path / "telemetry.duckdb")
    poller = LivePoller(
        TelemetryWriter(db, tmp_path / "parquet"), poll_hz=1_000_000, flush_seconds=0.01
    )
    poller.start()
    assert done.wait(60), "live poller never drained the scripted frames"
    session_id = poller.session_id
    poller.stop()
    assert session_id is not None
    return db, session_id
