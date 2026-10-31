"""The review's onboard video panel: the frame at the cursor and the visual notes.

Data-free like every widget here: the review view feeds it rows from
``model.vision`` and the frame bytes the server grabbed, and listens to its signals.
Every note line is server text, so it goes through ``label()`` (plain text only).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..model.format import clock
from ..theme import tokens as t
from .common import Panel, clear_layout, label

FRAME_W, FRAME_H = 480, 270

EMPTY_TEXT = (
    "No onboard video for this session. Add a recording of the same session (OBS or "
    "iRacing's own capture) to see the car at the review cursor and the visual notes "
    "for the moments the telemetry flagged."
)
SYNC_TEXT = (
    "Line the video up with the telemetry: find a frame where the car crosses the "
    "start/finish line and say which lap it starts."
)
NOTES_CAVEAT = (
    "Visual notes describe the onboard frames in words. They never carry numbers: "
    "every figure in the review comes from the telemetry."
)
CONFIDENCE_COLOR = {"high": t.INK, "medium": t.SOFT, "low": t.MUTED}
SPEND_TEXT = (
    "An analysis sends onboard frames to Kimi, one call per moment, and uses your Kimi "
    "Code quota. The plan below is the free dry run of what it would look at."
)


class VisionSpendDialog(QDialog):
    """Confirm a vision analysis: the plan's cost in words, Cancel as the default."""

    def __init__(self, plan_line: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Use your Kimi quota?")
        self.setModal(True)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        layout.addWidget(label("Analyse the onboard video?", size=16, bold=True))
        layout.addWidget(label(SPEND_TEXT, "soft", wrap=True))
        box = QFrame()
        box.setObjectName("inset")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(12, 10, 12, 10)
        self.plan_label = label(plan_line, mono=True, wrap=True)
        bl.addWidget(self.plan_label)
        layout.addWidget(box)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        go = QPushButton("Analyse and spend quota")
        go.setProperty("role", "spend")
        cancel.clicked.connect(self.reject)
        go.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(go)
        layout.addLayout(buttons)
        cancel.setDefault(True)  # the safe choice is the default


class NoteCard(QFrame):
    """One note: a head line, its kept observations, a hint. Click to jump to it."""

    clicked = Signal(dict)

    def __init__(self, row: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.row = row
        self.setObjectName("mini")
        color = t.severity_color(row.get("severity"))
        self.setStyleSheet(f"#mini {{ border-left: 3px solid {color}; }}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(row.get("head") or "visual note")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 2, 0, 2)
        layout.setSpacing(2)
        icon = t.SEVERITY_ICON.get(row.get("severity") or "info", "●")
        layout.addWidget(label(f"{icon} {row.get('head', '')}", "muted", mono=True, size=11))
        for line in row.get("lines") or []:
            text = f"{line['tag'].upper()} · {line['text']}"
            tail = f"  ({line['confidence']}" + (", telemetry agrees)" if line["checked"] else ")")
            layout.addWidget(
                label(text + tail, wrap=True, size=12, color=CONFIDENCE_COLOR[line["confidence"]])
            )
        if not row.get("lines"):
            layout.addWidget(label("Nothing kept for this moment.", "muted", size=12))
        if row.get("hint"):
            layout.addWidget(
                label(row["hint"], color=t.SEVERITY_TEXT["advisory"], wrap=True, size=11)
            )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit(self.row)
        super().mousePressEvent(event)


class OnboardPanel(Panel):
    add_clicked = Signal()
    video_changed = Signal(str)
    sync_requested = Signal(int, float)
    preview_requested = Signal(float)
    plan_clicked = Signal()
    analyse_clicked = Signal()
    cancel_clicked = Signal()
    note_picked = Signal(dict)
    filter_cleared = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Onboard video", parent)
        self.video_pick = QComboBox()
        self.video_pick.setAccessibleName("Onboard video")
        self.video_pick.setVisible(False)
        self.video_pick.currentIndexChanged.connect(self._picked)
        self.add = QPushButton("Add video…")
        self.add.clicked.connect(self.add_clicked)
        self.resync = QPushButton("Re-sync")
        self.resync.setToolTip("Line the video up with the telemetry again")
        self.resync.clicked.connect(lambda: self.sync_box.setVisible(True))
        self.resync.setVisible(False)
        self.header.addWidget(self.video_pick)
        self.header.addWidget(self.resync)
        self.header.addWidget(self.add)

        # empty state
        self.empty = label(EMPTY_TEXT, "soft", wrap=True)
        self.body.addWidget(self.empty)

        # sync row
        self.sync_box = QFrame()
        self.sync_box.setObjectName("inset")
        sl = QVBoxLayout(self.sync_box)
        sl.setContentsMargins(12, 10, 12, 10)
        sl.addWidget(label(SYNC_TEXT, "soft", wrap=True))
        row = QHBoxLayout()
        row.addWidget(label("Lap", "muted"))
        self.sync_lap = QComboBox()
        self.sync_lap.setAccessibleName("Lap that starts at this video time")
        row.addWidget(self.sync_lap)
        row.addWidget(label("starts at", "muted"))
        self.sync_time = QDoubleSpinBox()
        self.sync_time.setAccessibleName("Video time of the line crossing, seconds")
        self.sync_time.setDecimals(2)
        self.sync_time.setSingleStep(0.1)
        self.sync_time.setSuffix(" s")
        self.sync_time.setRange(0.0, 36000.0)
        self.sync_time.valueChanged.connect(self.preview_requested)
        row.addWidget(self.sync_time)
        row.addWidget(label("into the video", "muted"))
        self.sync_go = QPushButton("Set sync")
        self.sync_go.setProperty("role", "primary")
        self.sync_go.clicked.connect(
            lambda: (
                self.sync_requested.emit(
                    int(self.sync_lap.currentData()), float(self.sync_time.value())
                )
                if self.sync_lap.currentData() is not None
                else None
            )
        )
        row.addWidget(self.sync_go)
        row.addStretch(1)
        sl.addLayout(row)
        self.body.addWidget(self.sync_box)

        # frame + notes
        self.main = QWidget()
        ml = QHBoxLayout(self.main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(16)
        left = QVBoxLayout()
        self.frame = QLabel()
        self.frame.setObjectName("inset")
        self.frame.setFixedSize(FRAME_W, FRAME_H)
        self.frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame.setTextFormat(Qt.TextFormat.PlainText)
        self.frame.setWordWrap(True)
        self.frame.setAccessibleName("Onboard frame at the cursor")
        left.addWidget(self.frame)
        self.caption = label("", "muted", mono=True, size=11)
        left.addWidget(self.caption)
        left.addStretch(1)
        ml.addLayout(left)

        right = QVBoxLayout()
        right.setSpacing(8)
        actions = QHBoxLayout()
        self.analysis = label("No analysis yet.", "soft", wrap=True)
        actions.addWidget(self.analysis, 1)
        self.plan = QPushButton("Plan")
        self.plan.setToolTip("Free: list the moments an analysis would look at")
        self.plan.clicked.connect(self.plan_clicked)
        self.analyse = QPushButton("Analyse…")
        self.analyse.setProperty("role", "spend")
        self.analyse.clicked.connect(self.analyse_clicked)
        self.cancel = QPushButton("Stop")
        self.cancel.setToolTip("Stop after the moment in flight")
        self.cancel.clicked.connect(self.cancel_clicked)
        self.cancel.setVisible(False)
        actions.addWidget(self.plan)
        actions.addWidget(self.analyse)
        actions.addWidget(self.cancel)
        right.addLayout(actions)
        self.blocker = label("", color=t.SEVERITY_TEXT["advisory"], wrap=True, size=12)
        self.blocker.setVisible(False)
        right.addWidget(self.blocker)
        self.plan_line = label("", "muted", wrap=True, size=12)
        self.plan_line.setVisible(False)
        right.addWidget(self.plan_line)
        filt = QHBoxLayout()
        self.filter_label = label("", "muted")
        self.filter_clear = QPushButton("Show all")
        self.filter_clear.clicked.connect(self.filter_cleared)
        filt.addWidget(self.filter_label)
        filt.addWidget(self.filter_clear)
        filt.addStretch(1)
        self.filter_row = QWidget()
        self.filter_row.setLayout(filt)
        self.filter_row.setVisible(False)
        right.addWidget(self.filter_row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(FRAME_H - 40)
        holder = QWidget()
        self.notes = QVBoxLayout(holder)
        self.notes.setContentsMargins(0, 0, 0, 0)
        self.notes.setSpacing(6)
        scroll.setWidget(holder)
        right.addWidget(scroll, 1)
        right.addWidget(label(NOTES_CAVEAT, "muted", wrap=True, size=11))
        ml.addLayout(right, 1)
        self.body.addWidget(self.main)
        self.main.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._videos: list[dict] = []
        self._synced = self._blocked = self._running = False
        self.set_videos([], None, [])

    # ---- feeding --------------------------------------------------------
    def set_videos(self, videos: list[dict], chosen: dict | None, laps: list[dict]) -> None:
        """The session's videos, the one shown, and the laps the sync can anchor on."""
        from ..model import vision

        self._videos = videos
        self.video_pick.blockSignals(True)
        self.video_pick.clear()
        for v in videos:
            self.video_pick.addItem(vision.video_title(v), v.get("video_id"))
        if chosen:
            self.video_pick.setCurrentIndex(
                max(0, self.video_pick.findData(chosen.get("video_id")))
            )
        self.video_pick.setVisible(len(videos) > 1)
        self.video_pick.blockSignals(False)

        self.empty.setVisible(chosen is None)
        synced = vision.is_synced(chosen)
        self.sync_box.setVisible(chosen is not None and not synced)
        self.resync.setVisible(synced)
        self.main.setVisible(chosen is not None)
        self._synced = synced
        self._buttons()
        self.meta.setText(
            "" if chosen is None else f"{vision.video_title(chosen)} · {vision.sync_text(chosen)}"
        )
        self.sync_lap.blockSignals(True)
        self.sync_lap.clear()
        for lap in laps:
            if lap.get("start_session_time") is not None:
                self.sync_lap.addItem(f"{lap['lap']}", lap["lap"])
        self.sync_lap.blockSignals(False)
        if chosen is not None and not synced:
            self.set_frame(None, "Find the line crossing: the frame follows the time you type.")

    def set_frame(self, jpeg: bytes | None, caption: str) -> None:
        if jpeg:
            pix = QPixmap()
            if pix.loadFromData(jpeg):
                self.frame.setPixmap(
                    pix.scaled(
                        FRAME_W,
                        FRAME_H,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                self.caption.setText(caption)
                return
        self.frame.setPixmap(QPixmap())
        self.frame.setText(caption or "No frame.")
        self.caption.setText("")

    def set_notes(self, rows: list[dict], corner: str | None, total: int) -> None:
        clear_layout(self.notes)
        self.filter_row.setVisible(corner is not None)
        if corner is not None:
            self.filter_label.setText(f"Notes at {corner} · {len(rows)} of {total}")
        if not rows:
            text = (
                f"No visual notes at {corner}."
                if corner is not None and total
                else "No visual notes yet. Plan shows what an analysis would look at, for free."
            )
            self.notes.addWidget(label(text, "muted", wrap=True))
        for row in rows:
            card = NoteCard(row)
            card.clicked.connect(self.note_picked)
            self.notes.addWidget(card)
        self.notes.addStretch(1)

    def set_analysis(self, text: str) -> None:
        self.analysis.setText(text)

    def set_plan(self, text: str | None) -> None:
        self.plan_line.setVisible(bool(text))
        self.plan_line.setText(text or "")

    def set_blocker(self, text: str | None) -> None:
        """Why Analyse cannot run (mode off, no key, no ffmpeg); ``None`` when it can."""
        self.blocker.setVisible(bool(text))
        self.blocker.setText(text or "")
        self._blocked = bool(text)
        self._buttons()

    def set_running(self, running: bool) -> None:
        self._running = running
        self._buttons()
        self.analyse.setText("Analysing…" if running else "Analyse…")
        self.cancel.setVisible(running)

    def _buttons(self) -> None:
        self.plan.setEnabled(self._synced and not self._running)
        self.analyse.setEnabled(self._synced and not self._blocked and not self._running)

    def frame_caption(self, session_time: float | None, video_time: float | None) -> str:
        return f"cursor {clock(session_time)} · video {clock(video_time)}"

    def _picked(self, _index: int) -> None:
        vid = self.video_pick.currentData()
        if vid:
            self.video_changed.emit(vid)
