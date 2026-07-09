"""Run the coaching eval harness over a golden set of laps.

For each case: build the deterministic findings from the local store, run the multi-agent
Claude coach, then score it — deterministically (factuality vs ground truth) and, unless
disabled, with the LLM judge (clarity / actionability / grounding).

    python evals/run.py                 # uses evals/golden_laps.json
    python evals/run.py --no-judge      # skip the LLM judge (still needs a key for the coach)
    python evals/run.py --cases my.json

Needs a populated store (import a lap first) and ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evals.checks import aggregate, cross_check  # noqa: E402
from rtv.coaching.features import CoachingService  # noqa: E402
from rtv.config import get_settings  # noqa: E402
from rtv.store.duck import Database  # noqa: E402
from rtv.store.repository import Repository  # noqa: E402


def _findings(case: dict) -> dict:
    settings = get_settings()
    db = Database(settings.duckdb_path)
    try:
        repo = Repository(db, settings.parquet_dir)
        svc = CoachingService(repo)
        return svc.lap_findings(case["session_id"], case["main_lap"], case["ref_lap"]).to_dict()
    finally:
        db.close()


async def _run(args) -> int:
    cases = json.loads(Path(args.cases).read_text())
    cases = [c for c in cases if not str(c.get("name", "")).startswith("_")]
    if not cases:
        print(f"No cases in {args.cases}. Fill in real session_id / main_lap / ref_lap.")
        return 1
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — the coach needs it. See README.")
        return 1

    from anthropic import AsyncAnthropic

    from rtv.coaching.orchestrator import CoachOrchestrator

    client = AsyncAnthropic()
    model = get_settings().coaching_model
    orch = CoachOrchestrator(client=client, model=model)

    rows: list[dict] = []
    for case in cases:
        findings = _findings(case)
        result = (await orch.coach(findings)).model_dump()
        score = cross_check(result, findings)
        rows.append(score)
        print(f"\n# {case.get('name', case['session_id'])}")
        print(f"  outcome_ok={score['outcome_accuracy']}  "
              f"factuality={score['claim_factuality']:.2f}  "
              f"({score['supported']}/{score['claims']} claims)")
        for issue in score["issues"]:
            print(f"   ! {issue}")
        if not args.no_judge:
            from evals.judges import judge

            j = await judge(result, findings, client, model=model)
            print(f"  judge: clarity={j.clarity} action={j.actionability} "
                  f"grounding={j.grounding} — {j.comment}")

    agg = aggregate(rows)
    print("\n=== SUMMARY ===")
    for k, v in agg.items():
        print(f"  {k}: {v}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Coaching eval harness.")
    p.add_argument("--cases", default=str(ROOT / "evals" / "golden_laps.json"))
    p.add_argument("--no-judge", action="store_true", help="Skip the LLM judge.")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
