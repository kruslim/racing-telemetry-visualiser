#!/usr/bin/env python3
"""Publish the web frontend to docs/ for GitHub Pages.

The GitHub Pages showcase at https://kruslim.github.io/racing-telemetry-visualiser/
is the *real* analysis app (frontend/), served statically. With no backend to
reach, api.js falls back to the offline simulation (sim.js + lapdata.js) and the
chat pane coaches from the deterministic findings — so every worksheet, the track
map and the AI race engineer all work as a self-contained live demo.

frontend/ is the single source of truth. This copies its static assets into
docs/ (leaving the Markdown docs and .nojekyll in place). Run it after changing
anything under frontend/:

    python scripts/build_pages.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend"
DST = ROOT / "docs"

# Static assets that make up the app. Keep docs/*.md and docs/.nojekyll untouched.
SUFFIXES = {".html", ".css", ".js"}


def main() -> int:
    if not SRC.is_dir():
        raise SystemExit(f"frontend/ not found at {SRC}")
    DST.mkdir(exist_ok=True)
    (DST / ".nojekyll").touch()  # ensure Pages serves files starting with '_' etc.

    copied = []
    for path in sorted(SRC.iterdir()):
        if path.is_file() and path.suffix.lower() in SUFFIXES:
            shutil.copy2(path, DST / path.name)
            copied.append(path.name)

    print(f"Published {len(copied)} files from frontend/ -> docs/:")
    for name in copied:
        print(f"  {name}")
    print("\ndocs/index.html is now the live analysis app (offline demo).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
