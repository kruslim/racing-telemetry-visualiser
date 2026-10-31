"""The post-race review: a recorded race you scrub through, plus lap-vs-lap analysis.

Layout (the "Post-race review" artboard): session bar, race summary, replay
(lanes, car at cursor, radio and events so far, the agents as they stood), lap
analysis (lap table, traces, track map), coaching findings, agent audit. The
review replays the race journal; it never re-runs the agents.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..model import activity, review
from ..model import view_model as vm
from ..model.format import NA, clock, is_num, lap_time, thousands
from ..theme import tokens as t
from ..widgets.agents import FlowGraph, RunsWaterfall
from ..widgets.charts import LaneTimeline, TraceStack, TrackMap
from ..widgets.common import Panel, label
from ..widgets.dialogs import QuotaDialog, agents_will_run
from ..widgets.live import Banner
from ..widgets.review import (
    AuditPanel,
    CarStatePanel,
    FindingsPanel,
    LapTable,
    MiniList,
    SessionBar,
    SummaryPanel,
)
from .hub import Hub

CHANNELS = [
    ("Speed", "SPEED", "km/h", 3.6, 0, 320),
    ("Throttle", "THROTTLE", "%", 100, 0, 100),
    ("Brake", "BRAKE", "%", 100, 0, 100),
    ("SteeringWheelAngle", "STEERING", "deg", 57.29578, -180, 180),
    ("Gear", "GEAR", "", 1, 0, 7),
]
PLAY_SPEEDS = [1, 4, 16, 60]
POLL_JOB_MS = 700


class ReviewView(QWidget):
    def __init__(self, hub: Hub, parent: QWidget | None = None, *, embedded: bool = True):
        super().__init__(parent)
        self.hub = hub
        self.api = hub.api
        self.embedded = embedded  # same machine as the server: import by path
        self.setObjectName("root")
        self.session_id: str | None = None
        self.run_id: str | None = None
        self.runs_meta: list[dict] = []
        self.laps: list[dict] = []
        self.records: list[dict] = []
        self.lanes: dict = {}
        self.bounds: tuple[float, float] | None = None
        self.cursor: float | None = None
        self.main_lap: int | None = None
        self.ref_lap: int | None = None
        self.lap_length: float | None = None
        self.findings: dict | None = None
        self.map_points: list[dict] = []
        self._compare: dict = {}
        self._delta = None
        self._job: str | None = None
        self._recording = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.notice = Banner()
        outer.addWidget(self.notice)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)
        page = QWidget()
        page.setMaximumWidth(1600)
        body = QVBoxLayout(page)
        body.setContentsMargins(16, 16, 16, 24)
        body.setSpacing(16)
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addStretch(1)
        wl.addWidget(page, 100)
        wl.addStretch(1)
        scroll.setWidget(wrap)

        self.bar = SessionBar()
        body.addWidget(self.bar)
        self.empty = QFrame()
        self.empty.setObjectName("panel")
        self.empty.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(32, 40, 32, 40)
        el.addWidget(label("No sessions yet", size=20, bold=True), 0, Qt.AlignmentFlag.AlignHCenter)
        el.addWidget(
            label(
                "Import an iRacing .ibt to review a race: lap times, coaching "
                "findings, and the pitwall replayed from its journal.",
                "soft",
                wrap=True,
            ),
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        big = QPushButton("Import .ibt…")
        big.setProperty("role", "primary")
        big.setMinimumHeight(44)
        big.clicked.connect(self._import)
        el.addWidget(big, 0, Qt.AlignmentFlag.AlignHCenter)
        body.addWidget(self.empty)

        self.content = QWidget()
        cl = QVBoxLayout(self.content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(16)
        body.addWidget(self.content)
        body.addStretch(1)

        self.summary = SummaryPanel()
        cl.addWidget(self.summary)

        # replay
        self.replay_panel = Panel("Replay")
        self.play = QPushButton("▶ Play")
        self.play.setCheckable(True)
        self.play.setAccessibleName("Play the race")
        self.speed_group = QButtonGroup(self)
        self.replay_panel.header.addWidget(self.play)
        for i, s in enumerate(PLAY_SPEEDS):
            b = QPushButton(f"{s}×")
            b.setCheckable(True)
            b.setMinimumWidth(44)
            self.speed_group.addButton(b, i)
            self.replay_panel.header.addWidget(b)
        self.speed_group.button(0).setChecked(True)
        self.clock = label(NA, size=18, bold=True, mono=True)
        self.replay_panel.header.addWidget(self.clock)
        self.timeline = LaneTimeline(scrub=True)
        self.replay_panel.body.addWidget(self.timeline)
        self.replay_panel.body.addWidget(
            label(
                "Drag on the lanes to scrub; click an item to jump to it. Dashed spans ended"
                " without"
                " a call on air (refused or error).",
                "muted",
            )
        )
        row = QHBoxLayout()
        row.setSpacing(12)
        self.car = CarStatePanel()
        self.radio_so_far = MiniList("Radio so far")
        self.events_so_far = MiniList("Race events so far")
        row.addWidget(self.car, 1)
        row.addWidget(self.radio_so_far, 1)
        row.addWidget(self.events_so_far, 1)
        self.replay_panel.body.addLayout(row)
        self.flow = FlowGraph(reduced_motion=True)
        self.runs = RunsWaterfall()
        self.replay_panel.body.addWidget(label("THE PITWALL AT THE CURSOR", "label"))
        self.replay_panel.body.addWidget(self.flow)
        self.replay_panel.body.addWidget(self.runs)
        cl.addWidget(self.replay_panel)

        # lap analysis
        self.lap_panel = Panel("Lap analysis")
        self.main_pick = QComboBox()
        self.ref_pick = QComboBox()
        self.main_pick.setAccessibleName("Lap under review")
        self.ref_pick.setAccessibleName("Reference lap")
        for text, combo in (("Lap", self.main_pick), ("Reference", self.ref_pick)):
            self.lap_panel.header.addWidget(label(text, "muted"))
            self.lap_panel.header.addWidget(combo)
        self.lap_panel.header.addWidget(label("━ lap", color=t.LAP_TRACE, bold=True))
        self.lap_panel.header.addWidget(label("━ ref", color=t.REF_TRACE, bold=True))
        la = QHBoxLayout()
        la.setSpacing(16)
        self.lap_table = LapTable()
        self.traces = TraceStack()
        self.track = TrackMap()
        self.track.setMinimumWidth(340)
        la.addWidget(self.lap_table)
        la.addWidget(self.traces, 3)
        mapbox = QVBoxLayout()
        mapbox.addWidget(label("TRACK MAP · REF LAP", "label"))
        mapbox.addWidget(self.track, 1)
        self.track_facts = label("", "muted", size=11, wrap=True)
        mapbox.addWidget(self.track_facts)
        mapbox.addWidget(
            label("● off track / lock-up (advisory)   ● incident (critical)", "muted", size=11)
        )
        la.addLayout(mapbox, 2)
        self.lap_panel.body.addLayout(la)
        cl.addWidget(self.lap_panel)

        self.findings_panel = FindingsPanel()
        cl.addWidget(self.findings_panel)
        self.audit = AuditPanel()
        cl.addWidget(self.audit)

        # timers
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(100)
        self._play_timer.timeout.connect(self._tick)
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setSingleShot(True)
        self._cursor_timer.setInterval(120)
        self._cursor_timer.timeout.connect(self._load_cursor_state)
        self._job_timer = QTimer(self)
        self._job_timer.setInterval(POLL_JOB_MS)
        self._job_timer.timeout.connect(self._poll_job)

        # wiring
        hub.sessions_changed.connect(self._on_sessions)
        self.bar.session_changed.connect(self.select_session)
        self.bar.run_changed.connect(self.select_run)
        self.bar.record_clicked.connect(self._record)
        self.bar.import_clicked.connect(self._import)
        self.timeline.scrubbed.connect(self.set_cursor)
        self.play.toggled.connect(self._toggle_play)
        self.main_pick.currentIndexChanged.connect(lambda _i: self._laps_picked())
        self.ref_pick.currentIndexChanged.connect(lambda _i: self._laps_picked())
        self.lap_table.lap_picked.connect(self._lap_from_table)
        self.findings_panel.corner_picked.connect(self._corner)
        self.audit.filters_changed.connect(self._render_audit)
        self.api.get(
            "/coaching/agents",
            ok=lambda d: self.findings_panel.set_codes((d or {}).get("agents") or []),
            err=lambda *_: None,
        )
        self._on_sessions()

    # ---- state ---------------------------------------------------------
    def state_link(self) -> str:
        """The review's place, as the web review keeps it in its URL."""
        parts = [
            f"session={self.session_id or ''}",
            f"run={self.run_id or ''}",
            f"main={self.main_lap if self.main_lap is not None else ''}",
            f"ref={self.ref_lap if self.ref_lap is not None else ''}",
            f"t={round(self.cursor, 1) if is_num(self.cursor) else ''}",
        ]
        return "/review/?" + "&".join(parts)

    def _error(self, detail: str) -> None:
        self.notice.show_message(detail, "error")

    # ---- sessions and runs ---------------------------------------------
    def _on_sessions(self) -> None:
        sessions = self.hub.sessions
        if self.session_id not in {s.get("session_id") for s in sessions}:
            self.session_id = sessions[0]["session_id"] if sessions else None
        self.bar.set_sessions(sessions, self.session_id)
        if not sessions:
            self.bar.set_runs([], None)
        self.empty.setVisible(not sessions)
        self.content.setVisible(bool(sessions))
        if self.session_id and not self.laps:
            self.select_session(self.session_id)

    def select_session(self, session_id: str, run_id: str | None = None) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.records, self.laps, self.findings = [], [], None
        self.cursor = None
        self.bar.set_progress("loading the session…", None)
        self.api.get(
            f"/sessions/{session_id}/laps", ok=self._on_laps, err=lambda _s, d: self._error(d)
        )
        self._load_runs(run_id)

    def _load_runs(self, prefer: str | None = None) -> None:
        def ok(data):
            data = data or {}
            self.runs_meta = data.get("runs") or []
            choice = prefer if any(r.get("run_id") == prefer for r in self.runs_meta) else None
            choice = choice or data.get("default_run")
            self.bar.set_runs(self.runs_meta, choice)
            if choice:
                self.select_run(choice)
            else:
                self.run_id = None
                self.records = []
                self.bar.set_progress(
                    "No pitwall runs yet: record one from the telemetry to replay the race here.",
                    None,
                )
                self.summary.set_summary(None)
                self._load_summary()
                self._render_replay()
                self._render_audit()

        self.api.get(
            f"/sessions/{self.session_id}/pitwall-runs", ok=ok, err=lambda _s, d: self._error(d)
        )

    def select_run(self, run_id: str) -> None:
        self.run_id = run_id
        self.bar.set_progress("loading the race journal…", None)
        self._load_summary()

        def ok(data):
            data = data or {}
            self.records = data.get("records") or []
            meta = next((r for r in self.runs_meta if r.get("run_id") == run_id), {})
            self.bounds = review.bounds(meta, self.laps)
            self.lanes = review.lane_items(self.records, self.laps)
            truncated = " (truncated)" if data.get("truncated") else ""
            self.bar.set_progress(
                f"Ready · {len([lap for lap in self.laps if review.is_clean(lap)])} clean"
                f" laps · {thousands(len(self.records))} journal records{truncated} · "
                f"{len(self.lanes.get('radio') or [])} radio calls in this run",
                None,
            )
            if self.bounds and self.cursor is None:
                self.cursor = self.bounds[0]
            self._render_replay()
            self._render_audit()
            self._load_cursor_state()
            self._pins()

        self.api.get(
            f"/sessions/{self.session_id}/pitwall-log",
            {"run": run_id, "exclude": "state", "limit": 100000},
            ok=ok,
            err=lambda _s, d: self._error(d),
            key="review-log",
        )

    def _load_summary(self) -> None:
        self.api.get(
            f"/sessions/{self.session_id}/summary",
            {"run": self.run_id},
            ok=self.summary.set_summary,
            err=lambda _s, d: self._error(d),
            key="review-summary",
        )

    # ---- laps and analysis ---------------------------------------------
    def _on_laps(self, data) -> None:
        self.laps = (data or {}).get("laps") or []
        picks = review.default_laps(self.laps)
        self.main_lap, self.ref_lap = picks["main"], picks["ref"]
        for combo, chosen in ((self.main_pick, self.main_lap), (self.ref_pick, self.ref_lap)):
            combo.blockSignals(True)
            combo.clear()
            best = min((lap["lap_time"] for lap in self.laps if review.is_clean(lap)), default=None)
            for lap in self.laps:
                tags = []
                if not lap.get("is_valid"):
                    tags.append(
                        "not a race lap" if lap["lap"] == 0 or lap is self.laps[-1] else "invalid"
                    )
                if lap.get("is_in_lap"):
                    tags.append("in lap")
                if lap.get("is_out_lap"):
                    tags.append("out lap")
                if best is not None and lap.get("lap_time") == best and review.is_clean(lap):
                    tags.append("best")
                # Lap 0 (pre-start) and the unfinished last segment are not race laps.
                not_a_lap = not lap.get("is_valid") and (lap["lap"] == 0 or lap is self.laps[-1])
                time_text = NA if not_a_lap else lap_time(lap.get("lap_time"))
                combo.addItem(
                    f"{lap['lap']} · {time_text}" + (f" · {', '.join(tags)}" if tags else ""),
                    lap["lap"],
                )
            if chosen is not None:
                combo.setCurrentIndex(max(0, combo.findData(chosen)))
            combo.blockSignals(False)
        self.lap_table.set_laps(self.laps, self.main_lap, self.ref_lap)
        if self.run_id and self.records:
            meta = next((r for r in self.runs_meta if r.get("run_id") == self.run_id), {})
            self.bounds = review.bounds(meta, self.laps)
            self.lanes = review.lane_items(self.records, self.laps)
            self._render_replay()
        if self.main_lap is None:
            self.traces.set_message("No clean laps to compare in this session.")
            return
        self._load_analysis()

    def _lap_from_table(self, lap: int) -> None:
        self.main_pick.setCurrentIndex(max(0, self.main_pick.findData(lap)))

    def _laps_picked(self) -> None:
        main, ref = self.main_pick.currentData(), self.ref_pick.currentData()
        if main is None or ref is None or (main, ref) == (self.main_lap, self.ref_lap):
            return
        self.main_lap, self.ref_lap = main, ref
        self.lap_table.set_laps(self.laps, main, ref)
        self._load_analysis()

    def _load_analysis(self) -> None:
        sid, main, ref = self.session_id, self.main_lap, self.ref_lap
        self._compare, self._delta = {}, None
        self.traces.set_message("loading traces…")
        laps = f"{main},{ref}" if main != ref else f"{main}"
        for name, *_ in CHANNELS:
            self.api.get(
                f"/sessions/{sid}/compare",
                {"name": name, "laps": laps, "grid": 1000},
                ok=lambda d, n=name: self._on_compare(n, d),
                err=lambda _s, d, n=name: self._on_compare(n, None),
                key=f"compare-{name}",
            )
        self.api.get(
            "/coaching/delta",
            {"session_id": sid, "main_lap": main, "ref_lap": ref, "points": 500},
            ok=self._on_delta,
            err=lambda _s, _d: self._on_delta(None),
            key="delta",
        )
        self.api.get(
            f"/sessions/{sid}/trackmap",
            {"lap": ref, "max_points": 1500},
            ok=lambda d: (
                setattr(self, "map_points", (d or {}).get("points") or []),
                self.track.set_points(self.map_points),
                self._pins(),
            ),
            err=lambda _s, _d: self.track.set_points([]),
            key="trackmap",
        )
        self.api.get(
            f"/sessions/{sid}/track",
            {"lap": ref},
            ok=self._on_track,
            err=lambda _s, _d: self._on_track(None),
            key="track",
        )
        self.findings_panel.title.setText("Coaching findings · loading…")
        self.api.get(
            "/coaching/lap-findings",
            {"session_id": sid, "main_lap": main, "ref_lap": ref},
            ok=self._on_findings,
            err=lambda _s, d: self.findings_panel.set_error(d),
            key="findings",
        )

    def _on_compare(self, name: str, data) -> None:
        self._compare[name] = data
        if len(self._compare) == len(CHANNELS):
            self._draw_traces()

    def _on_delta(self, data) -> None:
        self._delta = data
        if data and is_num(data.get("lap_length_m")):
            self.lap_length = data["lap_length_m"]
        self._draw_traces()

    def _draw_traces(self) -> None:
        if len(self._compare) < len(CHANNELS):
            return
        main, ref = str(self.main_lap), str(self.ref_lap)
        x = None
        channels = []
        for name, title, unit, scale, lo, hi in CHANNELS:
            data = self._compare.get(name)
            if not data:
                continue
            x = x or data.get("x_values")
            laps = data.get("laps") or {}

            def scaled(values, s=scale):
                return [v * s if is_num(v) else None for v in values or []]

            channels.append((title, unit, scaled(laps.get(main)), scaled(laps.get(ref)), lo, hi))
        if not channels or not x:
            self.traces.set_message("No traces for this lap pair.")
            return
        length = self.lap_length or ((self.hub.state or {}).get("session") or {}).get(
            "lap_length_m"
        )
        if not is_num(length):
            length = 1.0
        delta = None
        if self._delta and self._delta.get("distance_m"):
            delta = (self._delta["distance_m"], self._delta["delta_s"])
        self.traces.set_data(channels, review.to_metres(x, length), delta, length)

    def _on_findings(self, data) -> None:
        self.findings = data
        if data and is_num(data.get("lap_length_m")):
            self.lap_length = data["lap_length_m"]
        self.findings_panel.set_findings(data)

    def _on_track(self, data) -> None:
        self.track.set_names(review.track_names(data))
        self.track_facts.setText(review.track_facts(data))

    def _corner(self, corner: dict) -> None:
        span = review.corner_span(corner)
        self.traces.set_highlight(span)
        if span and self.lap_length:
            self.track.set_highlight((span[0] / self.lap_length, span[1] / self.lap_length))

    def _pins(self) -> None:
        self.track.set_pins(review.track_events(self.records))

    # ---- replay ---------------------------------------------------------
    def set_cursor(self, t_s: float) -> None:
        if not self.bounds:
            return
        self.cursor = max(self.bounds[0], min(self.bounds[1], t_s))
        self._render_cursor()
        self._cursor_timer.start()

    def _toggle_play(self, on: bool) -> None:
        self.play.setText("❚❚ Pause" if on else "▶ Play")
        if on:
            self._play_timer.start()
        else:
            self._play_timer.stop()

    def _tick(self) -> None:
        if not self.bounds or self.cursor is None:
            return
        speed = PLAY_SPEEDS[max(0, self.speed_group.checkedId())]
        nxt = self.cursor + 0.1 * speed
        if nxt >= self.bounds[1]:
            nxt = self.bounds[1]
            self.play.setChecked(False)
        self.set_cursor(nxt)

    def _render_replay(self) -> None:
        lanes = self.lanes or {}
        if not self.bounds:
            self.timeline.set_lanes([])
            self.clock.setText(NA)
            return
        self.timeline.set_range(*self.bounds)
        rows = [
            (
                "laps",
                t.MUTED,
                [
                    {
                        **i,
                        "color": "#2a313a" if i["cls"] == "lap-clean" else "#1f252c",
                        "text": i["label"].replace("lap ", ""),
                    }
                    for i in lanes.get("laps", [])
                ],
            )
        ]
        rows.append(
            (
                "flags",
                t.MUTED,
                [
                    {**i, "color": t.FLAG.get(i.get("phase"), t.FLAG["unknown"])}
                    for i in lanes.get("flags", [])
                ],
            )
        )
        rows.append(
            (
                "race events",
                t.MUTED,
                [
                    {**i, "color": t.severity_color(i.get("severity"))}
                    for i in lanes.get("events", [])
                ],
            )
        )
        for name in activity.AGENTS:
            rows.append(
                (
                    t.AGENT_LABEL[name],
                    t.agent_text(name),
                    [
                        {**i, "quiet": bool(i.get("outcome") and i["outcome"] != "radio")}
                        for i in lanes.get(name, [])
                    ],
                )
            )
        rows.append(
            (
                "radio",
                t.MUTED,
                [
                    {**i, "color": t.agent_color(i.get("agent")), "width": 3}
                    for i in lanes.get("radio", [])
                ],
            )
        )
        self.timeline.set_lanes(rows)
        self._render_cursor()

    def _render_cursor(self) -> None:
        cur = self.cursor
        self.timeline.set_cursor(cur)
        lap = review.lap_at(self.laps, cur) if is_num(cur) else None
        self.clock.setText(
            f"{clock(cur)} / {clock(self.bounds[1]) if self.bounds else NA}"
            + (f" · lap {lap['lap']}" if lap else "")
        )
        calls = review.radio_up_to(self.records, cur)
        rows = []
        for rec in calls[:5]:
            d = rec.get("data") or {}
            ref = d.get("event_ref") or {}
            sev = d.get("priority")
            head = (
                f"{d.get('agent', '?').upper().replace('_', ' ')} · "
                + (f"{t.SEVERITY_ICON.get(sev, '')} {sev} · " if sev == "critical" else "")
                + (f"L{ref.get('lap')} " if ref.get("lap") is not None else "")
                + clock(rec.get("session_time"))
            )
            rows.append((head, d.get("spoken_text") or "", t.agent_color(d.get("agent"))))
        empty = (
            "This run was recorded with the agents off."
            if not any(r.get("kind") == "radio" for r in self.records)
            else "Nothing had aired yet."
        )
        self.radio_so_far.set_lines(rows, len(calls), empty)
        events = review.events_up_to(self.records, cur, limit=8)
        all_events = review.events_up_to(self.records, cur, limit=10**6)
        erows = []
        for rec in events:
            line = vm.event_line(rec.get("data") or {})
            sev = line["severity"] if line["severity"] in t.SEVERITY else "info"
            when = (f"L{line['lap']} " if line["lap"] is not None else "") + clock(
                line["session_time"]
            )
            erows.append(
                ("", f"{t.SEVERITY_ICON[sev]} {when} {line['text']}", t.severity_color(sev))
            )
        self.events_so_far.set_lines(erows, len(all_events), "No race events yet.")
        model = activity.reduce([r for r in self.records if r.get("kind") != "radio"], cur)
        for name in activity.AGENTS:
            self.flow.set_agent(name, model.agents[name].status)
            outcome = model.agents[name].last_outcome
            self.flow.set_sublabel(name, {"radio": "spoke"}.get(outcome, outcome or "idle"))
        has_agents = any(r.get("kind") == "run_start" for r in self.records)
        self.flow.set_mounted(has_agents)
        self.flow.set_sublabel("radio", f"{model.radio['aired']} aired")
        self.runs.set_runs(activity.recent_runs(model, 6), None)

    def _load_cursor_state(self) -> None:
        if not (self.session_id and self.run_id and is_num(self.cursor)):
            self.car.set_state(None, self.cursor)
            return
        self.api.get(
            f"/sessions/{self.session_id}/pitwall-log/state",
            {"t": round(self.cursor, 2), "run": self.run_id},
            ok=lambda d: self.car.set_state(
                ((d or {}).get("record") or {}).get("data"), self.cursor
            ),
            err=lambda _s, _d: self.car.set_state(None, self.cursor),
            key="cursor-state",
        )

    def _render_audit(self) -> None:
        rows = review.audit_rows(self.records)
        meta = next((r for r in self.runs_meta if r.get("run_id") == self.run_id), {})
        self.audit.set_rows(
            review.filter_audit(rows, **self.audit.filters()),
            len(rows),
            bool(meta.get("has_agents")),
        )

    # ---- record a run / import -----------------------------------------
    def _record(self) -> None:
        if not self.session_id:
            return
        if agents_will_run(self.hub.status):
            if QuotaDialog(self.hub.status, self).exec() != QDialog.DialogCode.Accepted:
                return
        self._recording = True
        self.bar.set_progress("recording a run…", 0.0)
        self.hub.start_replay(self.session_id, 0.0, done=self._job_timer.start)

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import an iRacing telemetry file",
            os.path.expanduser("~/Documents/iRacing/telemetry"),
            "iRacing telemetry (*.ibt)",
        )
        if not path:
            return
        name = os.path.basename(path)

        def started(data):
            self._job = (data or {}).get("job_id")
            self.bar.set_progress(f"importing {name}…", 0.0)
            self._job_timer.start()

        def failed(_s, detail):
            self.bar.set_progress(f"import failed: {detail}", None)

        if self.embedded:
            self.api.post("/import", {"path": path}, ok=started, err=failed)
        else:
            self.bar.set_progress(f"uploading {name}…", 0.0)
            self.api.upload(
                "/import/upload",
                path,
                {"filename": name},
                ok=started,
                err=failed,
                progress=lambda sent, total: self.bar.set_progress(
                    f"uploading {name}…", sent / total if total else None
                ),
            )

    def _poll_job(self) -> None:
        if self._recording:

            def ok(status):
                status = status or {}
                frames, total = status.get("frames") or 0, status.get("total") or 0
                if status.get("running"):
                    self.bar.set_progress(
                        f"recording a run… {thousands(frames)} / {thousands(total)} frames",
                        frames / total if total else None,
                    )
                    return
                self._job_timer.stop()
                self._recording = False
                if status.get("error"):
                    self.bar.set_progress(f"recording failed: {status['error']}", None)
                else:
                    self.bar.set_progress("run recorded", None)
                    self._load_runs_newest()

            self.api.get("/replay/status", ok=ok, key="record-status")
            return
        if not self._job:
            self._job_timer.stop()
            return

        def job_ok(job):
            job = job or {}
            status = job.get("status")
            if status in ("pending", "running"):
                self.bar.set_progress(
                    f"importing… {round((job.get('progress') or 0) * 100)}%",
                    job.get("progress") or 0,
                )
                return
            self._job_timer.stop()
            self._job = None
            if status == "complete":
                self.bar.set_progress("imported", None)
                self.session_id = job.get("session_id")
                self.laps = []
                self.hub.load_sessions()
            else:
                self.bar.set_progress(f"import failed: {job.get('error') or status}", None)

        self.api.get(f"/import/{self._job}", ok=job_ok, key="import-job")

    def _load_runs_newest(self) -> None:
        def ok(data):
            runs = (data or {}).get("runs") or []
            newest = max(runs, key=lambda r: r.get("started_wall_ms") or 0, default=None)
            self._load_runs(newest.get("run_id") if newest else None)

        self.api.get(f"/sessions/{self.session_id}/pitwall-runs", ok=ok)
