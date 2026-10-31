"""QPainter drawings: pit-window bar, sparkline, telemetry strip, traces, track map.

Data-free: each widget is handed a view (from ``rtv.desktop.model``) and paints it.
A missing input draws the axis and says why, never a flat line at zero.
"""

from __future__ import annotations

import math
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..model.format import NA, is_num, kmh, num
from ..theme import tokens as t
from .common import mono_font


def _pen(color: str, width: float = 1.0, style=Qt.PenStyle.SolidLine) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setStyle(style)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _small_font(size: int = 10, bold: bool = False) -> QFont:
    font = mono_font(size)
    if bold:
        font.setBold(True)
    return font


class PitWindowBar(QWidget):
    """Lap axis with the current lap, the window span, the dry edge and the flag."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._window: dict | None = None
        self._available = False
        self.setAccessibleName("Pit window")

    def set_view(self, window: dict | None, available: bool) -> None:
        self._window, self._available = window, available
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        axis_y = 20
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1c222a"))
        p.drawRoundedRect(QRectF(0, axis_y - 3, w, 6), 3, 3)
        win = self._window
        if not self._available or not win:
            p.setPen(QColor(t.MUTED))
            p.setFont(_small_font(11))
            p.drawText(
                QRectF(0, axis_y + 8, w, h - axis_y - 8),
                Qt.AlignmentFlag.AlignLeft,
                "no fuel model — pit window unavailable",
            )
            return
        first, last = win["first"], win["last"]
        span = max(1, last - first)

        def x(lap: float) -> float:
            return max(0.0, min(w, (lap - first) / span * w))

        if is_num(win.get("earliest")) and is_num(win.get("latest")):
            x0, x1 = x(win["earliest"]), x(win["latest"])
            color = QColor(t.FLAG["green"] if win.get("open") else "#3a4552")
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x0, axis_y - 6, max(3, x1 - x0), 12), 3, 3)
        marks = []
        if is_num(win.get("fuel_limit_lap")):
            marks.append(
                (
                    win["fuel_limit_lap"],
                    t.SEVERITY["critical"],
                    3,
                    f"dry ~L{num(win['fuel_limit_lap'], 1)}",
                )
            )
        if is_num(win.get("finish_lap")):
            marks.append(
                (win["finish_lap"], t.SEVERITY["advisory"], 3, f"flag L{num(win['finish_lap'], 0)}")
            )
        if is_num(win.get("lap")):
            marks.append((win["lap"], "#ffffff", 2, f"L{win['lap']} now"))
        p.setFont(_small_font(10))
        for lap, color, width, text in marks:
            px = x(lap)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawRect(QRectF(px - width / 2, axis_y - 11, width, 22))
            p.setPen(QColor(color if color != "#ffffff" else t.INK))
            tw = p.fontMetrics().horizontalAdvance(text)
            tx = max(0.0, min(w - tw, px - tw / 2))
            p.drawText(QPointF(tx, axis_y + 26), text)
        p.setPen(QColor(t.MUTED))
        p.drawText(QPointF(0, h - 2), f"L{first}")
        end = f"L{last}"
        p.drawText(QPointF(w - p.fontMetrics().horizontalAdvance(end), h - 2), end)


class Sparkline(QWidget):
    """One point per lap; the latest point is highlighted."""

    def __init__(self, color: str = t.ACCENT, parent: QWidget | None = None):
        super().__init__(parent)
        self.color = color
        self.values: list[float] = []
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, values: list[float]) -> None:
        self.values = [v for v in values if is_num(v)]
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        vals = self.values
        if len(vals) < 2:
            p.setPen(QColor(t.MUTED))
            p.setFont(_small_font(10))
            text = "one point per lap" if vals else "no laps of consumption yet"
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignVCenter, text)
            return
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * 0.2 or 0.05
        lo, hi = lo - pad, hi + pad
        pts = [
            QPointF(4 + i / (len(vals) - 1) * (w - 8), h - 4 - (v - lo) / (hi - lo) * (h - 8))
            for i, v in enumerate(vals)
        ]
        p.setPen(_pen(self.color, 1.5))
        for a, b in zip(pts, pts[1:], strict=False):
            p.drawLine(a, b)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self.color))
        for pt in pts[:-1]:
            p.drawEllipse(pt, 2, 2)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(pts[-1], 2.6, 2.6)


TELEMETRY = [
    ("Speed", "SPEED", "km/h", t.ACCENT),
    ("Throttle", "THROTTLE", "%", "#35c98a"),
    ("Brake", "BRAKE", "%", t.SEVERITY["critical"]),
    ("RPM", "RPM", "", t.SOFT),
    ("Gear", "GEAR", "", t.SEVERITY["advisory"]),
    ("SteeringWheelAngle", "STEERING", "deg", "#b18cff"),
]
WINDOW_S = 20.0


def telemetry_value(name: str, value) -> str:
    if not is_num(value):
        return NA
    if name == "Speed":
        return kmh(value)
    if name in ("Throttle", "Brake"):
        return num(value * 100, 0)
    if name == "SteeringWheelAngle":
        return num(math.degrees(value), 1)
    if name == "RPM":
        return f"{round(value):,}".replace(",", " ")
    return num(value, 0)


class TelemetryStrip(QWidget):
    """Rolling 20 s traces of six raw channels, straight from ``/ws/live``."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(86)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.samples = {name: deque() for name, *_ in TELEMETRY}
        self.latest: dict = {}
        self.available: set[str] | None = None  # None = catalog not seen yet
        self.frozen = False

    def set_catalog(self, names: set[str]) -> None:
        self.available = set(names)
        self.update()

    def push(self, t_s: float, values: dict) -> None:
        for name, *_ in TELEMETRY:
            value = values.get(name)
            if is_num(value):
                buf = self.samples[name]
                if buf and t_s < buf[-1][0]:
                    buf.clear()  # a new replay: the clock went backwards
                buf.append((t_s, float(value)))
                while buf and t_s - buf[0][0] > WINDOW_S:
                    buf.popleft()
        self.latest = values
        self.update()

    def clear(self) -> None:
        for buf in self.samples.values():
            buf.clear()
        self.latest = {}
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(TELEMETRY)
        cell = self.width() / n
        h = self.height()
        for i, (name, title, unit, color) in enumerate(TELEMETRY):
            x0 = i * cell
            if i:
                p.setPen(_pen(t.LINE))
                p.drawLine(QPointF(x0, 0), QPointF(x0, h))
            p.setPen(QColor(t.MUTED))
            p.setFont(_small_font(10, True))
            p.drawText(QPointF(x0 + 10, 14), title)
            missing = self.available is not None and name not in self.available
            if missing:
                p.setFont(_small_font(11))
                p.drawText(
                    QRectF(x0 + 10, 22, cell - 16, h - 22),
                    Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                    "not in this session",
                )
                continue
            p.setPen(QColor(t.MUTED if self.frozen else t.INK))
            p.setFont(_small_font(16, True))
            value = telemetry_value(name, self.latest.get(name))
            p.drawText(QPointF(x0 + 10, 36), value)
            if unit:
                value_w = p.fontMetrics().horizontalAdvance(value)
                p.setPen(QColor(t.MUTED))
                p.setFont(_small_font(10))
                p.drawText(QPointF(x0 + 14 + value_w, 36), unit)
            buf = self.samples[name]
            if len(buf) < 2:
                continue
            p.save()
            p.setClipRect(QRectF(x0 + 4, 40, cell - 8, h - 40))
            vals = [v for _, v in buf]
            lo, hi = min(vals), max(vals)
            if name in ("Throttle", "Brake"):
                lo, hi = 0.0, 1.0
            if hi - lo < 1e-6:
                hi = lo + 1
            t_end = buf[-1][0]
            top, bottom = 44, h - 6
            path = QPainterPath()
            for j, (ts, v) in enumerate(buf):
                px = x0 + 8 + (1 - (t_end - ts) / WINDOW_S) * (cell - 16)
                py = bottom - (v - lo) / (hi - lo) * (bottom - top)
                if j:
                    path.lineTo(px, py)
                else:
                    path.moveTo(px, py)
            p.setPen(_pen(t.MUTED if self.frozen else color, 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            p.restore()


class TraceStack(QWidget):
    """Stacked lap-vs-reference traces against lap distance, plus the time delta.

    ``set_data(channels, distance, delta)``: channels is a list of
    ``(title, unit, main_values, ref_values, lo, hi)`` on a shared metres axis.
    ``highlight`` is a corner's (entry, exit) in metres.
    """

    ROW = 70
    DELTA_ROW = 86
    GUTTER = 92

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.channels: list = []
        self.x_m: list[float] = []
        self.delta: tuple[list[float], list[float]] | None = None
        self.highlight: tuple[float, float] | None = None
        self.lap_length: float | None = None
        self.message = "pick a lap and a reference"
        self.hover_m: float | None = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._resize()

    def _resize(self) -> None:
        rows = len(self.channels) or 5
        self.setMinimumHeight(rows * self.ROW + self.DELTA_ROW + 24)

    def set_data(self, channels: list, x_m: list[float], delta, lap_length) -> None:
        self.channels, self.x_m, self.delta, self.lap_length = channels, x_m, delta, lap_length
        self.message = ""
        self._resize()
        self.update()

    def set_message(self, text: str) -> None:
        self.channels, self.delta, self.message = [], None, text
        self.update()

    def set_highlight(self, span: tuple[float, float] | None) -> None:
        self.highlight = span
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.lap_length:
            frac = (event.position().x() - self.GUTTER) / max(1, self.width() - self.GUTTER - 8)
            self.hover_m = max(0.0, min(1.0, frac)) * self.lap_length
            self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self.hover_m = None
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        x0, x1 = self.GUTTER, w - 8
        if self.message:
            p.setPen(QColor(t.MUTED))
            p.drawText(QRectF(0, 0, w, 60), Qt.AlignmentFlag.AlignCenter, self.message)
            return
        length = self.lap_length or (self.x_m[-1] if self.x_m else 1)

        def xm(m: float) -> float:
            return x0 + m / length * (x1 - x0)

        total_h = len(self.channels) * self.ROW + self.DELTA_ROW
        if self.highlight:
            a, b = self.highlight
            p.fillRect(QRectF(xm(a), 0, max(2, xm(b) - xm(a)), total_h), QColor(92, 184, 255, 34))
        y = 0
        for title, unit, main, ref, lo, hi in self.channels:
            self._label(p, y, title, unit)
            for values, color, width in ((ref, t.REF_TRACE, 1.3), (main, t.LAP_TRACE, 1.6)):
                self._line(p, values, self.x_m, xm, y + 6, y + self.ROW - 6, lo, hi, color, width)
            p.setPen(_pen(t.ROW_LINE))
            p.drawLine(QPointF(x0, y + self.ROW), QPointF(x1, y + self.ROW))
            y += self.ROW
        self._label(p, y, "TIME Δ", "s, + = slower")
        if self.delta:
            dist, ds = self.delta
            m = max((abs(v) for v in ds if is_num(v)), default=0) or 0.1
            mid = y + self.DELTA_ROW / 2
            p.setPen(_pen("#3a4552"))
            p.drawLine(QPointF(x0, mid), QPointF(x1, mid))
            self._line(p, ds, dist, xm, y + 6, y + self.DELTA_ROW - 6, -m, m, t.INK, 1.8)
            p.setPen(QColor(t.MUTED))
            p.setFont(_small_font(10))
            p.drawText(QPointF(x0 + 4, y + 14), f"+{m:.2f}")
            p.drawText(QPointF(x0 + 4, y + self.DELTA_ROW - 4), f"−{m:.2f}")
            if ds and is_num(ds[-1]):
                end = f"{'+' if ds[-1] >= 0 else '−'}{abs(ds[-1]):.3f} s"
                p.setPen(QColor(t.SEVERITY_TEXT["advisory"] if ds[-1] > 0 else t.GOOD))
                p.drawText(QPointF(x1 - p.fontMetrics().horizontalAdvance(end), y + 14), end)
        else:
            p.setPen(QColor(t.MUTED))
            p.drawText(QPointF(x0 + 4, y + self.DELTA_ROW / 2), "time delta unavailable")
        y += self.DELTA_ROW
        p.setPen(QColor(t.MUTED))
        p.setFont(_small_font(10))
        for i in range(5):
            m = length * i / 4
            text = f"{round(m):,} m".replace(",", " ")
            px = xm(m) - (p.fontMetrics().horizontalAdvance(text) if i == 4 else 0)
            p.drawText(QPointF(px, y + 16), text)
        if self.hover_m is not None:
            p.setPen(_pen("#ffffff", 1))
            p.drawLine(QPointF(xm(self.hover_m), 0), QPointF(xm(self.hover_m), total_h))
            p.drawText(QPointF(xm(self.hover_m) + 4, total_h - 4), f"{round(self.hover_m)} m")

    def _label(self, p: QPainter, y: float, title: str, unit: str) -> None:
        p.setPen(QColor(t.MUTED))
        p.setFont(_small_font(10, True))
        p.drawText(QPointF(4, y + 22), title)
        p.setFont(_small_font(10))
        p.drawText(QPointF(4, y + 36), unit)

    @staticmethod
    def _line(p, values, xs, xm, top, bottom, lo, hi, color, width) -> None:
        if not values or not xs:
            return
        span = (hi - lo) or 1
        path = QPainterPath()
        drawing = False
        for x, v in zip(xs, values, strict=False):
            if not is_num(v):
                drawing = False  # a gap in the data stays a gap
                continue
            py = bottom - (max(lo, min(hi, v)) - lo) / span * (bottom - top)
            if drawing:
                path.lineTo(xm(x), py)
            else:
                path.moveTo(xm(x), py)
                drawing = True
        p.setPen(_pen(color, width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


class TrackMap(QWidget):
    """The reference lap's outline from lat/lon, with event pins and a corner span."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.points: list[dict] = []
        self.pins: list[dict] = []
        self.highlight_pct: tuple[float, float] | None = None
        self.message = "no track map yet"
        self.setMinimumSize(300, 260)

    def set_points(self, points: list[dict]) -> None:
        self.points = [p for p in points if is_num(p.get("lat")) and is_num(p.get("lon"))]
        self.message = "" if self.points else "no GPS channels in this session"
        self.update()

    def set_pins(self, pins: list[dict]) -> None:
        self.pins = pins
        self.update()

    def set_highlight(self, span_pct: tuple[float, float] | None) -> None:
        self.highlight_pct = span_pct
        self.update()

    def _project(self):
        lat0 = sum(p["lat"] for p in self.points) / len(self.points)
        k = math.cos(math.radians(lat0))
        xs = [p["lon"] * k for p in self.points]
        ys = [-p["lat"] for p in self.points]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        pad = 18
        w, h = self.width() - 2 * pad, self.height() - 2 * pad
        s = min(w / ((maxx - minx) or 1), h / ((maxy - miny) or 1))
        ox = pad + (w - (maxx - minx) * s) / 2
        oy = pad + (h - (maxy - miny) * s) / 2
        return [
            QPointF((x - minx) * s + ox, (y - miny) * s + oy) for x, y in zip(xs, ys, strict=True)
        ]

    def _at(self, pts: list[QPointF], pct: float) -> QPointF:
        best, best_d = 0, math.inf
        for i, p in enumerate(self.points):
            d = abs(p.get("lap_dist_pct", 0) - pct)
            d = min(d, 1 - d)
            if d < best_d:
                best, best_d = i, d
        return pts[best]

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.points:
            p.setPen(QColor(t.MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            return
        pts = self._project()
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        path.closeSubpath()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(_pen("#3a4552", 7))
        p.drawPath(path)
        p.setPen(_pen(t.SOFT, 2))
        p.drawPath(path)
        if self.highlight_pct:
            a, b = self.highlight_pct
            seg = [
                pt
                for pt, src in zip(pts, self.points, strict=True)
                if a <= src.get("lap_dist_pct", -1) <= b
            ]
            if len(seg) > 1:
                hp = QPainterPath(seg[0])
                for pt in seg[1:]:
                    hp.lineTo(pt)
                p.setPen(_pen(t.ACCENT, 6))
                p.drawPath(hp)
        start = pts[0]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawRect(QRectF(start.x() - 7, start.y() - 2, 14, 4))
        for pin in self.pins:
            pt = self._at(pts, pin["pct"])
            sev = pin.get("severity") or "info"
            p.setPen(_pen(t.BG, 2))
            p.setBrush(QColor(t.severity_color(sev)))
            p.drawEllipse(pt, 6, 6)


class Hatch:
    """Brush helpers shared by the timeline and waterfall."""

    @staticmethod
    def dashed(color: str) -> QPen:
        return _pen(color, 1.2, Qt.PenStyle.DashLine)


class LaneTimeline(QWidget):
    """Lanes of items on a session-time axis, with an optional draggable cursor.

    ``set_lanes([(title, colour, [item...])])``; an item has ``t0``, optional
    ``t1``, ``color`` and ``quiet`` (an ended-without-speaking run, drawn hatched).
    Live mode follows "now" over a fixed window; review mode spans the session and
    emits ``scrubbed`` as the pointer drags.
    """

    scrubbed = Signal(float)
    item_clicked = Signal(dict)

    LANE_H = 20
    GUTTER = 122

    def __init__(self, parent: QWidget | None = None, *, scrub: bool = False):
        super().__init__(parent)
        self.lanes: list = []
        self.t0, self.t1 = 0.0, 1.0
        self.cursor: float | None = None
        self.scrub = scrub
        self._dragging = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        if scrub:
            self.setCursor(Qt.CursorShape.SizeHorCursor)

    def set_lanes(self, lanes: list) -> None:
        self.lanes = lanes
        self.setMinimumHeight(len(lanes) * (self.LANE_H + 4) + 22)
        self.update()

    def set_range(self, t0: float, t1: float) -> None:
        self.t0, self.t1 = t0, max(t0 + 1e-3, t1)
        self.update()

    def set_cursor(self, t_s: float | None) -> None:
        self.cursor = t_s
        self.update()

    def _x(self, t_s: float) -> float:
        return self.GUTTER + (t_s - self.t0) / (self.t1 - self.t0) * (
            self.width() - self.GUTTER - 6
        )

    def _t(self, x: float) -> float:
        frac = (x - self.GUTTER) / max(1, self.width() - self.GUTTER - 6)
        return self.t0 + max(0.0, min(1.0, frac)) * (self.t1 - self.t0)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        x, y = event.position().x(), event.position().y()
        hit = self._hit(x, y)
        if hit is not None:
            self.item_clicked.emit(hit)
        if self.scrub and x >= self.GUTTER:
            self._dragging = True
            self.scrubbed.emit(hit["t0"] if hit is not None else self._t(x))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self.scrubbed.emit(self._t(event.position().x()))
        hit = self._hit(event.position().x(), event.position().y())
        self.setToolTip(hit.get("label", "") if hit else "")

    def mouseReleaseEvent(self, _event) -> None:  # noqa: N802
        self._dragging = False

    def _hit(self, x: float, y: float) -> dict | None:
        lane = int(y // (self.LANE_H + 4))
        if not (0 <= lane < len(self.lanes)) or x < self.GUTTER:
            return None
        best, best_d = None, 6.0
        for item in self.lanes[lane][2]:
            a = self._x(item["t0"])
            b = self._x(item.get("t1", item["t0"]))
            d = 0 if a - 3 <= x <= b + 3 else min(abs(x - a), abs(x - b))
            if d < best_d:
                best, best_d = item, d
        return best

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        for i, (title, color, items) in enumerate(self.lanes):
            y = i * (self.LANE_H + 4)
            p.setPen(QColor(color))
            p.setFont(_small_font(11))
            p.drawText(
                QRectF(0, y, self.GUTTER - 8, self.LANE_H), Qt.AlignmentFlag.AlignVCenter, title
            )
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.CHROME))
            p.drawRoundedRect(QRectF(self.GUTTER, y, w - self.GUTTER - 6, self.LANE_H), 3, 3)
            p.save()
            p.setClipRect(QRectF(self.GUTTER, y, w - self.GUTTER - 6, self.LANE_H))
            for item in items:
                a = self._x(item["t0"])
                c = QColor(item.get("color") or color)
                if "t1" in item and item["t1"] is not None and item["t1"] > item["t0"]:
                    b = self._x(item["t1"])
                    rect = QRectF(a, y + 4, max(3, b - a), self.LANE_H - 8)
                    if item.get("quiet"):
                        p.setBrush(Qt.BrushStyle.NoBrush)
                        p.setPen(_pen(c.name(), 1.4, Qt.PenStyle.DashLine))
                        p.drawRect(rect)
                        p.setPen(Qt.PenStyle.NoPen)
                    else:
                        p.setBrush(c)
                        p.drawRoundedRect(rect, 2, 2)
                    if item.get("text") and rect.width() > 40:
                        p.setPen(QColor(t.BG))
                        p.setFont(_small_font(9))
                        p.drawText(
                            rect.adjusted(3, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, item["text"]
                        )
                        p.setPen(Qt.PenStyle.NoPen)
                else:
                    p.setBrush(c)
                    width = item.get("width", 4)
                    p.drawRoundedRect(QRectF(a - width / 2, y + 3, width, self.LANE_H - 6), 1, 1)
            p.restore()
        y = len(self.lanes) * (self.LANE_H + 4)
        p.setPen(QColor(t.MUTED))
        p.setFont(_small_font(10))
        from ..model.format import clock

        for i in range(5):
            ts = self.t0 + (self.t1 - self.t0) * i / 4
            text = clock(ts)
            px = self._x(ts) - (p.fontMetrics().horizontalAdvance(text) if i == 4 else 0)
            p.drawText(QPointF(px, y + 14), text)
        if self.cursor is not None and self.t0 <= self.cursor <= self.t1:
            cx = self._x(self.cursor)
            p.setPen(_pen("#ffffff", 2))
            p.drawLine(QPointF(cx, 0), QPointF(cx, y))
            if self.scrub:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#ffffff"))
                p.drawRoundedRect(QRectF(cx - 16, y + 2, 32, 8), 4, 4)
