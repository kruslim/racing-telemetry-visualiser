"""Chunked importer for recorded ``.ibt`` telemetry files.

Reads samples in tick-window chunks straight off the memory-mapped file so
memory stays bounded regardless of file size, normalises laps, and streams
columnar tables to a :class:`~rtv.ingest.sink.TelemetrySink`.
"""

from __future__ import annotations

import struct
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from rtv.catalog.builder import build_catalog
from rtv.catalog.types import mapping_for
from rtv.domain.models import Session, SessionInfoSnapshot, SessionKind
from rtv.ingest.frame import FrameBuffer
from rtv.ingest.normalize import LapTracker
from rtv.ingest.sink import TelemetrySink
from rtv.logging import get_logger

log = get_logger("ingest.ibt")

ProgressCb = Callable[[float], None]


def read_ibt_session_info(ibt) -> dict[str, Any]:
    """Read and parse the session-info YAML embedded in an .ibt file.

    The pyirsdk IBT class does not parse session info, but the bytes live in the
    file header. We read them directly and parse leniently.
    """
    try:
        header = ibt._header
        mem = ibt._shared_mem
        offset = header.session_info_offset
        length = header.session_info_len
        raw = bytes(mem[offset : offset + length])
        text = raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace")
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not parse .ibt session info: %s", exc)
        return {}


def _window_columns(ibt, names: list[str], start: int, count: int) -> dict[str, list]:
    """Read `count` records starting at `start` for the given variables.

    Mirrors IBT.get_all but bounded to a record window, using a single
    struct.unpack_from per (variable, record) over the mmap.
    """
    header = ibt._header
    mem = ibt._shared_mem
    buf_offset = header.var_buf[0].buf_offset
    buf_len = header.buf_len
    headers = ibt._var_headers_dict

    out: dict[str, list] = {}
    from rtv.catalog.types import IRType  # local import; cheap

    for name in names:
        vh = headers[name]
        fmt = mapping_for(IRType(vh.type)).struct_char * vh.count
        is_array = vh.count > 1
        base = vh.offset + buf_offset
        col: list[Any] = []
        for i in range(start, start + count):
            res = struct.unpack_from(fmt, mem, base + i * buf_len)
            col.append(list(res) if is_array else res[0])
        out[name] = col
    return out


def import_ibt(
    path: str | Path,
    sink: TelemetrySink,
    *,
    flatten_max: int = 6,
    chunk_rows: int = 10_000,
    session_id: str | None = None,
    progress: ProgressCb | None = None,
    label: str | None = None,
) -> str:
    """Import an .ibt file into the sink. Returns the created session_id."""
    import irsdk

    path = Path(path)
    session_id = session_id or str(uuid.uuid4())
    ibt = irsdk.IBT()
    ibt.open(str(path))
    try:
        catalog = build_catalog(
            ibt._var_headers, source="ibt", flatten_max=flatten_max
        )
        info = read_ibt_session_info(ibt)
        car_id, track_id, track_name, ir_sid, ir_subsid = _ids_from_info(info)

        catalog.car_id = car_id
        catalog.track_id = track_id

        total = int(ibt._disk_header.session_record_count)
        now = time.time()
        session = Session(
            session_id=session_id,
            kind=SessionKind.IBT,
            source_path=str(path),
            car_id=car_id,
            track_id=track_id,
            track_name=track_name,
            ir_session_id=ir_sid,
            ir_subsession_id=ir_subsid,
            schema_hash=catalog.schema_hash,
            started_at=now,
            sample_count=total,
            status="open",
            label=label,
        )
        sink.begin_session(session, catalog)
        if info:
            sink.write_session_info(
                SessionInfoSnapshot(
                    session_id=session_id,
                    update_seq=0,
                    captured_tick=0,
                    captured_at=now,
                    info=info,
                )
            )

        tracker = LapTracker(session_id)
        names = catalog.names
        has_session_time = "SessionTime" in catalog
        has_tick = "SessionTick" in catalog

        processed = 0
        for start in range(0, total, chunk_rows):
            count = min(chunk_rows, total - start)
            cols = _window_columns(ibt, names, start, count)
            buf = FrameBuffer(catalog)
            for r in range(count):
                idx = start + r
                values = {name: cols[name][r] for name in names}
                session_time = (
                    float(values["SessionTime"]) if has_session_time else float(idx)
                )
                tick = int(values["SessionTick"]) if has_tick else idx
                completed = tracker.update(
                    values, tick=tick, session_time=session_time
                )
                lap = completed.lap if completed else tracker_current_lap(tracker)
                buf.append(
                    values,
                    tick=tick,
                    session_time=session_time,
                    lap=lap,
                )
            sink.write_telemetry(session_id, buf.to_arrow())
            processed += count
            if progress:
                progress(processed / total if total else 1.0)

        tracker.finalize(tick=total - 1, session_time=float(total - 1))
        sink.write_laps(session_id, tracker.laps)
        sink.end_session(session_id, sample_count=total, ended_at=time.time())
        log.info(
            "Imported %s: %d samples, %d laps, %d variables",
            path.name,
            total,
            len(tracker.laps),
            len(catalog),
        )
        return session_id
    finally:
        ibt.close()


def tracker_current_lap(tracker: LapTracker) -> int:
    return tracker._current.lap if tracker._current else 0


def _ids_from_info(info: dict[str, Any]):
    """Extract car/track identifiers from a parsed session-info dict."""
    weekend = info.get("WeekendInfo", {}) if isinstance(info, dict) else {}
    track_id = _str_or_none(weekend.get("TrackID"))
    track_name = weekend.get("TrackDisplayName") or weekend.get("TrackName")
    ir_sid = _int_or_none(weekend.get("SessionID"))
    ir_subsid = _int_or_none(weekend.get("SubSessionID"))

    car_id = None
    driver_info = info.get("DriverInfo", {}) if isinstance(info, dict) else {}
    drivers = driver_info.get("Drivers")
    pcar_idx = driver_info.get("DriverCarIdx")
    if isinstance(drivers, list) and isinstance(pcar_idx, int):
        for d in drivers:
            if d.get("CarIdx") == pcar_idx:
                car_id = _str_or_none(d.get("CarID")) or d.get("CarScreenName")
                break
    return car_id, track_id, str(track_name) if track_name else None, ir_sid, ir_subsid


def _str_or_none(v: Any) -> str | None:
    return str(v) if v is not None else None


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
