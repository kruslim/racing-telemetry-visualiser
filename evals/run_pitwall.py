"""Run the pitwall eval harness over the generated golden sets.

For each case: hand the agent the exact race state and triggering event it would
have seen live, let it call its real tools, then score the answer against the
deterministic race state. What "scored" means depends on the role -- a pit call is
checked against the fuel window and the rejoin projection, an engineer advisory
against the trigger that fired, a spotter call against the car and gap in the
event, a coaching cue against the corner the pattern is actually at.

    python evals/run_pitwall.py                     # every agent (needs a key)
    python evals/run_pitwall.py --dry-run           # cases + ground truth, no key
    python evals/run_pitwall.py --agent spotter
    python evals/run_pitwall.py --model kimi-k3
    python evals/run_pitwall.py --real spa_run      # re-execute the real Spa runs
    python evals/run_pitwall.py --real spa_run --judge-provider gemini --out live.json

Needs the LLM key (KIMI_CODE_API_KEY by default) unless --dry-run. It does *not* need iRacing or a
populated store: the golden sets are generated from the scripted race, and ``--real`` cases
(``evals/real/<set>/``) carry the recorded state and tool results. ``--real`` re-executes
each recorded input against today's prompt and model, scores it with the same checks as the
offline replay (``tests/test_real_evals.py``) and prints ``spec_drift`` per case.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evals.pitwall_cases import build_agent_cases, build_all_cases  # noqa: E402
from evals.pitwall_checks import aggregate, check_for_agent, expected_calls  # noqa: E402
from rtv.pitwall.agents import AGENT_REGISTRY  # noqa: E402
from rtv.pitwall.framework import AgentRuntime, agent_model  # noqa: E402
from rtv.racestate.strategy_math import DEFAULT_PIT_LANE_LOSS_S  # noqa: E402

SPECS = {spec.name: spec for spec in AGENT_REGISTRY}


def _dry_run(sets: dict[str, list]) -> int:
    total = sum(len(c) for c in sets.values())
    print(f"{total} cases across {len(sets)} agents (no model called)\n")
    for agent, cases in sets.items():
        print(f"=== {agent} ({len(cases)} cases) ===")
        for case in cases:
            print(f"# {case.name}")
            print(f"  {json.dumps(case.describe(), default=str)}")
            if agent == "strategist":
                print(f"  defensible calls: {sorted(expected_calls(case.state))}")
            if case.note:
                print(f"  note: {case.note}")
        print()
    return 0


def _kwargs_for(agent: str, case, pit_loss: float) -> dict:
    if agent == "strategist":
        return {"pit_lane_loss_s": pit_loss}
    if agent == "vehicle_engineer":
        # The scripted session is never written to the store, so no CarSetup is
        # read back from it; the scenario's session-info document does carry one.
        return {"setup_available": True}
    if agent == "coach":
        # Nothing is recorded during an eval run, so get_corner_detail's
        # telemetry half is unavailable by construction -- which is exactly the
        # case worth scoring: a coach must not claim a trace it never saw.
        return {"telemetry_available": False}
    return {}


async def _run_agent(agent: str, cases: list, args) -> list[dict]:
    from rtv.pitwall.provider import KimiProvider

    spec = SPECS[agent]
    spec = replace(spec, model=agent_model(spec, override=args.model))
    runtime = AgentRuntime(
        spec,
        KimiProvider(),
        tool_config={
            "pit_lane_loss_s": args.pit_loss,
            "standings_window": 3,
        },
    )

    rows: list[dict] = []
    print(f"\n########## {agent} ({spec.model}) ##########")
    for case in cases:
        message = await runtime.invoke(case.event, case.state, events=case.events)
        if message is None:
            print(f"\n# {case.name}\n  ! the agent produced nothing")
            continue
        score = check_for_agent(
            agent,
            message.to_api(),
            case.state,
            case.event,
            **_kwargs_for(agent, case, args.pit_loss),
        )
        rows.append({**score, "agent": agent})
        print(f"\n# {case.name}  ({case.event.key}, lap {case.event.lap})")
        print(f'  radio: "{message.spoken_text}"')
        flags = " ".join(
            f"{k.replace('_ok', '')}={v}" for k, v in score.items() if k.endswith("_ok")
        )
        print(f"  score={score['score']:.2f} {flags}")
        if message.refused:
            print("  (grounded refusal)")
        for issue in score["issues"]:
            print(f"   ! {issue}")
    print(f"\n  {agent} stats: {runtime.stats()}")
    return rows


async def _run_real(args) -> int:
    from evals.real.cases import load_case_set
    from evals.real.live import aggregate as live_aggregate
    from evals.real.live import run_case

    cases = load_case_set(args.real)
    if args.agent:
        cases = [c for c in cases if c["agent"] == args.agent]
    if args.limit:
        cases = cases[: args.limit]
    if args.dry_run:
        from evals.real.offline import score_case

        print(f"{len(cases)} recorded cases from {args.real} (no model called)\n")
        for case in cases:
            row = score_case(case)
            print(f"# {row['case_id']}  score={row['checks']['score']:.2f}")
            print(f'  recorded: "{(case["answers"][-1]["output"] or {}).get("spoken_text")}"')
        return 0
    from rtv import llm
    from rtv.pitwall.provider import KimiProvider

    if not llm.is_configured():
        print(f"{llm.key_env_var()} not set. Use --dry-run to inspect the cases offline.")
        return 1
    judges = []
    for name in [p.strip() for p in args.judge_provider.split(",") if p.strip()]:
        from evals.judge_provider import judge_backend

        judges.append(judge_backend(name, args.judge_model or None))
    provider = KimiProvider()
    tool_config = {"pit_lane_loss_s": args.pit_loss, "standings_window": 3}
    rows = []
    for case in cases:
        row = await run_case(
            case, provider, model=args.model, tool_config=tool_config, judges=judges
        )
        rows.append(row)
        drift = ",".join(row["spec_drift"]) or "none"
        print(f"\n# {row['case_id']}  ({row['model']}, drift: {drift})")
        print(f'  recorded: "{row["recorded_spoken"]}"  score={row["recorded_score"]:.2f}')
        if row.get("new") is None:
            print(f"  ! {row.get('error')}")
            continue
        print(f'  new:      "{row["new_spoken"]}"  score={row["new_score"]:.2f}')
        for issue in row["new"]["check_issues"] + row["new"]["corners"]:
            print(f"   ! {issue}")
        for j in row.get("judges") or []:
            if j is None:
                print("  judge: no valid score")
                continue
            print(
                f"  judge {j['judge']}: usefulness={j['usefulness']} "
                f"actionability={j['actionability']} grounding={j['grounding']}"
            )
        if row["unrecorded_tool_calls"]:
            print(f"  unrecorded tool calls: {row['unrecorded_tool_calls']}")
    summary = live_aggregate(rows)
    print("\n=== SUMMARY ===")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    if args.out:
        Path(args.out).write_text(
            json.dumps({"summary": summary, "rows": rows}, indent=1, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {args.out}")
    return 0


async def _run(args) -> int:
    if args.real:
        return await _run_real(args)
    if args.agent:
        if args.agent not in SPECS:
            print(f"Unknown agent {args.agent!r}. Known: {', '.join(sorted(SPECS))}")
            return 1
        sets = {args.agent: build_agent_cases(args.agent)}
    else:
        sets = build_all_cases()

    if args.dry_run:
        return _dry_run(sets)
    from rtv import llm

    if not llm.is_configured():
        print(f"{llm.key_env_var()} not set. Use --dry-run to inspect the cases offline.")
        return 1

    rows: list[dict] = []
    for agent, cases in sets.items():
        rows.extend(await _run_agent(agent, cases, args))

    print("\n=== SUMMARY ===")
    for agent in sets:
        per_agent = [r for r in rows if r["agent"] == agent]
        if per_agent:
            print(f"  -- {agent} --")
            for key, value in aggregate(per_agent).items():
                print(f"    {key}: {value}")
    print("  -- all agents --")
    for key, value in aggregate(rows).items():
        print(f"    {key}: {value}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Pitwall agent eval harness.")
    p.add_argument("--dry-run", action="store_true", help="Show cases + ground truth only.")
    p.add_argument(
        "--agent", default="", help=f"One of: {', '.join(sorted(SPECS))}. Default: all."
    )
    p.add_argument("--model", default="", help="Override the agent's model.")
    p.add_argument(
        "--real", default="",
        help="Re-execute a recorded case set from evals/real/ (e.g. spa_run) instead of the "
        "generated golden sets.",
    )
    p.add_argument("--limit", type=int, default=0, help="--real: only the first N cases.")
    p.add_argument(
        "--judge-provider", default="",
        help="--real: advisory LLM judge(s), e.g. gemini or gemini,kimi. Default: none.",
    )
    p.add_argument("--judge-model", default="", help="--real: judge model override.")
    p.add_argument("--out", default="", help="--real: write the rows and summary as JSON.")
    p.add_argument(
        "--pit-loss", type=float, default=DEFAULT_PIT_LANE_LOSS_S,
        help="Pit-lane time loss in seconds used by the projection.",
    )
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
