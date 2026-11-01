"""Cut a real race journal into an eval case set, or rebuild a set's offline baseline.

    python scripts/build_real_evals.py --journal <run>.jsonl --out evals/real/spa_run
    python scripts/build_real_evals.py --baseline spa_run          # after a check change
    python scripts/build_real_evals.py --diff spa_run              # what would change

The journal must be schema 3 or later (``run_start`` ``state_full`` and ``validate``
``output_full``): record one with a Spa replay with agents on (``RTV_PITWALL_AGENTS=true``)
and take the newest file under ``data/sessions/<id>/runs/``. Every run with an answer becomes
one case; ``--max-cases`` keeps the first N per agent when a set must be smaller. Writing a set
also writes its ``baseline.json``. Free: no model is called.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evals.real import offline  # noqa: E402
from evals.real.cases import extract_cases, set_dir, write_case_set  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--journal", type=Path, help="A schema-3 race journal (.jsonl).")
    p.add_argument("--out", type=Path, help="The case set folder to write.")
    p.add_argument("--max-cases", type=int, default=0, help="Keep the first N cases per agent.")
    p.add_argument("--label", default="", help="Where the journal is kept, for source.json.")
    p.add_argument("--baseline", default="", help="Rebuild this set's baseline.json only.")
    p.add_argument("--diff", default="", help="Print how this set's baseline would change.")
    args = p.parse_args()

    if args.diff:
        lines = offline.diff(
            offline.read_baseline(args.diff), offline.to_baseline(offline.score_set(args.diff))
        )
        print("\n".join(lines) if lines else "No change.")
        return 0
    if args.baseline:
        path = offline.write_baseline(args.baseline)
        print(f"Wrote {path}")
        return 0
    if not args.journal or not args.out:
        p.error("--journal and --out are needed to cut a case set")

    cases = extract_cases(args.journal)
    if not cases:
        print("No schema-3 agent runs with an answer in that journal.")
        return 1
    if args.max_cases:
        kept: dict[str, int] = {}
        trimmed = []
        for case in cases:
            kept[case["agent"]] = kept.get(case["agent"], 0) + 1
            if kept[case["agent"]] <= args.max_cases:
                trimmed.append(case)
        cases = trimmed
    source = {
        "journal": args.label or args.journal.name,
        "journal_sha256": _sha256(args.journal),
        "session_id": cases[0].get("session_id"),
        "cases": len(cases),
    }
    out = write_case_set(cases, set_dir(args.out), source=source)
    path = offline.write_baseline(out)
    print(f"Wrote {len(cases)} cases to {out} and {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
