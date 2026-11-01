"""The review's debrief panel (stage 09 part B): the post-race debrief of the selected run.

Paints ``rtv.desktop.model.debrief``; it decides nothing. Each lap or corner the debrief
names is a link button: ``ref_clicked`` carries ``{kind, lap, corner}`` and the review opens
that lap in its lap analysis (and highlights the corner's window). ``regenerate_clicked``
fires only after the operator confirmed the dialog that says Kimi will be called.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..model import debrief as model
from ..theme import tokens as t
from .common import KeyValue, Panel, clear_layout, label

TONE = {"good": t.GOOD, "warn": t.SEVERITY["advisory"], "muted": t.MUTED}


def _ref_button(ref: dict, emit) -> QPushButton:
    b = QPushButton(ref["text"])
    b.setFlat(True)
    b.setProperty("role", "link")
    b.setAccessibleName(f"Open {ref['text']} in the lap analysis")
    b.setToolTip("Open in the lap analysis")
    b.clicked.connect(lambda _c=False, r=ref: emit(r))
    return b


class DebriefPanel(Panel):
    ref_clicked = Signal(dict)
    regenerate_clicked = Signal()

    def __init__(self, parent: QWidget | None = None, *, confirm=None):
        super().__init__("Debrief", parent)
        #: () -> bool; the confirmation dialog, replaceable in tests.
        self.confirm = confirm or self._confirm
        self.regen = QPushButton("Regenerate…")
        self.regen.setAccessibleName("Regenerate the debrief (calls Kimi)")
        self.regen.setToolTip(model.REGENERATE_TEXT)
        self.regen.clicked.connect(self._regenerate)
        self.header.addWidget(self.regen)
        self.note = label("", "muted", wrap=True)
        self.body.addWidget(self.note)
        self.result_box = QFrame()
        self.result_box.setObjectName("inset")
        self.result_grid = QGridLayout(self.result_box)
        self.body.addWidget(self.result_box)
        self.losses = QVBoxLayout()
        self.losses.setSpacing(4)
        self.body.addLayout(self.losses)
        self.sections = QVBoxLayout()
        self.sections.setSpacing(10)
        self.body.addLayout(self.sections)
        self.doc: dict | None = None
        self.set_debrief(None, None)

    def _confirm(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Regenerate the debrief?")
        box.setText("Regenerate the debrief? This calls Kimi.")
        box.setInformativeText(model.REGENERATE_TEXT)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _regenerate(self) -> None:
        if self.confirm():
            self.regenerate_clicked.emit()

    def set_busy(self, text: str) -> None:
        self.meta.setText(text)
        self.regen.setEnabled(False)

    def set_debrief(self, doc: dict | None, run_id: str | None) -> None:
        self.doc = doc
        self.regen.setEnabled(bool(run_id))
        clear_layout(self.result_grid)
        clear_layout(self.losses)
        clear_layout(self.sections)
        if not doc:
            self.meta.setText("")
            self.note.setText(model.empty_text(run_id))
            self.result_box.hide()
            return
        head = model.header(doc)
        self.meta.setText(head["meta"])
        self.note.setText(head["note"])
        self.note.setVisible(bool(head["note"]))
        self.result_box.show()
        for i, (key, value) in enumerate(model.result_rows(doc)):
            kv = KeyValue(key)
            kv.set(value)
            self.result_grid.addWidget(kv, i // 5, i % 5)
        rows = model.loss_rows(doc)
        if rows:
            self.losses.addWidget(label("RECURRING LOSSES (LAYER 1)", "label"))
        for row in rows:
            line = QHBoxLayout()
            ref = {"kind": "corner", "corner": row["corner"], "lap": row["lap"],
                   "text": row["title"]}
            line.addWidget(_ref_button(ref, self.ref_clicked.emit))
            line.addWidget(label(row["text"], "soft"), 1)
            if row["why"]:
                line.addWidget(label(row["why"], "muted"))
            self.losses.addLayout(line)
        for sec in model.section_rows(doc):
            box = QVBoxLayout()
            top = QHBoxLayout()
            top.addWidget(label(sec["title"].upper(), "label"))
            top.addWidget(label(sec["badge"], color=TONE.get(sec["tone"])))
            top.addStretch(1)
            box.addLayout(top)
            if sec["body"]:
                box.addWidget(label(sec["body"], "soft" if sec["status"] in {"ok", "repaired"}
                                    else "muted", wrap=True))
            self._refs(box, sec["refs"])
            for item in sec["items"]:
                box.addWidget(label("• " + item["text"], wrap=True))
                self._refs(box, item["refs"])
            self.sections.addLayout(box)

    def _refs(self, box: QVBoxLayout, refs: list[dict]) -> None:
        if not refs:
            return
        line = QHBoxLayout()
        line.setSpacing(4)
        for ref in refs:
            line.addWidget(_ref_button(ref, self.ref_clicked.emit))
        line.addStretch(1)
        box.addLayout(line)
