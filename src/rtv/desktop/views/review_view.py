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

from ..model import activity, review, vision
from ..model import plan as plan_model
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
from ..widgets.vision import OnboardPanel, VisionSpendDialog
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
POLL_VISION_MS = 1500
FRAME_WIDTH = 640
VIDEO_FILTER = "Video (*.mp4 *.mkv *.mov *.webm *.avi *.m4v)"


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
        self.videos: list[dict] = []
        self.video: dict | None = None
        self.vision_notes: list[dict] = []
        self.vision_status: dict | None = None
        self._vision_corner: str | None = None
        self._analysis: str | None = None

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

        # onboard video and its visual notes (docs/VISION.md)
        self.onboard = OnboardPanel()
        cl.addWidget(self.onboard)

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
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(200)
        self._preview_timer.timeout.connect(self._load_preview)
        self._vision_timer = QTimer(self)
        self._vision_timer.setInterval(POLL_VISION_MS)
        self._vision_timer.timeout.connect(self._poll_analysis)

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
        self.onboard.add_clicked.connect(self._add_video)
        self.onboard.video_changed.connect(self._pick_video)
        self.onboard.sync_requested.connect(self._sync_video)
        self.onboard.preview_requested.connect(lambda _vt: self._preview_timer.start())
        self.onboard.plan_clicked.connect(lambda: self._plan_vision(confirm=False))
        self.onboard.analyse_clicked.connect(lambda: self._plan_vision(confirm=True))
        self.onboard.cancel_clicked.connect(self._cancel_analysis)
        self.onboard.note_picked.connect(self._note_picked)
        self.onboard.filter_cleared.connect(lambda: self._filter_notes(None))
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
        self._load_vision()

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
        self.onboard.set_videos(self.videos, self.video, self.laps)  # laps to sync on
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
        self._filter_notes(corner.get("label") if corner else None)
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
                    if not plan_model.is_plan_item(i)
                ],
            )
        )
        plan_items = plan_model.timeline_items(self.records)
        if plan_items:
            rows.append(
                (
                    "race plan",
                    t.MUTED,
                    [
                        {
                            **i,
                            "color": t.GOOD if i["accepted"] else t.SEVERITY["advisory"],
                        }
                        for i in plan_items
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
        if self.vision_notes:
            rows.append(
                (
                    "camera",
                    t.MUTED,
                    [
                        {**i, "color": t.severity_color(i["severity"])}
                        for i in vision.timeline_items(self.vision_notes)
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
            badge = review.air_badge(d)
            head = (
                f"{d.get('agent', '?').upper().replace('_', ' ')} · "
                + (f"{t.SEVERITY_ICON.get(sev, '')} {sev} · " if sev == "critical" else "")
                + (f"L{ref.get('lap')} " if ref.get("lap") is not None else "")
                + clock(rec.get("session_time"))
                + (f" · {badge['text']}" if badge else "")
            )
            text = d.get("spoken_text") or ""
            expired = bool(badge and badge["kind"] in review.NOT_AIRED)
            if expired:
                text += f"  ({badge['reason']})"
            rows.append((head, text, t.agent_color(d.get("agent")), expired))
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
        self._load_frame()
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
            plan_model.audit_notes(self.records),
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

    # ---- onboard video and visual notes --------------------------------
    def _load_vision(self) -> None:
        sid = self.session_id
        self._vision_timer.stop()
        self._analysis = None
        self.videos, self.video, self.vision_notes = [], None, []
        self.onboard.set_running(False)
        self.onboard.set_plan(None)
        self.onboard.set_analysis(vision.analysis_text(None))
        self._render_video()
        self.api.get(
            "/vision/status",
            ok=self._on_vision_status,
            err=lambda _s, _d: self._on_vision_status(None),
            key="vision-status",
        )
        self.api.get(
            f"/sessions/{sid}/videos",
            ok=lambda d: self._on_videos((d or {}).get("videos") or []),
            err=lambda _s, _d: self._on_videos([]),
            key="vision-videos",
        )
        self.api.get(
            f"/sessions/{sid}/vision/analyses",
            ok=self._on_analyses,
            err=lambda _s, _d: None,
            key="vision-analyses",
        )

    def _on_vision_status(self, status: dict | None) -> None:
        self.vision_status = status
        self.onboard.set_blocker(vision.vision_blocker(status))

    def _on_videos(self, videos: list[dict], prefer: str | None = None) -> None:
        self.videos = videos
        self.video = vision.pick_video(videos, prefer or (self.video or {}).get("video_id"))
        self._render_video()
        self._load_frame()

    def _render_video(self) -> None:
        self.onboard.set_videos(self.videos, self.video, self.laps)
        self._render_notes()

    def _on_analyses(self, data: dict | None) -> None:
        data = data or {}
        aid = data.get("default_analysis")
        summary = next((a for a in data.get("analyses") or [] if a.get("analysis_id") == aid), None)
        self.onboard.set_analysis(vision.analysis_text(summary))
        if summary and summary.get("status") == "running":
            self._analysis = aid
            self.onboard.set_running(True)
            self._vision_timer.start()
        self._load_notes(aid)

    def _load_notes(self, aid: str | None) -> None:
        if not aid:
            self.vision_notes = []
            self._render_notes()
            self._render_replay()
            return

        def ok(data):
            self.vision_notes = (data or {}).get("items") or []
            self._render_notes()
            self._render_replay()

        self.api.get(
            f"/sessions/{self.session_id}/vision/notes",
            {"analysis": aid},
            ok=ok,
            err=lambda _s, d: self._error(d),
            key="vision-notes",
        )

    def _render_notes(self) -> None:
        rows = vision.note_rows(self.vision_notes, corner=self._vision_corner)
        self.onboard.set_notes(rows, self._vision_corner, len(self.vision_notes))

    def _filter_notes(self, corner: str | None) -> None:
        self._vision_corner = corner
        self._render_notes()

    def _note_picked(self, row: dict) -> None:
        t_s = row.get("session_time")
        if not is_num(t_s):
            return
        if self.bounds:
            self.set_cursor(t_s)
        else:  # no pitwall run: the cursor still drives the frame
            self.cursor = t_s
            self._load_frame()

    def _load_frame(self) -> None:
        video = self.video
        if not vision.is_synced(video):
            return
        vt = vision.to_video(video, self.cursor)
        if vt is None:
            text = "No cursor yet." if not is_num(self.cursor) else "The video does not cover"
            if is_num(self.cursor):
                text += f" {clock(self.cursor)}: it covers {vision.sync_text(video)}."
            self.onboard.set_frame(None, text)
            return
        caption = self.onboard.frame_caption(self.cursor, vt)
        self.api.get_bytes(
            f"/sessions/{self.session_id}/videos/{video['video_id']}/frame",
            {"video_time": round(vt, 3), "width": FRAME_WIDTH},
            ok=lambda jpeg: self.onboard.set_frame(jpeg, caption),
            err=lambda _s, d: self.onboard.set_frame(None, f"No frame: {d}"),
            key="vision-frame",
        )

    def _load_preview(self) -> None:
        """While syncing: the frame at the video time being typed."""
        video = self.video
        if not video or vision.is_synced(video):
            return
        vt = self.onboard.sync_time.value()
        self.api.get_bytes(
            f"/sessions/{self.session_id}/videos/{video['video_id']}/frame",
            {"video_time": round(vt, 3), "width": FRAME_WIDTH},
            ok=lambda jpeg: self.onboard.set_frame(jpeg, f"video {clock(vt)}"),
            err=lambda _s, d: self.onboard.set_frame(None, f"No frame: {d}"),
            key="vision-frame",
        )

    def _pick_video(self, video_id: str) -> None:
        self._on_videos(self.videos, video_id)

    def _add_video(self) -> None:
        if not self.session_id:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Add an onboard video of this session",
            os.path.expanduser("~/Videos"),
            VIDEO_FILTER,
        )
        if not path:
            return
        name = os.path.basename(path)
        sid = self.session_id

        def added(video):
            self.notice.show_message(f"Added {name}. Sync it to the telemetry next.", "info")
            self.api.get(
                f"/sessions/{sid}/videos",
                ok=lambda d: self._on_videos(
                    (d or {}).get("videos") or [], (video or {}).get("video_id")
                ),
                key="vision-videos",
            )

        def failed(_s, detail):
            self._error(f"Could not add {name}: {detail}")

        if self.embedded:
            self.api.post(f"/sessions/{sid}/videos", {"path": path}, ok=added, err=failed)
        else:
            self.onboard.meta.setText(f"uploading {name}…")
            self.api.upload(
                f"/sessions/{sid}/videos/upload",
                path,
                {"filename": name},
                ok=added,
                err=failed,
                progress=lambda sent, total: self.onboard.meta.setText(
                    f"uploading {name}… {round(100 * sent / total) if total else 0}%"
                ),
            )

    def _sync_video(self, lap: int, video_time: float) -> None:
        video = self.video
        if not video:
            return

        def ok(updated):
            others = [v for v in self.videos if v.get("video_id") != video["video_id"]]
            self._on_videos([*others, updated], video["video_id"])

        self.api.put(
            f"/sessions/{self.session_id}/videos/{video['video_id']}/sync",
            {"lap_starts": [{"lap": lap, "video_time": video_time}]},
            ok=ok,
            err=lambda _s, d: self._error(f"Sync failed: {d}"),
        )

    def _plan_vision(self, *, confirm: bool) -> None:
        video = self.video
        if not vision.is_synced(video):
            return
        sid = self.session_id

        def planned(plan):
            text = vision.plan_text(plan)
            self.onboard.set_plan(text)
            if not confirm or not (plan or {}).get("count"):
                return
            if VisionSpendDialog(text, self).exec() != QDialog.DialogCode.Accepted:
                return
            self.api.post(
                f"/sessions/{sid}/vision/analyses",
                {"video_id": video["video_id"]},
                ok=started,
                err=lambda _s, d: self.onboard.set_blocker(f"Analysis not started: {d}"),
            )

        def started(data):
            self._analysis = (data or {}).get("analysis_id")
            self.onboard.set_running(True)
            self.onboard.set_analysis(vision.analysis_text({"status": "running"}))
            self._vision_timer.start()

        self.api.post(
            f"/sessions/{sid}/vision/plan",
            {"video_id": video["video_id"]},
            ok=planned,
            err=lambda _s, d: self.onboard.set_plan(f"Plan failed: {d}"),
        )

    def _poll_analysis(self) -> None:
        aid = self._analysis
        if not aid:
            self._vision_timer.stop()
            return

        def ok(summary):
            summary = summary or {}
            self.onboard.set_analysis(vision.analysis_text(summary))
            self.vision_notes = summary.get("items") or []
            self._render_notes()
            if summary.get("status") == "running":
                return
            self._vision_timer.stop()
            self.onboard.set_running(False)
            self._load_notes(aid)  # the kept notes only, as the review shows them

        self.api.get(
            f"/sessions/{self.session_id}/vision/analyses/{aid}",
            ok=ok,
            err=lambda _s, _d: None,
            key="vision-analysis",
        )

    def _cancel_analysis(self) -> None:
        if self._analysis:
            self.api.post(
                f"/sessions/{self.session_id}/vision/analyses/{self._analysis}/cancel",
                ok=lambda _d: None,
                err=lambda _s, d: self._error(d),
            )
