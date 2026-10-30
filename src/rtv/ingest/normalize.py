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


#: How long after the line to wait for iRacing to publish the lap's official time.
#: ``LapLastLapTime`` lags the ``Lap`` counter by about 1-2 s (measured on a real
#: Spa race .ibt); reading it at the crossing returns the *previous* lap's time.
LAP_TIME_SETTLE_S = 5.0


class LapTracker:
    """Derives :class:`Lap` markers from successive telemetry ticks.

    Primary signal is the ``Lap`` channel. A lap is reported complete once its
    official ``LapLastLapTime`` has arrived (or :data:`LAP_TIME_SETTLE_S` has
    passed, in which case the start-to-start session time is used).

    Flags, all derived from the stream itself:

    * ``is_in_lap``: the car entered pit road during the lap;
    * ``is_out_lap``: the lap started on pit road, or the car left it during the lap;
    * ``is_valid`` is false for the pre-start segment (``Lap`` 0) and for the
      final segment of a recording, which never crossed the line.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.laps: list[Lap] = []
        self._current: Lap | None = None
        self._last_lap_num: int | None = None
        self._last_dist_pct: float | None = None
        self._last_on_pit: bool | None = None
        self._last_llt: float | None = None
        self._last_tick: int = 0
        self._last_session_time: float = 0.0
        # A crossed lap waiting for its official time: (lap, stale value, deadline).
        self._pending: tuple[Lap, float | None, float] | None = None

    @property
    def current_lap(self) -> int:
        """The lap number the latest tick belongs to (0 before the first tick)."""
        return self._current.lap if self._current else 0

    def update(self, values: dict[str, Any], *, tick: int, session_time: float) -> Lap | None:
        """Process one tick. Returns a lap whose time has just been settled, if any."""
        lap_num = _as_int(values.get("Lap"))
        dist_pct = _as_float(values.get("LapDistPct"))
        on_pit = bool(values.get("OnPitRoad") or False)
        llt = _as_float(values.get("LapLastLapTime"))
        completed: Lap | None = None

        if lap_num is None:
            return None

        if self._current is None:
            # First tick we see — open a lap.
            self._current = self._open(lap_num, tick, session_time, on_pit)
            self._last_lap_num = lap_num
        elif lap_num != self._last_lap_num and lap_num > (self._last_lap_num or -1):
            # Lap counter advanced -> close the current lap, open the next. A lap
            # already waiting for its time is settled first, from session time.
            completed = self._settle(None)
            closing = self._current
            closing.end_tick = tick
            closing.lap_time = session_time - closing.start_session_time
            if on_pit and self._last_on_pit is False:
                closing.is_in_lap = True  # turned into the pit lane on the line
            self._pending = (closing, self._last_llt, session_time + LAP_TIME_SETTLE_S)
            self._current = self._open(lap_num, tick, session_time, on_pit)
            self._last_lap_num = lap_num
        else:
            self._mark_pit_transition(on_pit)

        if completed is None and self._pending is not None:
            _lap, stale, deadline = self._pending
            if llt is not None and llt > 0 and llt != stale:
                completed = self._settle(llt)
            elif session_time >= deadline:
                completed = self._settle(None)

        self._last_dist_pct = dist_pct
        self._last_on_pit = on_pit
        self._last_llt = llt
        self._last_tick = tick
        self._last_session_time = session_time
        return completed

    def finalize(self, *, tick: int | None = None, session_time: float | None = None) -> None:
        """Close the in-progress lap at end of stream/import.

        ``tick`` / ``session_time`` default to the last tick seen; the final
        segment never crossed the line, so it is kept but marked not valid.
        """
        self._settle(None)
        if self._current is not None and self._current.end_tick is None:
            end_tick = self._last_tick if tick is None else tick
            end_time = self._last_session_time if session_time is None else session_time
            self._current.end_tick = max(end_tick, self._current.start_tick)
            self._current.lap_time = max(0.0, end_time - self._current.start_session_time)
            self._current.is_valid = False
            self.laps.append(self._current)
            self._current = None

    # ------------------------------------------------------------------ helpers
    def _open(self, lap_num: int, tick: int, session_time: float, on_pit: bool) -> Lap:
        return Lap(
            session_id=self.session_id,
            lap=lap_num,
            start_tick=tick,
            start_session_time=session_time,
            is_out_lap=on_pit,
            # Lap 0 is the grid / pre-start segment, not a lap anyone drove.
            is_valid=lap_num > 0,
        )

    def _mark_pit_transition(self, on_pit: bool) -> None:
        if self._current is None or self._last_on_pit is None or on_pit == self._last_on_pit:
            return
        if on_pit:
            self._current.is_in_lap = True
        else:
            self._current.is_out_lap = True

    def _settle(self, official_time: float | None) -> Lap | None:
        if self._pending is None:
            return None
        lap, _stale, _deadline = self._pending
        self._pending = None
        if official_time is not None:
            lap.lap_time = official_time
        self.laps.append(lap)
        return lap


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
