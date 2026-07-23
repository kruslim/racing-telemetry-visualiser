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
    python evals/run_pitwall.py --model claude-opus-4-8

Needs ANTHROPIC_API_KEY unless --dry-run. It does *not* need iRacing or a
populated store: the golden sets are generated from the scripted race.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evals.pitwall_cases import build_agent_cases, build_all_cases  # noqa: E402
from evals.pitwall_checks import aggregate, check_for_agent, expected_calls  # noqa: E402
from rtv.pitwall.agents import AGENT_REGISTRY  # noqa: E402
from rtv.pitwall.framework import AgentRuntime  # noqa: E402
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
    from rtv.pitwall.provider import AnthropicProvider

    spec = SPECS[agent]
    if args.model:
        spec = replace(spec, model=args.model)
    runtime = AgentRuntime(
        spec,
        AnthropicProvider(),
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


async def _run(args) -> int:
    if args.agent:
        if args.agent not in SPECS:
            print(f"Unknown agent {args.agent!r}. Known: {', '.join(sorted(SPECS))}")
            return 1
        sets = {args.agent: build_agent_cases(args.agent)}
    else:
        sets = build_all_cases()

    if args.dry_run:
        return _dry_run(sets)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set. Use --dry-run to inspect the cases offline.")
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
        "--pit-loss", type=float, default=DEFAULT_PIT_LANE_LOSS_S,
        help="Pit-lane time loss in seconds used by the projection.",
    )
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
