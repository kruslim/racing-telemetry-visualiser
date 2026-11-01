"""The app's live connection to one RTV server, shared by every window.

The hub owns the two sockets, the REST polls (agent status, health, replay and live
status), the radio channel and the agent-activity model, and re-emits what changed.
The live pitwall and the radio window are both views of one hub, so a call is
spoken once however many windows show it.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from ..client.api import Api
from ..client.sockets import live_socket, pitwall_socket
from ..model import activity
from ..model import view_model as vm
from ..tts import QtSpeaker, make_radio

POLL_MS = 2000
EVENT_LIMIT = 200


class Hub(QObject):
    state_changed = Signal()
    status_changed = Signal()  # pitwall status, health, replay, live
    sessions_changed = Signal()
    radio_added = Signal(dict)
    radio_seeded = Signal()
    radio_aired = Signal(str, float)
    activity_record = Signal(dict)
    activity_seeded = Signal()
    event_added = Signal(dict)
    events_seeded = Signal()
    feed_changed = Signal(str)
    telemetry_changed = Signal(dict)
    catalog_changed = Signal(set)
    on_air_changed = Signal(dict)
    notice = Signal(str)
    driver_unheard = Signal(str)  # a voice question the server could not take (no pitwall)

    def __init__(self, api: Api, parent: QObject | None = None, *, speak: bool = True):
        super().__init__(parent)
        self.api = api
        self.state: dict | None = None
        self.status: dict | None = None  # /pitwall/status
        self.health: dict | None = None
        self.replay: dict | None = None
        self.live: dict | None = None
        self.sessions: list[dict] = []
        self.tts: dict | None = None
        self.messages: list[dict] = []
        self.events: list[dict] = []
        self.model = activity.create_model()
        self.fuel_history = vm.FuelHistory()
        self.gaps = vm.GapTrendTracker()
        self.warnings = vm.TowerWarnings()
        self.aired: dict[str, float] = {}
        self.run_clock: dict[str, dict] = {}  # run_id -> {t0, t1} on the race clock
        self.feed_state = "connecting"
        self.speaker = QtSpeaker(parent=self) if speak else QtSpeaker(engine="mock", parent=self)
        self.radio = make_radio(self.speaker, lambda snap: self.on_air_changed.emit(snap))
        self.pitwall = pitwall_socket(api, parent=self)
        self.pitwall.message.connect(self._on_pitwall)
        self.pitwall.status.connect(self._on_socket)
        self.telemetry = live_socket(api, parent=self)
        self.telemetry.message.connect(self._on_live)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self.poll)
        self._radio_tick = QTimer(self)
        self._radio_tick.setInterval(1000)
        self._radio_tick.timeout.connect(self.radio.tick)
        api.failed.connect(self._failed)
        self.voice = None  # PushToTalk, when RTV_VOICE_ENABLED (attach_voice)
        self.voice_keys = None  # its KeyWatcher

    def attach_voice(self, voice, keys=None) -> None:
        """Wire push-to-talk: transcripts become questions, and the mic mutes on air."""
        self.voice, self.voice_keys = voice, keys
        voice.transcribed.connect(
            lambda text: self.ask(text, "voice", self.driver_unheard.emit)
        )
        self.on_air_changed.connect(
            lambda snap: voice.set_radio_speaking(bool(snap.get("speaking")))
        )

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self.load()
        self.pitwall.start()
        self.telemetry.start()
        self._poll.start()
        self._radio_tick.start()
        if self.voice_keys is not None:
            self.voice_keys.start()
        if self.voice is not None:
            self.voice.warm()

    def stop(self) -> None:
        self._poll.stop()
        self._radio_tick.stop()
        if self.voice_keys is not None:
            self.voice_keys.stop()
        self.pitwall.stop()
        self.telemetry.stop()
        self.radio.clear()

    def load(self) -> None:
        """History on connect: shown, never spoken."""
        self.poll()
        self.load_sessions()
        self.api.get("/pitwall/tts", ok=self._on_tts, err=lambda *_: None)
        self.api.get("/racestate/events", {"limit": 60}, ok=self._on_events, err=lambda *_: None)
        self.api.get(
            "/pitwall/activity", {"limit": 1000}, ok=self._on_activity_history, err=lambda *_: None
        )
        self.load_radio()

    def load_radio(self) -> None:
        def ok(data):
            self.messages = list((data or {}).get("messages") or [])
            for m in self.messages:
                self.radio.mark_heard(m.get("message_id") or "")  # history is not spoken
            self.radio_seeded.emit()

        # 503 when the agents are not mounted: an empty radio, not an error.
        self.api.get("/pitwall/radio", {"limit": 200}, ok=ok, err=lambda *_: None)

    def load_sessions(self) -> None:
        def ok(data):
            self.sessions = list((data or {}).get("sessions") or [])
            self.sessions_changed.emit()

        self.api.get("/sessions", {"limit": 50}, ok=ok)

    def poll(self) -> None:
        def store(name):
            def ok(data):
                setattr(self, name, data)
                self.status_changed.emit()

            return ok

        quiet = lambda *_: None  # noqa: E731 - polls report through the feed pill
        self.api.get("/pitwall/status", ok=store("status"), err=quiet, key="poll-status")
        self.api.get("/pitwall/health", ok=store("health"), err=quiet, key="poll-health")
        self.api.get("/replay/status", ok=store("replay"), err=quiet, key="poll-replay")
        self.api.get("/live/status", ok=store("live"), err=quiet, key="poll-live")

    # ---- socket frames -------------------------------------------------
    def _on_socket(self, state: str) -> None:
        self.feed_state = state
        self.feed_changed.emit(state)
        if state == "open":
            self.load_radio()

    def _on_pitwall(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "state":
            state = frame.get("state") or {}
            self.state = state
            self.fuel_history.update(state)
            self.gaps.update(state)
            self.warnings.clock(state.get("session_time"))
            self.state_changed.emit()
        elif kind == "event":
            event = frame.get("event") or {}
            self.events.insert(0, event)
            del self.events[EVENT_LIMIT:]
            self.warnings.observe(event)
            self.event_added.emit(event)
        elif kind == "radio":
            message = frame.get("message") or {}
            self.messages.append(message)
            del self.messages[:-400]
            self.radio.push(message)
            self.radio_added.emit(message)
        elif kind == "activity":
            self._apply_activity(frame.get("activity") or {}, live=True)
        elif kind == "error":
            self.notice.emit(str(frame.get("detail") or frame.get("message") or "socket error"))

    def now(self) -> float | None:
        t = (self.state or {}).get("session_time")
        return t if isinstance(t, (int, float)) else None

    def _apply_activity(self, record: dict, live: bool) -> None:
        activity.apply(self.model, record)
        kind = record.get("kind")
        run_id = record.get("run_id")
        now = self.now() if live else record.get("session_time")
        if kind == "run_start" and run_id:
            self.run_clock[run_id] = {"t0": record.get("session_time"), "t1": None}
        elif kind == "run_end" and run_id in self.run_clock:
            self.run_clock[run_id]["t1"] = now
        elif kind == "radio_aired":
            mid = (record.get("data") or {}).get("message_id")
            t = record.get("session_time")
            if mid and isinstance(t, (int, float)):
                self.aired[mid] = t
                self.radio_aired.emit(mid, t)
        if live:
            self.activity_record.emit(record)

    def _on_activity_history(self, data) -> None:
        for record in (data or {}).get("records") or []:
            self._apply_activity(record, live=False)
        self.activity_seeded.emit()

    def _on_events(self, data) -> None:
        events = list((data or {}).get("events") or [])
        events.sort(key=lambda e: e.get("session_time") or 0, reverse=True)
        self.events = events[:EVENT_LIMIT]
        for event in events:
            self.warnings.observe(event)
        self.events_seeded.emit()

    def _on_live(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "data":
            self.telemetry_changed.emit(frame)
        elif kind == "catalog":
            names = {v.get("name") for v in frame.get("variables") or [] if v.get("name")}
            self.catalog_changed.emit(names)

    def _on_tts(self, data) -> None:
        self.tts = data or {}
        self.speaker.set_voices(self.tts.get("voices") or {})

    def _failed(self, path: str, status: int, detail: str) -> None:
        # Polls fail quietly (the feed pill says it); actions surface the detail.
        pass

    # ---- actions -------------------------------------------------------
    def start_replay(self, session_id: str, speed: float, done=None) -> None:
        def ok(data):
            self.replay = data
            self.fuel_history.reset()
            self.gaps.reset()
            self.warnings.reset()
            self.status_changed.emit()
            if done:
                done()

        self.api.post(
            "/replay/start",
            {"session_id": session_id, "speed": speed},
            ok=ok,
            err=lambda _s, d: self.notice.emit(d),
        )

    def stop_replay(self) -> None:
        self.api.post(
            "/replay/stop",
            ok=lambda d: (setattr(self, "replay", d), self.status_changed.emit()),
            err=lambda _s, d: self.notice.emit(d),
        )

    def start_live(self) -> None:
        self.api.post(
            "/live/start", ok=lambda d: self.poll(), err=lambda _s, d: self.notice.emit(d)
        )

    def stop_live(self) -> None:
        self.api.post("/live/stop", ok=lambda d: self.poll(), err=lambda _s, d: self.notice.emit(d))

    def set_enabled(self, enabled: bool) -> None:
        self.api.post(
            "/pitwall/enabled",
            {"enabled": enabled},
            ok=lambda d: self.poll(),
            err=lambda _s, d: self.notice.emit(d),
        )

    def set_agent(self, name: str, enabled: bool) -> None:
        self.api.post(
            f"/pitwall/agents/{name}/enabled",
            {"enabled": enabled},
            ok=lambda d: self.poll(),
            err=lambda _s, d: self.notice.emit(d),
        )

    def reset_agents(self) -> None:
        self.api.post(
            "/pitwall/reset",
            ok=lambda d: (self.poll(), self.load_radio()),
            err=lambda _s, d: self.notice.emit(d),
        )

    def ask(self, text: str, source: str = "text", on_unheard=None) -> None:
        """Ask the pitwall a question. ``source`` is ``"text"`` or ``"voice"`` (C2).

        The transcript and its answer arrive on the radio socket; the answer's
        ``in_reply_to`` ties it to the transcript.
        """

        def err(status, detail):
            if status == 503 and on_unheard:
                on_unheard(text)
            else:
                self.notice.emit(detail)

        self.api.post("/pitwall/driver-message", {"text": text, "source": source}, err=err)

    def send_driver(self, text: str, on_unheard=None) -> None:
        """Typed text from the radio box: :meth:`ask` with ``source="text"``."""
        self.ask(text, "text", on_unheard)


def bind_voice(hub: Hub, feed, unheard) -> None:
    """Show a radio feed's push-to-talk row for the hub's voice, if it has one."""
    voice = hub.voice
    if voice is None:
        return
    feed.set_voice(voice.available, voice.key_label, voice.reason)
    if voice.state not in ("idle", "off"):
        feed.set_voice_state(voice.state, voice.detail)
    voice.state_changed.connect(feed.set_voice_state)
    feed.talk_pressed.connect(voice.press)
    feed.talk_released.connect(voice.release)
    hub.driver_unheard.connect(unheard)

