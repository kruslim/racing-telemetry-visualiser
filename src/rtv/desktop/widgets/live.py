"""Live-pitwall panels: flag band, status strip, replay bar, strategy, tower, ticker.

Each panel takes a view built by ``rtv.desktop.model.view_model`` and paints it;
none of them decides anything about the race.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..model import view_model as vm
from ..model.format import (
    NA,
    clock,
    humanise,
    is_num,
    lap_time,
    num,
    signed,
    thousands,
    trend_arrow,
)
from ..theme import tokens as t
from .charts import PitWindowBar, Sparkline
from .common import (
    KeyValue,
    Panel,
    clear_layout,
    label,
    pill,
    set_pill,
    set_text,
    severity_tag,
    tag,
)

FLAG_ICON = {"yellow": "⚠", "red": "■", "white": "□", "checkered": "⚑", "blue": "»", "unknown": "?"}


class FlagBand(QFrame):
    """Green is a hairline. Any other phase takes the full width, and yellow/red pulse."""

    def __init__(self, parent: QWidget | None = None, *, reduced_motion: bool = False):
        super().__init__(parent)
        self.reduced_motion = reduced_motion
        self.phase = "unknown"
        self._bright = True
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(14)
        layout.addStretch(1)
        self.icon = label("", size=24, bold=True)
        self.text = label("", size=26, bold=True)
        self.detail = label("", size=13, bold=True)
        for w in (self.icon, self.text, self.detail):
            layout.addWidget(w)
        layout.addStretch(1)
        self._pulse = QTimer(self)
        self._pulse.setInterval(650)
        self._pulse.timeout.connect(self._toggle)
        self.set_flag({"phase": "unknown", "label": "", "active": [], "alert": False})

    def set_flag(self, flag: dict, since: float | None = None) -> None:
        phase = flag.get("phase") or "unknown"
        self.phase = phase
        alert = flag.get("alert") and phase != "unknown"
        self.setAccessibleName(f"Flag: {phase}")
        if not alert:
            self.setFixedHeight(3)
            self.icon.hide(), self.text.hide(), self.detail.hide()
            self._pulse.stop()
            self._paint(t.FLAG.get(phase, t.FLAG["unknown"]), t.INK)
            return
        self.setFixedHeight(56)
        for w in (self.icon, self.text, self.detail):
            w.show()
        ink = t.FLAG_INK.get(phase, "#ffffff")
        self.icon.setText(FLAG_ICON.get(phase, "⚑"))
        self.text.setText(flag.get("label") or phase.upper())
        active = ", ".join(humanise(a) for a in flag.get("active") or [])
        self.detail.setText(active + (f" · since {clock(since)}" if is_num(since) else ""))
        self._paint(t.FLAG.get(phase, t.FLAG["unknown"]), ink)
        if phase in ("yellow", "red") and not self.reduced_motion:
            self._pulse.start()
        else:
            self._pulse.stop()

    def _toggle(self) -> None:
        self._bright = not self._bright
        dim = "#b39500" if self.phase == "yellow" else "#8f1518"
        color = t.FLAG.get(self.phase, t.FLAG["unknown"]) if self._bright else dim
        self.setStyleSheet(f"FlagBand {{ background: {color}; }}")

    def _paint(self, bg: str, ink: str) -> None:
        self._bright = True
        self.setStyleSheet(f"FlagBand {{ background: {bg}; }}")
        for w in (self.icon, self.text, self.detail):
            w.setStyleSheet(f"color: {ink}; font-weight: 800;")


class StatusStrip(QFrame):
    """Session, the big remaining figure, lap/position/times, conditions, feed, AI."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setStyleSheet(
            f"#panel {{ border-radius: 0; border-width: 0 0 1px 0; background: {t.PANEL}; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        def cell(*widgets, stretch=0):
            box = QWidget()
            inner = QHBoxLayout(box)
            inner.setContentsMargins(14, 8, 14, 8)
            inner.setSpacing(16)
            for w in widgets:
                inner.addWidget(w)
            layout.addWidget(box, stretch)
            return box

        self.session = KeyValue("Session", "big")
        self.session.value.setFont(self.font())
        cell(self.session)
        self.remaining = KeyValue("Time remaining", "hero")
        cell(self.remaining)
        self.lap = KeyValue("Lap", "value")
        self.position = KeyValue("Position", "value")
        self.last = KeyValue("Last", "value")
        self.best = KeyValue("Best", "value")
        cell(self.lap, self.position, self.last, self.best)
        self.track = KeyValue("Track", "big")
        self.air = KeyValue("Air", "big")
        cell(self.track, self.air)
        layout.addStretch(1)

        feed = QWidget()
        fl = QVBoxLayout(feed)
        fl.setContentsMargins(14, 8, 14, 8)
        fl.setSpacing(4)
        fl.addWidget(label("FEED", "label"))
        self.feed_pill = pill("CONNECTING…", "#1c222a", t.SOFT)
        self.feed_detail = label("", "muted", mono=True)
        fl.addLayout(_row(self.feed_pill, self.feed_detail))
        layout.addWidget(feed)
        ai = QWidget()
        al = QVBoxLayout(ai)
        al.setContentsMargins(14, 8, 14, 8)
        al.setSpacing(4)
        al.addWidget(label("PITWALL AI", "label"))
        self.ai_pill = pill("AI —", "#1c222a", t.SOFT)
        self.ai_detail = label("", "muted")
        self.ai_detail.setMaximumWidth(300)
        al.addLayout(_row(self.ai_pill, self.ai_detail))
        layout.addWidget(ai)

    def set_view(self, view: dict, state: dict | None, frozen: bool = False) -> None:
        state = state or {}
        session = view["session"]
        caps = state.get("capabilities") or {}
        if not caps.get("session"):
            self.session.set("waiting for telemetry", t.MUTED)
        else:
            self.session.set(f"{session['state']} · {session['track']}")
        remaining = view["remaining"]
        self.remaining.key.setText(remaining["unit"].upper())
        text = remaining["text"] if remaining["kind"] != "time" else clock(remaining["seconds"])
        ink = t.MUTED if frozen else t.INK
        self.remaining.set(text, ink)
        player = state.get("player") or {}
        self.lap.set(str(player["lap"]) if is_num(player.get("lap")) else NA, ink)
        pos = player.get("position")
        self.position.set(f"P{pos}" if is_num(pos) and pos > 0 else NA, ink)
        self.last.set(lap_time(player.get("last_lap_time")), ink)
        self.best.set(
            lap_time(player.get("best_lap_time")), t.MUTED if frozen else t.AGENT_TEXT["strategist"]
        )
        cond = view["conditions"]
        self.track.set(num(cond["track"], 1, " °C"), ink)
        self.air.set(num(cond["air"], 1, " °C"), ink)

    def set_feed(self, state: str, detail: str) -> None:
        colors = {
            "live": ("#13301f", t.GOOD, "● LIVE"),
            "replay": ("#1d2a3a", t.ACCENT_SOFT, "▶ REPLAY"),
            "offline": ("#3a1616", "#ffb3ae", "○ DISCONNECTED"),
            "reconnecting": ("#33280f", t.SEVERITY_TEXT["advisory"], "↻ RECONNECTING"),
            "connecting": ("#1c222a", t.SOFT, "CONNECTING…"),
            "stale": ("#2a2414", "#ffd27a", "STALE"),
            "idle": ("#1c222a", t.SOFT, "○ NO DATA"),
        }
        bg, fg, text = colors.get(state, colors["idle"])
        set_pill(self.feed_pill, text, bg, fg)
        self.feed_detail.setText(detail)

    def set_ai(self, llm: dict) -> None:
        if not llm.get("available"):
            set_pill(self.ai_pill, "AI OFF", "#1c222a", t.SOFT)
        elif llm.get("exhausted"):
            set_pill(
                self.ai_pill,
                f"AI · BUDGET {llm.get('calls')}/{llm.get('max_calls')}",
                "#3a1616",
                "#ffb3ae",
            )
        else:
            calls = llm.get("calls")
            cap = llm.get("max_calls")
            word = "MUTED" if llm.get("label") == "AI MUTED" else "ON"
            text = f"{word} · {NA if calls is None else calls}/{cap or NA} calls"
            tone = llm.get("tone")
            bg, fg = (
                ("#13301f", t.GOOD) if tone == "ok" else ("#33280f", t.SEVERITY_TEXT["advisory"])
            )
            if word == "MUTED":
                bg, fg = "#1c222a", t.SOFT
            set_pill(self.ai_pill, text, bg, fg)
        detail = llm.get("detail") or ""
        short = detail if len(detail) <= 48 else detail[:46].rstrip() + "…"
        self.ai_detail.setText(short)
        self.ai_detail.setToolTip(llm.get("detail") or "")


def _row(*widgets, spacing: int = 8) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for w in widgets:
        layout.addWidget(w)
    layout.addStretch(1)
    return layout


SPEEDS = [("1×", 1.0), ("2×", 2.0), ("4×", 4.0), ("8×", 8.0), ("max", 0.0)]


class ReplayBar(QFrame):
    """Pick a stored session and a speed, then start/stop; plus live capture."""

    start_replay = Signal(str, float)
    stop_replay = Signal()
    start_live = Signal()
    stop_live = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("chrome")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(10)
        layout.addWidget(label("REPLAY", "label"))
        self.sessions = QComboBox()
        self.sessions.setAccessibleName("Replay session")
        self.sessions.setMinimumWidth(200)
        self.sessions.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.sessions.setMinimumContentsLength(24)
        layout.addWidget(self.sessions)
        self.speed_group = QButtonGroup(self)
        speeds = QHBoxLayout()
        speeds.setSpacing(0)
        for i, (text, value) in enumerate(SPEEDS):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setProperty("speed", value)
            button.setAccessibleName(f"Replay speed {text}")
            button.setMinimumWidth(44)
            self.speed_group.addButton(button, i)
            speeds.addWidget(button)
        self.speed_group.button(1).setChecked(True)
        layout.addLayout(speeds)
        self.start = QPushButton("Start")
        self.start.setProperty("role", "primary")
        self.stop = QPushButton("Stop")
        self.stop.setProperty("role", "danger")
        layout.addWidget(self.start)
        layout.addWidget(self.stop)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1000)
        self.progress.setMinimumWidth(60)
        layout.addWidget(self.progress, 1)
        self.progress_text = label("", "muted", mono=True)
        layout.addWidget(self.progress_text)
        sep = QFrame()
        sep.setFixedSize(1, 24)
        sep.setStyleSheet(f"background: {t.LINE};")
        layout.addWidget(sep)
        self.live = QPushButton("Start live capture")
        self.live.setAccessibleName("Start or stop live capture from iRacing")
        layout.addWidget(self.live)
        self.note = label("", "muted")
        layout.addWidget(self.note)
        self.start.clicked.connect(self._start)
        self.stop.clicked.connect(self.stop_replay)
        self.live.clicked.connect(self._live)
        self._live_running = False
        self.set_sessions([])

    def set_sessions(self, sessions: list[dict]) -> None:
        current = self.sessions.currentData()
        self.sessions.clear()
        if not sessions:
            self.sessions.addItem("Pick a recorded session (import an .ibt first)", None)
            self.sessions.setEnabled(False)
            self.start.setEnabled(False)
            return
        self.sessions.setEnabled(True)
        for s in sessions:
            frames = thousands(s.get("sample_count"))
            text = f"{s.get('label') or s.get('kind')} — {s.get('track_name') or '?'}"
            self.sessions.addItem(f"{text} · {frames} frames", s.get("session_id"))
        if current:
            index = self.sessions.findData(current)
            if index >= 0:
                self.sessions.setCurrentIndex(index)
        self.start.setEnabled(True)

    def speed(self) -> float:
        button = self.speed_group.checkedButton()
        return float(button.property("speed")) if button else 1.0

    def _start(self) -> None:
        sid = self.sessions.currentData()
        if sid:
            self.start_replay.emit(sid, self.speed())

    def _live(self) -> None:
        (self.stop_live if self._live_running else self.start_live).emit()

    def set_status(self, replay: dict | None, live: dict | None) -> None:
        replay = replay or {}
        live = live or {}
        running = bool(replay.get("running"))
        self._live_running = bool(live.get("running"))
        total = replay.get("total") or 0
        frames = replay.get("frames") or 0
        self.progress.setValue(int(frames / total * 1000) if total else 0)
        if running:
            self.progress_text.setText(f"{thousands(frames)} / {thousands(total)} frames")
        elif replay.get("finished"):
            self.progress_text.setText("replay finished")
        elif replay.get("error"):
            self.progress_text.setText(f"replay failed: {replay['error']}")
        else:
            self.progress_text.setText("")
        has_sessions = self.sessions.isEnabled()
        self.start.setEnabled(has_sessions and not running and not self._live_running)
        self.stop.setEnabled(running)
        self.sessions.setEnabled(has_sessions and not running)
        for b in self.speed_group.buttons():
            b.setEnabled(not running and not self._live_running)
        self.live.setText("Stop live capture" if self._live_running else "Start live capture")
        self.live.setEnabled(not running)
        if self._live_running:
            self.note.setText(
                f"live: {live.get('state') or 'waiting for iRacing'}"
                " · replays are refused while live owns the engine"
            )
        elif running:
            self.note.setText("stop the replay to capture from iRacing")
        else:
            self.note.setText("")


class StrategyPanel(Panel):
    """Recommendation card, pit-window bar, fuel block with sparkline, tyre grid."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Strategy", parent)
        self._rec: object = object()  # never equal to a real recommendation
        # recommendation card
        self.card = QFrame()
        self.card.setObjectName("card")
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(14, 10, 14, 12)
        cl.setSpacing(6)
        top = QHBoxLayout()
        self.card_tag_slot = QHBoxLayout()
        top.addLayout(self.card_tag_slot)
        top.addStretch(1)
        self.card_meta = label("", "muted", mono=True)
        top.addWidget(self.card_meta)
        cl.addLayout(top)
        self.card_call = label("NO CALL YET", size=26, bold=True)
        cl.addWidget(self.card_call)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.k_target = KeyValue("Target lap")
        self.k_fuel = KeyValue("Fuel to add")
        self.k_rejoin = KeyValue("Rejoin")
        self.k_conf = KeyValue("Confidence")
        for i, kv in enumerate((self.k_target, self.k_fuel, self.k_rejoin, self.k_conf)):
            grid.addWidget(kv, i // 2, i % 2)
        cl.addLayout(grid)
        self.card_spoken = label("", "soft", wrap=True)
        cl.addWidget(self.card_spoken)
        self.body.addWidget(self.card)
        # pit window
        head = QHBoxLayout()
        head.addWidget(label("PIT WINDOW", "label"))
        head.addStretch(1)
        self.window_state = label("", bold=True, size=12)
        head.addWidget(self.window_state)
        self.body.addLayout(head)
        self.window = PitWindowBar()
        self.body.addWidget(self.window)
        # fuel
        fuel = QHBoxLayout()
        fuel.setSpacing(12)
        self.margin_box = QFrame()
        self.margin_box.setObjectName("card")
        ml = QVBoxLayout(self.margin_box)
        ml.setContentsMargins(12, 8, 12, 8)
        ml.setSpacing(0)
        ml.addWidget(label("FUEL MARGIN", "label"))
        self.margin = label(NA, "hero", mono=True)
        self.margin_l = label("", mono=True)
        ml.addWidget(self.margin)
        ml.addWidget(self.margin_l)
        fuel.addWidget(self.margin_box, 11)
        fg = QGridLayout()
        fg.setHorizontalSpacing(12)
        self.k_laps_fuel = KeyValue("Laps of fuel")
        self.k_to_finish = KeyValue("To finish")
        self.k_tank = KeyValue("In tank")
        self.k_per_lap = KeyValue("Per lap")
        fg.addWidget(self.k_laps_fuel, 0, 0)
        fg.addWidget(self.k_to_finish, 0, 1)
        fg.addWidget(self.k_tank, 1, 0)
        fg.addWidget(self.k_per_lap, 1, 1)
        fuel.addLayout(fg, 10)
        self.body.addLayout(fuel)
        spark = QHBoxLayout()
        spark.addWidget(label("PER-LAP USE", "label"))
        self.spark = Sparkline()
        spark.addWidget(self.spark, 1)
        self.spark_n = label("", "muted", mono=True)
        spark.addWidget(self.spark_n)
        self.body.addLayout(spark)
        # tyres
        tyre_head = QHBoxLayout()
        tyre_head.addWidget(label("TYRES", "label"))
        tyre_head.addStretch(1)
        self.tyre_meta = label("", "muted")
        tyre_head.addWidget(self.tyre_meta)
        self.body.addLayout(tyre_head)
        tg = QGridLayout()
        tg.setSpacing(8)
        self.tyres: dict[str, tuple[QLabel, QLabel]] = {}
        for i, corner in enumerate(vm.CORNERS):
            box = QFrame()
            box.setObjectName("card")
            bl = QHBoxLayout(box)
            bl.setContentsMargins(10, 6, 10, 6)
            bl.addWidget(label(corner, bold=True))
            bl.addStretch(1)
            temp = label(NA, mono=True)
            pressure = label(NA, mono=True)
            bl.addWidget(temp)
            bl.addSpacing(10)
            bl.addWidget(pressure)
            self.tyres[corner] = (temp, pressure)
            tg.addWidget(box, i // 2, i % 2)
        self.body.addLayout(tg)
        self.tyre_note = label("", "muted")
        self.body.addWidget(self.tyre_note)

    def set_recommendation(self, rec: dict | None) -> None:
        if rec == self._rec:
            return  # called every render; rebuilding the tag each tick makes it flicker
        self._rec = rec
        clear_layout(self.card_tag_slot)
        if not rec:
            self.card_call.setText("NO CALL YET")
            self.card_meta.setText("the strategist has not spoken")
            for kv in (self.k_target, self.k_fuel, self.k_rejoin, self.k_conf):
                kv.set(NA, t.MUTED)
            self.card_spoken.setText("")
            self.card.setStyleSheet(f"#card {{ background: {t.PANEL_2}; }}")
            return
        sev = rec["priority"]
        self.card_tag_slot.addWidget(severity_tag(sev))
        if rec["refused"]:
            self.card_tag_slot.addWidget(tag("withheld", "#2a2414", "#ffd27a"))
        lap = rec.get("lap")
        self.card_meta.setText(
            f"strategist · L{lap if lap is not None else NA} · {clock(rec.get('session_time'))}"
        )
        self.card_call.setText(rec["call"])
        self.k_target.set(num(rec["target_lap"], 0) if rec["target_lap"] is not None else NA)
        self.k_fuel.set(num(rec["fuel_to_add_l"], 1, " L"))
        rejoin = rec["rejoin_position"]
        self.k_rejoin.set(
            f"P{rejoin}" if rejoin is not None else "n/a · no standings",
            None if rejoin is not None else t.MUTED,
        )
        self.k_conf.set(rec["confidence"] or NA)
        self.card_spoken.setText(rec["spoken"])
        border = {"critical": "#4a2c2c", "advisory": "#4a3c1c"}.get(sev, t.LINE)
        fill = {"critical": "#1d1214", "advisory": "#1d1a12"}.get(sev, t.PANEL_2)
        self.card.setStyleSheet(f"#card {{ background: {fill}; border: 1px solid {border}; }}")

    def set_view(self, view: dict, history: list[dict], frozen: bool = False) -> None:
        stint = view["stint"]
        self.meta.setText(
            f"stint {stint['number'] if stint['number'] is not None else NA} · "
            f"{stint['laps_on_tyres'] if stint['laps_on_tyres'] is not None else NA} laps on tyres"
        )
        window = view["window"]
        self.window.set_view(window, view["available"])
        if view["available"] and window and window.get("earliest") is not None:
            word = "OPEN" if window["open"] else "CLOSED"
            set_text(
                self.window_state,
                f"{word} · laps {window['earliest']}"
                f"–{window['latest'] if window['latest'] is not None else NA}",
                t.GOOD if window["open"] else t.MUTED,
            )
        else:
            set_text(self.window_state, "unavailable", t.MUTED)
        fuel = view["fuel"]
        band = fuel["band"]
        color = t.MUTED if frozen else t.BAND[band]
        self.margin.setText(signed(fuel["margin_laps"], 2))
        self.margin.setStyleSheet(f"color: {color};")
        word = {"red": "short: stop needed", "amber": "tight", "green": "covered"}.get(band, "")
        self.margin_l.setText(
            f"{signed(fuel['margin_l'], 1, ' L')}{' · ' + word if word else ''}"
            if fuel["margin_l"] is not None
            else "laps · no fuel model"
        )
        self.margin_l.setStyleSheet(f"color: {color};")
        self.margin_box.setStyleSheet(
            f"#card {{ background: {t.BAND_BG[band]}; border: 1px solid "
            f"{t.BAND[band] if band != 'unknown' else t.LINE}; }}"
        )
        self.k_laps_fuel.set(num(fuel["laps_remaining"], 1))
        self.k_to_finish.set(num(fuel["laps_to_finish"], 1))
        pct = fuel.get("level_pct")
        tank = num(fuel["level"], 1, " L")
        self.k_tank.set(tank + (f"  {num(pct * 100, 0)}%" if is_num(pct) else ""))
        per = num(fuel["per_lap"], 3)
        std = fuel["per_lap_std"]
        self.k_per_lap.set(per + (f" ± {num(std, 3)}" if is_num(std) else ""))
        self.spark.set_values([h["per_lap"] for h in history])
        self.spark_n.setText(f"{len(history)} laps" if history else "")
        tyres = view["tyres"]
        self.tyre_meta.setText(f"stint {tyres['stint_laps']} laps")
        for corner in tyres["corners"]:
            temp_l, pres_l = self.tyres[corner["key"]]
            if not tyres["available"]:
                temp_l.setText(NA)
                pres_l.setText("")
                continue
            arrow_t = trend_arrow(corner["temp_trend"], vm.TYRE_TEMP_DEADBAND)
            arrow_p = trend_arrow(corner["pressure_trend"], vm.TYRE_PRESSURE_DEADBAND)
            temp_l.setText(f"{num(corner['temp'], 1, ' °C')} {arrow_t}".strip())
            pres_l.setText(f"{num(corner['pressure'], 1, ' kPa')} {arrow_p}".strip())
        self.tyre_note.setText("" if tyres["available"] else "no tyre channels")
        self.tyre_note.setVisible(not tyres["available"])


class TowerPanel(Panel):
    """Running order ±3 around the player; honest about .ibt files having no field."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Timing tower", parent)
        self.expanded = False
        self.toggle = QPushButton("Full field")
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self._toggled)
        self.header.addWidget(self.toggle)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(2)
        self.body.addLayout(self.rows)
        self.body.addStretch(1)
        self._last: tuple | None = None
        self.on_toggle = None

    def _toggled(self, on: bool) -> None:
        self.expanded = on
        if self.on_toggle:
            self.on_toggle()

    def set_view(self, view: dict, state: dict | None) -> None:
        caps = (state or {}).get("capabilities") or {}
        key = (
            view["no_field"],
            view["available"],
            bool(caps.get("session")),
            tuple(
                (
                    r["idx"],
                    r["position"],
                    r["gap_to_player"],
                    r["last_lap"],
                    r["trend"],
                    bool(r["warning"]),
                    r["on_pit_road"],
                )
                for r in view["rows"]
            ),
            view["player"]["position"],
            view["player"]["last_lap"],
            view["gaps_known"],
        )
        if key == self._last:
            return
        self._last = key
        clear_layout(self.rows)
        self.toggle.setVisible(view["available"])
        if not caps.get("session"):
            self.meta.setText("")
            self.rows.addWidget(label("waiting for telemetry", "muted"))
            return
        if view["no_field"]:
            self.meta.setText("replay of an .ibt")
            self.rows.addWidget(self._player_row(view["player"]))
            self.rows.addWidget(label("No field data in this recording", bold=True))
            self.rows.addWidget(
                label(
                    "iRacing .ibt files do not carry the other cars. Gaps, rivals and spotter "
                    "traffic calls only come from live telemetry.",
                    "muted",
                    wrap=True,
                )
            )
            return
        self.meta.setText(
            f"{view['total']} cars" + (f" · {view['hidden']} hidden" if view["hidden"] else "")
        )
        if not view["gaps_known"]:
            self.rows.addWidget(
                label(
                    "Gaps unavailable (no gap basis yet): shown as n/a.",
                    color=t.SEVERITY_TEXT["advisory"],
                    wrap=True,
                )
            )
        header = _tower_row("POS", "CAR", "GAP", "LAST", "")
        for w in header.findChildren(QLabel):
            w.setProperty("role", "label")
        self.rows.addWidget(header)
        for row in view["rows"]:
            status = ""
            color = t.SOFT
            if row["on_pit_road"]:
                status = "pit"
            if row["trend"] == "closing":
                status, color = "▲ closing", t.AGENT_TEXT["spotter"]
            elif row["trend"] == "opening":
                status = "▽ opening"
            if row["warning"]:
                kind = row["warning"]["kind"]
                status = {
                    "blue_flag": "» blue",
                    "traffic_close": "▲ traffic",
                    "rival_pitted": "pitted",
                }.get(kind, kind)
                color = "#a9c2ff" if kind == "blue_flag" else color
            pos = f"P{row['position']}" if row["position"] is not None else NA
            widget = _tower_row(
                pos,
                "You" if row["is_player"] else row["label"],
                "—" if row["is_player"] else _gap(row["gap_to_player"]),
                lap_time(row["last_lap"]),
                status,
                color,
            )
            if row["is_player"]:
                _mark_player(widget)
            self.rows.addWidget(widget)

    def _player_row(self, player: dict) -> QWidget:
        pos = player["position"]
        widget = _tower_row(f"P{pos}" if pos else NA, "You", "", lap_time(player["last_lap"]), "")
        _mark_player(widget)
        return widget


def _gap(value) -> str:
    from ..model.format import gap

    return gap(value)


def _mark_player(widget: QWidget) -> None:
    widget.setObjectName("playerRow")
    widget.setStyleSheet(
        "#playerRow { background: #1a2532; border: 1px solid #2f4a66; border-radius: 6px; }"
    )


def _tower_row(
    pos: str, car: str, gap: str, last: str, status: str, status_color: str = t.SOFT
) -> QWidget:
    widget = QFrame()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(10, 5, 10, 5)
    layout.setSpacing(8)
    for text, width, bold, color in (
        (pos, 46, True, None),
        (car, 0, False, None),
        (gap, 70, False, None),
        (last, 84, False, None),
        (status, 90, False, status_color),
    ):
        lab = label(text, mono=True, bold=bold, color=color)
        if width:
            lab.setFixedWidth(width)
            layout.addWidget(lab)
        else:
            layout.addWidget(lab, 1)
    return widget


class TickerStrip(QFrame):
    """Deterministic race events, newest first, with their severity as icon and colour."""

    LIMIT = 8

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("chromeTop")
        self.setFixedHeight(36)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(12)
        self.count = label("RACE EVENTS 0", "label")
        layout.addWidget(self.count)
        holder = QWidget()
        # The newest events win the space; older ones are clipped, never forcing width.
        holder.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.items = QHBoxLayout(holder)
        self.items.setContentsMargins(0, 0, 0, 0)
        self.items.setSpacing(14)
        layout.addWidget(holder, 1)
        self.lines: list[dict] = []
        self.total = 0

    def set_events(self, lines: list[dict], total: int) -> None:
        lines = lines[: self.LIMIT]
        if lines == self.lines and total == self.total and self.items.count():
            return  # the live view renders at 10 Hz; rebuilding every tick blanks the strip
        self.lines = lines
        self.total = total
        self.count.setText(f"RACE EVENTS {total}")
        clear_layout(self.items)
        if not self.lines:
            self.items.addWidget(label("no race events yet", "muted"))
        for line in self.lines:
            sev = line["severity"] if line["severity"] in t.SEVERITY else "info"
            when = (f"L{line['lap']} " if line["lap"] is not None else "") + clock(
                line["session_time"]
            )
            lab = label(f"{t.SEVERITY_ICON[sev]} {when} {line['text']}", mono=True, size=12)
            lab.setStyleSheet(
                f"color: {t.SEVERITY_TEXT[sev] if sev != 'info' else t.INK}; font-size: 12px;"
            )
            lab.setToolTip(f"{sev}: {humanise(line['type'])}")
            self.items.addWidget(lab)
        self.items.addStretch(1)


class Banner(QFrame):
    """A full-width notice: errors (with the server's detail), budget, reconnecting."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)
        self.icon = label("", bold=True, size=16)
        self.text = label("", wrap=True)
        self.text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.close = QPushButton("Dismiss")
        self.close.clicked.connect(self.hide)
        layout.addWidget(self.icon)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.close)
        self.hide()

    def show_message(self, text: str, tone: str = "error", dismissable: bool = True) -> None:
        bg, fg, icon = {
            "error": ("#2a1414", "#ffd5d2", "ⓘ"),
            "budget": ("#3a1616", "#ffd5d2", "⊘"),
            "warn": ("#33280f", "#ffe3a3", "↻"),
        }.get(tone, ("#1c222a", t.INK, "ⓘ"))
        self.setStyleSheet(f"Banner {{ background: {bg}; }}")
        self.icon.setText(icon)
        self.icon.setStyleSheet(f"color: {fg};")
        self.text.setText(text)
        self.text.setStyleSheet(f"color: {fg};")
        self.close.setVisible(dismissable)
        self.setAccessibleName(text)
        self.show()
