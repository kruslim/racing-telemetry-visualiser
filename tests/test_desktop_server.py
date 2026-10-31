"""RTV Desktop end to end, offline: the embedded server, the window, an empty store.

Boots ``EmbeddedServer`` on a free port with a fresh data directory, opens the main
window on it, waits for the pitwall socket and the first polls, and checks the
brief's empty-store and agents-off states before shutting everything down.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("duckdb")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel  # noqa: E402


def _wait(app, predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_paths_point_a_frozen_build_at_local_appdata(monkeypatch, tmp_path):
    from rtv.desktop import paths

    monkeypatch.delenv("RTV_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("sys.frozen", True, raising=False)
    paths.prepare_environment()
    assert os.environ["RTV_DATA_DIR"] == str(tmp_path / "RTV" / "data")
    assert (tmp_path / "RTV" / "data").is_dir()


def test_a_windowed_exe_without_streams_logs_to_the_data_dir(monkeypatch, tmp_path):
    import sys

    from rtv.desktop import paths

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    paths._ensure_streams(tmp_path)
    try:
        assert sys.stderr is not None and sys.stdout is sys.stderr
        print("server log line", file=sys.stderr)
    finally:
        sys.stderr.close()
    assert "server log line" in (tmp_path / "rtv-desktop.log").read_text(encoding="utf-8")


def test_args_parse():
    from rtv.desktop.__main__ import parse_args

    args = parse_args(["--server", "http://127.0.0.1:8000", "--mute"])
    assert args.server == "http://127.0.0.1:8000" and args.mute


def test_embedded_server_and_window_on_an_empty_store(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("RTV_PITWALL_AGENTS", raising=False)
    from rtv.config import get_settings

    get_settings.cache_clear()
    app = qapp
    from rtv.desktop.client.api import Api
    from rtv.desktop.server import EmbeddedServer
    from rtv.desktop.views.hub import Hub
    from rtv.desktop.views.main_window import MainWindow

    server = EmbeddedServer()
    base = server.start(timeout=60)
    hub = Hub(Api(base), speak=False)
    window = MainWindow(hub, embedded=True, reduced_motion=True, server_note=base)
    try:
        window.resize(1366, 768)
        window.show()
        hub.start()
        assert _wait(app, lambda: hub.feed_state == "open"), hub.feed_state
        assert _wait(app, lambda: hub.status is not None and hub.replay is not None)
        assert _wait(app, lambda: hub.sessions == [] and not window.review.empty.isHidden())
        assert hub.status["available"] is False  # agents off by default: a state
        app.processEvents()
        window.live._render()
        shown = [lab.text() for lab in window.live.findChildren(QLabel)]
        assert "AI OFF" in shown
        assert "waiting for telemetry" in shown
        assert not window.live.replay.start.isEnabled()  # nothing to replay yet
        window.grab()
    finally:
        hub.stop()
        window.close()
        window.deleteLater()
        hub.deleteLater()
        app.processEvents()
        server.stop()
        get_settings.cache_clear()
