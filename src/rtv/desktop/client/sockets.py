"""WebSocket feeds: ``/ws/pitwall`` (race state, events, radio, activity) and ``/ws/live``.

Both reconnect with backoff (500 ms doubling to 8 s) and report their own state
(``connecting``, ``open``, ``reconnecting``); while a socket is down the views
freeze and grey what they show, never zero it.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket

BACKOFF_START_MS = 500
BACKOFF_MAX_MS = 8000


class JsonSocket(QObject):
    """One JSON WebSocket that resubscribes on every (re)connect."""

    message = Signal(dict)
    status = Signal(str)

    def __init__(self, url: QUrl, subscribe: dict, parent: QObject | None = None):
        super().__init__(parent)
        self._url = url
        self._subscribe = subscribe
        self._backoff = BACKOFF_START_MS
        self._wanted = False
        self.state = "closed"
        self._ws = QWebSocket()
        self._ws.setParent(self)
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_text)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._open)

    def start(self) -> None:
        self._wanted = True
        self._set("connecting")
        self._open()

    def stop(self) -> None:
        self._wanted = False
        self._retry.stop()
        self._ws.close()
        self._set("closed")

    def send(self, payload: dict) -> None:
        self._ws.sendTextMessage(json.dumps(payload))

    def _open(self) -> None:
        if self._wanted:
            self._ws.open(self._url)

    def _on_connected(self) -> None:
        self._backoff = BACKOFF_START_MS
        self._set("open")
        self.send(self._subscribe)

    def _on_disconnected(self) -> None:
        if not self._wanted:
            return
        self._set("reconnecting")
        self._retry.start(self._backoff)
        self._backoff = min(BACKOFF_MAX_MS, self._backoff * 2)

    def _on_text(self, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if isinstance(frame, dict):
            self.message.emit(frame)

    def _set(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.status.emit(state)

    @property
    def retry_in_ms(self) -> int:
        return self._retry.remainingTime() if self._retry.isActive() else 0


TELEMETRY_CHANNELS = ["Speed", "Throttle", "Brake", "RPM", "Gear", "SteeringWheelAngle"]


def pitwall_socket(api, rate_hz: float = 10, parent: QObject | None = None) -> JsonSocket:
    return JsonSocket(api.ws_url("/ws/pitwall"), {"op": "subscribe", "rate_hz": rate_hz}, parent)


def live_socket(api, rate_hz: float = 20, parent: QObject | None = None) -> JsonSocket:
    return JsonSocket(
        api.ws_url("/ws/live"),
        {"op": "subscribe", "channels": TELEMETRY_CHANNELS, "rate_hz": rate_hz},
        parent,
    )
