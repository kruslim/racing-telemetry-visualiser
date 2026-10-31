"""Run the coaching eval harness over a golden set of laps.

For each case: build the deterministic findings from the local store, run the multi-agent
Kimi coach, then score it — deterministically (factuality vs ground truth) and, unless
disabled, with the LLM judge (clarity / actionability / grounding).

    python evals/run.py                 # uses evals/golden_laps.json
    python evals/run.py --no-judge      # skip the LLM judge (still needs a key for the coach)
    python evals/run.py --cases my.json
    python evals/run.py --judge-provider gemini,kimi   # grade the same plans with both

Needs a populated store (import a lap first) and the LLM key (KIMI_CODE_API_KEY by
default; see rtv.llm). The judge runs on Gemini by default (GEMINI_API_KEY; see
evals/judge_provider.py), so Kimi does not grade its own coach.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
    from rtv import llm

    if not llm.is_configured():
        print(f"{llm.key_env_var()} not set — the coach needs it. See README.")
        return 1

    from rtv.coaching.orchestrator import CoachOrchestrator
    from rtv.pitwall.provider import KimiProvider

    provider = KimiProvider()
    model = llm.model("reasoning")
    orch = CoachOrchestrator(provider, model=model)

    judges = []
    if not args.no_judge:
        from evals.judge_provider import JudgeConfigError, judge_backend

        names = [n.strip() for n in (args.judge_provider or "").split(",") if n.strip()]
        try:
            # A model id names one provider's model, so it only applies to a single judge.
            model_id = args.judge_model if len(names) <= 1 else None
            judges = [judge_backend(n, model_id) for n in names or [None]]
        except JudgeConfigError as exc:
            print(f"judge: {exc}")
            return 1
    scores: dict[str, list[dict]] = {j.label: [] for j in judges}

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
        for jb in judges:
            from evals.judges import judge

            j = await judge(result, findings, jb.provider, model=jb.model)
            name = case.get("name", case["session_id"])
            if j is None:
                print(f"  judge {jb.label}: no valid score returned")
                scores[jb.label].append({"case": name, "score": None})
            else:
                print(f"  judge {jb.label}: clarity={j.clarity} action={j.actionability} "
                      f"grounding={j.grounding} — {j.comment}")
                scores[jb.label].append({"case": name, "score": j.model_dump()})

    agg = aggregate(rows)
    print("\n=== SUMMARY ===")
    for k, v in agg.items():
        print(f"  {k}: {v}")
    if scores:
        print("\n" + judge_table(scores))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": agg, "judges": scores}, indent=2))
    return 0


def judge_table(scores: dict[str, list[dict]]) -> str:
    """A Markdown table of judge scores, one row per case and judge, plus means."""
    lines = [
        "| Judge | Case | Clarity | Actionability | Grounding |",
        "|---|---|---|---|---|",
    ]
    for label, entries in scores.items():
        valid = [e["score"] for e in entries if e["score"]]
        for e in entries:
            s = e["score"]
            cells = (
                [str(s["clarity"]), str(s["actionability"]), str(s["grounding"])]
                if s else ["no score"] * 3
            )
            lines.append(f"| {label} | {e['case']} | " + " | ".join(cells) + " |")
        if valid:
            means = [
                f"{sum(v[k] for v in valid) / len(valid):.2f}"
                for k in ("clarity", "actionability", "grounding")
            ]
            lines.append(f"| {label} | **mean ({len(valid)})** | " + " | ".join(means) + " |")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Coaching eval harness.")
    p.add_argument("--cases", default=str(ROOT / "evals" / "golden_laps.json"))
    p.add_argument("--no-judge", action="store_true", help="Skip the LLM judge.")
    p.add_argument(
        "--judge-provider",
        default="",
        help="gemini, anthropic or kimi; comma-separate to grade with several "
        "(default: RTV_JUDGE_PROVIDER, else gemini).",
    )
    p.add_argument(
        "--judge-model", default=None, help="Judge model id (RTV_JUDGE_MODEL); one provider only."
    )
    p.add_argument("--out", default="", help="Write the summary and judge scores as JSON.")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
