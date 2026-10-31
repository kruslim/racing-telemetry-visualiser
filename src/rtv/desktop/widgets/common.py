"""Small shared building blocks: plain-text labels, panels, tags, pills, stat cards.

Every label that can show server text is created through ``label()``, which pins
``Qt.PlainText``: a QLabel otherwise guesses rich text from anything that looks
like HTML, which is the desktop twin of the web frontend's ``innerHTML`` rule.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..model.format import NA
from ..theme import tokens as t


def mono_font(size: int | None = None, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont()
    font.setFamilies(t.MONO_FAMILIES)
    font.setStyleHint(QFont.StyleHint.Monospace)
    if size:
        font.setPixelSize(size)
    font.setWeight(weight)
    return font


def label(
    text: str = "",
    role: str | None = None,
    *,
    mono: bool = False,
    color: str | None = None,
    wrap: bool = False,
    size: int | None = None,
    bold: bool = False,
) -> QLabel:
    widget = QLabel()
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setText(text)
    if role:
        widget.setProperty("role", role)
    if wrap:
        widget.setWordWrap(True)
    if mono:
        widget.setFont(mono_font())
    style = []
    if color:
        style.append(f"color: {color};")
    if size:
        style.append(f"font-size: {size}px;")
    if bold:
        style.append("font-weight: 700;")
    if style:
        widget.setStyleSheet(" ".join(style))
    return widget


def set_text(widget: QLabel, text: str | None, color: str | None = None) -> None:
    """Set server-sourced text; ``None`` reads ``n/a``, never blank or zero."""
    widget.setText(NA if text is None else str(text))
    if color is not None:
        widget.setStyleSheet(_merge_color(widget.styleSheet(), color))


def _merge_color(style: str, color: str) -> str:
    kept = [part for part in style.split(";") if part.strip() and "color" not in part.split(":")[0]]
    return "; ".join([*kept, f"color: {color}"]) + ";"


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def set_role(widget: QWidget, role: str) -> None:
    widget.setProperty("role", role)
    repolish(widget)


def tag(text: str, bg: str, fg: str) -> QLabel:
    widget = label(text.upper(), "tag")
    widget.setStyleSheet(f"background: {bg}; color: {fg};")
    widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return widget


def severity_tag(severity: str | None) -> QLabel:
    sev = severity if severity in t.SEVERITY else "info"
    return tag(f"{t.SEVERITY_ICON[sev]} {sev}", t.SEVERITY_BG[sev], t.SEVERITY_TEXT[sev])


def outcome_tag(outcome: str | None) -> QLabel:
    key = outcome or "unfinished"
    word = "spoke" if key == "radio" else key
    bg, fg = t.OUTCOME.get(key, t.OUTCOME["unfinished"])
    if key == "error":
        word = "✕ error"
    return tag(word, bg, fg)


def pill(text: str, bg: str, fg: str) -> QLabel:
    widget = label(text, "pill")
    widget.setStyleSheet(f"background: {bg}; color: {fg};")
    widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return widget


def set_pill(widget: QLabel, text: str, bg: str, fg: str) -> None:
    widget.setText(text)
    widget.setStyleSheet(f"background: {bg}; color: {fg};")


def clear_layout(layout: QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        child = item.widget()
        if child is not None:
            child.setParent(None)
            child.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def hbox(*widgets: QWidget, spacing: int = 8, margins: tuple = (0, 0, 0, 0)) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    for widget in widgets:
        if widget is None:
            layout.addStretch(1)
        else:
            layout.addWidget(widget)
    return layout


def vbox(*widgets: QWidget, spacing: int = 8, margins: tuple = (0, 0, 0, 0)) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    for widget in widgets:
        if widget is None:
            layout.addStretch(1)
        else:
            layout.addWidget(widget)
    return layout


class Panel(QFrame):
    """A titled panel: header row (title + right-hand meta) over a body layout."""

    def __init__(self, title: str, parent: QWidget | None = None, *, subtitle: str = ""):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setAccessibleName(title)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QWidget()
        header.setObjectName("panelHeader")
        self.header = QHBoxLayout(header)
        self.header.setContentsMargins(14, 9, 14, 9)
        self.header.setSpacing(8)
        self.title = label(title, "title")
        self.header.addWidget(self.title)
        if subtitle:
            self.header.addWidget(label(subtitle, "muted"))
        self.header.addStretch(1)
        self.meta = label("", "muted")
        self.header.addWidget(self.meta)
        outer.addWidget(header)
        self.body_widget = QWidget()
        self.body = QVBoxLayout(self.body_widget)
        self.body.setContentsMargins(14, 10, 14, 12)
        self.body.setSpacing(8)
        outer.addWidget(self.body_widget, 1)


class KeyValue(QWidget):
    """A small uppercase label over a value."""

    def __init__(self, key: str, role: str = "big", parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self.key = label(key.upper(), "label")
        self.value = label(NA, role, mono=True)
        layout.addWidget(self.key)
        layout.addWidget(self.value)

    def set(self, text: str | None, color: str | None = None) -> None:
        set_text(self.value, text, color)


class StatCard(QFrame):
    """A summary stat: label, value, small print."""

    def __init__(self, key: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        self.key = label(key.upper(), "label")
        self.value = label(NA, "value", mono=True)
        self.sub = label("", "muted", wrap=True)
        layout.addWidget(self.key)
        layout.addWidget(self.value)
        layout.addWidget(self.sub)

    def set(self, value: str | None, sub: str = "", color: str | None = None) -> None:
        set_text(self.value, value, color)
        self.sub.setText(sub)


def reduced_motion_default() -> bool:
    """The Windows "show animations" setting; elsewhere, motion stays on."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        enabled = ctypes.c_bool(True)
        # SPI_GETCLIENTAREAANIMATION
        ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
        return bool(ok) and not enabled.value
    except Exception:  # noqa: BLE001 - a preference read must never stop the app
        return False
