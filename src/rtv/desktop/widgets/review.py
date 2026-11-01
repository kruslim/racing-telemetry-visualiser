"""Post-race review panels: session bar, summary, car at cursor, laps, findings, audit.

Everything is rendered as the backend served it: lap times, ``/summary`` and
``/coaching/lap-findings`` are never recomputed here.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..model import review
from ..model import session as session_model
from ..model.activity import AGENTS
from ..model.format import (
    NA,
    agent_badge,
    clock,
    humanise,
    integer,
    is_num,
    kmh,
    lap_time,
    num,
    signed,
)
from ..model.review import is_clean
from ..theme import tokens as t
from .common import KeyValue, Panel, StatCard, clear_layout, label, mono_font


def _item(
    text: str, color: str | None = None, mono: bool = True, align_right: bool = False
) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    if mono:
        item.setFont(mono_font(12))
    if color:
        from PySide6.QtGui import QColor

        item.setForeground(QColor(color))
    if align_right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


def make_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.setShowGrid(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setWordWrap(True)
    table.horizontalHeader().setHighlightSections(False)
    # Left-aligned, or the stretched last column centres its title far from its values.
    table.horizontalHeader().setDefaultAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().setDefaultSectionSize(30)
    return table


def fit_height(table: QTableWidget, max_rows: int | None = None) -> None:
    rows = table.rowCount() if max_rows is None else min(max_rows, table.rowCount())
    table.resizeRowsToContents()
    height = table.horizontalHeader().height() + 4
    for r in range(rows):
        height += table.rowHeight(r)
    table.setFixedHeight(max(height, 64))


class SessionBar(QFrame):
    """Session and run pickers, Record a run, Import .ibt, status and progress."""

    session_changed = Signal(str)
    run_changed = Signal(str)
    record_clicked = Signal()
    import_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(label("Session", "muted"))
        self.sessions = QComboBox()
        self.sessions.setMinimumWidth(380)
        self.sessions.setAccessibleName("Session")
        row.addWidget(self.sessions)
        row.addWidget(label("Pitwall run", "muted"))
        self.runs = QComboBox()
        self.runs.setMinimumWidth(320)
        self.runs.setAccessibleName("Pitwall run")
        row.addWidget(self.runs)
        row.addStretch(1)
        self.record = QPushButton("Record a run from the telemetry")
        self.import_btn = QPushButton("Import .ibt…")
        self.import_btn.setProperty("role", "primary")
        row.addWidget(self.record)
        row.addWidget(self.import_btn)
        layout.addLayout(row)
        status = QHBoxLayout()
        self.status = label("", "muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(240)
        self.progress.hide()
        status.addWidget(self.status, 1)
        status.addWidget(self.progress)
        layout.addLayout(status)
        self.sessions.currentIndexChanged.connect(
            lambda _i: (
                self.sessions.currentData()
                and self.session_changed.emit(self.sessions.currentData())
            )
        )
        self.runs.currentIndexChanged.connect(
            lambda _i: self.runs.currentData() and self.run_changed.emit(self.runs.currentData())
        )
        self.record.clicked.connect(self.record_clicked)
        self.import_btn.clicked.connect(self.import_clicked)

    def set_sessions(self, sessions: list[dict], selected: str | None) -> None:
        self.sessions.blockSignals(True)
        self.sessions.clear()
        if not sessions:
            self.sessions.addItem("No sessions yet — import an .ibt", None)
        for s in sessions:
            when = (s.get("started_at") or "")[:16].replace("T", " ")
            text = (
                f"{s.get('label') or s.get('kind')} · {s.get('track_name') or '?'}"
                f" · {'imported' if s.get('kind') == 'ibt' else 'recorded'} {when}"
            )
            self.sessions.addItem(text, s.get("session_id"))
        if selected:
            index = self.sessions.findData(selected)
            if index >= 0:
                self.sessions.setCurrentIndex(index)
        self.sessions.setEnabled(bool(sessions))
        self.record.setEnabled(bool(sessions))
        self.sessions.blockSignals(False)

    def set_runs(self, runs: list[dict], selected: str | None) -> None:
        self.runs.blockSignals(True)
        self.runs.clear()
        if not runs:
            self.runs.addItem("No pitwall runs yet — record one", None)
        for run in runs:
            # Labelled with its session type: one live session holds practice,
            # qualifying and race runs (stage 07).
            self.runs.addItem(session_model.run_label(run), run.get("run_id"))
        if selected:
            index = self.runs.findData(selected)
            if index >= 0:
                self.runs.setCurrentIndex(index)
        self.runs.setEnabled(bool(runs))
        self.runs.blockSignals(False)

    def set_progress(self, text: str, fraction: float | None) -> None:
        self.status.setText(text)
        if fraction is None:
            self.progress.hide()
        else:
            self.progress.show()
            self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 1000))


class SummaryPanel(Panel):
    """Stat cards, the stints table, and why any figure is missing."""

    CARDS = [
        "Result",
        "Laps",
        "Best lap",
        "Median",
        "Pit stops",
        "Incidents",
        "Fuel used",
        "Fuel added",
        "Radio calls",
        "Refused / ungrounded",
        "Agent runs",
        "Model latency",
    ]

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Race summary", parent)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.cards = {}
        for i, key in enumerate(self.CARDS):
            card = StatCard(key)
            self.cards[key] = card
            grid.addWidget(card, i // 6, i % 6)
        self.body.addLayout(grid)
        self.stints = make_table(["Stint", "Laps", "Count", "Clean", "Best", "Median", "σ"])
        self.body.addWidget(self.stints)
        self.notes = label("", "muted", wrap=True)
        self.body.addWidget(self.notes)

    def set_summary(self, s: dict | None) -> None:
        if not s:
            for card in self.cards.values():
                card.set(NA)
            self.stints.setRowCount(0)
            self.notes.setText("")
            return
        start, finish = s.get("start_position"), s.get("finish_position")
        moved = ""
        if is_num(start) and is_num(finish):
            delta = start - finish
            moved = f"{'+' if delta > 0 else ''}{delta} places" if delta else "held position"
        self.cards["Result"].set(
            f"P{start if start is not None else NA} → P{finish if finish is not None else NA}",
            moved,
        )
        self.cards["Laps"].set(integer(s.get("laps")), f"{integer(s.get('clean_laps'))} clean")
        self.cards["Best lap"].set(
            lap_time(s.get("best_time")),
            f"lap {s.get('best_lap')}" if s.get("best_lap") else "",
            t.AGENT_TEXT["strategist"],
        )
        self.cards["Median"].set(
            lap_time(s.get("median_time")), f"σ {num(s.get('stdev_time'), 3)} s"
        )
        self.cards["Pit stops"].set(integer(s.get("pit_stops")))
        events = s.get("events") or {}
        self.cards["Incidents"].set(
            integer(s.get("incidents")), f"{events.get('offtrack', 0)} off-tracks"
        )
        self.cards["Fuel used"].set(
            num(s.get("fuel_used_l"), 2, " L"), f"{num(s.get('fuel_per_lap_l'), 3)} L / lap"
        )
        self.cards["Fuel added"].set(num(s.get("fuel_added_l"), 2, " L"))
        by_agent = s.get("radio_by_agent") or {}
        split = " · ".join(f"{humanise(a)} {n}" for a, n in by_agent.items())
        self.cards["Radio calls"].set(integer(s.get("radio_calls")), split)
        self.cards["Refused / ungrounded"].set(
            f"{integer(s.get('radio_refused'))} / {integer(s.get('radio_ungrounded'))}",
            "grounded refusals",
        )
        self.cards["Agent runs"].set(
            integer(s.get("agent_runs")),
            f"{integer(s.get('model_calls'))} model · {integer(s.get('tool_calls'))} tool calls",
        )
        latency = s.get("model_latency_ms")
        self.cards["Model latency"].set(
            num(latency / 1000, 1, " s") if is_num(latency) else NA,
            f"{integer(s.get('model_errors'))} errors",
        )
        stints = s.get("stints") or []
        self.stints.setRowCount(len(stints))
        for r, st in enumerate(stints):
            values = [
                str(st.get("stint")),
                f"{st.get('from_lap')}–{st.get('to_lap')}",
                integer(st.get("laps")),
                integer(st.get("clean_laps")),
                f"{lap_time(st.get('best_time'))}  L{st.get('best_lap')}",
                lap_time(st.get("median_time")),
                num(st.get("stdev_time"), 3, " s"),
            ]
            for c, v in enumerate(values):
                self.stints.setItem(r, c, _item(v))
        fit_height(self.stints)
        notes = s.get("notes") or []
        self.notes.setText("\n".join(f"• {n}" for n in notes))
        self.notes.setVisible(bool(notes))


class CarStatePanel(QFrame):
    """The race state as it stood at the cursor."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("inset")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        self.title = label("CAR AT THE CURSOR", "label")
        layout.addWidget(self.title)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        self.fields = {}
        keys = [
            "Lap",
            "Position",
            "Flag",
            "Speed",
            "Gear · rpm",
            "Last",
            "Fuel",
            "Margin",
            "Window",
            "Tyres °C",
            "Tyres kPa",
            "Track · air",
        ]
        for i, key in enumerate(keys):
            kv = KeyValue(key, "big")
            self.fields[key] = kv
            grid.addWidget(kv, i // 3, i % 3)
        layout.addLayout(grid)
        self.note = label("", "muted", wrap=True)
        layout.addWidget(self.note)

    def set_state(self, state: dict | None, t_s: float | None) -> None:
        self.title.setText(f"CAR AT THE CURSOR · {clock(t_s)}")
        state = state or {}
        f = self.fields
        player = state.get("player") or {}
        fuel = state.get("fuel") or {}
        flags = state.get("flags") or {}
        tyres = state.get("tyres") or {}
        cond = state.get("conditions") or {}
        pct = player.get("lap_dist_pct")
        f["Lap"].set(
            f"{player['lap']}" + (f"  {num(pct * 100, 0)}%" if is_num(pct) else "")
            if is_num(player.get("lap"))
            else NA
        )
        pos = player.get("position")
        f["Position"].set(f"P{pos}" if is_num(pos) and pos > 0 else NA)
        phase = flags.get("phase")
        f["Flag"].set(phase or NA, t.FLAG.get(phase or "", t.MUTED) if phase != "green" else t.GOOD)
        f["Speed"].set(kmh(player.get("speed")) + (" km/h" if is_num(player.get("speed")) else ""))
        f["Gear · rpm"].set(f"{integer(player.get('gear'))} · {integer(player.get('rpm'))}")
        f["Last"].set(lap_time(player.get("last_lap_time")))
        level_pct = fuel.get("level_pct")
        f["Fuel"].set(
            num(fuel.get("level"), 1, " L")
            + (f"  {num(level_pct * 100, 0)}%" if is_num(level_pct) else "")
        )
        from ..model.view_model import margin_band

        f["Margin"].set(
            signed(fuel.get("margin_laps"), 2, " laps"),
            t.BAND[margin_band(fuel.get("margin_laps"))],
        )
        if is_num(fuel.get("pit_window_earliest_lap")):
            word = "open" if fuel.get("window_open") else "closed"
            f["Window"].set(
                f"{word} {fuel['pit_window_earliest_lap']}–{fuel.get('pit_window_latest_lap')}",
                t.GOOD if fuel.get("window_open") else t.MUTED,
            )
        else:
            f["Window"].set(NA)
        temps, pres = tyres.get("temps") or {}, tyres.get("pressures") or {}
        f["Tyres °C"].set(" ".join(num(temps.get(c), 1) for c in ("LF", "RF", "LR", "RR")))
        f["Tyres kPa"].set(" ".join(num(pres.get(c), 1) for c in ("LF", "RF", "LR", "RR")))
        f["Track · air"].set(
            f"{num(cond.get('track_temp'), 1)} · {num(cond.get('air_temp'), 1)} °C"
        )
        caps = state.get("capabilities") or {}
        self.note.setText(
            ""
            if caps.get("standings") or not caps.get("session")
            else "No gaps or standings: .ibt recordings carry no other cars."
        )
        if not state:
            self.note.setText("no recorded state at this time")


class MiniList(QFrame):
    """A titled inset list of lines (radio so far, events so far)."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("inset")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        self.title = label(title.upper(), "label")
        self.base = title.upper()
        layout.addWidget(self.title)
        self.lines = QVBoxLayout()
        self.lines.setSpacing(6)
        layout.addLayout(self.lines)
        layout.addStretch(1)

    def set_lines(self, rows: list[tuple], count: int, empty: str) -> None:
        """rows: (header, text, colour) or (header, text, colour, struck)."""
        self.title.setText(f"{self.base} · {count}")
        clear_layout(self.lines)
        if not rows:
            self.lines.addWidget(label(empty, "muted", wrap=True))
        for head, text, color, *rest in rows:
            struck = bool(rest and rest[0])
            box = QFrame()
            box.setObjectName("mini")
            box.setStyleSheet(f"#mini {{ border-left: 3px solid {color}; }}")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(8, 1, 0, 1)
            bl.setSpacing(1)
            if head:
                bl.addWidget(label(head, "muted", mono=True, size=11))
            body = label(text, wrap=True, size=12, color=t.SOFT if struck else None)
            if struck:
                font = body.font()
                font.setStrikeOut(True)
                body.setFont(font)
            bl.addWidget(body)
            self.lines.addWidget(box)


class LapTable(QTableWidget):
    """Every lap with its time, delta to best and flags; lap 0 and the last are marked."""

    lap_picked = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(["Lap", "Time", "Δ best", ""])
        self.verticalHeader().hide()
        self.setShowGrid(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setStretchLastSection(True)
        self.verticalHeader().setDefaultSectionSize(26)
        self.setFixedWidth(290)
        self.cellClicked.connect(self._clicked)
        self._laps: list[dict] = []

    def _clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._laps):
            self.lap_picked.emit(self._laps[row]["lap"])

    def set_laps(self, laps: list[dict], main: int | None, ref: int | None) -> None:
        self._laps = laps
        clean = [lap["lap_time"] for lap in laps if is_clean(lap)]
        best = min(clean) if clean else None
        self.setRowCount(len(laps))
        for r, lap in enumerate(laps):
            number = lap.get("lap")
            last = r == len(laps) - 1
            if number == 0:
                flags, color = "pre-start", t.MUTED
            elif last and not lap.get("is_valid"):
                flags, color = "unfinished", t.MUTED
            else:
                parts = [
                    p
                    for p, on in (
                        ("in lap", lap.get("is_in_lap")),
                        ("out lap", lap.get("is_out_lap")),
                        ("invalid", not lap.get("is_valid")),
                    )
                    if on
                ]
                flags, color = ", ".join(parts), None
            if number == ref:
                flags = ("ref · " + flags) if flags else "ref"
                color = t.REF_TRACE
            if number == main:
                flags = ("lap · " + flags) if flags else "lap"
                color = t.LAP_TRACE
            valid_time = number != 0 and not (last and not lap.get("is_valid"))
            time_text = (
                lap_time(lap.get("lap_time"))
                if valid_time
                else (lap_time(lap.get("lap_time")) if number == 0 else NA)
            )
            delta = NA
            if valid_time and is_num(best) and is_num(lap.get("lap_time")):
                d = lap["lap_time"] - best
                delta = "best" if abs(d) < 5e-4 else f"+{d:.3f}"
            self.setItem(r, 0, _item(str(number), color))
            self.setItem(r, 1, _item(time_text, color))
            self.setItem(r, 2, _item(delta, color))
            self.setItem(r, 3, _item(flags, color or t.SOFT, mono=False))


class FindingsPanel(Panel):
    """Chief summary, top opportunities, corners (click to highlight), sectors, KPIs."""

    corner_picked = Signal(dict)

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Coaching findings", parent)
        self.meta.setText("deterministic, from the telemetry (Layer 1)")
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.c_delta = StatCard("Lap delta")
        self.c_lost = StatCard("Lost in corners")
        self.c_losing = StatCard("Corners losing")
        self.c_spread = StatCard("Clean-lap spread")
        for c in (self.c_delta, self.c_lost, self.c_losing, self.c_spread):
            cards.addWidget(c, 1)
        self.top = QFrame()
        self.top.setObjectName("card")
        tl = QVBoxLayout(self.top)
        tl.setContentsMargins(12, 10, 12, 10)
        tl.addWidget(label("TOP OPPORTUNITIES", "label"))
        self.top_lines = QVBoxLayout()
        tl.addLayout(self.top_lines)
        cards.addWidget(self.top, 2)
        self.body.addLayout(cards)
        self.error = label("", wrap=True, color="#ffb3ae")
        self.error.hide()
        self.body.addWidget(self.error)
        self.corners = make_table(
            ["Corner", "Type", "Sector", "Min speed lap / ref", "Net Δt", "Analysts"]
        )
        self.corners.cellClicked.connect(self._corner)
        self.body.addWidget(self.corners)
        lower = QHBoxLayout()
        lower.setSpacing(16)
        self.sectors = make_table(["Sector", "Lap", "Ref", "Δ"])
        self.kpis = make_table(["Driver input", "Lap", "Ref"])
        sl, kl = QVBoxLayout(), QVBoxLayout()
        sl.addWidget(label("SECTORS", "label"))
        sl.addWidget(self.sectors)
        kl.addWidget(label("DRIVER INPUTS", "label"))
        kl.addWidget(self.kpis)
        lower.addLayout(sl, 1)
        lower.addLayout(kl, 1)
        self.body.addLayout(lower)
        self.notes = label("", "muted", wrap=True)
        self.body.addWidget(self.notes)
        self._corners: list[dict] = []
        self.codes: dict[str, str] = {}

    def set_codes(self, agents: list[dict]) -> None:
        self.codes = {a.get("id"): a.get("code") or a.get("id", "?")[:3].upper() for a in agents}

    def _corner(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._corners):
            self.corner_picked.emit(self._corners[row])

    def set_error(self, text: str) -> None:
        self.error.setText(f"Findings unavailable: {text}")
        self.error.show()
        self.corners.setRowCount(0)
        self._corners = []

    def set_findings(self, f: dict | None) -> None:
        self.error.hide()
        if not f:
            return
        self.title.setText(f"Coaching findings · lap {f.get('main_lap')} vs lap {f.get('ref_lap')}")
        chief = f.get("chief") or {}
        net = chief.get("net_lap")
        self.c_delta.set(
            signed(net, 3, " s"),
            f"{lap_time(f.get('main_time'))} vs {lap_time(f.get('ref_time'))}",
            t.SEVERITY_TEXT["advisory"] if is_num(net) and net > 0 else t.GOOD,
        )
        self.c_lost.set(num(chief.get("lost"), 3, " s"), "gross")
        self.c_losing.set(f"{chief.get('losing_n', NA)} of {chief.get('total', NA)}")
        self.c_spread.set(num(chief.get("time_spread"), 3, " s"), "slowest − fastest")
        clear_layout(self.top_lines)
        for item in chief.get("top3") or []:
            self.top_lines.addWidget(
                label(
                    f"{review.priority_title(item, f.get('corners'))} — {item.get('why')} — "
                    f"{num(item.get('gain'), 2)} s",
                    size=13,
                )
            )
        if not chief.get("top3"):
            self.top_lines.addWidget(label("no corner losing time", "muted"))
        self._corners = f.get("corners") or []
        self.corners.setRowCount(len(self._corners))
        for r, c in enumerate(self._corners):
            dt = c.get("net_dt")
            color = t.SEVERITY_TEXT["advisory"] if is_num(dt) and dt > 0 else t.GOOD
            diags = c.get("diags") or []
            lines = [
                f"{self.codes.get(d.get('agent'), str(d.get('agent', '?'))[:3].upper())}  "
                f"{'✓ ' if d.get('good') else ''}{d.get('text')}"
                for d in diags
            ]
            sector = c.get("sector")
            self.corners.setItem(r, 0, _item(review.corner_title(c), t.INK, mono=not c.get("name")))
            self.corners.setItem(r, 1, _item(str(c.get("type") or ""), mono=False))
            self.corners.setItem(r, 2, _item(f"S{sector}" if sector is not None else NA))
            self.corners.setItem(
                r, 3, _item(f"{num(c.get('min_main'), 1)} / {num(c.get('min_ref'), 1)} km/h")
            )
            self.corners.setItem(r, 4, _item(signed(dt, 3, " s"), color))
            self.corners.setItem(
                r,
                5,
                _item(
                    "\n".join(lines) if lines else "no diagnosis",
                    None if lines else t.MUTED,
                    mono=False,
                ),
            )
        fit_height(self.corners)
        sectors = f.get("sectors") or []
        self.sectors.setRowCount(len(sectors))
        for r, s in enumerate(sectors):
            d = s.get("delta")
            self.sectors.setItem(r, 0, _item(str(s.get("name"))))
            self.sectors.setItem(r, 1, _item(num(s.get("main"), 3)))
            self.sectors.setItem(r, 2, _item(num(s.get("ref"), 3)))
            self.sectors.setItem(
                r,
                3,
                _item(signed(d, 3), t.SEVERITY_TEXT["advisory"] if is_num(d) and d > 0 else t.GOOD),
            )
        fit_height(self.sectors)
        kpis = f.get("kpis") or {}
        main, ref = kpis.get("main") or {}, kpis.get("ref") or {}
        rows = [
            ("Trail-brake", "trail_brake_pct", 1, "%"),
            ("Avg throttle", "avg_throttle_pct", 1, "%"),
            ("Throttle smoothness", "throttle_smoothness", 1, ""),
            ("Steering corrections", "steer_corrections", 0, ""),
            ("Coasting", "coasting_pct", 2, "%"),
        ]
        self.kpis.setRowCount(len(rows))
        for r, (name, key, digits, unit) in enumerate(rows):
            self.kpis.setItem(r, 0, _item(name, mono=False))
            self.kpis.setItem(r, 1, _item(num(main.get(key), digits, unit)))
            self.kpis.setItem(r, 2, _item(num(ref.get(key), digits, unit)))
        fit_height(self.kpis)
        notes = f.get("notes") or []
        self.notes.setText("\n".join(f"• {n}" for n in notes))
        self.notes.setVisible(bool(notes))

    def select_corner(self, index: int | None) -> None:
        if index is None:
            self.corners.clearSelection()
        else:
            self.corners.selectRow(index)


OUTCOMES = ["", "radio", "refused", "error", "unfinished"]


class AuditPanel(Panel):
    """One row per agent run, with filters and sorts; an honest empty state."""

    filters_changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Agent audit", parent)
        self.agent = QComboBox()
        self.agent.addItem("all agents", "")
        for a in AGENTS:
            self.agent.addItem(t.AGENT_LABEL[a], a)
        self.outcome = QComboBox()
        for o in OUTCOMES:
            self.outcome.addItem({"": "all outcomes", "radio": "spoke"}.get(o, o), o)
        self.sort = QComboBox()
        for key, text in (
            ("time", "sort: time"),
            ("latency", "sort: slowest"),
            ("calls", "sort: most calls"),
        ):
            self.sort.addItem(text, key)
        for combo, name in (
            (self.agent, "Agent filter"),
            (self.outcome, "Outcome filter"),
            (self.sort, "Sort"),
        ):
            combo.setAccessibleName(name)
            combo.currentIndexChanged.connect(lambda _i: self.filters_changed.emit())
            self.header.addWidget(combo)
        self.table = make_table(
            [
                "Time",
                "Lap",
                "Agent",
                "Trigger",
                "Model",
                "Calls",
                "Tools",
                "Validator",
                "Outcome",
                "Latency",
                "What aired",
            ]
        )
        self.body.addWidget(self.table)
        self.empty = label("", "empty", wrap=True)
        self.body.addWidget(self.empty)

    def filters(self) -> dict:
        return {
            "agent": self.agent.currentData() or "",
            "outcome": self.outcome.currentData() or "",
            "sort": self.sort.currentData() or "time",
        }

    def set_rows(self, rows: list[dict], total: int, agents_recorded: bool) -> None:
        self.meta.setText(f"{total} runs")
        if not total:
            self.table.hide()
            self.empty.setText(
                "This run was recorded with the agents off. Agent figures in the summary read 0;"
                " record a run with the agents on to audit them (it asks before spending Kimi"
                " quota)."
                if not agents_recorded
                else "No agent runs in this run."
            )
            self.empty.show()
            return
        self.empty.hide()
        self.table.show()
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            outcome = row.get("outcome") or "unfinished"
            validator = (
                "✓ ok"
                if row["validations"] and not row["unsupported"]
                else (
                    "withheld " + ", ".join(map(str, row["unsupported"]))[:40]
                    if row["unsupported"]
                    else "—"
                )
            )
            vcolor = t.GOOD if validator.startswith("✓") else "#ffd27a"
            latency = row.get("latency_ms")
            values = [
                (clock(row.get("t")), None),
                (str(row.get("lap") if row.get("lap") is not None else NA), None),
                (agent_badge(row.get("agent")).lower(), t.agent_text(row.get("agent"))),
                (humanise(row.get("event_key")), None),
                (row.get("model") or NA, None),
                (str(row["llm_calls"]), None),
                (str(len(row["tools"])), None),
                (validator, vcolor),
                (
                    {"radio": "spoke"}.get(outcome, outcome),
                    t.OUTCOME.get(outcome, t.OUTCOME["unfinished"])[1],
                ),
                (f"{latency / 1000:.1f} s" if is_num(latency) else NA, None),
                (row.get("spoken") or "", t.SOFT),
            ]
            for c, (text, color) in enumerate(values):
                self.table.setItem(r, c, _item(text, color, mono=c not in (3, 10)))
        fit_height(self.table, 40)
