"""The radio on its own: a compact window that can stay on top of the sim."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QVBoxLayout, QWidget

from ..theme import tokens as t
from ..widgets.common import label
from ..widgets.radio import RadioFeed
from .hub import Hub, bind_voice


class RadioWindow(QWidget):
    def __init__(self, hub: Hub, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.hub = hub
        self.setWindowTitle("RTV — Pitwall radio")
        self.setObjectName("root")
        self.resize(480, 900)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        top = QWidget()
        top.setObjectName("chrome")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(14, 8, 14, 8)
        tl.addWidget(label("Pitwall radio", "title"))
        self.conn = label("", "muted")
        tl.addWidget(self.conn)
        tl.addStretch(1)
        self.on_top = QCheckBox("on top")
        self.on_top.setAccessibleName("Keep the radio window on top")
        self.on_top.toggled.connect(self._on_top)
        tl.addWidget(self.on_top)
        layout.addWidget(top)
        self.feed = RadioFeed(compact=True)
        layout.addWidget(self.feed, 1)

        self.feed.seed(hub.messages)
        for mid, t_s in hub.aired.items():
            self.feed.aired[mid] = t_s
        hub.radio_added.connect(self.feed.add)
        hub.radio_seeded.connect(lambda: self.feed.seed(hub.messages))
        hub.radio_aired.connect(self.feed.mark_aired)
        hub.on_air_changed.connect(
            lambda snap: self.feed.set_on_air(snap.get("speaking"), len(snap.get("queue") or []))
        )
        hub.feed_changed.connect(self._conn)
        self.feed.send_text.connect(lambda text: hub.send_driver(text, self._unheard))
        bind_voice(hub, self.feed, self._unheard)
        self.feed.volume_changed.connect(hub.radio.set_volume)
        self.feed.master_toggled.connect(lambda on: hub.radio.set_master_muted(not on))
        self.feed.mute_toggled.connect(hub.radio.set_agent_muted)
        self.feed.set_muted(set(hub.radio.muted_agents()))
        if not hub.speaker.available:
            self.feed.set_speech_note(hub.speaker.reason)
        self._conn(hub.feed_state)

    def _unheard(self, text: str) -> None:
        self.feed.add(
            {
                "agent": "driver",
                "spoken_text": text,
                "speak": False,
                "session_time": self.hub.now(),
                "heard": False,
            }
        )

    def _conn(self, state: str) -> None:
        color = {"open": t.GOOD, "reconnecting": t.SEVERITY["advisory"]}.get(state, t.MUTED)
        word = {"open": "connected"}.get(state, state)
        self.conn.setText(f"● {word}")
        self.conn.setStyleSheet(f"color: {color}; font-size: 12px;")

    def _on_top(self, on: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()
