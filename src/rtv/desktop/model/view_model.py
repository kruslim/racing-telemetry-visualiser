"""RaceState -> what a pit stand shows. Pure functions, no Qt.

A port of ``frontend/js/pitwall/view-model.js``: everything that can be *wrong*
(which lap the pit-window bar starts at, whether a fuel margin is amber or red,
whether a rival is closing) is decided here, where it can be asserted without a
window. A value the session could not back stays ``None`` and renders as ``n/a``.
"""

from __future__ import annotations

import math
from typing import Any

from .format import NA, gap, is_num, lap_time, num, signed

MARGIN_AMBER_LAPS = 1.0
TOWER_WINDOW = 3
GAP_TREND_RATE = 0.05
GAP_TREND_MIN_DT = 1.0
TYRE_TEMP_DEADBAND = 0.5
TYRE_PRESSURE_DEADBAND = 0.2
WARNING_TTL_S = 12.0

CORNERS = ("LF", "RF", "LR", "RR")

FLAG_LABELS = {
    "green": "GREEN",
    "yellow": "YELLOW — FULL COURSE CAUTION",
    "red": "RED FLAG",
    "white": "WHITE — FINAL LAP",
    "checkered": "CHEQUERED FLAG",
    "unknown": "FLAG UNKNOWN",
}


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# status strip
# ---------------------------------------------------------------------------
def status_view(state: dict | None, ctx: dict | None = None) -> dict:
    """The top band. ``ctx`` carries socket and REST knowledge the state lacks."""
    ctx = ctx or {}
    state = _d(state)
    session = _d(state.get("session"))
    flags = _d(state.get("flags"))
    conditions = _d(state.get("conditions"))
    phase = flags.get("phase") or "unknown"
    caps = _d(state.get("capabilities"))
    return {
        "session": {
            "state": session.get("state") or NA,
            "source": session.get("source") or NA,
            "track": session.get("track_name") or session.get("track_id") or NA,
            "car": session.get("car_id") or NA,
        },
        "remaining": _remaining_view(session),
        "flag": {
            "phase": phase,
            "label": FLAG_LABELS.get(phase, str(phase).upper()),
            "active": flags.get("active") if isinstance(flags.get("active"), list) else [],
            # Green is the absence of news: the band only takes the screen
            # when something is actually being flagged.
            "alert": phase != "green",
        },
        "conditions": {
            "track": conditions.get("track_temp"),
            "air": conditions.get("air_temp"),
            "track_trend": conditions.get("track_temp_trend"),
            "air_trend": conditions.get("air_temp_trend"),
            "available": bool(caps.get("conditions")),
        },
        "connection": connection_view(state, ctx),
        "llm": llm_view(ctx.get("pitwall_status")),
    }


def _remaining_view(session: dict) -> dict:
    if is_num(session.get("laps_remaining")):
        total = f" / {session['laps_total']}" if is_num(session.get("laps_total")) else ""
        return {
            "kind": "laps",
            "text": f"{session['laps_remaining']}{total}",
            "unit": "laps left",
        }
    if is_num(session.get("time_remaining")):
        return {
            "kind": "time",
            "text": None,
            "seconds": session["time_remaining"],
            "unit": "time left",
        }
    return {"kind": "unknown", "text": NA, "unit": "remaining"}


def connection_view(state: dict | None, ctx: dict | None = None) -> dict:
    """Socket down beats everything; stale beats live; then replay, live, idle."""
    ctx = ctx or {}
    state = _d(state)
    if ctx.get("socket") is False:
        return {
            "state": "offline",
            "label": "OFFLINE",
            "detail": ctx.get("socket_detail") or "socket closed",
        }
    if state.get("stale"):
        return {
            "state": "stale",
            "label": "STALE",
            "detail": state.get("stale_reason") or "telemetry stopped",
        }
    replay = ctx.get("replay") or None
    if replay and replay.get("running"):
        speed = replay.get("speed")
        speed_text = f"{_trim(speed)}×" if is_num(speed) and speed else "max"
        return {
            "state": "replay",
            "label": "REPLAY",
            "detail": f"{speed_text} · {replay.get('frames') or 0} frames",
        }
    source = _d(state.get("session")).get("source")
    if source == "replay":
        return {"state": "replay", "label": "REPLAY", "detail": "replayed frames"}
    frames = _d(state.get("metrics")).get("frames")
    if is_num(frames) and frames > 0:
        return {"state": "live", "label": "LIVE", "detail": f"{frames} frames"}
    return {"state": "idle", "label": "NO DATA", "detail": "waiting for frames"}


def _trim(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def llm_view(status: dict | None) -> dict:
    """What ``/api/v1/pitwall/status`` means for a badge in the corner."""
    if not status:
        return {"available": False, "label": "AI —", "detail": "status unknown", "tone": "muted"}
    if not status.get("available"):
        return {
            "available": False,
            "label": "AI OFF",
            "detail": status.get("reason") or "agent layer not mounted",
            "tone": "muted",
        }
    used = status.get("calls_used") if is_num(status.get("calls_used")) else None
    cap = (
        status.get("max_calls_per_session") if is_num(status.get("max_calls_per_session")) else None
    )
    degraded = [
        a for a in status.get("agents") or [] if a.get("degraded") or a.get("disabled_reason")
    ]
    exhausted = bool(status.get("budget_exhausted"))
    tone = "warn" if exhausted or not status.get("healthy") or degraded else "ok"
    detail = f"{NA if used is None else used}{f'/{cap}' if cap else ''} calls"
    if degraded:
        detail += " · degraded: " + ", ".join(a.get("name", "?") for a in degraded)
    if exhausted:
        detail += " · budget spent"
    return {
        "available": True,
        "label": "AI MUTED" if status.get("enabled") is False else "AI LIVE",
        "calls": used,
        "max_calls": cap,
        "remaining": status.get("calls_remaining"),
        "exhausted": exhausted,
        "degraded": [a.get("name") for a in degraded],
        "detail": detail,
        "tone": tone,
    }


# ---------------------------------------------------------------------------
# strategy
# ---------------------------------------------------------------------------
def strategy_view(state: dict | None) -> dict:
    """The pit-window bar, the fuel block and the tyre widget."""
    state = _d(state)
    caps = _d(state.get("capabilities"))
    fuel = _d(state.get("fuel"))
    player = _d(state.get("player"))
    session = _d(state.get("session"))
    lap = player.get("lap") if is_num(player.get("lap")) else None

    fuel_limit_lap = (
        lap + fuel["laps_remaining"]
        if lap is not None and is_num(fuel.get("laps_remaining"))
        else None
    )
    finish_lap = (
        lap + fuel["laps_to_finish"]
        if lap is not None and is_num(fuel.get("laps_to_finish"))
        else None
    )
    marks = [
        m
        for m in (
            lap,
            fuel.get("pit_window_earliest_lap"),
            fuel.get("pit_window_latest_lap"),
            fuel_limit_lap,
            finish_lap,
        )
        if is_num(m)
    ]
    window = None
    if marks:
        window = {
            # A lap of air either side: a window off the end of the axis is
            # worse than none.
            "first": max(0, math.floor(min(marks)) - 1),
            "last": math.ceil(max(marks)) + 1,
            "lap": lap,
            "earliest": _num_or_none(fuel.get("pit_window_earliest_lap")),
            "latest": _num_or_none(fuel.get("pit_window_latest_lap")),
            "fuel_limit_lap": fuel_limit_lap,
            "finish_lap": finish_lap,
            "open": bool(fuel.get("window_open")),
            "laps_total": _num_or_none(session.get("laps_total")),
        }
    return {
        "available": bool(caps.get("fuel")),
        "window": window,
        "fuel": {
            "level": fuel.get("level"),
            "level_pct": fuel.get("level_pct"),
            "capacity": fuel.get("capacity"),
            "per_lap": fuel.get("per_lap"),
            "per_lap_std": fuel.get("per_lap_std"),
            "samples": fuel.get("samples") or 0,
            "laps_remaining": fuel.get("laps_remaining"),
            "laps_to_finish": fuel.get("laps_to_finish"),
            "margin_laps": fuel.get("margin_laps"),
            "margin_l": fuel.get("margin_l"),
            "band": margin_band(fuel.get("margin_laps")),
        },
        "tyres": tyre_view(state),
        "stint": {
            "number": player.get("stint"),
            "laps_on_tyres": player.get("laps_on_tyres"),
            "start_lap": player.get("stint_start_lap"),
        },
    }


def _num_or_none(value: Any) -> Any:
    return value if is_num(value) else None


def margin_band(margin_laps: Any) -> str:
    """Green / amber / red. Negative means a stop is required, not a dry tank."""
    if not is_num(margin_laps):
        return "unknown"
    if margin_laps < 0:
        return "red"
    if margin_laps < MARGIN_AMBER_LAPS:
        return "amber"
    return "green"


def tyre_view(state: dict | None) -> dict:
    state = _d(state)
    caps = _d(state.get("capabilities"))
    tyres = _d(state.get("tyres"))
    temps = _d(tyres.get("temps"))
    pressures = _d(tyres.get("pressures"))
    temp_trend = _d(tyres.get("temp_trend"))
    pressure_trend = _d(tyres.get("pressure_trend"))
    corners = [
        {
            "key": key,
            "temp": _num_or_none(temps.get(key)),
            "pressure": _num_or_none(pressures.get(key)),
            "temp_trend": _num_or_none(temp_trend.get(key)),
            "pressure_trend": _num_or_none(pressure_trend.get(key)),
        }
        for key in CORNERS
    ]
    return {
        "available": bool(caps.get("tyres")),
        "stint_laps": tyres.get("stint_laps") or 0,
        "corners": corners,
        "imbalance": {
            "front": _axle_spread(temps.get("LF"), temps.get("RF")),
            "rear": _axle_spread(temps.get("LR"), temps.get("RR")),
        },
    }


def _axle_spread(left: Any, right: Any) -> float | None:
    if not is_num(left) or not is_num(right):
        return None
    return left - right


def recommendation_view(message: dict | None) -> dict | None:
    """The strategist's latest call. Reads only the validated ``data`` contract."""
    if not message:
        return None
    data = _d(message.get("data"))
    parts = []
    if is_num(data.get("target_lap")):
        parts.append(f"lap {data['target_lap']}")
    if is_num(data.get("fuel_to_add_l")):
        parts.append(f"{num(data['fuel_to_add_l'], 1)} L")
    if is_num(data.get("rejoin_position")):
        parts.append(f"rejoin P{data['rejoin_position']}")
    confidence = data.get("confidence")
    if is_num(confidence):
        parts.append(f"conf {num(confidence, 2)}")
    call = data.get("recommendation") or ("refused" if message.get("refused") else "hold")
    event_ref = _d(message.get("event_ref"))
    return {
        "call": str(call).replace("_", " ").upper(),
        "parts": parts,
        "target_lap": _num_or_none(data.get("target_lap")),
        "fuel_to_add_l": _num_or_none(data.get("fuel_to_add_l")),
        "rejoin_position": _num_or_none(data.get("rejoin_position")),
        "confidence": confidence if isinstance(confidence, str) else None,
        "risks": data.get("risks") if isinstance(data.get("risks"), list) else [],
        "spoken": message.get("spoken_text") or "",
        "priority": message.get("priority") or "info",
        "grounded": message.get("grounded") is not False,
        "refused": bool(message.get("refused")),
        "lap": event_ref.get("lap"),
        "session_time": message.get("session_time"),
    }


# ---------------------------------------------------------------------------
# timing tower
# ---------------------------------------------------------------------------
def tower_view(
    state: dict | None,
    expanded: bool = False,
    window: int = TOWER_WINDOW,
    trends: GapTrendTracker | None = None,
    warnings: TowerWarnings | None = None,
) -> dict:
    """The cars either side of us. Collapsed shows P±3, expanded the field."""
    state = _d(state)
    standings = _d(state.get("standings"))
    cars = list(standings.get("cars") or [])
    caps = _d(state.get("capabilities"))
    cars.sort(key=_car_order)

    player_at = next((i for i, car in enumerate(cars) if car.get("is_player")), -1)
    rows = []
    for car in cars:
        last = car.get("last_lap_time")
        best = car.get("best_lap_time")
        rows.append(
            {
                "idx": car.get("idx"),
                "label": f"#{car.get('idx')}",
                "is_player": bool(car.get("is_player")),
                "position": _num_or_none(car.get("position")),
                "lap": _num_or_none(car.get("lap")),
                "last_lap": last if is_num(last) and last > 0 else None,
                "best_lap": best if is_num(best) and best > 0 else None,
                "gap_to_player": _num_or_none(car.get("gap_to_player")),
                "on_pit_road": car.get("on_pit_road") is True,
                "track_surface": car.get("track_surface"),
                "trend": trends.trend(car.get("idx")) if trends else None,
                "warning": warnings.for_car(car.get("idx")) if warnings else None,
            }
        )
    visible = rows
    hidden = 0
    if not expanded and player_at >= 0 and len(rows) > window * 2 + 1:
        first = max(0, player_at - window)
        last = min(len(rows), player_at + window + 1)
        visible = rows[first:last]
        hidden = len(rows) - len(visible)

    player = _d(state.get("player"))
    position = player.get("position")
    last_lap = player.get("last_lap_time")
    return {
        "available": bool(caps.get("standings")),
        # A bound session without standings, as opposed to nothing bound yet.
        "no_field": not caps.get("standings") and bool(caps.get("session")),
        "player": {
            "position": position if is_num(position) and position > 0 else None,
            "last_lap": last_lap if is_num(last_lap) and last_lap > 0 else None,
        },
        "gap_basis": standings.get("gap_basis") or None,
        "gaps_known": bool(standings.get("gap_basis")),
        "rows": visible,
        "total": len(rows),
        "hidden": hidden,
        "expanded": expanded,
    }


def _car_order(car: dict) -> tuple:
    pos = car.get("position")
    laps = car.get("laps_completed")
    return (
        pos if is_num(pos) else math.inf,
        -(laps if is_num(laps) else -math.inf),
        car.get("idx") or 0,
    )


class GapTrendTracker:
    """Per-car gap trend: closing, opening or steady.

    Pairs closer than ``min_dt`` apart are noise, and a pair that straddles a
    change of ``gap_basis`` is discarded: the ruler changed, not the car.
    """

    def __init__(self, min_dt: float = GAP_TREND_MIN_DT, rate: float = GAP_TREND_RATE):
        self.min_dt = min_dt
        self.rate = rate
        self._samples: dict[Any, dict] = {}
        self._trends: dict[Any, str] = {}

    def update(self, state: dict | None) -> GapTrendTracker:
        state = _d(state)
        standings = _d(state.get("standings"))
        t = state.get("session_time") if is_num(state.get("session_time")) else None
        basis = standings.get("gap_basis") or None
        cars = standings.get("cars")
        if t is None or not isinstance(cars, list):
            return self
        for car in cars:
            g = car.get("gap_to_player")
            if not is_num(g):
                continue
            idx = car.get("idx")
            previous = self._samples.get(idx)
            self._samples[idx] = {"gap": g, "time": t, "basis": basis}
            if not previous:
                continue
            dt = t - previous["time"]
            if dt < self.min_dt:
                self._samples[idx] = previous  # keep the older anchor
                continue
            if previous["basis"] != basis:
                continue
            closing = (abs(previous["gap"]) - abs(g)) / dt
            if closing > self.rate:
                self._trends[idx] = "closing"
            elif closing < -self.rate:
                self._trends[idx] = "opening"
            else:
                self._trends[idx] = "steady"
        return self

    def trend(self, idx: Any) -> str | None:
        return self._trends.get(idx)

    def reset(self) -> None:
        self._samples.clear()
        self._trends.clear()


class TowerWarnings:
    """Blue flags and closing traffic pinned to a car, aged out on session time."""

    KINDS = ("blue_flag", "traffic_close", "rival_pitted")

    def __init__(self, ttl: float = WARNING_TTL_S):
        self.ttl = ttl
        self._by_car: dict[Any, dict] = {}
        self._now = 0.0

    def observe(self, event: dict | None) -> TowerWarnings:
        if not event or event.get("event_type") not in self.KINDS:
            return self
        payload = _d(event.get("payload"))
        idx = payload.get("car_idx")
        if not is_num(idx):
            return self
        at = event.get("session_time")
        self._by_car[idx] = {
            "kind": event["event_type"],
            "at": at if is_num(at) else self._now,
            "payload": payload,
        }
        return self

    def clock(self, session_time: Any) -> TowerWarnings:
        if is_num(session_time):
            self._now = session_time
        return self

    def for_car(self, idx: Any) -> dict | None:
        warning = self._by_car.get(idx)
        if not warning:
            return None
        if self._now - warning["at"] > self.ttl:
            del self._by_car[idx]
            return None
        return warning

    def reset(self) -> None:
        self._by_car.clear()


class FuelHistory:
    """One sample of the engine's own rolling per-lap consumption per lap."""

    def __init__(self, limit: int = 40):
        self.limit = limit
        self.samples: list[dict] = []
        self._last_lap: Any = None

    def update(self, state: dict | None) -> bool:
        state = _d(state)
        player = _d(state.get("player"))
        fuel = _d(state.get("fuel"))
        lap, per_lap = player.get("lap"), fuel.get("per_lap")
        if not is_num(lap) or not is_num(per_lap):
            return False
        if self._last_lap == lap and self.samples and self.samples[-1]["lap"] == lap:
            # per_lap moves within a lap as the rolling mean refreshes.
            self.samples[-1]["per_lap"] = per_lap
            return False
        self._last_lap = lap
        self.samples.append({"lap": lap, "per_lap": per_lap})
        del self.samples[: max(0, len(self.samples) - self.limit)]
        return True

    def points(self) -> list[dict]:
        return list(self.samples)

    def reset(self) -> None:
        self.samples = []
        self._last_lap = None


# ---------------------------------------------------------------------------
# event ticker
# ---------------------------------------------------------------------------
def _q(value: Any) -> Any:
    return "?" if value is None else value


def _lap_completed(p: dict) -> str:
    tail = " (not green)" if p.get("green") is False else ""
    return f"lap {_q(p.get('lap'))} in {lap_time(p.get('lap_time'))}{tail}"


def _offtrack(p: dict) -> str:
    pct = p.get("lap_dist_pct")
    where = num(pct * 100, 0) if is_num(pct) else NA
    return f"off track ({p.get('surface') or '?'}) at {where}%"


def _health(p: dict) -> str:
    text = p.get("measure") or "health"
    if "value" in p:
        text += f" {num(p.get('value'), 1)}"
    if isinstance(p.get("warnings"), list) and p["warnings"]:
        text += f" [{', '.join(map(str, p['warnings']))}]"
    return text


EVENT_TEXT = {
    "flag_change": lambda p: f"flag {p.get('from') or '?'} → {p.get('to') or '?'}",
    "lap_completed": _lap_completed,
    "personal_best": lambda p: f"personal best {lap_time(p.get('lap_time'))}",
    "session_fastest_lap": lambda p: f"session fastest {lap_time(p.get('lap_time'))}",
    "lockup": lambda p: (
        f"lockup {p.get('wheel') or p.get('corner') or ''} slip {num(p.get('slip'), 2)}"
    ),
    "wheelspin": lambda p: (
        f"wheelspin {p.get('wheel') or p.get('corner') or ''} slip {num(p.get('slip'), 2)}"
    ),
    "offtrack": _offtrack,
    "incident": lambda p: (
        f"incident +{p.get('delta') if p.get('delta') is not None else 1}"
        f" ({_q(p.get('incidents'))} total)"
    ),
    "pit_entry": lambda p: f"pit entry, stint {_q(p.get('stint'))}, {num(p.get('fuel'), 1)} L",
    "pit_exit": lambda p: f"pit exit, stint {_q(p.get('stint'))}, {num(p.get('fuel'), 1)} L",
    "stint_start": lambda p: f"stint {_q(p.get('stint'))} begins",
    "pit_window_open": lambda p: (
        f"pit window lap {_q(p.get('earliest_lap'))}–{_q(p.get('latest_lap'))}"
    ),
    "pit_window_closing": lambda p: f"pit window closing, latest lap {_q(p.get('latest_lap'))}",
    "fuel_margin_low": lambda p: f"fuel margin {signed(p.get('margin_laps'), 2)} laps",
    "fuel_critical": lambda p: f"fuel critical: {num(p.get('laps_remaining'), 2)} laps left",
    "stint_lap_milestone": lambda p: f"{_q(p.get('laps_on_tyres'))} laps on this set",
    "rival_pitted": lambda p: f"car #{_q(p.get('car_idx'))} (P{_q(p.get('position'))}) pitted",
    "blue_flag": lambda p: f"blue flag: car #{_q(p.get('car_idx'))} at {gap(p.get('gap'))}",
    "recurring_issue": lambda p: (
        f"{p.get('issue') or 'issue'} ×{_q(p.get('occurrences'))} at {p.get('corner') or '?'}"
    ),
    "tyre_out_of_band": lambda p: (
        f"{p.get('measure') or 'tyre'} {p.get('corner') or ''} = {num(p.get('value'), 1)}"
    ),
    "car_health_warning": _health,
    "traffic_close": lambda p: (
        f"car #{_q(p.get('car_idx'))} {p.get('side') or ''} at {gap(p.get('gap'))}"
    ),
    "weather_change": lambda p: (
        f"{p.get('measure') or 'weather'} {_q(p.get('from'))} → {_q(p.get('to'))}"
    ),
    "regulation_change": lambda p: (
        f"{p.get('regulation') or 'regulation'} from lap {_q(p.get('applies_from_lap'))}"
    ),
}


def event_line(event: dict | None) -> dict | None:
    """One compact ticker line. Unknown event types degrade to key=value pairs."""
    if not event:
        return None
    payload = _d(event.get("payload"))
    render = EVENT_TEXT.get(event.get("event_type"))
    text = None
    if render:
        try:
            text = " ".join(render(payload).split())
        except Exception:  # noqa: BLE001 - a malformed payload is still an event
            text = None
    if not text:
        text = " ".join(f"{k}={payload[k]}" for k in list(payload)[:3])
    lap = event.get("lap")
    t = event.get("session_time")
    return {
        "key": event.get("key") or event.get("event_type"),
        "type": event.get("event_type"),
        "severity": event.get("severity") or "info",
        "lap": lap if is_num(lap) else None,
        "session_time": t if is_num(t) else None,
        "text": text,
        "injected": payload.get("injected") is True,
        "director_id": payload.get("director_id"),
    }
