"""The main window: app bar, the Live / Review switch, and the pages behind it."""

from __future__ import annotations

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..theme import tokens as t
from ..widgets.common import label
from ..widgets.dialogs import AiControls
from .hub import Hub
from .live_view import LiveView
from .radio_window import RadioWindow
from .review_view import ReviewView


class MainWindow(QMainWindow):
    def __init__(
        self,
        hub: Hub,
        *,
        embedded: bool = True,
        reduced_motion: bool = False,
        server_note: str = "",
    ):
        super().__init__()
        self.hub = hub
        self.setWindowTitle("RTV — Racing Telemetry Visualiser")
        self.resize(1680, 1000)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("chrome")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 6, 16, 6)
        bl.setSpacing(12)
        bl.addWidget(label("↗ RTV", color=t.ACCENT, bold=True, size=15))
        bl.addWidget(label("Racing Telemetry Visualiser", "muted"))
        self.tabs = QButtonGroup(self)
        self.live_tab = QPushButton("Live pitwall")
        self.review_tab = QPushButton("Post-race review && lap analysis")
        for i, button in enumerate((self.live_tab, self.review_tab)):
            button.setCheckable(True)
            self.tabs.addButton(button, i)
            bl.addWidget(button)
        self.live_tab.setChecked(True)
        bl.addStretch(1)
        self.server = label(server_note, "muted", mono=True)
        bl.addWidget(self.server)
        radio = QPushButton("Radio window")
        controls = QPushButton("AI controls")
        docs = QPushButton("API docs")
        docs.setProperty("role", "link")
        for b in (radio, controls, docs):
            bl.addWidget(b)
        layout.addWidget(bar)

        self.pages = QStackedWidget()
        self.live = LiveView(hub, reduced_motion=reduced_motion)
        self.review = ReviewView(hub, embedded=embedded)
        self.pages.addWidget(self.live)
        self.pages.addWidget(self.review)
        layout.addWidget(self.pages, 1)

        self.tabs.idClicked.connect(self.pages.setCurrentIndex)
        radio.clicked.connect(self.open_radio)
        controls.clicked.connect(self.open_controls)
        docs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(hub.api.base_url + "/docs")))
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self._page(0))
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self._page(1))
        QShortcut(QKeySequence("Ctrl+R"), self, self.open_radio)
        self.radio_window: RadioWindow | None = None
        self.controls: AiControls | None = None
        hub.status_changed.connect(self._status)

        settings = QSettings("RTV", "Desktop")
        geometry = settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _page(self, index: int) -> None:
        self.tabs.button(index).setChecked(True)
        self.pages.setCurrentIndex(index)

    def open_radio(self) -> None:
        if self.radio_window is None:
            self.radio_window = RadioWindow(self.hub)
        self.radio_window.show()
        self.radio_window.raise_()
        self.radio_window.activateWindow()

    def open_controls(self) -> None:
        if self.controls is None:
            self.controls = AiControls(self)
            self.controls.set_enabled.connect(self.hub.set_enabled)
            self.controls.set_agent.connect(self.hub.set_agent)
            self.controls.reset.connect(self.hub.reset_agents)
        self.controls.set_status(self.hub.status)
        self.controls.show()
        self.controls.raise_()

    def _status(self) -> None:
        if self.controls is not None and self.controls.isVisible():
            self.controls.set_status(self.hub.status)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        QSettings("RTV", "Desktop").setValue("geometry", self.saveGeometry())
        if self.radio_window is not None:
            self.radio_window.close()
        self.hub.stop()
        super().closeEvent(event)
        QApplication.quit()
