"""The live view's Onboard panel: the camera, live or replayed, and the live visual notes.

It polls ``GET /vision/live`` once a second and fetches a frame a few times a second:
the capture's newest frame (``/vision/live/frame``) while a capture runs, or the
server-grabbed frame of the synced recording at the replay's session time. The view
logic is ``model.live_onboard``; every note line is server text, through ``label()``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from ..model.live_onboard import (
    CAPTURE_FRAME_MS,
    VIDEO_FRAME_MS,
    frame_request,
    onboard_view,
)
from ..theme import tokens as t
from .common import Panel, clear_layout, label
from .vision import CONFIDENCE_COLOR

FRAME_W, FRAME_H = 432, 243
STATUS_MS = 1000


class LiveOnboardPanel(Panel):
    """Data-free display. :meth:`bind` gives it an API and a session clock to follow."""

    capture_toggled = Signal(bool)  # True = start

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Onboard", parent)
        self.view: dict[str, Any] = onboard_view(None)
        self.toggle = QPushButton("● Capture video")
        self.toggle.setToolTip("Capture the sim window with ffmpeg (free; no model call)")
        self.toggle.clicked.connect(
            lambda: self.capture_toggled.emit(not self.view["capture_running"])
        )
        self.header.addWidget(self.toggle)
        self.frame = QLabel()
        self.frame.setObjectName("inset")
        self.frame.setFixedSize(FRAME_W, FRAME_H)
        self.frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame.setWordWrap(True)
        self.frame.setAccessibleName("onboard camera")
        self.body.addWidget(self.frame)
        self.vlm_line = label("", "muted", mono=True, size=11, wrap=True)
        self.body.addWidget(self.vlm_line)
        notes_holder = QWidget()
        self.notes = QVBoxLayout(notes_holder)
        self.notes.setContentsMargins(0, 0, 0, 0)
        self.notes.setSpacing(6)
        self.body.addWidget(notes_holder)
        self._note_ids: tuple = ()
        self._api: Any = None
        self._now: Callable[[], float | None] = lambda: None
        self._on_notice: Callable[[str], None] = lambda _m: None
        self._status_timer: QTimer | None = None
        self._frame_timer: QTimer | None = None
        self.render(self.view)

    # ---- display ---------------------------------------------------------
    def render(self, view: dict) -> None:
        self.view = view
        self.meta.setText(view["source_line"] if view["mode"] != "empty" else "")
        self.toggle.setText("■ Stop video" if view["capture_running"] else "● Capture video")
        self.vlm_line.setText(view["vlm_line"])
        if view["mode"] == "empty":
            self.set_frame(None, view["source_line"])
        ids = tuple(n["id"] for n in view["notes"])
        if ids != self._note_ids:
            self._note_ids = ids
            clear_layout(self.notes)
            for note in view["notes"]:
                self.notes.addWidget(label(note["head"], "muted", mono=True, size=11))
                for line in note["lines"]:
                    self.notes.addWidget(
                        label(
                            f"{line['kind'].upper()} · {line['text']}  ({line['confidence']})",
                            wrap=True,
                            size=12,
                            color=CONFIDENCE_COLOR.get(line["confidence"], t.MUTED),
                        )
                    )
                if note["empty"]:
                    self.notes.addWidget(
                        label(note["empty"], color=t.SEVERITY_TEXT["advisory"], wrap=True, size=11)
                    )

    def set_frame(self, jpeg: bytes | None, placeholder: str = "") -> None:
        if jpeg:
            pix = QPixmap()
            if pix.loadFromData(jpeg):
                self.frame.setPixmap(
                    pix.scaled(
                        FRAME_W, FRAME_H,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )  # fmt: skip
                return
        if placeholder:
            self.frame.setPixmap(QPixmap())
            self.frame.setText(placeholder)

    # ---- data ------------------------------------------------------------
    def bind(
        self,
        api: Any,
        now: Callable[[], float | None],
        *,
        on_notice: Callable[[str], None] | None = None,
        start: bool = True,
    ) -> None:
        """Poll the vision API through ``api``; ``now`` is the live/replay session time."""
        self._api, self._now = api, now
        if on_notice is not None:
            self._on_notice = on_notice
        self.capture_toggled.connect(self._toggle_capture)
        if not start:
            return
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(STATUS_MS)
        self._status_timer.timeout.connect(self.poll)
        self._status_timer.start()
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(CAPTURE_FRAME_MS)
        self._frame_timer.timeout.connect(self.fetch_frame)
        self._frame_timer.start()
        self.poll()

    def poll(self) -> None:
        if self._api is None:
            return
        self._api.get(
            "/vision/live",
            ok=lambda data: self.render(onboard_view(data)),
            err=lambda *_: None,  # the status strip already says when the server is gone
            key="live-onboard",
        )

    def fetch_frame(self) -> None:
        if self._api is None:
            return
        if self._frame_timer is not None:
            ms = CAPTURE_FRAME_MS if self.view["mode"] == "capture" else VIDEO_FRAME_MS
            if self._frame_timer.interval() != ms:
                self._frame_timer.setInterval(ms)
        req = frame_request(self.view, self._now(), width=FRAME_W)
        if req is None:
            if self.view["mode"] == "video":
                self.set_frame(None, "The recording does not cover this moment.")
            return
        path, params = req
        # 204 (no frame yet) arrives as empty bytes: keep the last frame up.
        self._api.get_bytes(
            path, params, ok=lambda data: self.set_frame(data or None), err=lambda *_: None,
            key="live-onboard-frame",
        )  # fmt: skip

    def _toggle_capture(self, start: bool) -> None:
        action = "start" if start else "stop"

        def ok(data: dict) -> None:
            if not start and (data or {}).get("video"):
                self._on_notice("Capture saved and synced to the session; the review can show it.")
            self.poll()

        self._api.post(
            f"/vision/live/capture/{action}", {}, ok=ok,
            err=lambda _s, d: self._on_notice(f"Capture {action} failed: {d}"),
        )  # fmt: skip
