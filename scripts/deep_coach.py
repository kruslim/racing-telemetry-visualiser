"""Run the multi-agent Claude coaching pipeline on a stored lap.

Builds the deterministic findings straight from the local store (no server needed),
then runs the fan-out -> synthesise -> verify orchestration over the Claude API.

Examples:
    # Just the deterministic findings (no API key needed):
    python scripts/deep_coach.py <session_id> <main_lap> <ref_lap> --findings-only

    # Full multi-agent coaching (needs ANTHROPIC_API_KEY):
    python scripts/deep_coach.py <session_id> <main_lap> <ref_lap>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from rtv.coaching.features import CoachingService  # noqa: E402
from rtv.config import get_settings  # noqa: E402
from rtv.store.duck import Database  # noqa: E402
from rtv.store.repository import Repository  # noqa: E402


def build_findings(session_id: str, main_lap: int, ref_lap: int) -> dict:
    settings = get_settings()
    db = Database(settings.duckdb_path)
    try:
        repo = Repository(db, settings.parquet_dir)
        return CoachingService(repo).lap_findings(session_id, main_lap, ref_lap).to_dict()
    finally:
        db.close()


def _print_findings(f: dict) -> None:
    ch = f["chief"]
    print(f"\nLap {f['main_lap']} vs {f['ref_lap']}  ({f.get('track_name') or f.get('track_id')})")
    print(f"  net lap delta: {ch['net_lap']:+.3f}s   time available: {ch['lost']:.3f}s   "
          f"losing {ch['losing_n']}/{ch['total']} corners")
    for i, p in enumerate(ch["top3"], 1):
        print(f"  {i}. {p['label']}  +{p['gain']:.3f}s  — {p['why']}")
    for note in f.get("notes", []):
        print(f"  note: {note}")


def _print_result(result) -> None:
    if result.clean:
        print(f"\n{result.message}")
        return
    plan = result.plan
    print(f"\n=== Coach ({result.model}) ===")
    print(f"  {plan.headline}")
    print(f"  Next-lap focus: {plan.one_lap_focus}")
    for i, p in enumerate(plan.priorities, 1):
        print(f"  {i}. {p.corner}  +{p.gain_s:.3f}s — {p.why}")
    print("\n  Specialist cues:")
    for a in result.advices:
        print(f"   [{a.corner}] {a.diagnosis}")
        for cue in a.cues:
            print(f"     - {cue}")
    v = result.verdict
    if v:
        ok = sum(1 for c in v.claims if c.supported)
        print(f"\n  Verifier: {ok}/{len(v.claims)} claims supported by the findings")
        for c in v.claims:
            if not c.supported:
                print(f"   ✗ REJECTED [{c.corner}] {c.text} — {c.reason}")


async def _run(args) -> int:
    findings = build_findings(args.session_id, args.main_lap, args.ref_lap)
    if args.json and args.findings_only:
        print(json.dumps(findings, indent=2))
        return 0
    _print_findings(findings)
    if args.findings_only:
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\nANTHROPIC_API_KEY not set — re-run with --findings-only, or export the key.")
        return 1

    from rtv.coaching.orchestrator import CoachOrchestrator

    orch = CoachOrchestrator(model=get_settings().coaching_model)
    result = await orch.coach(findings)
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        _print_result(result)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Multi-agent Claude coaching for a stored lap.")
    p.add_argument("session_id")
    p.add_argument("main_lap", type=int)
    p.add_argument("ref_lap", type=int)
    p.add_argument("--findings-only", action="store_true", help="Skip the API; show findings only.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a readable report.")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
