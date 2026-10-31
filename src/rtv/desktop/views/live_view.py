"""The live pitwall: a race in progress (live or replayed) with the agents at work.

Layout (the "Live pitwall" artboard): flag band, status strip, replay bar, then
three columns: pitwall at work (flow graph, timeline, agent runs, raw telemetry);
strategy and the timing tower; the radio. The ticker runs along the bottom.
Painting is throttled to 10 Hz; socket frames only update the stored state.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..model import activity
from ..model import view_model as vm
from ..model.format import clock, humanise, integer, is_num, thousands
from ..model.radio import latest_strategist
from ..theme import tokens as t
from ..widgets.agents import FlowGraph, RunsWaterfall
from ..widgets.charts import LaneTimeline, TelemetryStrip
from ..widgets.common import Panel, label
from ..widgets.dialogs import QuotaDialog, agents_will_run
from ..widgets.live import (
    Banner,
    FlagBand,
    ReplayBar,
    StatusStrip,
    StrategyPanel,
    TickerStrip,
    TowerPanel,
)
from ..widgets.radio import RadioFeed
from .hub import Hub

RENDER_MS = 100
TIMELINE_S = 120.0


def _scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setWidget(widget)
    return area


class LiveView(QWidget):
    def __init__(self, hub: Hub, parent: QWidget | None = None, *, reduced_motion: bool = False):
        super().__init__(parent)
        self.hub = hub
        self.setObjectName("root")
        self._dirty = True
        self._selected_run: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.notice = Banner()
        self.budget = Banner()
        self.reconnect = Banner()
        self.flag = FlagBand(reduced_motion=reduced_motion)
        self.status = StatusStrip()
        self.replay = ReplayBar()
        for w in (self.notice, self.budget, self.reconnect, self.flag, self.status, self.replay):
            outer.addWidget(w)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(12)
        outer.addWidget(splitter, 1)

        # column A: the pitwall at work
        col_a = QWidget()
        a = QVBoxLayout(col_a)
        a.setContentsMargins(16, 12, 0, 12)
        a.setSpacing(12)
        self.flow_panel = Panel("Pitwall at work")
        legend = QHBoxLayout()
        legend.setSpacing(12)
        for agent in activity.AGENTS:
            legend.addWidget(label(f"● {t.AGENT_LABEL[agent]}", color=t.agent_text(agent), size=11))
        legend.addWidget(label("┄ radio heard as context", "muted", size=11))
        legend.addWidget(label("● failed / blocked", color=t.SEVERITY["critical"], size=11))
        self.flow_panel.header.addLayout(legend)
        self.flow = FlowGraph(reduced_motion=reduced_motion)
        self.flow_panel.body.addWidget(self.flow)
        a.addWidget(self.flow_panel)
        self.timeline_panel = Panel("Timeline", subtitle="· last 2 min, following now")
        self.timeline = LaneTimeline()
        self.timeline_panel.body.addWidget(self.timeline)
        a.addWidget(self.timeline_panel)
        self.runs_panel = Panel("Agent runs")
        self.runs = RunsWaterfall()
        self.runs_panel.body.addWidget(self.runs)
        self.run_detail = label(
            "Click a run for what it was shown, the tools it called and what aired.",
            "muted",
            wrap=True,
        )
        self.run_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.runs_panel.body.addWidget(self.run_detail)
        a.addWidget(self.runs_panel, 1)
        self.telemetry_panel = Panel("Raw telemetry", subtitle="· rolling 20 s")
        self.telemetry = TelemetryStrip()
        self.telemetry_panel.body.addWidget(self.telemetry)
        a.addWidget(self.telemetry_panel)
        splitter.addWidget(_scroll(col_a))

        # column B: strategy and the tower
        col_b = QWidget()
        b = QVBoxLayout(col_b)
        b.setContentsMargins(0, 12, 0, 12)
        b.setSpacing(12)
        self.strategy = StrategyPanel()
        self.tower = TowerPanel()
        self.tower.on_toggle = self._mark
        b.addWidget(self.strategy)
        b.addWidget(self.tower, 1)
        splitter.addWidget(_scroll(col_b))

        # column C: the radio
        radio_frame = QFrame()
        radio_frame.setObjectName("panel")
        rl = QVBoxLayout(radio_frame)
        rl.setContentsMargins(0, 0, 0, 0)
        self.radio = RadioFeed()
        rl.addWidget(self.radio)
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 12, 16, 12)
        hl.addWidget(radio_frame)
        splitter.addWidget(holder)
        splitter.setSizes([820, 560, 480])

        self.ticker = TickerStrip()
        outer.addWidget(self.ticker)

        # wiring
        hub.state_changed.connect(self._mark)
        hub.status_changed.connect(self._on_status)
        hub.sessions_changed.connect(lambda: self.replay.set_sessions(hub.sessions))
        hub.feed_changed.connect(self._on_feed)
        hub.radio_added.connect(self._on_radio)
        hub.radio_seeded.connect(lambda: self.radio.seed(hub.messages))
        hub.radio_aired.connect(self.radio.mark_aired)
        hub.on_air_changed.connect(
            lambda snap: self.radio.set_on_air(snap.get("speaking"), len(snap.get("queue") or []))
        )
        hub.activity_record.connect(self._on_activity)
        hub.activity_seeded.connect(self._mark)
        hub.event_added.connect(lambda _e: self._mark())
        hub.events_seeded.connect(self._mark)
        hub.telemetry_changed.connect(self._on_telemetry)
        hub.catalog_changed.connect(self.telemetry.set_catalog)
        hub.notice.connect(lambda d: self.notice.show_message(d, "error"))
        self.replay.start_replay.connect(self._start_replay)
        self.replay.stop_replay.connect(hub.stop_replay)
        self.replay.start_live.connect(hub.start_live)
        self.replay.stop_live.connect(hub.stop_live)
        self.radio.send_text.connect(self._send)
        self.radio.volume_changed.connect(hub.radio.set_volume)
        self.radio.master_toggled.connect(lambda on: hub.radio.set_master_muted(not on))
        self.radio.mute_toggled.connect(hub.radio.set_agent_muted)
        self.runs.run_clicked.connect(self._show_run)
        self.timeline.item_clicked.connect(self._timeline_item)
        if not hub.speaker.available:
            self.radio.set_speech_note(hub.speaker.reason)

        self._timer = QTimer(self)
        self._timer.setInterval(RENDER_MS)
        self._timer.timeout.connect(self._render)
        self._timer.start()
        self._on_feed(hub.feed_state)

    # ---- inputs --------------------------------------------------------
    def _mark(self) -> None:
        self._dirty = True

    def _on_telemetry(self, frame: dict) -> None:
        if is_num(frame.get("t")):  # a frame without a session clock cannot be placed
            self.telemetry.push(frame["t"], frame.get("v") or {})

    def _on_radio(self, message: dict) -> None:
        self.radio.add(message)
        self._dirty = True

    def _on_activity(self, record: dict) -> None:
        self.flow.pulse(activity.edges_for(record))
        self._dirty = True

    def _on_feed(self, state: str) -> None:
        frozen = state in ("reconnecting", "closed")
        self.telemetry.frozen = frozen
        if state == "reconnecting":
            secs = max(1, round(self.hub.pitwall.retry_in_ms / 1000))
            self.reconnect.show_message(
                f"Reconnecting to the pitwall feed… retry in {secs} s. Numbers are frozen"
                " at the last frame.",
                "warn",
                dismissable=False,
            )
        else:
            self.reconnect.hide()
        self._dirty = True

    def _on_status(self) -> None:
        hub = self.hub
        self.replay.set_status(hub.replay, hub.live)
        status = hub.status or {}
        if status.get("available") and status.get("budget_exhausted"):
            self.budget.show_message(
                f"AI budget used up: no more calls this session "
                f"({integer(status.get('calls_used'))} / "
                f"{integer(status.get('max_calls_per_session'))} model calls). The ticker and"
                " strategy panel keep running.",
                "budget",
                dismissable=False,
            )
        else:
            self.budget.hide()
        self._dirty = True

    def _start_replay(self, session_id: str, speed: float) -> None:
        if agents_will_run(self.hub.status):
            if QuotaDialog(self.hub.status, self).exec() != QDialog.DialogCode.Accepted:
                return
        self.hub.start_replay(session_id, speed)

    def _send(self, text: str) -> None:
        def unheard(message: str) -> None:
            self.radio.add(
                {
                    "agent": "driver",
                    "spoken_text": message,
                    "speak": False,
                    "session_time": self.hub.now(),
                    "heard": False,
                }
            )

        self.hub.send_driver(text, unheard)

    def _timeline_item(self, item: dict) -> None:
        run_id = (item.get("record") or {}).get("run_id")
        if run_id:
            self._show_run(run_id)

    def _show_run(self, run_id: str) -> None:
        self._selected_run = run_id
        run = self.hub.model.runs.get(run_id)
        if not run:
            return
        ctx = (
            ", ".join(
                t.AGENT_LABEL.get(m.get("agent"), str(m.get("agent"))) for m in run.context_radio
            )
            or "no earlier radio"
        )
        lines = [
            f"{run.run_id} · woken by {humanise(run.trigger or run.event_key)}"
            f" at {clock(run.session_time)} · model {run.model or 'n/a'}",
            f"Shown: {ctx} · {run.context_events if run.context_events is not None else 0}"
            " race events",
        ]
        for span in run.spans:
            if span["kind"] == "tool":
                result = span.get("result") or {}
                keys = len(result.get("keys") or []) if isinstance(result, dict) else 0
                lines.append(
                    f"  tool {span['name']} {span.get('args') or {}} → "
                    f"{result.get('type', '?')}, {keys} keys · "
                    f"{span.get('latency_ms') or 0:.2f} ms"
                )
            else:
                ms = span.get("latency_ms")
                state = (
                    "ok" if span.get("ok") else ("error" if span.get("ok") is False else "working")
                )
                lines.append(
                    f"  model call {span.get('call')} · {(ms or 0) / 1000:.1f} s · {state}"
                )
        for v in run.validations:
            lines.append(
                "  validator: "
                + ("ok" if v["ok"] else "withheld " + ", ".join(map(str, v["unsupported"])))
            )
        aired = next((m for m in self.hub.messages if m.get("message_id") == run.message_id), None)
        if aired:
            lines.append(f"Aired: {aired.get('spoken_text')}")
        elif run.outcome:
            lines.append(f"Outcome: {run.outcome}")
        self.run_detail.setText("\n".join(lines))

    # ---- painting ------------------------------------------------------
    def _render(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        hub = self.hub
        state = hub.state or {}
        socket_down = hub.feed_state in ("reconnecting", "closed")
        ctx = {
            "socket": False if socket_down else None,
            "replay": hub.replay,
            "pitwall_status": hub.status,
        }
        if socket_down:
            ctx["socket_detail"] = "reconnecting"
        view = vm.status_view(state, ctx)
        self.flag.set_flag(view["flag"], (state.get("flags") or {}).get("since_session_time"))
        self.status.set_view(view, state, frozen=socket_down or bool(state.get("stale")))
        conn = view["connection"]
        feed_state = "reconnecting" if socket_down else conn["state"]
        if hub.feed_state == "connecting":
            feed_state = "connecting"
        detail = conn["detail"]
        if conn["state"] == "replay" and hub.replay and hub.replay.get("running"):
            detail = (
                f"{thousands(hub.replay.get('frames'))} / "
                f"{thousands(hub.replay.get('total'))} frames"
            )
        if state.get("stale"):
            detail = state.get("stale_reason") or detail
        self.status.set_feed(feed_state, detail)
        llm = view["llm"]
        self.status.set_ai(llm)
        self.strategy.set_view(
            vm.strategy_view(state), hub.fuel_history.points(), frozen=socket_down
        )
        self.strategy.set_recommendation(vm.recommendation_view(latest_strategist(hub.messages)))
        self.tower.set_view(
            vm.tower_view(
                state, expanded=self.tower.expanded, trends=hub.gaps, warnings=hub.warnings
            ),
            state,
        )
        lines = [vm.event_line(e) for e in hub.events[: TickerStrip.LIMIT]]
        total = (state.get("metrics") or {}).get("events")
        self.ticker.set_events(lines, total if is_num(total) else len(hub.events))
        self._render_agents(state, llm)

    def _render_agents(self, state: dict, llm: dict) -> None:
        hub = self.hub
        model = hub.model
        mounted = bool(llm.get("available"))
        self.flow.set_mounted(mounted)
        caps = state.get("capabilities") or {}
        status_agents = {a.get("name"): a for a in (hub.status or {}).get("agents") or []}
        for name in activity.AGENTS:
            agent = model.agents[name]
            info = status_agents.get(name, {})
            disabled = bool(info.get("disabled_reason") or info.get("degraded")) or (
                mounted and info.get("enabled") is False
            )
            self.flow.set_agent(name, agent.status, disabled)
            if name == "spotter" and caps.get("session") and not caps.get("standings"):
                self.flow.set_sublabel(name, "no field data (live only)")
            elif not mounted:
                self.flow.set_sublabel(name, "off")
            elif agent.status != "idle":
                since = agent.since
                now = hub.now()
                took = f" · {now - since:.0f} s" if is_num(since) and is_num(now) else ""
                self.flow.set_sublabel(name, f"{agent.status}{took}")
            elif agent.last_outcome:
                word = {"radio": "spoke"}.get(agent.last_outcome, agent.last_outcome)
                self.flow.set_sublabel(name, word)
            else:
                self.flow.set_sublabel(name, "idle")
        metrics = state.get("metrics") or {}
        self.flow.set_sublabel("telemetry", (state.get("session") or {}).get("source") or "")
        if is_num(metrics.get("avg_update_ms")):
            self.flow.set_sublabel("engine", f"{metrics['avg_update_ms']:.3f} ms")
        self.flow.set_sublabel(
            "detectors",
            f"{integer(metrics.get('events'))} events" if is_num(metrics.get("events")) else "",
        )
        skipped = (hub.status or {}).get("skipped_busy")
        self.flow.set_sublabel("orchestrator", f"{skipped} skipped busy" if skipped else "")
        self.flow.set_sublabel(
            "validator",
            f"{model.validator['failed']} withheld" if model.validator["failed"] else "",
        )
        self.flow.set_sublabel("radio", f"{model.radio['aired']} aired")
        runs = activity.recent_runs(model, 12)
        mean = activity.mean_latency(model)
        self.runs_panel.meta.setText(
            f"{len(model.runs)} runs · mean {mean / 1000:.1f} s"
            if mean
            else ("agents off" if not mounted else "0 runs")
        )
        wall = max((r.end_wall or r.start_wall or 0 for r in runs), default=None)
        self.runs.set_runs(runs, wall)
        if not mounted and not runs:
            reason = (llm.get("detail") or "the agent layer is not mounted").rstrip(".")
            self.run_detail.setText(
                f"No agent runs: {reason}. The race-state engine, "
                "strategy panel and ticker run regardless."
            )
        self._render_timeline()

    def _render_timeline(self) -> None:
        hub = self.hub
        now = hub.now()
        bound = ((hub.state or {}).get("capabilities") or {}).get("session")
        if not is_num(now) or not bound:
            self.timeline.set_lanes([])
            self.timeline_panel.meta.setText("waiting for telemetry")
            return
        t0 = max(0.0, now - TIMELINE_S)
        self.timeline.set_range(t0, max(now, t0 + 1))
        self.timeline_panel.meta.setText(f"{clock(t0)} → {clock(now)}")
        events = [
            {
                "t0": e["session_time"],
                "color": t.severity_color(e.get("severity")),
                "label": f"{humanise(e.get('event_type'))}: {vm.event_line(e)['text']}",
                "record": e,
            }
            for e in hub.events
            if is_num(e.get("session_time")) and e["session_time"] >= t0
        ]
        lanes = [("race events", t.MUTED, events)]
        for name in activity.AGENTS:
            items = []
            for run in hub.model.runs.values():
                if run.agent != name:
                    continue
                span = hub.run_clock.get(run.run_id) or {}
                start = span.get("t0", run.session_time)
                end = span.get("t1") or (now if run.end_wall is None else start)
                if not is_num(start) or (is_num(end) and end < t0):
                    continue
                items.append(
                    {
                        "t0": start,
                        "t1": end,
                        "quiet": run.outcome not in (None, "radio"),
                        "label": f"{run.run_id} · {run.outcome or 'working'}",
                        "record": {"run_id": run.run_id},
                    }
                )
            lanes.append((t.AGENT_LABEL[name], t.agent_text(name), items))
        aired = []
        for m in hub.messages:
            when = hub.aired.get(m.get("message_id") or "", m.get("session_time"))
            if is_num(when) and when >= t0 and m.get("agent") != "driver":
                aired.append(
                    {
                        "t0": when,
                        "color": t.agent_color(m.get("agent")),
                        "width": 3,
                        "label": f"{m.get('agent')}: {m.get('spoken_text')}",
                    }
                )
        lanes.append(("radio aired", t.MUTED, aired))
        self.timeline.set_lanes(lanes)
