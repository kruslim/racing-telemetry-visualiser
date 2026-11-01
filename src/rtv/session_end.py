"""The session-end hook: what runs when one session segment's journal run closes.

``AppServices.on_session_end`` is a :class:`SessionEndHooks` (a plain list of
callbacks). Each callback receives one :class:`SessionEnded` record per closed
journal run, on a worker thread, so nothing here can block the 60 Hz frame
path; a callback that raises is logged and ignored, and the others still run.

Built in:

* :func:`store_run_summary` computes the deterministic
  :func:`~rtv.racestate.summary.race_summary` for the run (only that run's laps)
  and stores it as ``<run_id>.summary.json`` next to the journal file, which
  ``GET /sessions/{id}/summary?run=`` then serves.
* A :class:`Notifier` (``RTV_PITWALL_SESSION_END_NOTIFIER``): ``none`` by
  default, or ``file`` (:class:`FileNotifier`), which logs a one-line result and
  writes ``<run_id>.notice.txt``. Nothing on the default path touches the
  network; a webhook or e-mail notifier would have to be import-guarded and
  off by default.

Stage 09's debrief subscribes here (see ``docs/PITWALL.md``).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from rtv.logging import get_logger
from rtv.racestate.summary import race_summary
from rtv.store import journal

log = get_logger("session_end")


@dataclass(frozen=True)
class SessionEnded:
    """One closed journal run (one session segment, or the part of it a run saw)."""

    session_id: str
    run_id: str
    #: The run's JSON-lines file (``data/sessions/<id>/runs/<run_id>.jsonl``).
    journal_path: Path
    source: str
    session_num: int | None
    #: practice | qualify | race, or None when the session did not say.
    session_type: str | None
    #: Why the run closed: ``session_changed``, ``replay_finished``,
    #: ``iRacing telemetry disconnected``, ``shutdown``, ...
    reason: str | None
    #: Billable model calls in the segment (every layer), None without agents.
    calls_used: int | None = None
    calls_by_layer: dict[str, int] = field(default_factory=dict)

    @property
    def summary_path(self) -> Path:
        return journal.summary_path(self.journal_path)

    def to_api(self) -> dict[str, Any]:
        out = asdict(self)
        out["journal_path"] = str(self.journal_path)
        return out


SessionEndCallback = Callable[[SessionEnded], None]


class SessionEndHooks(list):
    """The callbacks, plus the single worker that runs them off the frame path."""

    def __init__(self, *callbacks: SessionEndCallback) -> None:
        super().__init__(callbacks)
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._pending: list[Future] = []
        self.fired = 0
        self.errors = 0

    def fire(self, ended: SessionEnded) -> None:
        """Queue every callback for ``ended``; returns immediately."""
        callbacks = list(self)
        if not callbacks:
            return
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="rtv-session-end"
                )
            self.fired += 1
            self._pending = [f for f in self._pending if not f.done()]
            self._pending.append(self._executor.submit(self._run, callbacks, ended))

    def _run(self, callbacks: list[SessionEndCallback], ended: SessionEnded) -> None:
        for callback in callbacks:
            try:
                callback(ended)
            except Exception:
                self.errors += 1
                log.exception(
                    "Session-end callback %s failed for %s/%s",
                    getattr(callback, "__name__", callback), ended.session_id, ended.run_id,
                )

    def wait(self, timeout: float | None = 10.0) -> bool:
        """Block until every queued callback has run (tests, shutdown)."""
        with self._lock:
            pending = list(self._pending)
        for fut in pending:
            try:
                fut.result(timeout=timeout)
            except Exception:  # pragma: no cover - _run never raises
                return False
        return True

    def close(self) -> None:
        self.wait()
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None


def run_summary(repo: Any, session_id: str, path: Path) -> dict[str, Any]:
    """The summary of one run: that run's laps plus its journal records."""
    records = list(journal.iter_records(path))
    laps = repo.get_laps(session_id) if repo is not None else []
    out = race_summary(journal.laps_for_run(laps, records), records)
    return {"session_id": session_id, "run_id": path.stem, **out}


def store_run_summary(repo: Any) -> SessionEndCallback:
    """The built-in subscriber: write ``<run_id>.summary.json`` beside the run."""

    def store_summary(ended: SessionEnded) -> None:
        summary = run_summary(repo, ended.session_id, ended.journal_path)
        summary["session_num"] = ended.session_num
        summary["session_type"] = ended.session_type
        tmp = ended.summary_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
        tmp.replace(ended.summary_path)
        log.info("Stored the summary of %s/%s", ended.session_id, ended.run_id)

    return store_summary


# ---- notifiers ------------------------------------------------------------


class Notifier(Protocol):
    def notify(self, ended: SessionEnded, summary: dict[str, Any]) -> None: ...


class NullNotifier:
    """The default: nothing leaves the process."""

    def notify(self, ended: SessionEnded, summary: dict[str, Any]) -> None:
        return None


class FileNotifier:
    """Logs one line and writes ``<run_id>.notice.txt`` next to the journal run."""

    def notify(self, ended: SessionEnded, summary: dict[str, Any]) -> None:
        text = notice_text(ended, summary)
        log.info("Session ended: %s", text)
        path = ended.journal_path.with_name(f"{ended.run_id}.notice.txt")
        path.write_text(text + "\n", encoding="utf-8")


def notice_text(ended: SessionEnded, summary: dict[str, Any]) -> str:
    """A one-line, number-for-number restatement of the stored summary."""
    kind = (ended.session_type or "session").capitalize()
    parts = [f"{kind} {ended.session_id} run {ended.run_id} ended ({ended.reason or 'closed'})"]
    if summary.get("laps") is not None:
        parts.append(f"{summary['laps']} laps")
    if summary.get("best_time") is not None:
        parts.append(f"best {summary['best_time']:.3f} s on lap {summary.get('best_lap')}")
    if summary.get("finish_position"):
        parts.append(f"finished P{summary['finish_position']}")
    parts.append(f"{summary.get('radio_calls', 0)} radio calls")
    if ended.calls_used is not None:
        parts.append(f"{ended.calls_used} model calls")
    return ", ".join(parts)


NOTIFIERS: dict[str, Callable[[], Notifier]] = {
    "none": NullNotifier,
    "file": FileNotifier,
}


def build_notifier(name: str) -> Notifier:
    key = (name or "none").strip().lower()
    factory = NOTIFIERS.get(key)
    if factory is None:
        log.warning("Unknown RTV_PITWALL_SESSION_END_NOTIFIER %r; using none.", name)
        return NullNotifier()
    return factory()


def notify_with(notifier: Notifier) -> SessionEndCallback:
    """A callback that hands the stored summary (or a fresh one) to ``notifier``."""

    def notify(ended: SessionEnded) -> None:
        path = ended.summary_path
        summary = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        notifier.notify(ended, summary)

    return notify
