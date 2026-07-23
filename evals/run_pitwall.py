"""Run the STRATEGIST eval harness over the generated golden set.

For each case: hand the agent the exact race state and triggering event it would
have seen live, let it call its real tools, then score the answer against the
deterministic race state -- grounding, radio discipline, whether the stop lap is
inside the fuel window, whether the claimed rejoin matches the projection, and
whether the decision is inside the defensible band.

    python evals/run_pitwall.py                 # the real strategist (needs a key)
    python evals/run_pitwall.py --dry-run       # cases + ground truth only, no key
    python evals/run_pitwall.py --model claude-opus-4-8

Needs ANTHROPIC_API_KEY unless --dry-run. It does *not* need iRacing or a
populated store: the golden set is generated from the scripted race.
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

from evals.pitwall_cases import build_cases  # noqa: E402
from evals.pitwall_checks import aggregate, cross_check, expected_calls  # noqa: E402
from rtv.pitwall.agents.strategist import STRATEGIST  # noqa: E402
from rtv.pitwall.framework import AgentRuntime  # noqa: E402
from rtv.racestate.strategy_math import DEFAULT_PIT_LANE_LOSS_S  # noqa: E402


def _dry_run(cases) -> int:
    print(f"{len(cases)} cases (no model called)\n")
    for case in cases:
        print(f"# {case.name}")
        print(f"  {json.dumps(case.describe(), default=str)}")
        print(f"  defensible calls: {sorted(expected_calls(case.state))}")
        if case.note:
            print(f"  note: {case.note}")
    return 0


async def _run(args) -> int:
    cases = build_cases()
    if args.dry_run:
        return _dry_run(cases)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set. Use --dry-run to inspect the cases offline.")
        return 1

    from rtv.pitwall.provider import AnthropicProvider

    spec = replace(STRATEGIST, model=args.model) if args.model else STRATEGIST
    runtime = AgentRuntime(
        spec,
        AnthropicProvider(),
        tool_config={"pit_lane_loss_s": args.pit_loss, "standings_window": 3},
    )

    rows: list[dict] = []
    for case in cases:
        message = await runtime.invoke(case.event, case.state, events=case.events)
        if message is None:
            print(f"\n# {case.name}\n  ! the agent produced nothing")
            continue
        score = cross_check(
            message.to_api(), case.state, pit_lane_loss_s=args.pit_loss
        )
        rows.append(score)
        print(f"\n# {case.name}  ({case.event.key}, lap {case.event.lap})")
        print(f'  radio: "{message.spoken_text}"')
        print(
            f"  call={score['recommendation']} score={score['score']:.2f} "
            f"grounded={score['grounding_ok']} decision={score['decision_ok']} "
            f"rejoin={score['rejoin_ok']} window={score['window_ok']}"
        )
        if message.refused:
            print("  (grounded refusal)")
        for issue in score["issues"]:
            print(f"   ! {issue}")

    print("\n=== SUMMARY ===")
    for key, value in aggregate(rows).items():
        print(f"  {key}: {value}")
    print(f"  agent stats: {runtime.stats()}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Pitwall strategist eval harness.")
    p.add_argument("--dry-run", action="store_true", help="Show cases + ground truth only.")
    p.add_argument("--model", default="", help="Override the strategist's model.")
    p.add_argument(
        "--pit-loss", type=float, default=DEFAULT_PIT_LANE_LOSS_S,
        help="Pit-lane time loss in seconds used by the projection.",
    )
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
