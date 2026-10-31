#!/usr/bin/env python3
"""Publish the post-race review to docs/ for GitHub Pages, in static mode.

``frontend/`` is the source of truth; ``docs/`` holds copies GitHub Pages serves
(Settings > Pages: deploy from the ``main`` branch, folder ``/docs``). This copies the
review page, the stylesheets it links and every JS module it imports, then writes
``docs/index.html``, which opens the review on an exported session::

    docs/review/index.html     <- frontend/review/index.html
    docs/css/*.css             <- the stylesheets the page links
    docs/js/**.js              <- review-app.js and everything it imports
    docs/data/<name>/          <- scripts/export_session.py (left alone here)
    docs/index.html            -> review/?static=../data/<name>/

Export the session first, then build::

    python scripts/export_session.py --ibt race.ibt --journal run.jsonl --out docs/data/spa
    python scripts/build_pages.py

``docs/review``, ``docs/css`` and ``docs/js`` are rebuilt from scratch each time, so a
file deleted from ``frontend/`` disappears from them too. The hand-written
``docs/*.md`` and anything else in ``docs/`` are not touched.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend"
DST = ROOT / "docs"
PAGE = "review/index.html"
#: The folders this script owns inside docs/.
OWNED = ("review", "css", "js")

_IMPORT = re.compile(
    r"""^\s*(?:import|export)[^'"]*?from\s*['"]([^'"]+)['"]|^\s*import\s*['"]([^'"]+)['"]""",
    re.M,
)
_ASSET = re.compile(r"""(?:href|src)=["']([^"':]+\.(?:css|js))["']""")


def page_assets(page: Path) -> list[Path]:
    """The stylesheets and scripts ``page`` links, as files under frontend/."""
    out = []
    for ref in _ASSET.findall(page.read_text("utf-8")):
        if ref.startswith("/"):
            raise SystemExit(f"{page}: absolute asset path {ref} breaks under the Pages subpath")
        path = (page.parent / ref).resolve()
        if not path.is_file():
            raise SystemExit(f"{page}: links missing file {ref}")
        out.append(path)
    return out


def module_graph(entries: list[Path]) -> list[Path]:
    """Every JS module reachable from ``entries`` through relative imports."""
    seen: dict[Path, None] = {}
    todo = [p for p in entries if p.suffix == ".js"]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen[path] = None
        for a, b in _IMPORT.findall(path.read_text("utf-8")):
            spec = a or b
            if not spec.startswith(("./", "../")):
                raise SystemExit(f"{path}: bare import {spec} cannot be served statically")
            target = (path.parent / spec).resolve()
            if not target.is_file():
                raise SystemExit(f"{path}: import {spec} not found")
            todo.append(target)
    return sorted(seen)


def exports(dst: Path) -> list[str]:
    data = dst / "data"
    return sorted(p.parent.name for p in data.glob("*/export.json")) if data.is_dir() else []


def index_html(name: str) -> str:
    target = html.escape(f"review/?static=../data/{name}/", quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Racing Telemetry Visualiser &mdash; post-race review</title>
<meta http-equiv="refresh" content="0; url={target}">
</head>
<body>
<p>Opening the post-race review: <a href="{target}">{target}</a></p>
</body>
</html>
"""


def build(src: Path = SRC, dst: Path = DST, default: str | None = None) -> list[str]:
    page = src / PAGE
    if not page.is_file():
        raise SystemExit(f"{page} not found")
    assets = page_assets(page)
    files = [page, *[a for a in assets if a.suffix == ".css"], *module_graph(assets)]

    names = exports(dst)
    if not names:
        raise SystemExit(
            "no export in docs/data/: run scripts/export_session.py --out docs/data/<name> first"
        )
    pick = default or ("spa" if "spa" in names else names[0])
    if pick not in names:
        raise SystemExit(f"no export docs/data/{pick}/ (have: {', '.join(names)})")

    dst.mkdir(exist_ok=True)
    for name in OWNED:
        shutil.rmtree(dst / name, ignore_errors=True)
    copied = []
    for path in files:
        rel = path.resolve().relative_to(src.resolve())
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(rel.as_posix())
    (dst / ".nojekyll").touch()  # serve the files as they are, no Jekyll pass

    (dst / "index.html").write_text(index_html(pick), encoding="utf-8")
    return sorted(copied)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--default", help="Export docs/index.html opens (default: spa)")
    args = ap.parse_args(argv)
    copied = build(SRC.resolve(), DST, args.default)
    print(f"Published {len(copied)} files from frontend/ -> docs/:")
    for rel in copied:
        print(f"  {rel}")
    print(f"exports: {', '.join(exports(DST))}; docs/index.html opens the default one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
