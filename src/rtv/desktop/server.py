"""The RTV server, embedded: uvicorn on a background thread inside the desktop app.

The window is a client of the same FastAPI app the web frontend uses; embedding it
just means one process to start. ``EmbeddedServer.start()`` binds 127.0.0.1 on the
requested port (or a free one) and waits until ``/api/v1/health`` answers.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request


def free_port(preferred: int | None = None, host: str = "127.0.0.1") -> int:
    """``preferred`` if it is free, else any free port."""
    for port in ([preferred] if preferred else []) + [0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise RuntimeError("no free port")


def wait_healthy(base_url: str, timeout: float = 30.0) -> dict:
    """Poll ``/api/v1/health`` until it answers, or raise ``TimeoutError``."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/v1/health", timeout=2) as response:
                return json.loads(response.read() or b"{}")
        except Exception as exc:  # noqa: BLE001 - not up yet
            last = exc
            time.sleep(0.15)
    raise TimeoutError(f"RTV server at {base_url} did not answer: {last}")


class EmbeddedServer:
    def __init__(
        self, port: int | None = None, host: str = "127.0.0.1", log_level: str = "warning"
    ):
        self.host = host
        self.port = free_port(port, host)
        self.log_level = log_level
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 60.0) -> str:
        import uvicorn

        from rtv.main import app  # imported here, after paths.prepare_environment()

        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level=self.log_level,
            workers=1,
            lifespan="on",
            ws="auto",
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # the GUI owns the process
        self._thread = threading.Thread(target=self._server.run, name="rtv-server", daemon=True)
        self._thread.start()
        wait_healthy(self.base_url, timeout)
        return self.base_url

    def stop(self, timeout: float = 10.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
        self._server = None
        self._thread = None
