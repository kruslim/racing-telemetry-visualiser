"""Live telemetry poller.

Runs in a dedicated thread (the shared-memory read is a blocking memcpy that
would otherwise starve the asyncio loop). Reads every variable each tick, decodes
laps, batches to the sink, and pushes the latest frame to a hub for WebSocket
fan-out.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from rtv.catalog.builder import build_catalog
from rtv.catalog.models import Catalog
from rtv.domain.models import (
    ConnectionState,
    Session,
    SessionInfoSnapshot,
    SessionKind,
)
from rtv.ingest.frame import Frame, FrameBuffer
from rtv.ingest.ibt import _ids_from_info
from rtv.ingest.normalize import LapTracker
from rtv.ingest.session import read_live_session_info, subsession_id
from rtv.ingest.sink import TelemetrySink
from rtv.logging import get_logger

log = get_logger("ingest.live")

FrameCb = Callable[[Frame, Catalog], None]
StateCb = Callable[[ConnectionState, str | None], None]

_RECONNECT_BACKOFF = 2.0  # seconds between startup attempts when disconnected


class LivePoller:
    """Owns the single shared-memory feed. Start/stop is idempotent."""

    def __init__(
        self,
        sink: TelemetrySink,
        *,
        poll_hz: int = 60,
        flush_seconds: float = 0.5,
        flatten_max: int = 6,
        on_frame: FrameCb | None = None,
        on_state: StateCb | None = None,
    ) -> None:
        self._sink = sink
        self._poll_hz = poll_hz
        self._flush_seconds = flush_seconds
        self._flatten_max = flatten_max
        self._on_frame = on_frame
        self._on_state = on_state

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = ConnectionState.DISCONNECTED
        self._session_id: str | None = None
        self._lock = threading.Lock()

    # ---- lifecycle ------------------------------------------------------
    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="rtv-live-poller", daemon=True
            )
            self._thread.start()
            log.info("Live poller started (%d Hz).", self._poll_hz)

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            if not self.running:
                return
            self._stop.set()
            thread = self._thread
        if thread:
            thread.join(timeout=timeout)
        self._set_state(ConnectionState.DISCONNECTED, None)
        log.info("Live poller stopped.")

    # ---- main loop ------------------------------------------------------
    def _run(self) -> None:
        try:
            import irsdk
        except Exception as exc:  # pragma: no cover
            log.error("pyirsdk unavailable: %s", exc)
            self._set_state(ConnectionState.DISCONNECTED, None)
            return

        ir = irsdk.IRSDK()
        period = 1.0 / self._poll_hz
        catalog: Catalog | None = None
        tracker: LapTracker | None = None
        buf: FrameBuffer | None = None
        last_flush = time.perf_counter()
        last_info_update = -1
        current_subsession: int | None = None

        while not self._stop.is_set():
            if not (ir.is_initialized and ir.is_connected):
                self._set_state(ConnectionState.DISCONNECTED, None)
                try:
                    ir.startup()
                except Exception as exc:  # pragma: no cover
                    log.debug("startup failed: %s", exc)
                if not (ir.is_initialized and ir.is_connected):
                    catalog = None
                    self._stop.wait(_RECONNECT_BACKOFF)
                    continue

            loop_start = time.perf_counter()

            # (Re)build catalog / open a session on (re)connect or subsession change.
            info_update = getattr(ir, "session_info_update", 0)
            if catalog is None or info_update != last_info_update:
                info = read_live_session_info(ir)
                sub = subsession_id(info)
                if catalog is None or (sub is not None and sub != current_subsession):
                    catalog, tracker, buf = self._open_session(ir, info)
                    current_subsession = sub
                else:
                    self._write_info(info, ir, info_update)
                last_info_update = info_update

            assert catalog is not None and tracker is not None and buf is not None

            frame = self._read_tick(ir, catalog, tracker)
            if frame is not None:
                buf.append(
                    frame.values,
                    tick=frame.tick,
                    session_time=frame.session_time,
                    lap=frame.lap,
                    wall_time=frame.wall_time,
                )
                if self._on_frame:
                    self._on_frame(frame, catalog)

            now = time.perf_counter()
            if buf is not None and len(buf) and (now - last_flush) >= self._flush_seconds:
                self._flush(buf)
                last_flush = now

            # Pace the loop to the target period (skip sleep if behind).
            elapsed = time.perf_counter() - loop_start
            if elapsed < period:
                self._stop.wait(period - elapsed)

        # Shutdown: flush remainder and finalise.
        if buf is not None and len(buf):
            self._flush(buf)
        if self._session_id and tracker is not None:
            tracker.finalize(tick=0, session_time=0.0)
            self._sink.write_laps(self._session_id, tracker.laps)
            self._sink.end_session(
                self._session_id, sample_count=0, ended_at=time.time()
            )
        try:
            ir.shutdown()
        except Exception:  # pragma: no cover
            pass

    # ---- helpers --------------------------------------------------------
    def _open_session(self, ir, info: dict[str, Any]):
        # Finalise any previous session.
        if self._session_id:
            self._sink.end_session(
                self._session_id, sample_count=0, ended_at=time.time()
            )

        session_id = str(uuid.uuid4())
        catalog = build_catalog(
            ir._var_headers, source="live", flatten_max=self._flatten_max
        )
        car_id, track_id, track_name, ir_sid, ir_subsid = _ids_from_info(info)
        catalog.car_id = car_id
        catalog.track_id = track_id

        session = Session(
            session_id=session_id,
            kind=SessionKind.LIVE,
            car_id=car_id,
            track_id=track_id,
            track_name=track_name,
            ir_session_id=ir_sid,
            ir_subsession_id=ir_subsid,
            schema_hash=catalog.schema_hash,
            started_at=time.time(),
            poll_hz=self._poll_hz,
            status="open",
        )
        self._sink.begin_session(session, catalog)
        if info:
            self._sink.write_session_info(
                SessionInfoSnapshot(
                    session_id=session_id,
                    update_seq=getattr(ir, "session_info_update", 0),
                    captured_tick=0,
                    captured_at=time.time(),
                    info=info,
                )
            )
        self._session_id = session_id
        self._set_state(ConnectionState.IN_SESSION, session_id)
        log.info("Opened live session %s (%d variables).", session_id[:8], len(catalog))
        return catalog, LapTracker(session_id), FrameBuffer(catalog)

    def _write_info(self, info: dict[str, Any], ir, seq: int) -> None:
        if self._session_id and info:
            self._sink.write_session_info(
                SessionInfoSnapshot(
                    session_id=self._session_id,
                    update_seq=seq,
                    captured_tick=0,
                    captured_at=time.time(),
                    info=info,
                )
            )

    def _read_tick(self, ir, catalog: Catalog, tracker: LapTracker) -> Frame | None:
        try:
            ir.freeze_var_buffer_latest()
        except Exception:  # pragma: no cover
            return None
        try:
            values: dict[str, Any] = {}
            for name in catalog.names:
                values[name] = ir[name]
        except Exception:  # pragma: no cover
            return None
        finally:
            try:
                ir.unfreeze_var_buffer_latest()
            except Exception:  # pragma: no cover
                pass

        session_time = float(values.get("SessionTime") or 0.0)
        tick = int(values.get("SessionTick") or 0)
        completed = tracker.update(values, tick=tick, session_time=session_time)
        lap = completed.lap if completed else (tracker._current.lap if tracker._current else 0)
        if completed and self._session_id:
            self._sink.write_laps(self._session_id, [completed])
        return Frame(
            tick=tick,
            session_time=session_time,
            lap=lap,
            values=values,
            wall_time=time.time(),
        )

    def _flush(self, buf: FrameBuffer) -> None:
        if self._session_id is None or not len(buf):
            return
        try:
            self._sink.write_telemetry(self._session_id, buf.to_arrow())
        except Exception as exc:  # pragma: no cover
            log.error("telemetry flush failed: %s", exc)
        finally:
            buf.clear()

    def _set_state(self, state: ConnectionState, session_id: str | None) -> None:
        if state != self._state:
            self._state = state
            if self._on_state:
                self._on_state(state, session_id)
