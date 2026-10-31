"""Compare the pitwall agents across race-journal runs, side by side.

Reads two (or more) journals written by the session recorder
(``data/sessions/<id>/runs/*.jsonl``) and prints model calls, agent runs, refusals,
validator failures before and after repair, latency per agent, tokens, calls by
trigger and late or expired radio calls. Free: it reads files, it calls no model.

Run:
  python scripts/compare_runs.py BEFORE.jsonl AFTER.jsonl
  python scripts/compare_runs.py BEFORE.jsonl AFTER.jsonl --labels "run 2" "run 3"
  python scripts/compare_runs.py BEFORE.jsonl AFTER.jsonl --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make src importable when run directly.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from rtv.store.run_stats import compare_markdown, load_run_stats  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("journals", nargs="+", type=Path, help="journal .jsonl files, oldest first")
    parser.add_argument("--labels", nargs="+", help="column names (default: run ids)")
    parser.add_argument("--json", action="store_true", help="print the raw statistics as JSON")
    parser.add_argument("--out", type=Path, help="also write the output to this file")
    args = parser.parse_args(argv)

    missing = [p for p in args.journals if not p.is_file()]
    if missing:
        parser.error("no such journal: " + ", ".join(map(str, missing)))
    if args.labels and len(args.labels) != len(args.journals):
        parser.error("--labels needs one label per journal")

    stats = [load_run_stats(p) for p in args.journals]
    if args.labels:
        for s, label in zip(stats, args.labels, strict=True):
            s["label"] = label
    if args.json:
        text = json.dumps(stats, indent=2) + "\n"
    else:
        text = compare_markdown(stats, args.labels)
    sys.stdout.write(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
