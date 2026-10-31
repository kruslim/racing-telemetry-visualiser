"""The radio feed: on-air strip, audio controls, the calls (newest first), driver input.

Used by the live pitwall and the radio window. Refusals are shown as grounded
refusals, not errors; a driver message nests the calls that aired within 45 s as
"after your message", never as a reply.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..model.activity import AGENTS
from ..model.format import agent_badge, clock, is_num
from ..model.radio import thread_feed
from ..theme import tokens as t
from .common import clear_layout, label, severity_tag, tag

EMPTY_TEXT = (
    "Nothing on the radio yet. Agents speak when RTV_PITWALL_AGENTS=true and "
    "KIMI_CODE_API_KEY is set; the deterministic ticker below runs regardless."
)


def _when(message: dict, aired: dict[str, float]) -> str:
    ref = message.get("event_ref") or {}
    lap = ref.get("lap")
    event_t = ref.get("session_time")
    if not is_num(event_t):
        event_t = message.get("session_time")
    text = (f"L{lap} · " if lap is not None else "") + clock(event_t)
    aired_t = aired.get(message.get("message_id") or "")
    if is_num(aired_t) and is_num(event_t) and aired_t - event_t >= 1:
        text += f" → aired {clock(aired_t)}"
    return text


class RadioEntry(QFrame):
    """One call (and, for a driver message, the calls that aired after it)."""

    toggled_detail = Signal(str)

    def __init__(
        self,
        message: dict,
        children: list[dict],
        aired: dict[str, float],
        expanded: set[str],
        muted: set[str],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        agent = message.get("agent") or "pitwall"
        is_driver = agent == "driver"
        refused = bool(message.get("refused"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(4)
        color = t.agent_color(agent)
        style = "dashed" if refused else "solid"
        bg = "#170f12" if message.get("priority") == "critical" else "transparent"
        if is_driver:
            bg = "#13171d"
        self.setObjectName("radioEntry")
        self.setStyleSheet(
            f"#radioEntry {{ background: {bg}; border-left: 3px {style} "
            f"{color if not is_driver else t.LINE_2}; border-bottom: 1px solid {t.ROW_LINE}; }}"
        )
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(label(agent_badge(agent), color=t.agent_text(agent), bold=True, size=12))
        if is_driver:
            head.addWidget(tag("not spoken", "#1c222a", t.SOFT))
        else:
            head.addWidget(severity_tag(message.get("priority")))
        if refused:
            head.addWidget(tag("withheld · grounded refusal", "#2a2414", "#ffd27a"))
        if agent in muted:
            head.addWidget(tag("muted", "#1c222a", t.SOFT))
        head.addWidget(label(_when(message, aired), "muted", mono=True))
        head.addStretch(1)
        layout.addLayout(head)
        spoken = label(
            message.get("spoken_text") or "", "radio", wrap=True, color=t.SOFT if refused else None
        )
        spoken.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(spoken)
        if not is_driver:
            foot = QHBoxLayout()
            foot.setSpacing(10)
            ungrounded = message.get("ungrounded") or []
            if message.get("grounded") is not False and not refused:
                foot.addWidget(label("✓ grounded", color=t.GOOD, size=11))
            if ungrounded:
                foot.addWidget(
                    label(
                        "could not back: " + ", ".join(map(str, ungrounded)),
                        color="#ffd27a",
                        size=11,
                    )
                )
            tools = message.get("tools_used") or []
            if tools:
                tools_l = label(" · ".join(tools), "muted", size=11)
                tools_l.setWordWrap(True)
                foot.addWidget(tools_l, 1)
            else:
                foot.addStretch(1)
            mid = message.get("message_id") or ""
            if message.get("detail_text"):
                why = QPushButton("why ▴" if mid in expanded else "why ▾")
                why.setProperty("role", "link")
                why.setAccessibleName("Show why")
                why.clicked.connect(lambda: self.toggled_detail.emit(mid))
                foot.addWidget(why)
            layout.addLayout(foot)
            if mid in expanded and message.get("detail_text"):
                detail = label(message["detail_text"], wrap=True, color=t.SOFT, size=12)
                detail.setStyleSheet(
                    f"color: {t.SOFT}; font-size: 12px; background: {t.CHROME};"
                    " border-radius: 6px; padding: 8px 10px;"
                )
                detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                layout.addWidget(detail)
        if is_driver:
            thread = QFrame()
            thread.setObjectName("thread")
            thread.setStyleSheet(f"#thread {{ border-left: 2px solid {t.LINE_2}; }}")
            tl = QVBoxLayout(thread)
            tl.setContentsMargins(10, 2, 0, 0)
            tl.setSpacing(4)
            tl.addWidget(label("after your message", "muted", size=11))
            for child in children:
                entry = RadioEntry(child, [], aired, expanded, muted)
                entry.toggled_detail.connect(self.toggled_detail)
                tl.addWidget(entry)
            if not children:
                tl.addWidget(label("no call aired within 45 s yet", "muted", size=11))
            row = QHBoxLayout()
            row.setContentsMargins(14, 0, 0, 0)
            row.addWidget(thread)
            layout.addLayout(row)
            if message.get("heard") is False:
                layout.addWidget(
                    label(
                        "Nobody heard it: the pitwall agent layer is not running.",
                        color=t.SEVERITY_TEXT["advisory"],
                        size=12,
                        wrap=True,
                    )
                )


class RadioFeed(QWidget):
    """The whole radio column."""

    send_text = Signal(str)
    volume_changed = Signal(float)
    master_toggled = Signal(bool)
    mute_toggled = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None, *, compact: bool = False):
        super().__init__(parent)
        self.messages: list[dict] = []
        self.aired: dict[str, float] = {}
        self.expanded: set[str] = set()
        self.muted: set[str] = set()
        self.empty_text = EMPTY_TEXT
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QWidget()
        head.setObjectName("panelHeader")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(14, 9, 14, 9)
        hl.addWidget(label("Radio", "title"))
        hl.addStretch(1)
        self.count = label("0 calls · 0 queued", "muted")
        hl.addWidget(self.count)
        if not compact:
            outer.addWidget(head)

        self.on_air = QWidget()
        self.on_air.setObjectName("chrome")
        al = QHBoxLayout(self.on_air)
        al.setContentsMargins(14, 7, 14, 7)
        al.setSpacing(10)
        self.on_air_tag = tag("off air", "#1c222a", t.SOFT)
        self.on_air_text = label("", size=12)
        self.on_air_text.setMinimumWidth(10)
        al.addWidget(self.on_air_tag)
        al.addWidget(self.on_air_text, 1)
        if compact:
            al.addWidget(self.count)
        outer.addWidget(self.on_air)

        controls = QWidget()
        cl = QHBoxLayout(controls)
        cl.setContentsMargins(14, 6, 14, 6)
        cl.setSpacing(8)
        self.master = QCheckBox("Radio on")
        self.master.setChecked(True)
        self.master.toggled.connect(self.master_toggled)
        cl.addWidget(self.master)
        cl.addWidget(label("Volume", "muted"))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(90)
        self.volume.setFixedWidth(100)
        self.volume.setAccessibleName("Radio volume")
        self.volume.valueChanged.connect(lambda v: self.volume_changed.emit(v / 100))
        cl.addWidget(self.volume)
        cl.addStretch(1)
        outer.addWidget(controls)
        mutes = QWidget()
        cl = QHBoxLayout(mutes)
        cl.setContentsMargins(14, 0, 14, 6)
        cl.setSpacing(8)
        cl.addWidget(label("Mute:", "muted"))
        self.mute_buttons: dict[str, QPushButton] = {}
        for agent in AGENTS:
            button = QPushButton("engineer" if agent == "vehicle_engineer" else agent)
            button.setCheckable(True)
            button.setAccessibleName(f"Mute {t.AGENT_LABEL[agent]}")
            button.setStyleSheet(f"color: {t.agent_text(agent)}; min-height: 28px; padding: 0 8px;")
            button.toggled.connect(lambda on, a=agent: self._mute(a, on))
            self.mute_buttons[agent] = button
            cl.addWidget(button)
        cl.addStretch(1)
        outer.addWidget(mutes)
        self.speech_note = label("", "muted", wrap=True)
        self.speech_note.setContentsMargins(14, 0, 14, 4)
        self.speech_note.hide()
        outer.addWidget(self.speech_note)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        self.list = QVBoxLayout(holder)
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setSpacing(0)
        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll, 1)

        driver = QWidget()
        driver.setObjectName("chromeTop")
        dl = QHBoxLayout(driver)
        dl.setContentsMargins(14, 8, 14, 8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("say something to the pitwall…")
        self.input.setAccessibleName("Message to the pitwall")
        self.input.setMaxLength(500)
        self.send = QPushButton("Send")
        self.send.setProperty("role", "primary")
        dl.addWidget(self.input, 1)
        dl.addWidget(self.send)
        outer.addWidget(driver)
        self.send.clicked.connect(self._send)
        self.input.returnPressed.connect(self._send)
        self.render()

    def _send(self) -> None:
        text = self.input.text().strip()
        if text:
            self.send_text.emit(text)
            self.input.clear()

    def _mute(self, agent: str, on: bool) -> None:
        if on:
            self.muted.add(agent)
        else:
            self.muted.discard(agent)
        self.mute_toggled.emit(agent, on)
        self.render()

    def set_muted(self, agents: set[str]) -> None:
        for agent, button in self.mute_buttons.items():
            button.blockSignals(True)
            button.setChecked(agent in agents)
            button.blockSignals(False)
        self.muted = set(agents)

    def set_speech_note(self, text: str) -> None:
        self.speech_note.setText(text)
        self.speech_note.setVisible(bool(text))

    def seed(self, messages: list[dict]) -> None:
        self.messages = [m for m in messages if m]
        self.render()

    def add(self, message: dict) -> None:
        mid = message.get("message_id")
        if mid and any(m.get("message_id") == mid for m in self.messages):
            return
        self.messages.append(message)
        self.render()

    def mark_aired(self, message_id: str, session_time: float) -> None:
        if message_id and message_id not in self.aired:
            self.aired[message_id] = session_time
            self.render()

    def set_on_air(self, speaking: dict | None, queued: int) -> None:
        if speaking:
            agent = speaking.get("agent")
            self.on_air_tag.setText("● ON AIR")
            self.on_air_tag.setStyleSheet("background: #173322; color: #8ff0c3;")
            self.on_air_text.setText(f"{agent_badge(agent)} · {speaking.get('spoken_text')}")
        else:
            self.on_air_tag.setText("OFF AIR")
            self.on_air_tag.setStyleSheet(f"background: #1c222a; color: {t.SOFT};")
            self.on_air_text.setText("")
        calls = sum(1 for m in self.messages if m.get("agent") != "driver")
        self.count.setText(f"{calls} calls · {queued} queued")

    def _toggle(self, mid: str) -> None:
        if mid in self.expanded:
            self.expanded.discard(mid)
        else:
            self.expanded.add(mid)
        self.render()

    def render(self) -> None:
        bar = self.scroll.verticalScrollBar()
        at_top = bar.value() <= 4
        old = bar.value()
        clear_layout(self.list)
        entries = thread_feed(self.messages)
        if not entries:
            self.list.addWidget(label(self.empty_text, "empty", wrap=True))
        for entry in reversed(entries):  # newest first
            widget = RadioEntry(
                entry["message"], entry["children"], self.aired, self.expanded, self.muted
            )
            widget.toggled_detail.connect(self._toggle)
            self.list.addWidget(widget)
        self.list.addStretch(1)
        calls = sum(1 for m in self.messages if m.get("agent") != "driver")
        queued = self.count.text().split("·")[-1].strip() if self.count.text() else ""
        self.count.setText(f"{calls} calls · {queued or '0 queued'}")
        if not at_top:
            bar.setValue(old)
