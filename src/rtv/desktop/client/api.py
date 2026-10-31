"""The REST client: QNetworkAccessManager on the Qt event loop, JSON in and out.

Every call takes ``ok(data)`` and ``err(status, detail)`` callbacks. Errors carry
the server's FastAPI ``{"detail": ...}`` text, which the window shows as is (the
brief's notice bar). A ``key`` aborts the previous in-flight call with the same
key, so a scrub that fires twenty state requests only waits for the last one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from PySide6.QtCore import QByteArray, QFile, QIODevice, QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

API = "/api/v1"

Ok = Callable[[Any], None]
Err = Callable[[int, str], None]


def detail_of(status: int, body: bytes) -> str:
    """The server's ``detail`` text, or something honest when there is none."""
    try:
        data = json.loads(body or b"null")
    except ValueError:
        data = None
    if isinstance(data, dict) and "detail" in data:
        detail = data["detail"]
        if isinstance(detail, list):  # pydantic validation errors
            return "; ".join(str(d.get("msg", d)) for d in detail if isinstance(d, dict)) or str(
                detail
            )
        return str(detail)
    if status == 0:
        return "the RTV server is not reachable"
    return f"HTTP {status}"


class Api(QObject):
    """A thin JSON client for one RTV server."""

    #: Emitted for every failed call: (path, status, detail).
    failed = Signal(str, int, str)

    def __init__(self, base_url: str, parent: QObject | None = None):
        super().__init__(parent)
        self.base_url = base_url.rstrip("/")
        self._net = QNetworkAccessManager(self)
        self._keyed: dict[str, QNetworkReply] = {}

    # ---- urls ----------------------------------------------------------
    def url(self, path: str, params: dict | None = None) -> QUrl:
        query = ""
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                query = "?" + urlencode(clean)
        prefix = "" if path.startswith(("/ws", "/docs")) else API
        return QUrl(f"{self.base_url}{prefix}{path}{query}")

    def ws_url(self, path: str) -> QUrl:
        base = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        return QUrl(f"{base}{path}")

    # ---- calls ---------------------------------------------------------
    def get(
        self,
        path: str,
        params: dict | None = None,
        ok: Ok | None = None,
        err: Err | None = None,
        key: str | None = None,
    ) -> QNetworkReply:
        request = QNetworkRequest(self.url(path, params))
        return self._track(self._net.get(request), path, ok, err, key)

    def post(
        self,
        path: str,
        body: Any = None,
        params: dict | None = None,
        ok: Ok | None = None,
        err: Err | None = None,
        key: str | None = None,
    ) -> QNetworkReply:
        request = QNetworkRequest(self.url(path, params))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        payload = QByteArray(json.dumps(body if body is not None else {}).encode())
        return self._track(self._net.post(request, payload), path, ok, err, key)

    def put(
        self,
        path: str,
        body: Any = None,
        params: dict | None = None,
        ok: Ok | None = None,
        err: Err | None = None,
        key: str | None = None,
    ) -> QNetworkReply:
        request = QNetworkRequest(self.url(path, params))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        payload = QByteArray(json.dumps(body if body is not None else {}).encode())
        return self._track(self._net.put(request, payload), path, ok, err, key)

    def get_bytes(
        self,
        path: str,
        params: dict | None = None,
        ok: Callable[[bytes], None] | None = None,
        err: Err | None = None,
        key: str | None = None,
    ) -> QNetworkReply:
        """GET a binary body (a JPEG frame); ``ok`` gets the raw bytes."""
        request = QNetworkRequest(self.url(path, params))
        return self._track(self._net.get(request), path, ok, err, key, raw=True)

    def upload(
        self,
        path: str,
        file_path: str,
        params: dict | None = None,
        ok: Ok | None = None,
        err: Err | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> QNetworkReply | None:
        """POST a file's raw bytes, streamed from disk (``.ibt`` files run 100-200 MB)."""
        source = QFile(file_path)
        if not source.open(QIODevice.OpenModeFlag.ReadOnly):
            if err:
                err(0, f"cannot read {file_path}")
            return None
        request = QNetworkRequest(self.url(path, params))
        request.setHeader(
            QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/octet-stream"
        )
        reply = self._net.post(request, source)
        source.setParent(reply)  # lives exactly as long as the upload
        if progress:
            reply.uploadProgress.connect(progress)
        return self._track(reply, path, ok, err, None)

    def _track(
        self,
        reply: QNetworkReply,
        path: str,
        ok: Ok | None,
        err: Err | None,
        key: str | None,
        *,
        raw: bool = False,
    ) -> QNetworkReply:
        if key:
            previous = self._keyed.get(key)
            if previous is not None:
                previous.setProperty("rtv_aborted", True)
                previous.abort()
            self._keyed[key] = reply

        def finished() -> None:
            if key and self._keyed.get(key) is reply:
                del self._keyed[key]
            aborted = bool(reply.property("rtv_aborted"))
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
            body = bytes(reply.readAll().data())
            reply.deleteLater()
            if aborted:
                return
            if 200 <= int(status) < 300:
                if raw:
                    if ok:
                        ok(body)
                    return
                try:
                    data = json.loads(body) if body else None
                except ValueError:
                    data = None
                if ok:
                    ok(data)
                return
            detail = detail_of(int(status), body)
            if reply.error() != QNetworkReply.NetworkError.NoError and int(status) == 0:
                detail = detail_of(0, b"")
            self.failed.emit(path, int(status), detail)
            if err:
                err(int(status), detail)

        reply.finished.connect(finished)
        return reply
