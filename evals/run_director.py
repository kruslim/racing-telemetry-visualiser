"""Rehearse a scripted race (a race-director scenario) against the pitwall agents.

The race director makes things happen that did not happen -- a caution, rain, a stop
made mandatory -- so it runs only here, from the eval path, never in the app. It walks
the script with :class:`~rtv.director.ScriptedDirector` over the synthetic director race
or a stored session (read-only), runs the agents inline, and writes everything to the
output directory you name: the journal (``<out>/<session_id>/runs/*-eval.jsonl``, source
"eval", the scenario name on the lifecycle record), ``report.json``, ``REPORT.md``,
``events.jsonl`` and ``radio.jsonl``. It refuses to write under ``data/sessions``.

    python evals/run_director.py --out eval-out/director              # offline, scripted
    python evals/run_director.py --out eval-out/director --live       # Kimi (quota)
    python evals/run_director.py --out eval-out/spa --session <id> --data-dir data --live
    python evals/run_director.py --scenario my_script.json --out eval-out/x --speed 4
    python evals/run_director.py --scenario incident_limit --out eval-out/rc  # race control

Offline (the default) the agents answer from a scripted provider, which needs no key and
proves the routing, the templates, the overlay, the plan checks and the grounding.
``--live`` needs KIMI_CODE_API_KEY and spends Kimi quota. A replayed ``.ibt`` has no
CarIdx channels, so traffic injections have no field there; the report says so.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from evals.director_rehearsal import (  # noqa: E402
    EXAMPLE_SCRIPT,
    SCENARIOS,
    rehearse,
    write_outputs,
)
from rtv.director import ScenarioScript  # noqa: E402


def _store_source(session_id: str, data_dir: Path):
    """A stored session as a replay source, read only (``store_source``)."""
    from rtv.racestate.replay import store_source
    from rtv.store.duck import Database
    from rtv.store.repository import Repository

    db = Database(data_dir / "telemetry.duckdb")
    repo = Repository(db, data_dir / "parquet")
    return store_source(
        db, data_dir / "parquet", session_id, session_info=repo.get_session_info(session_id)
    )


async def _run(args) -> int:
    script = ScenarioScript.load(SCENARIOS.get(args.scenario, args.scenario))
    if args.live:
        from rtv import llm

        if not llm.is_configured():
            print(f"{llm.key_env_var()} not set; run without --live for the offline rehearsal.")
            return 1
    if args.session:
        source = _store_source(args.session, Path(args.data_dir))
        label = f"store:{args.session}"
    else:
        source, label = None, "synthetic"
    out = Path(args.out)
    rehearsal = await rehearse(
        script, source, out_dir=out, live=args.live, speed=args.speed, source_label=label
    )
    paths = write_outputs(rehearsal, out)
    print(rehearsal.markdown())
    print(f"Journal: {rehearsal.journal}")
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0 if rehearsal.ok else 2


def main() -> int:
    p = argparse.ArgumentParser(description="Rehearse a race-director scenario (evals only).")
    p.add_argument(
        "--scenario", default=str(EXAMPLE_SCRIPT),
        help="ScenarioScript JSON, or a name: " + ", ".join(SCENARIOS) + ".",
    )
    p.add_argument("--out", required=True, help="Output directory (never data/sessions).")
    p.add_argument("--session", default="", help="Rehearse over this stored session.")
    p.add_argument("--data-dir", default="data", help="Data directory of --session.")
    p.add_argument("--live", action="store_true", help="Run the agents on Kimi (quota).")
    p.add_argument(
        "--speed", type=float, default=0.0,
        help="Pace frames at this multiple of real time (0 = flat out). Triggers are in "
        "session seconds either way.",
    )
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
