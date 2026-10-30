"""Offline smoke test of the pitwall agent layer (no iRacing, no API key).

Replays the scripted synthetic race through the real race-state engine, routes the
resulting events through the real :class:`PitwallOrchestrator`, and drives the real
:class:`AgentRuntime` -- tool loop, citation validator, radio queue and all -- with
a scripted provider standing in for Claude.

It then asserts the things that would actually go wrong in a race:

    1. the strategist wakes on the scripted strategy events, and only those
    2. it really calls its deterministic tools, and cites what they returned
    3. a fabricated number is caught in-loop and becomes a grounded refusal
    4. critical calls pre-empt queued advisories on the single radio channel
    5. two advisories about the same stop collapse to the newest one
    6. the kill switch means zero model calls, not merely a quiet radio

Run:  python scripts/smoke_pitwall_agents.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from rtv.pitwall.agents.strategist import STRATEGIST, PitCall, priority_for  # noqa: E402
from rtv.pitwall.framework import RadioPriority  # noqa: E402
from rtv.pitwall.orchestrator import PitwallOrchestrator  # noqa: E402
from rtv.pitwall.provider import ScriptedProvider, tool_use_turn  # noqa: E402
from rtv.pitwall.radio import RadioFeed  # noqa: E402
from rtv.racestate import RaceStateEngine  # noqa: E402
from rtv.racestate.detectors import DEFAULT_CONFIG  # noqa: E402
from rtv.racestate.scenario import (  # noqa: E402
    ScenarioSpec,
    scenario_catalog,
    scenario_frames,
    scenario_session_info,
)

#: A stint milestone every 2 green laps so the 6-lap scenario reaches one.
CONFIG = replace(DEFAULT_CONFIG, stint_milestone_laps=2)

#: The strategy events the scripted race is expected to raise, in order.
EXPECTED_TRIGGERS = [
    "fuel_margin_low",
    "stint_lap_milestone",
    "flag_change:yellow",
    "pit_window_open",
    "pit_window_closing",
    "fuel_critical",
]


# --------------------------------------------------------------------------
# a scripted "model" that behaves like a well-grounded strategist
# --------------------------------------------------------------------------
def _last_tool_results(messages) -> list[dict]:
    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, list):
            out = []
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                try:
                    out.append(json.loads(block["content"]))
                except (TypeError, ValueError, KeyError):
                    continue
            if out:
                return out
    return []


def grounded_strategist(*, turn, messages, output_model, **_):
    """Turn 0 calls two tools; turn 1 answers using only what they returned."""
    if turn == 0:
        return tool_use_turn(
            [("get_fuel_projection", {}), ("simulate_pit_outcome", {})]
        )
    fuel, outcome = ({}, {})
    for result in _last_tool_results(messages):
        if "pit_window_latest_lap" in result:
            fuel = result
        elif "rejoin_position" in result:
            outcome = result
    rejoin = outcome.get("rejoin_position")
    add = outcome.get("fuel_to_add_l")
    earliest = fuel.get("pit_window_earliest_lap")
    latest = fuel.get("pit_window_latest_lap")
    margin = fuel.get("margin_laps")

    if rejoin is None or add is None or not outcome.get("grounded"):
        return PitCall(
            priority=RadioPriority.INFO,
            spoken_text="Standby - no rejoin projection, I can't call a stop yet.",
            detail_text=f"simulate_pit_outcome is not grounded: {outcome.get('reason')}.",
            recommendation="hold",
            confidence="low",
            rationale="The deterministic projection reported missing inputs.",
        )

    if fuel.get("window_open"):
        call, confidence = "box_now", "high"
        spoken = f"Box this lap, {add:.1f} litres, we come out P{rejoin}."
        detail = (
            f"Window is lap {earliest} to {latest}. Adding {add:.1f} L covers "
            f"{outcome.get('laps_after_stop')} laps at {fuel.get('per_lap_l')} L per "
            f"lap. Rejoin P{rejoin}, {outcome.get('gap_to_car_ahead_after')}s behind "
            "the car ahead."
        )
    elif margin is not None and margin < 0:
        call, confidence = "extend", "medium"
        spoken = f"Stay out, we're {abs(margin):.1f} laps short. Window opens lap {earliest}."
        detail = (
            f"Fuel margin is {margin} laps, so a stop is owed, but the window does not "
            f"open until lap {earliest} - stopping now would need a second stop. "
            f"Stopping then projects P{rejoin}."
        )
    else:
        call, confidence = "stay_out", "high"
        spoken = f"No stop needed, {margin:.1f} laps in hand. Push on."
        detail = (
            f"Fuel margin is {margin} laps against {fuel.get('laps_to_finish')} laps to "
            f"run at {fuel.get('per_lap_l')} L per lap. Track position is worth more "
            f"than the {outcome.get('effective_loss_s')}s a stop costs."
        )

    return PitCall(
        priority=priority_for(call),
        spoken_text=spoken,
        detail_text=detail,
        recommendation=call,
        target_lap=earliest if call != "stay_out" else None,
        fuel_to_add_l=add if call != "stay_out" else None,
        rejoin_position=rejoin,
        confidence=confidence,
        rationale=f"Fuel margin {margin} laps; a stop projects P{rejoin}.",
        risks=["Assumes the field holds station over the stop."],
    )


def fabricating_strategist(*, turn, **_):
    """A strategist that invents a rejoin position nothing supports."""
    return PitCall(
        priority=RadioPriority.CRITICAL,
        spoken_text="Box now, 41.7 litres, you rejoin P19 just ahead of car 63.",
        detail_text="Rejoin P19 with a 7.3 second cushion to the car behind.",
        recommendation="box_now",
        target_lap=99,
        rejoin_position=19,
        confidence="high",
        rationale="Invented figures, none of which came from a tool.",
    )


def _fmt(value, spec=".2f", dash="-"):
    return dash if value is None else format(value, spec)


# --------------------------------------------------------------------------
async def main() -> int:
    spec = ScenarioSpec()
    catalog = scenario_catalog(spec)
    engine = RaceStateEngine(source="replay", config=CONFIG)
    engine.bind_catalog(catalog, session_id="smoke-scenario")
    engine.set_session_info(scenario_session_info(spec))

    provider = ScriptedProvider(grounded_strategist)
    feed = RadioFeed()
    orch = PitwallOrchestrator(
        engine, provider, [STRATEGIST], feed=feed,
        tool_config={"pit_lane_loss_s": 25.0}, clock=None,
    )

    # ---- 1 + 2: routing and the tool loop -------------------------------
    # Frame by frame, so every agent sees the state that was current when its
    # trigger fired -- replaying first and dispatching afterwards would score the
    # strategist against a race that had already finished.
    fired: list[str] = []
    messages = []
    emitted = []
    frames = 0
    for frame in scenario_frames(spec):
        seen = len(engine.bus.history())
        engine.on_frame(frame, catalog)
        frames += 1
        for event in engine.bus.history()[seen:]:
            produced = await orch.handle_event(event, engine.snapshot())
            if produced:
                fired.append(event.key)
                messages.extend(produced)
        emitted.extend(feed.pump(frame.session_time))
    events = engine.bus.history()

    print("=== replay ===")
    print(f"  {frames} frames, {len(events)} race events")

    print("\n=== radio feed ===")
    print(
        f"  {feed.published} published, {feed.superseded} superseded, "
        f"{len(emitted)} on air, {len(feed.pending())} still queued"
    )
    for message in emitted:
        print(
            f"  t={message.session_time:7.2f}s [{message.priority.value:<8}] "
            f"{message.agent}: {message.spoken_text}"
        )
        print(
            f"           tools={message.tools_used} grounded={message.grounded} "
            f"call={message.data.get('recommendation')} "
            f"rejoin={message.data.get('rejoin_position')}"
        )

    print("\n=== assertions ===")
    assert fired == EXPECTED_TRIGGERS, f"expected {EXPECTED_TRIGGERS}, got {fired}"
    print(f"  strategist woke on exactly the strategy events: {' -> '.join(fired)}")

    assert all(m.tools_used for m in messages), "an agent answered without calling a tool"
    assert all("simulate_pit_outcome" in m.tools_used for m in messages)
    print(f"  every call used its deterministic tools ({len(provider.calls)} model turns)")

    grounded = [m for m in messages if m.grounded and not m.refused]
    assert grounded, "no grounded messages were produced"
    boxed = [m for m in grounded if m.data.get("recommendation") == "box_now"]
    assert boxed, "the scripted race never produced a pit call"
    assert len(grounded) == len(messages), "a scripted-but-grounded call was rejected"
    print(
        f"  {len(grounded)}/{len(messages)} calls fully grounded; "
        f"pit call rejoin P{boxed[-1].data['rejoin_position']}, "
        f"{_fmt(boxed[-1].data['fuel_to_add_l'], '.1f')} L"
    )

    # Every critical call must have reached the air. Nothing else in the pitwall
    # matters if a "box now, you're on fumes" can be dropped for a tidier queue.
    criticals = [m for m in messages if m.priority is RadioPriority.CRITICAL]
    assert criticals, "the scripted race never produced a critical call"
    aired = {m.seq for m in emitted}
    missing = [m for m in criticals if m.seq not in aired]
    assert not missing, f"{len(missing)} critical calls never aired"
    print(
        f"  radio channel drained on the session clock: {feed.published} published, "
        f"{feed.superseded} superseded, {len(emitted)} aired, "
        f"all {len(criticals)} critical calls among them"
    )

    # ---- 3: a fabricated number becomes a grounded refusal ---------------
    liar = PitwallOrchestrator(
        engine, ScriptedProvider(fabricating_strategist), [STRATEGIST],
        feed=RadioFeed(), tool_config={"pit_lane_loss_s": 25.0}, clock=None,
    )
    window = next(e for e in events if e.key == "pit_window_open")
    out = await liar.handle_event(window, engine.snapshot())
    assert out, "the fabricating agent produced nothing at all"
    bad = out[0]
    assert bad.refused, "an invented rejoin position was allowed onto the radio"
    assert bad.priority is RadioPriority.INFO
    # Both halves are checked: the spoken figures and the structured payload the
    # pitwall UI would have rendered.
    assert "rejoin_position=19" in bad.ungrounded, bad.ungrounded
    assert "target_lap=99" in bad.ungrounded, bad.ungrounded
    assert "41.7" in bad.ungrounded, bad.ungrounded
    print(
        f"  fabricated figures caught in-loop {sorted(bad.ungrounded)} -> "
        f'grounded refusal: "{bad.spoken_text}"'
    )

    # ---- 4 + 5: radio discipline ----------------------------------------
    disc = RadioFeed()
    first = messages[0].model_copy(deep=True)
    first.priority, first.subject, first.spoken_text = (
        RadioPriority.ADVISORY, "pit_stop", "Window opens next lap.",
    )
    second = first.model_copy(deep=True)
    second.spoken_text = "Window now open, plan is lap five."
    critical = first.model_copy(deep=True)
    critical.priority, critical.subject = RadioPriority.CRITICAL, "fuel"
    critical.spoken_text = "Box box box, we are out of fuel."

    disc.publish(first)
    superseded = disc.publish(second)
    disc.publish(critical)
    order = disc.flush()
    assert superseded is not None and superseded.spoken_text == first.spoken_text
    assert [m.spoken_text for m in order] == [critical.spoken_text, second.spoken_text]
    print("  critical pre-empted the queue; the stale pit-window advisory was superseded")

    # ---- 6: the kill switch is a *cost* control, not a mute -------------
    counted = ScriptedProvider(grounded_strategist)
    off = PitwallOrchestrator(
        engine, counted, [STRATEGIST], feed=RadioFeed(), enabled=False, clock=None
    )
    for event in events:
        await off.handle_event(event, engine.snapshot())
    assert counted.calls == [], f"{len(counted.calls)} model calls made while disabled"
    print("  kill switch held: 0 model calls across the whole race")

    print("\nPitwall agent layer smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
