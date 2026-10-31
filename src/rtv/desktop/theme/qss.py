"""The dark Qt stylesheet, built from the tokens.

Widgets opt into roles with ``setObjectName`` / the ``role`` property rather than
inline styles, so the whole look lives here and in ``tokens.py``.
"""

from __future__ import annotations

from . import tokens as t


def stylesheet() -> str:
    return f"""
* {{
    color: {t.INK};
    font-size: {t.SIZE_BODY}px;
}}
QMainWindow, QDialog, QWidget#root {{ background: {t.BG}; }}
QWidget#chrome {{ background: {t.CHROME}; border-bottom: 1px solid {t.LINE}; }}
QWidget#chromeTop {{ background: {t.CHROME}; border-top: 1px solid {t.LINE}; }}
QFrame#panel {{
    background: {t.PANEL};
    border: 1px solid {t.LINE};
    border-radius: {t.RADIUS_PANEL}px;
}}
QFrame#card {{ background: {t.PANEL_2}; border-radius: {t.RADIUS_PANEL}px; }}
QFrame#inset {{
    background: {t.CHROME};
    border: 1px solid {t.LINE};
    border-radius: {t.RADIUS_PANEL}px;
}}
QWidget#panelHeader {{ border-bottom: 1px solid {t.LINE}; }}
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-weight: 600; font-size: 13px; }}
QLabel[role="label"] {{
    color: {t.MUTED}; font-size: {t.SIZE_LABEL}px; font-weight: 600; letter-spacing: 1px;
}}
QLabel[role="muted"] {{ color: {t.MUTED}; font-size: 12px; }}
QLabel[role="soft"] {{ color: {t.SOFT}; }}
QLabel[role="hero"] {{ font-size: {t.SIZE_HERO}px; font-weight: 800; }}
QLabel[role="value"] {{ font-size: {t.SIZE_VALUE}px; font-weight: 700; }}
QLabel[role="big"] {{ font-size: 16px; font-weight: 600; }}
QLabel[role="radio"] {{ font-size: {t.SIZE_RADIO}px; }}
QLabel[role="empty"] {{
    color: {t.SOFT}; background: {t.CHROME}; border-radius: 6px; padding: 10px 12px;
}}
QLabel[role="tag"] {{
    font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px;
}}
QLabel[role="pill"] {{
    font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 12px;
}}
QPushButton, QToolButton {{
    min-height: {t.HIT}px;
    padding: 0 12px;
    border: 1px solid {t.LINE_2};
    border-radius: {t.RADIUS_CONTROL}px;
    background: {t.PANEL_2};
}}
QPushButton:hover, QToolButton:hover {{ border-color: #4a5563; }}
QPushButton:focus, QToolButton:focus, QComboBox:focus, QLineEdit:focus, QSlider:focus {{
    border: 2px solid {t.ACCENT};
}}
QPushButton:checked, QToolButton:checked {{ background: {t.SELECT}; color: #ffffff; }}
QPushButton:disabled, QToolButton:disabled, QComboBox:disabled {{ color: #5c6672; }}
QPushButton[role="primary"] {{ background: #1d3a55; border-color: #2f5f8a; }}
QPushButton[role="danger"] {{ background: #2a1414; border-color: #5a2a2a; color: #ffb3ae; }}
QPushButton[role="spend"] {{ background: #5a3a0f; border-color: #8a5a14; color: #ffe3a3; }}
QPushButton[role="link"] {{
    border: none; background: transparent; color: {t.ACCENT}; padding: 0 4px;
    min-height: 24px;
}}
QComboBox, QLineEdit {{
    min-height: {t.HIT}px;
    padding: 0 8px;
    border: 1px solid {t.LINE_2};
    border-radius: {t.RADIUS_CONTROL}px;
    background: {t.PANEL_2};
}}
QComboBox QAbstractItemView {{
    background: {t.PANEL_2}; selection-background-color: {t.SELECT};
    border: 1px solid {t.LINE_2};
}}
QCheckBox {{ spacing: 6px; }}
QProgressBar {{
    border: none; background: #1c222a; border-radius: 3px; max-height: 6px;
}}
QProgressBar::chunk {{ background: {t.ACCENT}; border-radius: 3px; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {t.LINE_2}; border-radius: 5px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {t.LINE_2}; border-radius: 5px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QTableWidget {{
    background: transparent; border: 1px solid {t.LINE}; border-radius: 6px;
    gridline-color: {t.ROW_LINE}; selection-background-color: #12243a;
}}
QHeaderView::section {{
    background: {t.PANEL}; color: {t.MUTED}; border: none;
    border-bottom: 1px solid {t.LINE}; padding: 6px 8px; font-size: 11px; font-weight: 600;
}}
QTableCornerButton::section {{ background: {t.PANEL}; border: none; }}
QToolTip {{ background: {t.PANEL_2}; color: {t.INK}; border: 1px solid {t.LINE_2}; }}
QSplitter::handle {{ background: {t.BG}; }}
QTabBar::tab {{
    min-height: {t.HIT}px; padding: 0 14px; background: {t.PANEL_2};
    border: 1px solid {t.LINE_2}; color: {t.SOFT};
}}
QTabBar::tab:selected {{ background: {t.SELECT}; color: #ffffff; }}
QSlider::groove:horizontal {{ height: 4px; background: {t.LINE_2}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 14px; margin: -6px 0; border-radius: 7px; background: {t.ACCENT};
}}
"""
