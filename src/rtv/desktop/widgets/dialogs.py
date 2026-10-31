"""Dialogs: the Kimi quota prompt and the AI controls.

The quota prompt stands before anything that makes the agents run (recording a run
or starting a replay while they are mounted and enabled), with the brief's copy.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..model.activity import AGENTS
from ..model.format import NA, integer
from ..theme import tokens as t
from .common import KeyValue, label

QUOTA_TEXT = (
    "The pitwall agents are switched on, so replaying this session will call Kimi "
    "and use your quota. Continue?"
)


def agents_will_run(status: dict | None) -> bool:
    """Mounted and enabled: a replay or recorded run would call Kimi."""
    return bool(status and status.get("available") and status.get("enabled") is not False)


class QuotaDialog(QDialog):
    def __init__(self, status: dict | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Use your Kimi quota?")
        self.setModal(True)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        layout.addWidget(label("Use your Kimi quota?", size=16, bold=True))
        layout.addWidget(label(QUOTA_TEXT, "soft", wrap=True))
        box = QFrame()
        box.setObjectName("inset")
        grid = QGridLayout(box)
        status = status or {}
        used = KeyValue("Used")
        used.set(
            f"{integer(status.get('calls_used'))} / {integer(status.get('max_calls_per_session'))}"
        )
        remaining = KeyValue("Remaining")
        remaining.set(integer(status.get("calls_remaining")))
        budget = KeyValue("Budget")
        budget.set(
            "exhausted" if status.get("budget_exhausted") else "available",
            t.SEVERITY["critical"] if status.get("budget_exhausted") else None,
        )
        for i, kv in enumerate((used, remaining, budget)):
            grid.addWidget(kv, 0, i)
        layout.addWidget(box)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        go = QPushButton("Continue and spend quota")
        go.setProperty("role", "spend")
        cancel.clicked.connect(self.reject)
        go.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(go)
        layout.addLayout(buttons)
        cancel.setDefault(True)  # the safe choice is the default


class AiControls(QDialog):
    """AI master switch, one toggle per agent, reset; disabled with a reason when off."""

    set_enabled = Signal(bool)
    set_agent = Signal(str, bool)
    reset = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("AI controls")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        layout.addWidget(label("Pitwall AI", size=15, bold=True))
        self.reason = label("", "muted", wrap=True)
        layout.addWidget(self.reason)
        self.master = QCheckBox("Agents on (global kill switch)")
        self.master.toggled.connect(self._master)
        layout.addWidget(self.master)
        grid = QGridLayout()
        self.agents: dict[str, QCheckBox] = {}
        self.agent_notes: dict[str, object] = {}
        for i, name in enumerate(AGENTS):
            box = QCheckBox(t.AGENT_LABEL[name])
            box.setStyleSheet(f"color: {t.agent_text(name)};")
            box.toggled.connect(lambda on, n=name: self._agent(n, on))
            note = label("", "muted", size=11)
            self.agents[name] = box
            self.agent_notes[name] = note
            grid.addWidget(box, i, 0)
            grid.addWidget(note, i, 1)
        layout.addLayout(grid)
        row = QHBoxLayout()
        self.budget = label("", "muted", mono=True)
        row.addWidget(self.budget, 1)
        self.reset_btn = QPushButton("Reset cooldowns && radio")
        self.reset_btn.clicked.connect(self.reset)
        row.addWidget(self.reset_btn)
        layout.addLayout(row)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
        self._updating = False

    def _master(self, on: bool) -> None:
        if not self._updating:
            self.set_enabled.emit(on)

    def _agent(self, name: str, on: bool) -> None:
        if not self._updating:
            self.set_agent.emit(name, on)

    def set_status(self, status: dict | None) -> None:
        self._updating = True
        status = status or {}
        available = bool(status.get("available"))
        self.reason.setText(
            ""
            if available
            else f"{status.get('reason') or 'The agent layer is not running.'}"
            " The app cannot mount "
            "the agents: start it with RTV_PITWALL_AGENTS=true and KIMI_CODE_API_KEY set."
        )
        self.reason.setVisible(not available)
        self.master.setEnabled(available)
        self.master.setChecked(available and status.get("enabled") is not False)
        by_name = {a.get("name"): a for a in status.get("agents") or []}
        for name, box in self.agents.items():
            info = by_name.get(name, {})
            box.setEnabled(available)
            box.setChecked(bool(info.get("enabled", False)) if available else False)
            note = self.agent_notes[name]
            reason = info.get("disabled_reason")
            if reason or info.get("degraded"):
                note.setText(f"degraded: {reason or 'breaker tripped'}")
            elif available:
                note.setText(
                    f"{info.get('invocations', 0)} runs · {info.get('llm_calls', 0)} calls"
                )
            else:
                note.setText("")
        self.reset_btn.setEnabled(available)
        if available:
            self.budget.setText(
                f"{status.get('calls_used', NA)} / {status.get('max_calls_per_session', NA)} calls"
                f" · {status.get('calls_remaining', NA)} left"
            )
        else:
            self.budget.setText("")
        self._updating = False
