"""The agent layer drawn: the "pitwall at work" flow graph and the runs waterfall.

Every packet that moves on the graph is a real activity record (``edges_for``);
there is no idle animation. With reduced motion, edges flash instead of carrying
a moving packet.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..model.activity import AGENTS, Run
from ..model.format import humanise
from ..theme import tokens as t
from .common import mono_font

# column, row-in-column (agents and llm/tools stack)
LAYOUT = {
    "telemetry": (0, 0),
    "engine": (1, 0),
    "detectors": (2, 0),
    "bus": (3, 0),
    "orchestrator": (4, 0),
    "strategist": (5, 0),
    "vehicle_engineer": (5, 1),
    "spotter": (5, 2),
    "coach": (5, 3),
    "race_control": (5, 4),
    "llm": (6, 0),
    "tools": (6, 1),
    "validator": (7, 0),
    "radio": (8, 0),
    "driver": (9, 0),
}
COLUMN_WEIGHT = [1.0, 0.9, 1.0, 0.7, 1.2, 1.9, 0.9, 1.0, 0.8, 0.8]
PACKET_MS = 700
FLASH_MS = 900


def _pen(color: str, width: float = 1.0, style=Qt.PenStyle.SolidLine) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setStyle(style)
    return pen


class FlowGraph(QWidget):
    """telemetry → engine → detectors → bus → orchestrator → agents ↔ llm/tools → ..."""

    def __init__(self, parent: QWidget | None = None, *, reduced_motion: bool = False):
        super().__init__(parent)
        self.setMinimumHeight(168)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.reduced_motion = reduced_motion
        self.mounted = True
        self.sub: dict[str, str] = {}
        self.status: dict[str, str] = {}
        self.disabled: set[str] = set()
        self._packets: list[dict] = []
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self.setAccessibleName("Pitwall at work")

    # ---- inputs -------------------------------------------------------
    def set_mounted(self, mounted: bool) -> None:
        self.mounted = mounted
        self.update()

    def set_sublabel(self, node: str, text: str) -> None:
        if self.sub.get(node) != text:
            self.sub[node] = text
            self.update()

    def set_agent(self, name: str, status: str, disabled: bool = False) -> None:
        self.status[name] = status
        if disabled:
            self.disabled.add(name)
        else:
            self.disabled.discard(name)
        self.update()

    def pulse(self, edges: list[dict]) -> None:
        """Send one packet along each edge a record travelled."""
        now = time.monotonic() * 1000
        for edge in edges:
            if edge.get("from") not in LAYOUT or edge.get("to") not in LAYOUT:
                continue
            delay = PACKET_MS * 0.6 if edge.get("delay") else 0
            self._packets.append({**edge, "start": now + delay})
        del self._packets[:-120]
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def _tick(self) -> None:
        now = time.monotonic() * 1000
        life = FLASH_MS if self.reduced_motion else PACKET_MS
        self._packets = [p for p in self._packets if now - p["start"] < life]
        if not self._packets:
            self._timer.stop()
        self.update()

    # ---- geometry -----------------------------------------------------
    def _rects(self) -> dict[str, QRectF]:
        w, h = self.width(), self.height()
        total = sum(COLUMN_WEIGHT)
        xs, x = [], 4.0
        for weight in COLUMN_WEIGHT:
            span = (w - 8) * weight / total
            xs.append((x, span))
            x += span
        rects = {}
        for node, (col, row) in LAYOUT.items():
            x0, span = xs[col]
            gap = 14
            bw = span - gap
            if col == 5:
                # Five agents stacked: 5 x 24 + 4 x 6 fits the 168 px minimum.
                bh = 24
                y = 8 + row * (bh + 6)
            elif col == 6:
                bh = 28
                y = 28 + row * (bh + 26)
            else:
                bh = 34
                y = (h - 30) / 2 - bh / 2
            rects[node] = QRectF(x0 + gap / 2, y, bw, bh)
        return rects

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rects = self._rects()
        now = time.monotonic() * 1000
        hot = {(pk["from"], pk["to"]) for pk in self._packets if pk["start"] <= now}

        def edge(a: str, b: str, color: str = "#3a4552", dashed: bool = False) -> None:
            ra, rb = rects[a], rects[b]
            start = QPointF(ra.right(), ra.center().y())
            end = QPointF(rb.left(), rb.center().y())
            if (a, b) in hot or (b, a) in hot:
                color = t.ACCENT
            p.setPen(_pen(color, 1.4, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine))
            p.drawLine(start, end)

        chain = ["telemetry", "engine", "detectors", "bus", "orchestrator"]
        for a, b in zip(chain, chain[1:], strict=False):
            edge(a, b)
        for agent in AGENTS:
            edge("orchestrator", agent)
            edge(agent, "llm")
            edge(agent, "tools")
        edge("llm", "validator")
        edge("tools", "validator")
        edge("validator", "radio")
        edge("radio", "driver")
        # radio heard as context: a dashed return path under the graph
        ra = rects["radio"]
        y = self.height() - 12
        path = QPainterPath(QPointF(ra.center().x(), ra.bottom()))
        path.lineTo(ra.center().x(), y)
        path.lineTo(rects["coach"].center().x(), y)
        path.lineTo(rects["coach"].center().x(), rects["coach"].bottom())
        context_hot = any(pk.get("context") for pk in self._packets if pk["start"] <= now)
        p.setPen(_pen(t.ACCENT if context_hot else "#4a5563", 1.2, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        for node, rect in rects.items():
            self._node(p, node, rect)

        for pk in self._packets:
            if pk["start"] > now:
                continue
            a, b = rects[pk["from"]], rects[pk["to"]]
            color = QColor(t.agent_color(pk.get("agent")) if pk.get("agent") else t.ACCENT)
            if pk.get("blocked") or pk.get("ok") is False:
                color = QColor(t.SEVERITY["critical"])
            if self.reduced_motion:
                continue  # the edge itself is lit above
            frac = min(1.0, (now - pk["start"]) / PACKET_MS)
            sa = QPointF(a.center())
            sb = QPointF(b.center())
            pos = sa + (sb - sa) * frac
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawEllipse(pos, 4, 4)

    def _node(self, p: QPainter, node: str, rect: QRectF) -> None:
        is_agent = node in AGENTS
        status = self.status.get(node, "idle")
        dim = (is_agent or node in ("llm", "tools", "validator")) and not self.mounted
        disabled = node in self.disabled
        border, fill, ink = t.LINE_2, t.PANEL_2, t.INK
        if is_agent:
            border = t.agent_color(node)
            ink = t.agent_text(node)
            if status != "idle":
                fill = "#10241b" if node == "coach" else "#1d1729"
        if dim or disabled:
            border, ink = "#2a313a", "#5c6672"
        p.setPen(
            _pen(
                border,
                2 if is_agent and status != "idle" else 1,
                Qt.PenStyle.DashLine if disabled else Qt.PenStyle.SolidLine,
            )
        )
        p.setBrush(QColor(fill))
        p.drawRoundedRect(rect, 6, 6)
        font = mono_font(11 if rect.width() > 70 else 9)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(ink))
        name = {"vehicle_engineer": "engineer", "race_control": "race control"}.get(node, node)
        if not is_agent:
            name = p.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight,
                                              int(rect.width()) - 4)
        if is_agent:
            p.drawText(rect.adjusted(8, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, name)
            sub = self.sub.get(node) or ("" if status == "idle" else status)
            if sub:
                p.setFont(mono_font(9))
                p.setPen(QColor(t.MUTED if not dim else "#5c6672"))
                metrics = p.fontMetrics()
                text = metrics.elidedText(sub, Qt.TextElideMode.ElideRight, int(rect.width() / 2))
                p.drawText(
                    rect.adjusted(0, 0, -6, 0),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                    text,
                )
        else:
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, name)
            sub = self.sub.get(node, "")
            if sub:
                p.setFont(mono_font(9))
                p.setPen(QColor(t.MUTED))
                below = QRectF(rect.left() - 10, rect.bottom() + 2, rect.width() + 20, 14)
                metrics = p.fontMetrics()
                p.drawText(
                    below,
                    Qt.AlignmentFlag.AlignHCenter,
                    metrics.elidedText(sub, Qt.TextElideMode.ElideRight, int(below.width())),
                )


class RunsWaterfall(QWidget):
    """One row per agent run; model and tool spans on the run's own wall clock."""

    run_clicked = Signal(str)
    ROW = 30

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.runs: list[Run] = []
        self.selected: str | None = None
        self.now_wall: float | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(self.ROW * 3)

    def set_runs(self, runs: list[Run], now_wall: float | None = None) -> None:
        self.runs = runs
        self.now_wall = now_wall
        self.setMinimumHeight(max(self.ROW * 3, self.ROW * len(runs) + 4))
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        index = int(event.position().y() // self.ROW)
        if 0 <= index < len(self.runs):
            self.selected = self.runs[index].run_id
            self.run_clicked.emit(self.selected)
            self.update()

    def _end(self, run: Run) -> float | None:
        if run.end_wall is not None:
            return run.end_wall
        return self.now_wall

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        if not self.runs:
            p.setPen(QColor(t.MUTED))
            p.drawText(
                QRectF(0, 0, w, self.ROW * 2), Qt.AlignmentFlag.AlignCenter, "No agent runs yet."
            )
            return
        cols = [0, 150, 300, 390, w - 70]
        durations = [
            (self._end(r) or r.start_wall or 0) - (r.start_wall or 0)
            for r in self.runs
            if r.start_wall is not None
        ]
        scale = max(durations, default=0) or 1
        bar_x0, bar_x1 = cols[3], cols[4] - 10
        for i, run in enumerate(self.runs):
            y = i * self.ROW
            if run.run_id == self.selected:
                p.fillRect(
                    QRectF(0, y, w, self.ROW),
                    QColor("#13201a" if run.end_wall is None else "#152230"),
                )
            p.setPen(_pen(t.ROW_LINE))
            p.drawLine(QPointF(0, y + self.ROW), QPointF(w, y + self.ROW))
            font = mono_font(11)
            font.setBold(True)
            p.setFont(font)
            p.setPen(QColor(t.agent_text(run.agent)))
            p.drawText(
                QRectF(cols[0] + 4, y, cols[1] - 8, self.ROW),
                Qt.AlignmentFlag.AlignVCenter,
                p.fontMetrics().elidedText(run.run_id, Qt.TextElideMode.ElideRight, cols[1] - 10),
            )
            p.setFont(mono_font(11))
            p.setPen(QColor(t.INK))
            p.drawText(
                QRectF(cols[1], y, cols[2] - cols[1] - 6, self.ROW),
                Qt.AlignmentFlag.AlignVCenter,
                p.fontMetrics().elidedText(
                    humanise(run.trigger or run.event_key),
                    Qt.TextElideMode.ElideRight,
                    cols[2] - cols[1] - 8,
                ),
            )
            outcome = run.outcome or ("working" if run.end_wall is None else "unfinished")
            bg, fg = t.OUTCOME.get(outcome, t.OUTCOME["unfinished"])
            word = {"radio": "SPOKE", "error": "✕ ERROR"}.get(outcome, outcome.upper())
            tag = QRectF(cols[2], y + 7, 74, self.ROW - 14)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(bg))
            p.drawRoundedRect(tag, 4, 4)
            p.setPen(QColor(fg))
            small = mono_font(9)
            small.setBold(True)
            p.setFont(small)
            p.drawText(tag, Qt.AlignmentFlag.AlignCenter, word)
            if run.start_wall is not None:
                color = QColor(t.agent_color(run.agent))
                for span in run.spans:
                    s0 = span.get("start_wall")
                    s1 = span.get("end_wall") or self._end(run)
                    if s0 is None or s1 is None:
                        continue
                    a = bar_x0 + (s0 - run.start_wall) / scale * (bar_x1 - bar_x0)
                    b = bar_x0 + (s1 - run.start_wall) / scale * (bar_x1 - bar_x0)
                    if span["kind"] == "tool":
                        p.setBrush(QColor(t.SOFT))
                        rect = QRectF(a, y + 9, max(4, b - a), self.ROW - 18)
                    else:
                        c = QColor(color)
                        if span.get("ok") is False:
                            c = QColor(t.SEVERITY["critical"])
                        p.setBrush(c)
                        rect = QRectF(a, y + 9, max(3, b - a), self.ROW - 18)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.drawRoundedRect(rect, 2, 2)
                end = self._end(run)
                if run.validations and end is not None:
                    ok = all(v["ok"] for v in run.validations) and run.outcome != "refused"
                    vx = bar_x0 + (end - run.start_wall) / scale * (bar_x1 - bar_x0)
                    p.setBrush(QColor(t.GOOD if ok else t.SEVERITY["advisory"]))
                    p.drawRect(QRectF(vx + 2, y + 6, 3, self.ROW - 12))
            p.setFont(mono_font(11))
            p.setPen(QColor(t.INK))
            latency = run.latency_ms
            text = f"{latency / 1000:.1f} s" if latency is not None else "…"
            p.drawText(
                QRectF(cols[4], y, 66, self.ROW),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                text,
            )
