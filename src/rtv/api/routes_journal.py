"""Race journal endpoints: replay what the pitwall saw, did and said in a session.

Read-only views over :mod:`rtv.store.journal`. Mounted with the v1 routes so the
post-race review works whether or not the pitwall is enabled right now: a journal
recorded earlier stays readable.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from rtv.api.deps import get_services
from rtv.racestate.summary import race_summary
from rtv.services import AppServices
from rtv.store import journal

router = APIRouter(tags=["journal"])


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _run_file(services: AppServices, session_id: str, run: str | None) -> tuple[str, Path]:
    root = services.settings.journal_dir
    try:
        if run is None:
            run = journal.default_run(journal.list_runs(root, session_id))
            if run is None:
                raise HTTPException(404, f"No journal recorded for session {session_id}")
        path = journal.run_path(root, session_id, run)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, f"Unknown journal run {run} for session {session_id}")
    return run, path


@router.get("/sessions/{session_id}/pitwall-runs", summary="Recorded journal runs of a session")
def list_runs(session_id: str, services: AppServices = Depends(get_services)) -> dict:
    try:
        runs = journal.list_runs(services.settings.journal_dir, session_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "session_id": session_id,
        "default_run": journal.default_run(runs),
        "recording": services.recorder.active if services.recorder is not None else None,
        "runs": runs,
    }


@router.get(
    "/sessions/{session_id}/pitwall-log",
    summary="Journal records (race events, radio, agent activity, state) of one run",
)
def get_log(
    session_id: str,
    run: str | None = Query(None, description="Run id; default: newest run with agents."),
    kinds: str | None = Query(None, description="Comma-separated kinds to include."),
    exclude: str | None = Query(
        "state", description="Comma-separated kinds to leave out (state by default)."
    ),
    agent: str | None = None,
    t_from: float | None = Query(None, alias="from", description="Session time, s."),
    t_to: float | None = Query(None, alias="to", description="Session time, s."),
    after_seq: int = Query(0, ge=0),
    limit: int = Query(20_000, ge=1, le=200_000),
    services: AppServices = Depends(get_services),
) -> dict:
    run_id, path = _run_file(services, session_id, run)
    want = _split(kinds)
    records = journal.read_records(
        path,
        kinds=want,
        # Naming a kind explicitly overrides the default exclusion of state.
        exclude=[k for k in (_split(exclude) or []) if not want or k not in want],
        agent=agent,
        t_from=t_from,
        t_to=t_to,
        after_seq=after_seq,
        limit=limit,
    )
    return {
        "session_id": session_id,
        "run_id": run_id,
        "count": len(records),
        "truncated": len(records) >= limit,
        "records": records,
    }


@router.get(
    "/sessions/{session_id}/pitwall-log/state",
    summary="The race-state snapshot at (or just before) a session time",
)
def get_state(
    session_id: str,
    t: float = Query(..., description="Session time, s."),
    run: str | None = None,
    services: AppServices = Depends(get_services),
) -> dict:
    run_id, path = _run_file(services, session_id, run)
    rec = journal.state_at(path, t)
    if rec is None:
        raise HTTPException(404, f"No state snapshots in run {run_id}")
    return {"session_id": session_id, "run_id": run_id, "record": rec}


@router.get(
    "/sessions/{session_id}/summary",
    summary="Deterministic post-race summary: laps, stints, fuel, events, radio, agents",
)
def get_summary(
    session_id: str,
    run: str | None = None,
    services: AppServices = Depends(get_services),
) -> dict:
    if services.repo.get_session(session_id) is None:
        raise HTTPException(404, f"Unknown session {session_id}")
    laps = services.repo.get_laps(session_id)
    run_id: str | None = None
    records = None
    try:
        run_id, path = _run_file(services, session_id, run)
        records = journal.iter_records(path)
    except HTTPException as exc:
        if run is not None or exc.status_code != 404:
            raise
    return {"session_id": session_id, "run_id": run_id, **race_summary(laps, records)}
