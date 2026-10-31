"""Cut a lap range of a stored session into a small, self-describing test fixture.

A real live recording is the only source of ``CarIdx*`` field data (``.ibt``
files carry none), and a whole race is far too big to commit. :func:`trim_session`
copies a few laps of it, every column including the 64-wide per-car arrays, plus
the race-journal records from the same stretch of session time, into a
directory::

    <out>/fixture.json          session row, laps, catalog, session info, bounds
    <out>/frames.parquet        every stored column of the chosen laps, by tick
    <out>/journal/<run>.jsonl   journal records inside the same session-time span

:func:`seed_fixture` puts such a directory back into a store through the
production writer, so a test replays it with :func:`rtv.racestate.replay.store_source`
or serves it over the API exactly like the original session. Nothing is
generated: a fixture is a verbatim slice of real data.

``scripts/trim_fixture.py`` is the command-line front end.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rtv.catalog.builder import RawHeader, build_catalog
from rtv.domain.models import Lap, Session, SessionInfoSnapshot, SessionKind
from rtv.ingest.frame import META_COLUMNS
from rtv.store import journal

FIXTURE_FILE = "fixture.json"
FRAMES_FILE = "frames.parquet"
JOURNAL_DIR = "journal"
FORMAT_VERSION = 1

#: Channels a fixture always keeps, whatever ``channels`` says: the replay and
#: the lap tracker cannot work without them.
ALWAYS_KEEP = ("SessionTime", "SessionTick", "Lap", "LapDistPct")


@dataclass(frozen=True)
class TrimResult:
    out_dir: Path
    session_id: str
    laps: list[int]
    frames: int
    channels: int
    journal_records: dict[str, int]
    session_time: tuple[float, float]

    def to_api(self) -> dict[str, Any]:
        return {
            "out_dir": str(self.out_dir),
            "session_id": self.session_id,
            "laps": self.laps,
            "frames": self.frames,
            "channels": self.channels,
            "journal_records": self.journal_records,
            "session_time": list(self.session_time),
        }


def parse_laps(spec: str) -> tuple[int, int]:
    """``"4"`` -> (4, 4); ``"3-5"`` -> (3, 5)."""
    text = spec.strip()
    try:
        if "-" in text:
            lo, hi = (int(p) for p in text.split("-", 1))
        else:
            lo = hi = int(text)
    except ValueError as exc:
        raise ValueError(f"Lap range must be N or N-M, got {spec!r}") from exc
    if lo < 0 or hi < lo:
        raise ValueError(f"Lap range must be N or N-M with N <= M, got {spec!r}")
    return lo, hi


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def _keep(name: str, patterns: list[str] | None) -> bool:
    if patterns is None or name in ALWAYS_KEEP:
        return True
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def trim_session(
    db,
    parquet_dir: Path,
    session_id: str,
    laps: tuple[int, int],
    out_dir: Path,
    *,
    journal_root: Path | None = None,
    channels: list[str] | None = None,
    overwrite: bool = False,
) -> TrimResult:
    """Copy laps ``laps[0]..laps[1]`` of ``session_id`` into ``out_dir``.

    ``channels`` is an optional list of glob patterns (``["CarIdx*", "Speed"]``)
    to keep; the default keeps every column. ``journal_root`` is the
    ``data/sessions`` directory; without it no journal records are copied.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    session = db.query_one("SELECT * FROM sessions WHERE session_id = ?", [session_id])
    if session is None:
        raise KeyError(f"No stored session {session_id!r}")
    lap_rows = db.query_dicts(
        "SELECT lap, start_tick, end_tick, start_session_time, lap_time, is_valid, "
        "is_out_lap, is_in_lap FROM laps WHERE session_id = ? AND lap BETWEEN ? AND ? "
        "ORDER BY lap",
        [session_id, laps[0], laps[1]],
    )
    session_dir = Path(parquet_dir) / f"session_id={session_id}"
    files = sorted(
        p
        for lap in range(laps[0], laps[1] + 1)
        for p in (session_dir / f"lap={lap}").glob("*.parquet")
    )
    if not files:
        raise KeyError(f"No telemetry for session {session_id!r} laps {laps[0]}-{laps[1]}")

    catalog_rows = [
        r
        for r in db.query_dicts(
            "SELECT name, ir_type, type_label, count, unit, descr, storage, decoder, columns "
            "FROM variable_catalog WHERE session_id = ? ORDER BY name",
            [session_id],
        )
        if _keep(r["name"], channels)
    ]
    keep_cols = list(META_COLUMNS)
    for r in catalog_rows:
        keep_cols.extend(json.loads(r["columns"]) if r["columns"] else [r["name"]])

    # pyarrow, not DuckDB: DuckDB folds the meta ``lap`` and the ``Lap`` channel
    # into one case-insensitive name, and a fixture must keep both verbatim.
    tables = [pq.read_table(p) for p in files]
    table = pa.concat_tables(tables, promote_options="default")
    table = table.select([c for c in keep_cols if c in table.column_names])
    table = table.take(pc.sort_indices(table, sort_keys=[("tick", "ascending")]))
    times = table.column("session_time")
    t0, t1 = float(pc.min(times).as_py()), float(pc.max(times).as_py())
    tick0, tick1 = (
        int(pc.min(table.column("tick")).as_py()),
        int(pc.max(table.column("tick")).as_py()),
    )

    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        if not overwrite:
            raise FileExistsError(f"{out_dir} is not empty (pass overwrite=True)")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_dir / FRAMES_FILE, compression="zstd")

    info_row = db.query_one(
        "SELECT update_seq, info_json FROM session_info_snapshots WHERE session_id = ? "
        "ORDER BY update_seq DESC LIMIT 1",
        [session_id],
    )
    copied = (
        _trim_journal(journal_root, session_id, out_dir / JOURNAL_DIR, (t0, t1), (tick0, tick1))
        if journal_root is not None
        else {}
    )

    meta = {
        "format": FORMAT_VERSION,
        "source_session_id": session_id,
        "laps": [laps[0], laps[1]],
        "session_time": [t0, t1],
        "tick": [tick0, tick1],
        "frames": table.num_rows,
        "session": {
            **{k: v for k, v in session.items() if k not in {"started_at", "ended_at"}},
            "started_at": _epoch(session.get("started_at")),
            "ended_at": _epoch(session.get("ended_at")),
        },
        "lap_rows": lap_rows,
        "catalog": catalog_rows,
        "session_info": {
            "update_seq": info_row["update_seq"],
            "info": json.loads(info_row["info_json"]) if info_row["info_json"] else {},
        }
        if info_row
        else None,
        "journal": copied,
    }
    (out_dir / FIXTURE_FILE).write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    return TrimResult(
        out_dir=out_dir,
        session_id=session_id,
        laps=[r["lap"] for r in lap_rows] or list(range(laps[0], laps[1] + 1)),
        frames=table.num_rows,
        channels=len(catalog_rows),
        journal_records=copied,
        session_time=(t0, t1),
    )


def _trim_journal(
    root: Path,
    session_id: str,
    out: Path,
    span: tuple[float, float],
    ticks: tuple[int, int],
) -> dict[str, int]:
    """Every run's records inside ``span``; lifecycle records are always kept."""
    folder = journal.session_dir(root, session_id)
    if not folder.is_dir():
        return {}
    copied: dict[str, int] = {}
    for path in sorted(folder.glob("*.jsonl")):
        kept: list[str] = []
        for rec in journal.iter_records(path):
            if rec.get("kind") == "lifecycle":
                kept.append(json.dumps(rec, separators=(",", ":")))
                continue
            t, tick = rec.get("session_time"), rec.get("tick")
            if t is not None:
                inside = span[0] <= float(t) <= span[1]
            elif tick is not None:
                inside = ticks[0] <= int(tick) <= ticks[1]
            else:
                inside = False
            if inside:
                kept.append(json.dumps(rec, separators=(",", ":")))
        if len(kept) > sum(1 for line in kept if '"kind":"lifecycle"' in line):
            out.mkdir(parents=True, exist_ok=True)
            (out / path.name).write_text("\n".join(kept) + "\n", encoding="utf-8")
            copied[path.stem] = len(kept)
    return copied


def read_fixture(fixture_dir: Path) -> dict[str, Any]:
    meta = json.loads((Path(fixture_dir) / FIXTURE_FILE).read_text(encoding="utf-8"))
    if meta.get("format") != FORMAT_VERSION:
        raise ValueError(f"Unsupported fixture format {meta.get('format')!r}")
    return meta


def seed_fixture(
    writer,
    fixture_dir: Path,
    *,
    session_id: str | None = None,
    journal_root: Path | None = None,
    flatten_max: int = 6,
) -> str:
    """Write a trimmed fixture into a store through ``writer``; return its session id.

    The catalog is rebuilt with the same builder the live and ``.ibt`` paths use,
    so the stored storage strategy (list, flattened, scalar) must come out the same
    as the source's; a mismatch means ``flatten_max`` differs and is refused.
    """
    import pyarrow.parquet as pq

    fixture_dir = Path(fixture_dir)
    meta = read_fixture(fixture_dir)
    sid = session_id or meta["source_session_id"]
    rows = meta["catalog"]
    catalog = build_catalog(
        [
            RawHeader(
                name=r["name"],
                type=int(r["ir_type"]),
                count=int(r["count"] or 1),
                desc=r["descr"] or "",
                unit=r["unit"] or "",
            )
            for r in rows
        ],
        source=meta["session"].get("kind") or "ibt",
        flatten_max=flatten_max,
        car_id=meta["session"].get("car_id"),
        track_id=meta["session"].get("track_id"),
    )
    for r in rows:
        if catalog[r["name"]].storage.value != r["storage"]:
            raise ValueError(
                f"{r['name']} was stored as {r['storage']!r} but rebuilds as "
                f"{catalog[r['name']].storage.value!r}; seed with the source's flatten_max"
            )

    s = meta["session"]
    writer.begin_session(
        Session(
            session_id=sid,
            kind=SessionKind(s.get("kind") or "ibt"),
            source_path=s.get("source_path"),
            car_id=s.get("car_id"),
            track_id=s.get("track_id"),
            track_name=s.get("track_name"),
            ir_session_id=s.get("ir_session_id"),
            ir_subsession_id=s.get("ir_subsession_id"),
            schema_hash=s.get("schema_hash"),
            started_at=s.get("started_at"),
            poll_hz=s.get("poll_hz"),
            status="open",
            label=s.get("label"),
        ),
        catalog,
    )
    info = meta.get("session_info")
    if info:
        writer.write_session_info(
            SessionInfoSnapshot(
                session_id=sid,
                update_seq=int(info["update_seq"]),
                captured_tick=int(meta["tick"][0]),
                captured_at=float(s.get("started_at") or 0.0),
                info=info["info"],
            )
        )
    writer.write_telemetry(sid, pq.read_table(fixture_dir / FRAMES_FILE))
    writer.write_laps(
        sid,
        [
            Lap(
                session_id=sid,
                lap=int(r["lap"]),
                start_tick=r["start_tick"],
                end_tick=r["end_tick"],
                start_session_time=r["start_session_time"],
                lap_time=r["lap_time"],
                is_valid=r["is_valid"],
                is_out_lap=bool(r["is_out_lap"]),
                is_in_lap=bool(r["is_in_lap"]),
            )
            for r in meta["lap_rows"]
        ],
    )
    writer.end_session(sid, sample_count=int(meta["frames"]), ended_at=s.get("ended_at") or 0.0)

    src = fixture_dir / JOURNAL_DIR
    if journal_root is not None and src.is_dir():
        dest = journal.session_dir(journal_root, sid)
        dest.mkdir(parents=True, exist_ok=True)
        for path in sorted(src.glob("*.jsonl")):
            lines = []
            for rec in journal.iter_records(path):
                if rec.get("kind") == "lifecycle" and isinstance(rec.get("data"), dict):
                    if "session_id" in rec["data"]:
                        rec["data"]["session_id"] = sid
                lines.append(json.dumps(rec, separators=(",", ":")))
            (dest / path.name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sid
