"""``rtv-desktop``: start the embedded server (or attach to one) and open the window.

rtv-desktop                      embedded server on 127.0.0.1, free port
rtv-desktop --port 8000          embedded, on 8000 if free
rtv-desktop --server http://127.0.0.1:8000   attach to a running ``run.ps1`` server
rtv-desktop --data-dir D:\\rtv-data          keep the store somewhere else
"""

from __future__ import annotations

import argparse
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rtv-desktop", description=__doc__.splitlines()[0])
    parser.add_argument("--server", help="attach to an RTV server instead of embedding one")
    parser.add_argument("--port", type=int, default=None, help="port for the embedded server")
    parser.add_argument("--data-dir", default=None, help="data directory (RTV_DATA_DIR)")
    parser.add_argument(
        "--reduced-motion",
        action="store_true",
        default=None,
        help="no flag pulse or moving packets (default: the system setting)",
    )
    parser.add_argument("--mute", action="store_true", help="start with the radio muted")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from . import paths

    paths.prepare_environment(args.data_dir)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QMessageBox

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv[:1])
    app.setApplicationName("RTV")
    app.setOrganizationName("RTV")
    from pathlib import Path

    from PySide6.QtGui import QIcon

    app.setWindowIcon(QIcon(str(Path(__file__).with_name("rtv.png"))))
    from .theme import tokens
    from .theme.qss import stylesheet
    from .widgets.common import reduced_motion_default

    font = QFont()
    font.setFamilies(tokens.SANS_FAMILIES)
    font.setPixelSize(tokens.SIZE_BODY)
    app.setFont(font)
    app.setStyle("Fusion")
    app.setStyleSheet(stylesheet())

    embedded = None
    if args.server:
        base_url = args.server.rstrip("/")
        note = f"attached \u00b7 {base_url}"
    else:
        from .server import EmbeddedServer

        embedded = EmbeddedServer(args.port)
        try:
            base_url = embedded.start()
        except Exception as exc:  # noqa: BLE001 - show it, don't crash silently
            QMessageBox.critical(
                None,
                "RTV",
                f"The RTV server did not start:\n\n{exc}\n\n"
                "Details are in rtv-desktop.log in the data folder.",
            )
            return 1
        note = f"embedded \u00b7 {base_url}"

    from .client.api import Api
    from .views.hub import Hub
    from .views.main_window import MainWindow

    api = Api(base_url)
    hub = Hub(api)
    reduced = reduced_motion_default() if args.reduced_motion is None else args.reduced_motion
    window = MainWindow(
        hub, embedded=embedded is not None, reduced_motion=reduced, server_note=note
    )
    if args.mute:
        hub.radio.set_master_muted(True)
        window.live.radio.master.setChecked(False)
    window.show()
    hub.start()
    code = app.exec()
    hub.stop()
    if embedded is not None:
        embedded.stop()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
