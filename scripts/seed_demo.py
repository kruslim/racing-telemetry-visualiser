"""Seed the simulated demo session into the local store.

    python scripts/seed_demo.py            # create the demo session if absent
    python scripts/seed_demo.py --force    # re-generate even if present

Then start the server (``uvicorn rtv.main:app --app-dir src``) and the demo
session shows up under GET /api/v1/sessions with real chart + coaching data.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from rtv.config import get_settings  # noqa: E402
from rtv.demo.seed import seed_demo_session  # noqa: E402
from rtv.store.duck import Database  # noqa: E402
from rtv.store.repository import Repository  # noqa: E402
from rtv.store.writer import TelemetryWriter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the simulated demo session.")
    parser.add_argument("--force", action="store_true", help="Re-generate even if present.")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    db = Database(settings.duckdb_path)
    try:
        repo = Repository(db, settings.parquet_dir)
        writer = TelemetryWriter(db, settings.parquet_dir)
        sid = seed_demo_session(repo, writer, force=args.force)
        if sid:
            print(f"Seeded demo session: {sid}")
        else:
            print("Demo session already present (use --force to re-generate).")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
