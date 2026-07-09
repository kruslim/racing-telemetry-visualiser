"""LiveHub — thread-safe holder of the latest live frame and connection state.

The poller thread pushes here; WebSocket sender tasks read the latest snapshot at
their own rate (coalescing / drop-oldest). No per-client queue ever grows.
"""

from __future__ import annotations

import threading

from rtv.catalog.models import Catalog
from rtv.domain.models import ConnectionState
from rtv.ingest.frame import Frame


class LiveHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._catalog: Catalog | None = None
        self._state = ConnectionState.DISCONNECTED
        self._session_id: str | None = None
        # Monotonic counter bumped on every state change so clients notice.
        self._state_seq = 0

    # ---- producer side (poller thread) ---------------------------------
    def publish_frame(self, frame: Frame, catalog: Catalog) -> None:
        with self._lock:
            self._latest = frame
            self._catalog = catalog

    def publish_state(self, state: ConnectionState, session_id: str | None) -> None:
        with self._lock:
            self._state = state
            self._session_id = session_id
            self._state_seq += 1

    # ---- consumer side (WS tasks) --------------------------------------
    def snapshot(self) -> tuple[Frame | None, Catalog | None]:
        with self._lock:
            return self._latest, self._catalog

    def state(self) -> tuple[ConnectionState, str | None, int]:
        with self._lock:
            return self._state, self._session_id, self._state_seq

    @property
    def catalog(self) -> Catalog | None:
        with self._lock:
            return self._catalog
