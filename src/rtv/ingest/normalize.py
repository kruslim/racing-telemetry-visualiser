"""Normalisation shared by the live and .ibt paths.

The main job here is deriving lap markers from the raw telemetry stream. Both
sources feed ticks through a :class:`LapTracker`, so live and offline laps are
detected identically.
"""

from __future__ import annotations

from typing import Any

from rtv.domain.models import Lap
from rtv.logging import get_logger

log = get_logger("ingest.normalize")


class LapTracker:
    """Derives :class:`Lap` markers from successive telemetry ticks.

    Primary signal is the ``Lap`` channel (most reliable). We also watch
    ``LapDistPct`` for the start/finish wrap as a cross-check, and flag out-laps
    (lap that begins on pit road) and in-laps (lap that ends entering pits).
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.laps: list[Lap] = []
        self._current: Lap | None = None
        self._last_lap_num: int | None = None
        self._last_dist_pct: float | None = None

    def update(self, values: dict[str, Any], *, tick: int, session_time: float) -> Lap | None:
        """Process one tick. Returns a just-completed Lap, if any."""
        lap_num = _as_int(values.get("Lap"))
        dist_pct = _as_float(values.get("LapDistPct"))
        on_pit = bool(values.get("OnPitRoad") or False)
        completed: Lap | None = None

        if lap_num is None:
            return None

        if self._current is None:
            # First tick we see — open a lap.
            self._current = Lap(
                session_id=self.session_id,
                lap=lap_num,
                start_tick=tick,
                start_session_time=session_time,
                is_out_lap=on_pit,
            )
            self._last_lap_num = lap_num
        elif lap_num != self._last_lap_num and lap_num > (self._last_lap_num or -1):
            # Lap counter advanced -> close the current lap, open the next.
            self._current.end_tick = tick
            last_lap_time = _as_float(values.get("LapLastLapTime"))
            if last_lap_time and last_lap_time > 0:
                self._current.lap_time = last_lap_time
            else:
                self._current.lap_time = session_time - self._current.start_session_time
            self._current.is_in_lap = on_pit
            self.laps.append(self._current)
            completed = self._current

            self._current = Lap(
                session_id=self.session_id,
                lap=lap_num,
                start_tick=tick,
                start_session_time=session_time,
                is_out_lap=on_pit,
            )
            self._last_lap_num = lap_num

        self._last_dist_pct = dist_pct
        return completed

    def finalize(self, *, tick: int, session_time: float) -> None:
        """Close the in-progress lap at end of stream/import."""
        if self._current is not None and self._current.end_tick is None:
            self._current.end_tick = tick
            self._current.lap_time = session_time - self._current.start_session_time
            self.laps.append(self._current)
            self._current = None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
