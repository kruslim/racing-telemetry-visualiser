"""Offline smoke test of the deterministic race-state engine (no iRacing, no API key).

Replays the scripted synthetic race as fast as possible through the *same*
``RaceStateEngine.on_frame`` ingress the live poller uses, prints the resulting
event log and race state, and asserts the scripted milestones fired in order:

    yellow flag -> lock-up -> pit window open -> pit entry -> pit exit

Run:  python scripts/smoke_pitwall.py
"""

from __future__ import annotations

import os
import sys
import time

# Make src importable when run directly.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from rtv.racestate import RaceStateEngine  # noqa: E402
from rtv.racestate.replay import ReplayDriver, scenario_source  # noqa: E402
from rtv.racestate.scenario import ScenarioSpec  # noqa: E402

#: The scripted milestones the scenario stages, in order.
EXPECTED = [
    "flag_change:yellow",
    "lockup",
    "pit_window_open",
    "pit_entry",
    "pit_exit",
]


def _fmt(value, spec: str = ".2f", dash: str = "-") -> str:
    return dash if value is None else format(value, spec)


def main() -> int:
    spec = ScenarioSpec()
    engine = RaceStateEngine(source="replay")

    started = time.perf_counter()
    frames = ReplayDriver(engine).run(scenario_source(spec), speed=0)
    elapsed = time.perf_counter() - started

    events = engine.bus.history()
    state = engine.snapshot()

    print("=== replay ===")
    print(
        f"  {frames} frames ({spec.laps} laps @ {spec.hz} Hz) replayed in "
        f"{elapsed:.2f}s = {frames / elapsed:,.0f} frames/s "
        f"({elapsed / frames * 1000:.4f} ms/frame wall)"
    )
    m = state.metrics
    print(
        f"  engine self-timing: avg {m.avg_update_ms:.4f} ms  max {m.max_update_ms:.4f} ms"
        f"  (budget 16 ms/frame)"
    )

    print("\n=== event log ===")
    for e in events:
        detail = ", ".join(f"{k}={v}" for k, v in e.payload.items() if v is not None)
        print(
            f"  t={e.session_time:7.2f}s lap={str(e.lap):>4}  "
            f"{e.key:<22} {e.severity.value:<9} {detail[:96]}"
        )

    print("\n=== final race state ===")
    p, f, t = state.player, state.fuel, state.tyres
    print(
        f"  player   P{p.position} lap {p.lap} stint {p.stint} "
        f"(started lap {p.stint_start_lap}), best {_fmt(p.best_lap_time, '.3f')}s"
    )
    print(
        f"  fuel     {_fmt(f.level, '.2f')} L of {_fmt(f.capacity, '.1f')} L | "
        f"{_fmt(f.per_lap, '.3f')} L/lap (n={f.samples}) | "
        f"{_fmt(f.laps_remaining, '.2f')} laps left | margin {_fmt(f.margin_l, '+.2f')} L | "
        f"window {f.pit_window_earliest_lap}-{f.pit_window_latest_lap} "
        f"(open={f.window_open})"
    )
    print(
        "  tyres    "
        + " ".join(f"{c}={_fmt(v, '.1f')}C" for c, v in sorted(t.temps.items()))
        + f" | stint laps {t.stint_laps}"
    )
    print(f"  flags    {state.flags.phase.value} for {state.flags.time_in_state:.1f}s")

    order = [c for c in state.standings.cars]
    print(
        f"  field    {len(order)} cars, gaps by {state.standings.gap_basis} "
        f"(ref lap {_fmt(state.standings.reference_lap_time, '.2f')}s)"
    )
    for c in order:
        tag = " <- player" if c.is_player else ""
        print(
            f"    P{str(c.position):<3} car {c.idx:<3} "
            f"ahead {_fmt(c.gap_ahead, '+.2f'):>7}s  behind {_fmt(c.gap_behind, '+.2f'):>7}s"
            f"{tag}"
        )

    off = [k for k, v in state.capabilities.model_dump().items()
           if v is False and k != "missing"]
    print(f"  capabilities off: {off or 'none'}")

    # ---- assertions -----------------------------------------------------
    print("\n=== assertions ===")
    milestones = [e.key for e in events if e.key in set(EXPECTED)]
    assert milestones == EXPECTED, f"expected {EXPECTED}, got {milestones}"
    print(f"  ordered milestone sequence OK: {' -> '.join(milestones)}")

    assert m.max_update_ms < 16.0, f"frame budget exceeded: {m.max_update_ms} ms"
    print(f"  frame budget OK: max {m.max_update_ms:.4f} ms < 16 ms")

    # Replaying the same session again must yield an identical event log.
    second = RaceStateEngine(source="replay")
    ReplayDriver(second).run(scenario_source(spec), speed=0)

    def signature(evs):
        return [
            (e.event_type, e.tick, e.session_time, e.lap, e.severity, e.payload)
            for e in evs
        ]

    assert signature(second.bus.history()) == signature(events), (
        "replay is not deterministic"
    )
    print(f"  replay determinism OK: {len(events)} events reproduced exactly")

    print("\nPitwall race-state engine smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
