"""The race journal: what the pitwall saw, did and said, kept after the race.

Race events and radio calls otherwise live only in bounded in-memory ring buffers,
so once a session ends nothing but the telemetry remains. The
:class:`SessionRecorder` appends every race event, every aired radio call, every
agent activity record (:mod:`rtv.pitwall.activity`) and a periodic race-state
snapshot to one JSON-lines file per *run* of a session::

    data/sessions/<session_id>/runs/<run_id>.jsonl

A run is one pass over a session: the live capture, or a replay of a stored
session through the engine. Runs never overwrite each other, so replaying an
imported ``.ibt`` (free, no agents) cannot clobber a live run the agents spoke in.

Every line uses the activity envelope shared with the live view::

    {"seq": 12, "wall_ms": ..., "session_time": 1834.2, "tick": 110052,
     "kind": "race_event", "agent": null, "run_id": null, "event_key": "...",
     "data": {...}}

``seq`` is renumbered by the recorder so it is monotonic across every kind in the
file (the activity log's own counter is kept as ``data.activity_seq``). The kinds
the recorder adds are ``lifecycle`` (run start/end), ``race_event``, ``radio`` and
``state``; everything else is an activity record passed through unchanged.

JSON lines rather than a DuckDB table: it is append-only on the hot path, needs no
second writer on the database the live poller is using, and a crash mid-race loses
at most the last flush interval.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rtv.logging import get_logger

log = get_logger("store.journal")

#: Kinds the recorder writes itself; anything else is an activity record.
RECORDER_KINDS = ("lifecycle", "race_event", "radio", "state")

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def _check_id(value: str) -> str:
    """Session and run ids become path segments: refuse anything that could escape."""
    if not _SAFE_ID.match(value) or value in {".", ".."}:
        raise ValueError(f"Unsafe journal id {value!r}")
    return value


class _Run:
    """One open journal file."""

    def __init__(self, path: Path, session_id: str, run_id: str, source: str) -> None:
        self.path = path
        self.session_id = session_id
        self.run_id = run_id
        self.source = source
        self.fh = path.open("a", encoding="utf-8")
        self.seq = 0
        self.last_state_t: float | None = None
        self.last_state_lap: int | None = None
        self.last_flush = time.monotonic()
        self.last_time: tuple[float | None, int | None] = (None, None)
        #: The session segment this run records (session_num, session_type,
        #: segment), once known; see :mod:`rtv.racestate.lifecycle`.
        self.segment: dict[str, Any] | None = None


class SessionRecorder:
    """Writes the journal of whatever session the engine is currently consuming.

    Thread-safe: race events arrive on the engine's thread, radio on the asyncio
    loop, activity from agent workers. Every source is a plain callback, so the
    recorder costs nothing when no run is open.
    """

    def __init__(
        self,
        root: Path,
        *,
        state_interval_s: float = 1.0,
        flush_interval_s: float = 1.0,
        snapshot: Callable[[], Any] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.state_interval_s = state_interval_s
        self.flush_interval_s = flush_interval_s
        #: ``() -> RaceState`` (normally ``engine.snapshot``), for state records.
        self._snapshot = snapshot
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._run: _Run | None = None

    # ---- run lifecycle -------------------------------------------------

    @property
    def active(self) -> dict[str, str] | None:
        run = self._run
        if run is None:
            return None
        return {"session_id": run.session_id, "run_id": run.run_id, "source": run.source}

    def active_run(self) -> dict[str, Any] | None:
        """:attr:`active` plus the run's file ``path`` and its ``segment``."""
        run = self._run
        if run is None:
            return None
        return {
            "session_id": run.session_id,
            "run_id": run.run_id,
            "source": run.source,
            "path": str(run.path),
            "segment": dict(run.segment) if run.segment else None,
        }

    def begin(
        self,
        session_id: str,
        source: str,
        *,
        segment: dict[str, Any] | None = None,
        **meta: Any,
    ) -> str:
        """Open a new run for ``session_id``; closes any run already open.

        ``segment`` (``session_num``, ``session_type``, ``segment``) goes into the
        start record when the session segment is already known.
        """
        self.end()
        _check_id(session_id)
        stamp = datetime.fromtimestamp(self._wall_clock(), tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = self.root / session_id / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        # The counter keeps two runs started in the same second apart and still
        # sorting in the order they were started.
        n = 1 + len(list(run_dir.glob(f"{stamp}-*.jsonl")))
        run_id = f"{stamp}-{n:02d}-{source}"
        with self._lock:
            self._run = _Run(run_dir / f"{run_id}.jsonl", session_id, run_id, source)
            self._run.segment = dict(segment) if segment else None
            self._write_locked(
                "lifecycle",
                {
                    "phase": "start",
                    "source": source,
                    "session_id": session_id,
                    **(segment or {}),
                    **meta,
                },
            )
        log.info("Journal run %s/%s started", session_id, run_id)
        return run_id

    def annotate_segment(self, segment: dict[str, Any]) -> bool:
        """Name the session segment of the open run (a ``lifecycle`` ``segment`` record).

        Used when the run was opened before the first frame said which session
        it is. False when no run is open or the run already has a segment.
        """
        with self._lock:
            run = self._run
            if run is None or run.segment is not None:
                return False
            run.segment = dict(segment)
            self._write_locked("lifecycle", {"phase": "segment", **segment})
            return True

    def end(self, *, state: Any = None, **meta: Any) -> dict[str, Any] | None:
        """Write the final state and close the open run, if any.

        ``state`` overrides the final snapshot (the engine's last state of a
        segment that has already been reset). Returns what :meth:`active_run`
        said about the run, or None when nothing was open.
        """
        if self._run is None:
            return None
        # Snapshot before taking our lock: the engine publishes race events (and
        # so calls on_race_event) while holding its own lock, so taking the two in
        # the other order here could deadlock.
        if state is None:
            state = self._take_snapshot()
        closed = self.active_run()
        with self._lock:
            run = self._run
            if run is None:
                return None
            self._write_state_locked(state, force=True)
            self._write_locked("lifecycle", {"phase": "end", **meta})
            try:
                run.fh.close()
            except OSError:  # pragma: no cover - disk gone
                log.exception("Could not close journal %s", run.path)
            self._run = None
        log.info("Journal run %s/%s closed", run.session_id, run.run_id)
        return closed

    # ---- sources -------------------------------------------------------

    def on_race_event(self, event: Any) -> None:
        if self._run is None:
            return
        data = event.to_api() if hasattr(event, "to_api") else dict(event)
        with self._lock:
            self._write_locked(
                "race_event",
                data,
                session_time=data.get("session_time"),
                tick=data.get("tick"),
                event_key=data.get("key"),
            )

    def on_radio(self, message: Any) -> None:
        if self._run is None:
            return
        data = message.model_dump(mode="json") if hasattr(message, "model_dump") else dict(message)
        with self._lock:
            run = self._run
            if run is None:
                return
            self._write_locked(
                "radio",
                data,
                session_time=data.get("session_time"),
                tick=run.last_time[1],
                agent=data.get("agent"),
                event_key=(data.get("event_ref") or {}).get("key"),
            )

    def on_activity(self, record: dict[str, Any]) -> None:
        if self._run is None:
            return
        data = dict(record.get("data") or {})
        if record.get("seq") is not None:
            data["activity_seq"] = record["seq"]
        with self._lock:
            self._write_locked(
                str(record.get("kind")),
                data,
                session_time=record.get("session_time"),
                tick=record.get("tick"),
                agent=record.get("agent"),
                run_id=record.get("run_id"),
                event_key=record.get("event_key"),
                wall_ms=record.get("wall_ms"),
            )

    def on_frame(self, session_time: float, tick: int, lap: int | None = None) -> None:
        """Called per frame after the engine; writes a state snapshot when due."""
        run = self._run
        if run is None:
            return
        run.last_time = (session_time, tick)
        due = (
            run.last_state_t is None
            or session_time - run.last_state_t >= self.state_interval_s
            or session_time < run.last_state_t  # session clock reset
            or (lap is not None and lap != run.last_state_lap)
        )
        if not due:
            return
        state = self._take_snapshot()
        with self._lock:
            if self._run is run:
                run.last_state_lap = lap
                self._write_state_locked(state)

    # ---- writing -------------------------------------------------------

    def _take_snapshot(self) -> Any:
        if self._snapshot is None:
            return None
        try:
            return self._snapshot()
        except Exception:  # pragma: no cover - never break the feed
            log.exception("Journal could not snapshot the race state")
            return None

    def _write_state_locked(self, state: Any, *, force: bool = False) -> None:
        run = self._run
        if run is None or state is None:
            return
        data = state.to_api() if hasattr(state, "to_api") else dict(state)
        t = data.get("session_time")
        if not force and t is not None and run.last_state_t == t:
            return
        run.last_state_t = t
        self._write_locked(
            "state", data, session_time=t, tick=data.get("tick"), final=force or None
        )

    def _write_locked(
        self,
        kind: str,
        data: dict[str, Any],
        *,
        session_time: float | None = None,
        tick: int | None = None,
        agent: str | None = None,
        run_id: str | None = None,
        event_key: str | None = None,
        wall_ms: int | None = None,
        final: bool | None = None,
    ) -> None:
        run = self._run
        if run is None:
            return
        if session_time is None:
            session_time = run.last_time[0]
        if tick is None:
            tick = run.last_time[1]
        run.seq += 1
        item: dict[str, Any] = {
            "seq": run.seq,
            "wall_ms": wall_ms if wall_ms is not None else int(self._wall_clock() * 1000),
            "session_time": session_time,
            "tick": tick,
            "kind": kind,
            "agent": agent,
            "run_id": run_id,
            "event_key": event_key,
            "data": data,
        }
        if final:
            item["final"] = True
        try:
            run.fh.write(json.dumps(item, separators=(",", ":"), default=str) + "\n")
            now = time.monotonic()
            if kind == "lifecycle" or now - run.last_flush >= self.flush_interval_s:
                run.fh.flush()
                run.last_flush = now
        except (OSError, TypeError, ValueError):  # pragma: no cover - never break the feed
            log.exception("Journal write failed for %s", kind)


# ---- reading -------------------------------------------------------------


#: Lifecycle fields naming a run's session segment.
_SEGMENT_KEYS = ("session_num", "session_type", "segment")


def summary_path(journal_path: Path) -> Path:
    """Where a run's stored summary lives: ``<run_id>.summary.json`` beside it."""
    journal_path = Path(journal_path)
    return journal_path.with_name(f"{journal_path.stem}.summary.json")


def laps_for_run(
    laps: list[dict[str, Any]], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The lap rows one run covers: a session's lap table spans every segment.

    A lap belongs to the run when the run's state snapshots saw the player on
    that lap and its start time lies inside the run's session-time bounds.
    A run with no snapshots keeps every lap (nothing to judge by).
    """
    seen: set[int] = set()
    t0 = t1 = None
    for rec in records:
        t = rec.get("session_time")
        if isinstance(t, int | float):
            t0 = t if t0 is None else min(t0, t)
            t1 = t if t1 is None else max(t1, t)
        if rec.get("kind") == "state":
            lap = ((rec.get("data") or {}).get("player") or {}).get("lap")
            if isinstance(lap, int):
                seen.add(lap)
    if not seen:
        return list(laps)
    out = []
    for lap in laps:
        if lap.get("lap") not in seen:
            continue
        start = lap.get("start_session_time")
        if isinstance(start, int | float) and t0 is not None and t1 is not None:
            # A lap starts a frame or two before the snapshot that first sees it.
            if not (t0 - 1.0 <= start <= t1):
                continue
        out.append(lap)
    return out


def session_dir(root: Path, session_id: str) -> Path:
    return Path(root) / _check_id(session_id) / "runs"


def run_path(root: Path, session_id: str, run_id: str) -> Path:
    return session_dir(root, session_id) / f"{_check_id(run_id)}.jsonl"


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Every record in a journal file; a torn last line (a crash) is skipped."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def summarize_run(path: Path) -> dict[str, Any]:
    """Counts and bounds of one run, read without keeping it in memory."""
    counts: dict[str, int] = {}
    agents: set[str] = set()
    start: dict[str, Any] = {}
    segment: dict[str, Any] = {}
    end: dict[str, Any] | None = None
    t0 = t1 = None
    for rec in iter_records(path):
        kind = rec.get("kind")
        counts[kind] = counts.get(kind, 0) + 1
        if rec.get("agent"):
            agents.add(rec["agent"])
        t = rec.get("session_time")
        if t is not None:
            t0 = t if t0 is None else min(t0, t)
            t1 = t if t1 is None else max(t1, t)
        if kind == "lifecycle":
            phase = (rec.get("data") or {}).get("phase")
            if phase == "start":
                start = rec
            elif phase == "segment":
                segment = rec.get("data") or {}
            elif phase == "end":
                end = rec
    data = start.get("data") or {}
    seg = {**{k: data.get(k) for k in _SEGMENT_KEYS if k in data}, **segment}
    end_data = (end or {}).get("data") or {}
    return {
        "run_id": path.stem,
        "source": data.get("source"),
        "started_wall_ms": start.get("wall_ms"),
        "ended_wall_ms": end.get("wall_ms") if end else None,
        "complete": end is not None,
        "session_time_start": t0,
        "session_time_end": t1,
        "counts": counts,
        "agents": sorted(agents),
        "has_agents": bool(counts.get("run_start") or counts.get("radio")),
        # The session segment (practice / qualify / race) this run recorded.
        "session_num": seg.get("session_num"),
        "session_type": seg.get("session_type"),
        "segment": seg.get("segment"),
        "end_reason": end_data.get("reason"),
        "calls_used": end_data.get("calls_used"),
        "meta": {
            k: v
            for k, v in data.items()
            if k not in {"phase", "source", "session_id", *_SEGMENT_KEYS}
        },
    }


def list_runs(root: Path, session_id: str) -> list[dict[str, Any]]:
    """Every run of a session, oldest first."""
    folder = session_dir(root, session_id)
    if not folder.is_dir():
        return []
    return [summarize_run(p) for p in sorted(folder.glob("*.jsonl"))]


def default_run(runs: list[dict[str, Any]]) -> str | None:
    """The run to show by default: the newest one the agents spoke in, else the newest."""
    if not runs:
        return None
    with_agents = [r for r in runs if r["has_agents"]]
    return (with_agents or runs)[-1]["run_id"]


def read_records(
    path: Path,
    *,
    kinds: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    agent: str | None = None,
    t_from: float | None = None,
    t_to: float | None = None,
    after_seq: int = 0,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Filtered records, in file order."""
    want = set(kinds) if kinds else None
    skip = set(exclude) if exclude else set()
    out: list[dict[str, Any]] = []
    for rec in iter_records(path):
        if rec.get("seq", 0) <= after_seq:
            continue
        kind = rec.get("kind")
        if (want is not None and kind not in want) or kind in skip:
            continue
        if agent is not None and rec.get("agent") != agent:
            continue
        t = rec.get("session_time")
        if t is not None and t_from is not None and t < t_from:
            continue
        if t is not None and t_to is not None and t > t_to:
            continue
        out.append(rec)
        if limit is not None and len(out) >= limit:
            break
    return out


def state_at(path: Path, t: float) -> dict[str, Any] | None:
    """The last state snapshot at or before session time ``t`` (else the first)."""
    best: dict[str, Any] | None = None
    first: dict[str, Any] | None = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if '"kind":"state"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            st = rec.get("session_time")
            if first is None:
                first = rec
            if st is not None and st <= t and (
                best is None or st >= (best.get("session_time") or 0)
            ):
                best = rec
    return best or first
