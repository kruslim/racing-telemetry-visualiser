"""Cut a few laps of a stored session (frames incl. CarIdx*, plus its journal) into a fixture.

Usage (PowerShell, with the server stopped so DuckDB is not locked):

    .venv\\Scripts\\python.exe scripts\\trim_fixture.py --list
    .venv\\Scripts\\python.exe scripts\\trim_fixture.py --session <id> --laps 3-5 `
        --out fixtures\\rig-ai-race

``--channels "CarIdx*,Speed,Throttle"`` keeps only matching channels (the ones
the replay needs are always kept). ``--data-dir`` defaults to ``RTV_DATA_DIR``
(``./data``). The fixture format and the loader live in ``rtv.store.fixture``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from rtv.store.fixture import parse_laps, trim_session  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data-dir", type=Path, help="RTV data dir (default: RTV_DATA_DIR or ./data)")
    p.add_argument("--list", action="store_true", help="List stored sessions and exit")
    p.add_argument("--session", help="Session id to trim")
    p.add_argument("--laps", help="Lap range, N or N-M (inclusive)")
    p.add_argument("--out", type=Path, help="Output directory (must be empty)")
    p.add_argument("--channels", help="Comma-separated glob patterns of channels to keep")
    p.add_argument("--no-journal", action="store_true", help="Skip the race-journal records")
    p.add_argument("--overwrite", action="store_true", help="Replace a non-empty --out")
    return p


def _data_dir(arg: Path | None) -> Path:
    if arg is not None:
        return arg
    from rtv.config import Settings

    return Settings().data_dir


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    data_dir = _data_dir(args.data_dir)
    db_path = data_dir / "telemetry.duckdb"
    if not db_path.exists():
        print(f"No store at {db_path}", file=sys.stderr)
        return 2

    from rtv.store.duck import Database

    db = Database(db_path)
    try:
        if args.list:
            rows = db.query_dicts(
                "SELECT s.session_id, s.kind, s.track_name, s.started_at, "
                "(SELECT count(*) FROM laps l WHERE l.session_id = s.session_id) AS laps "
                "FROM sessions s ORDER BY s.started_at"
            )
            for r in rows:
                print(
                    f"{r['session_id']}  {r['kind']:4}  laps={r['laps']:<3} "
                    f"{r['track_name'] or '?'}  {r['started_at']}"
                )
            return 0
        if not (args.session and args.laps and args.out):
            print("--session, --laps and --out are required (or --list)", file=sys.stderr)
            return 2
        try:
            laps = parse_laps(args.laps)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        channels = (
            [c.strip() for c in args.channels.split(",") if c.strip()] if args.channels else None
        )
        try:
            result = trim_session(
                db,
                data_dir / "parquet",
                args.session,
                laps,
                args.out,
                journal_root=None if args.no_journal else data_dir / "sessions",
                channels=channels,
                overwrite=args.overwrite,
            )
        except (KeyError, FileExistsError) as exc:
            print(str(exc).strip("'\""), file=sys.stderr)
            return 1
    finally:
        db.close()
    print(json.dumps(result.to_api(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
