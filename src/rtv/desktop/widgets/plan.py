"""The live race-plan panel (stage 08): the current plan and its recent revisions.

Paints ``rtv.desktop.model.plan.plan_card`` and ``revision_rows``; it decides nothing.
A missing element reads ``n/a`` with the plan's own reason, never zero.
"""

from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QVBoxLayout, QWidget

from ..model import plan as plan_model
from ..theme import tokens as t
from .common import KeyValue, Panel, clear_layout, label, set_text

TONE = {"good": t.GOOD, "warn": t.SEVERITY["advisory"], "muted": t.MUTED}


class PlanPanel(Panel):
    """Six plan elements with provenance, then the newest revisions (accepted and not)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Race plan", parent)
        self.reason = label("", "muted", wrap=True)
        self.body.addWidget(self.reason)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.values: dict[str, KeyValue] = {}
        self.sources: dict[str, object] = {}
        for i, (key, title) in enumerate(plan_model.LABELS.items()):
            cell = QWidget()
            col = QVBoxLayout(cell)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(0)
            kv = KeyValue(title, "value")
            src = label("", "muted", wrap=True)
            col.addWidget(kv)
            col.addWidget(src)
            self.values[key] = kv
            self.sources[key] = src
            grid.addWidget(cell, i // 3, i % 3)
        self.grid_widget = QWidget()
        self.grid_widget.setLayout(grid)
        self.body.addWidget(self.grid_widget)
        self.body.addWidget(label("REVISIONS", "label"))
        self.revisions = QVBoxLayout()
        self.revisions.setSpacing(4)
        self.body.addLayout(self.revisions)
        self._last: tuple | None = None

    def set_view(self, state: dict | None) -> None:
        card = plan_model.plan_card(state)
        plan = (state or {}).get("plan") if isinstance(state, dict) else None
        rows = plan_model.revision_rows(plan if isinstance(plan, dict) else None, limit=4)
        key = (repr(card), repr(rows))
        if key == self._last:
            return
        self._last = key
        self.card, self.rows = card, rows
        if not card["available"]:
            self.meta.setText("")
            self.reason.setText(card["reason"])
            self.reason.show()
            self.grid_widget.hide()
        else:
            self.meta.setText(f"{card['version']} · {card['stops_done']} stop(s) made")
            self.reason.hide()
            self.grid_widget.show()
            for row in card["rows"]:
                color = None
                if row["key"] == "tyre_posture" and row["grounded"]:
                    color = TONE.get(card["posture_tone"])
                elif not row["grounded"]:
                    color = t.MUTED
                self.values[row["key"]].set(row["value"], color)
                note = row["source_label"] if row["grounded"] else row["note"]
                if row["grounded"] and row["version"] is not None:
                    note = f"{note} · v{row['version']}"
                set_text(self.sources[row["key"]], note)
                self.sources[row["key"]].setToolTip(row["note"])
        clear_layout(self.revisions)
        if not rows:
            self.revisions.addWidget(label("No revisions yet.", "muted"))
        for row in rows:
            color = None if row["accepted"] else t.SEVERITY["advisory"]
            self.revisions.addWidget(label(row["title"], "label", mono=True, color=color))
            line = row["changes"] + (f" · {row['why']}" if row["why"] else "")
            self.revisions.addWidget(label(line, "soft", wrap=True))
