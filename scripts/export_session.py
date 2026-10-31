#!/usr/bin/env python3
"""Export one stored session as static JSON for the web review's static mode.

The post-race review (``frontend/review/``) normally reads the REST API. With
``?static=<folder>/`` it reads the files this script writes instead, so a recorded
session can be published on GitHub Pages with no server. Every file is the body of
the REST call the review makes (made here through the real app with FastAPI's test
client), so the static page shows exactly what the live API would:

    sessions.json                                   /sessions (this session only)
    sessions/<id>/laps.json                         /sessions/<id>/laps
    sessions/<id>/pitwall-runs.json                 /sessions/<id>/pitwall-runs
    sessions/<id>/summary/<run|none>.json           /sessions/<id>/summary[?run=]
    sessions/<id>/pitwall-log/<run>.json            /sessions/<id>/pitwall-log?run=
    sessions/<id>/pitwall-log/<run>-state.json      ...?run=&kinds=state (for the cursor)
    sessions/<id>/trackmap/<lap>.json               /sessions/<id>/trackmap?lap=
    sessions/<id>/compare/<channel>/<lap>.json      /sessions/<id>/compare?name=&laps=<lap>
    sessions/<id>/lap-findings/<main>-<ref>.json    /coaching/lap-findings
    sessions/<id>/delta/<main>-<ref>.json           /coaching/delta
    export.json                                     what was exported, for people

``frontend/js/review/static-data.js`` maps the review's requests onto this layout.
No model is called: the agents' work comes from the recorded journal, and the
pitwall, its agents and vision are switched off for the export.

Export a session from an existing data folder::

    python scripts/export_session.py --data-dir data --session <id> --out docs/data/<name>

Or import an .ibt and its recorded journal(s) into a scratch store first (the
session id is taken from the journal, so the runs line up)::

    python scripts/export_session.py --ibt race.ibt --journal run.jsonl --out docs/data/spa

Then ``python scripts/build_pages.py`` copies the review next to it in ``docs/``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: The channels on the review's lap traces (TRACE_CHANNELS in review-app.js).
TRACE_CHANNELS = ("Speed", "Throttle", "Brake", "SteeringWheelAngle", "Gear")
#: The query parameters the review sends with each call.
COMPARE_GRID = 1000
TRACKMAP_POINTS = 1500
LOG_LIMIT = 200_000
#: Fields of /sessions that describe the machine the export ran on, not the race.
LOCAL_FIELDS = ("source_path",)


def _seg(value: Any) -> str:
    """A path segment, encoded as static-data.js encodes it (encodeURIComponent)."""
    return quote(str(value), safe="-_.!~*'()")


def _write(out: Path, rel: str, body: Any) -> int:
    path = out / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    path.write_text(data, encoding="utf-8")
    return len(data.encode("utf-8"))


def journal_session_id(path: Path) -> str:
    """The session id a recorded journal belongs to (its lifecycle start record)."""
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("kind") == "lifecycle" and (rec.get("data") or {}).get("phase") == "start":
                sid = rec["data"].get("session_id")
                if sid:
                    return str(sid)
    raise SystemExit(f"{path}: no lifecycle start record with a session_id")


def import_into(data_dir: Path, ibt: Path, journals: Iterable[Path]) -> str:
    """Import ``ibt`` under the journals' session id and file the journals beside it."""
    from rtv.ingest.ibt import import_ibt
    from rtv.store.duck import Database
    from rtv.store.journal import session_dir
    from rtv.store.writer import TelemetryWriter

    journals = [Path(j) for j in journals]
    ids = {journal_session_id(j) for j in journals}
    if len(ids) != 1:
        raise SystemExit(f"the journals belong to different sessions: {sorted(ids)}")
    sid = ids.pop()
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "telemetry.duckdb")
    try:
        writer = TelemetryWriter(db, data_dir / "parquet")
        print(f"importing {ibt.name} as session {sid} ...", flush=True)
        import_ibt(ibt, writer, chunk_rows=20_000, session_id=sid)
    finally:
        db.close()
    runs = session_dir(data_dir / "sessions", sid)
    runs.mkdir(parents=True, exist_ok=True)
    for j in journals:
        shutil.copy2(j, runs / j.name)
    return sid


class _Api:
    def __init__(self, client) -> None:
        self.client = client

    def get(self, path: str, *, ok404: bool = False) -> Any:
        r = self.client.get(f"/api/v1{path}")
        if r.status_code == 200:
            return r.json()
        if ok404 and r.status_code in (400, 404, 422):
            return None
        raise SystemExit(f"GET {path} -> {r.status_code}: {r.text[:300]}")


def export(data_dir: Path, session_id: str, out: Path) -> dict[str, Any]:
    """Write the static files for ``session_id`` from ``data_dir`` into ``out``."""
    os.environ["RTV_DATA_DIR"] = str(data_dir)
    # Read-only: no poller, no agents, no vision model; nothing here may call Kimi.
    os.environ["RTV_AUTOSTART_LIVE"] = "false"
    os.environ["RTV_PITWALL_AGENTS"] = "false"
    os.environ["RTV_VISION_MODE"] = "off"
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    if out.exists():
        if any(out.iterdir()) and not (out / "export.json").is_file():
            raise SystemExit(f"{out} is not empty and holds no export.json; refusing to clear it")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    sizes: dict[str, int] = {}

    def put(rel: str, body: Any) -> None:
        group = rel.split("/")[2] if rel.startswith("sessions/") and rel.count("/") >= 2 else rel
        sizes[group] = sizes.get(group, 0) + _write(out, rel, body)

    with TestClient(create_app()) as client:
        api = _Api(client)
        sessions = api.get("/sessions?limit=200")["sessions"]
        session = next((s for s in sessions if s["session_id"] == session_id), None)
        if session is None:
            raise SystemExit(f"no session {session_id} in {data_dir}")
        session = {k: (None if k in LOCAL_FIELDS else v) for k, v in session.items()}
        put("sessions.json", {"count": 1, "sessions": [session]})

        base = f"/sessions/{session_id}"
        d = f"sessions/{_seg(session_id)}"
        laps = api.get(f"{base}/laps")
        put(f"{d}/laps.json", laps)

        runs = api.get(f"{base}/pitwall-runs")
        runs["recording"] = None
        put(f"{d}/pitwall-runs.json", runs)
        run_ids = [r["run_id"] for r in runs.get("runs", [])]

        summary = api.get(f"{base}/summary", ok404=True)
        if summary is not None:
            put(f"{d}/summary/none.json", summary)
        for run in run_ids:
            q = quote(run)
            put(f"{d}/summary/{_seg(run)}.json", api.get(f"{base}/summary?run={q}"))
            put(
                f"{d}/pitwall-log/{_seg(run)}.json",
                api.get(f"{base}/pitwall-log?run={q}&limit={LOG_LIMIT}"),
            )
            put(
                f"{d}/pitwall-log/{_seg(run)}-state.json",
                api.get(f"{base}/pitwall-log?run={q}&kinds=state&limit={LOG_LIMIT}"),
            )

        timed = [lap["lap"] for lap in laps.get("laps", []) if (lap.get("lap_time") or 0) > 0]
        for lap in timed:
            body = api.get(f"{base}/trackmap?lap={lap}&max_points={TRACKMAP_POINTS}", ok404=True)
            if body is not None:
                put(f"{d}/trackmap/{lap}.json", body)
            for name in TRACE_CHANNELS:
                body = api.get(
                    f"{base}/compare?name={name}&laps={lap}&grid={COMPARE_GRID}", ok404=True
                )
                if body is not None:
                    put(f"{d}/compare/{_seg(name)}/{lap}.json", body)

        # Findings and the delta are per pair: every ordered pair of complete, valid laps
        # (partial first and last laps have nothing to compare).
        valid = [
            lap["lap"]
            for lap in laps.get("laps", [])
            if (lap.get("lap_time") or 0) > 0 and lap.get("is_valid")
        ]
        pairs = 0
        for main in valid:
            for ref in valid:
                if main == ref:
                    continue
                qs = f"session_id={quote(session_id)}&main_lap={main}&ref_lap={ref}"
                findings = api.get(f"/coaching/lap-findings?{qs}", ok404=True)
                delta = api.get(f"/coaching/delta?{qs}", ok404=True)
                if findings is not None:
                    put(f"{d}/lap-findings/{main}-{ref}.json", findings)
                    pairs += 1
                if delta is not None:
                    put(f"{d}/delta/{main}-{ref}.json", delta)
            print(f"  findings for lap {main} done", flush=True)

    manifest = {
        "session_id": session_id,
        "track_name": session.get("track_name"),
        "car_id": session.get("car_id"),
        "runs": run_ids,
        "laps": timed,
        "pair_laps": valid,
        "pairs": pairs,
        "channels": list(TRACE_CHANNELS),
        "bytes": dict(sorted(sizes.items())),
    }
    _write(out, "export.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="Output folder, e.g. docs/data/spa")
    ap.add_argument("--data-dir", type=Path, help="Existing RTV data folder to export from")
    ap.add_argument("--session", help="Session id (with --data-dir)")
    ap.add_argument("--ibt", type=Path, help="Import this .ibt into a scratch store first")
    ap.add_argument(
        "--journal", type=Path, action="append", default=[], help="Recorded journal run (.jsonl)"
    )
    args = ap.parse_args(argv)

    if args.ibt:
        if not args.journal:
            ap.error("--ibt needs at least one --journal (the recorded pitwall run)")
        scratch = Path(tempfile.mkdtemp(prefix="rtv_export_"))
        try:
            sid = import_into(scratch, args.ibt, args.journal)
            manifest = export(scratch, sid, args.out)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    elif args.data_dir and args.session:
        manifest = export(args.data_dir, args.session, args.out)
    else:
        ap.error("give --ibt and --journal, or --data-dir and --session")

    total = sum(manifest["bytes"].values())
    print(
        f"exported session {manifest['session_id']}: {len(manifest['laps'])} laps, "
        f"{len(manifest['runs'])} run(s), {manifest['pairs']} lap pairs, "
        f"{total / 1e6:.1f} MB -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
