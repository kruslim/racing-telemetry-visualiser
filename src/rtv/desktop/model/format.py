"""Formatting for a pit stand: pure, total, and honest about missing data.

A port of ``frontend/js/pitwall/format.js``. Every function takes something that may
legitimately be ``None`` (``RaceState`` says ``null`` whenever the session's catalog
could not back a number) and returns ``n/a`` rather than a zero.
"""

from __future__ import annotations

import math
import time as _time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

NA = "n/a"
MINUS = "−"


def is_num(value: Any) -> bool:
    """A finite real number. Booleans are not numbers here, as in JS."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def num(value: Any, digits: int = 1, suffix: str = "") -> str:
    """A fixed-point number, or ``n/a``."""
    if not is_num(value):
        return NA
    return f"{to_fixed(value, digits)}{suffix}"


def integer(value: Any, suffix: str = "") -> str:
    """A whole number, or ``n/a``."""
    if not is_num(value):
        return NA
    return f"{_js_round(value)}{suffix}"


def thousands(value: Any, suffix: str = "") -> str:
    """A whole number with a thin-space thousands separator: ``151 892``."""
    if not is_num(value):
        return NA
    return f"{_js_round(value):,}".replace(",", " ") + suffix


def lap_time(seconds: Any) -> str:
    """Lap time as a driver reads it: ``1:32.418``, or ``52.104`` under a minute."""
    if not is_num(seconds) or seconds <= 0:
        return NA
    minutes = math.floor(seconds / 60)
    rest = seconds - minutes * 60
    if minutes <= 0:
        return to_fixed(rest, 3)
    return f"{minutes}:{to_fixed(rest, 3).rjust(6, '0')}"


def clock(seconds: Any) -> str:
    """A countdown: ``1:02:33`` with hours, ``12:33`` without."""
    if not is_num(seconds) or seconds < 0:
        return NA
    total = math.floor(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _sign(value: float) -> str:
    return "+" if value > 0 else MINUS if value < 0 else ""


def gap(seconds: Any, digits: int = 2) -> str:
    """A signed gap in seconds: ``+1.23``, ``−0.40``, ``n/a``."""
    if not is_num(seconds):
        return NA
    return f"{_sign(seconds)}{to_fixed(abs(seconds), digits)}"


def signed(value: Any, digits: int = 1, suffix: str = "") -> str:
    """A signed quantity that is not a gap (a trend, a margin)."""
    if not is_num(value):
        return NA
    return f"{_sign(value)}{to_fixed(abs(value), digits)}{suffix}"


def trend_arrow(value: Any, deadband: float = 0) -> str:
    """An arrow for a rate of change; ``''`` when unknown, so nothing is drawn."""
    if not is_num(value):
        return ""
    if value > deadband:
        return "▲"
    if value < -deadband:
        return "▼"
    return "–"


def humanise(key: Any) -> str:
    """``pit_window_open`` -> ``pit window open``."""
    return str(key or "").replace("_", " ").replace(":", " ").strip()


def agent_badge(agent: Any) -> str:
    """``vehicle_engineer`` -> ``VEHICLE ENGINEER``."""
    return humanise(agent).upper() or "?"


def stamp(timestamp: Any, session_time: Any) -> str:
    """Wall-clock ``HH:MM:SS`` from epoch seconds, else the session clock."""
    if is_num(timestamp):
        return _time.strftime("%H:%M:%S", _time.localtime(timestamp))
    # Replay carries no wall clock on purpose, so fall back to the session clock.
    if is_num(session_time):
        return clock(session_time)
    return NA


def kmh(speed_ms: Any, digits: int = 1) -> str:
    """m/s on the wire, km/h on screen."""
    if not is_num(speed_ms):
        return NA
    return num(speed_ms * 3.6, digits)


def to_fixed(value: float, digits: int) -> str:
    """``Number.prototype.toFixed``: the exact binary value, a half rounds away from zero.

    Python's ``f"{x:.2f}"`` rounds a half to even, so ``2.5`` gave ``2`` here and ``3``
    on the web pages. ``-0`` prints without a sign, as in JS.
    """
    exact = Decimal(value if value != 0 else 0.0)
    return format(exact.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP), "f")


def _js_round(value: float) -> int:
    # Math.round: halves go up, unlike Python's banker's rounding.
    return math.floor(value + 0.5)
