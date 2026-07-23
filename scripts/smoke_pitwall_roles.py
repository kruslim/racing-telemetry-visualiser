"""Offline smoke test of the stage-3 pitwall roles (no iRacing, no API key).

Replays a variant of the scripted race -- one that locks up at the *same corner*
on three separate laps -- through the real race-state engine, routes the events
through the real :class:`PitwallOrchestrator` with all four agents mounted, and
drives the real :class:`AgentRuntime` with a scripted provider standing in for
Claude. A DuckDB/Parquet store is seeded from the same scenario so
``get_corner_detail`` exercises the genuine Layer-1 coaching service.

It asserts the things that would actually go wrong:

    1. each role wakes on its own triggers and on nobody else's
    2. a *single* lockup wakes no one; only the aggregated recurrence does
    3. get_corner_detail really reaches Layer-1 and returns measured numbers
    4. get_setup_snapshot really reads the session-info YAML
    5. tool scoping is enforced: an agent cannot call another agent's tool
    6. an invented tyre temperature becomes a grounded refusal
    7. four agents merge onto one channel and every critical call airs

Run:  python scripts/smoke_pitwall_roles.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from smoke_pitwall_agents import grounded_strategist  # noqa: E402

from rtv.coaching.features import CoachingService  # noqa: E402
from rtv.pitwall.agents import AGENT_REGISTRY  # noqa: E402
from rtv.pitwall.agents.coach import COACH, CoachCall  # noqa: E402
from rtv.pitwall.agents.spotter import SPOTTER, SpotterCall  # noqa: E402
from rtv.pitwall.agents.vehicle_engineer import (  # noqa: E402
    VEHICLE_ENGINEER,
    EngineerCall,
)
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
    seed_scenario_session,
)
from rtv.store.duck import Database  # noqa: E402
from rtv.store.repository import Repository  # noqa: E402
from rtv.store.writer import TelemetryWriter  # noqa: E402

SESSION_ID = "smoke-roles"

#: Lock up at the same corner on laps 1, 2 and 4 (lap 3 is the caution, so it
#: cannot), script a real corner just after the braking zone for Layer-1 to find,
#: and lower the tyre-drift threshold under the scenario's scripted 2 C/lap climb.
SPEC = ScenarioSpec(lockup_laps=(1, 2), corner_pcts=(0.44,))
CONFIG = replace(DEFAULT_CONFIG, stint_milestone_laps=2, tyre_temp_trend_c_per_lap=1.0)

EXPECTED_WAKES = {
    "strategist": [
        "fuel_margin_low",
        "stint_lap_milestone",
        "flag_change:yellow",
        "pit_window_open",
        "pit_window_closing",
        "fuel_critical",
    ],
    "vehicle_engineer": ["tyre_out_of_band", "recurring_issue", "pit_entry"],
    "spotter": ["flag_change:yellow"],
    "coach": ["stint_lap_milestone", "recurring_issue"],
}


# --------------------------------------------------------------------------
# scripted "models", one per role
# --------------------------------------------------------------------------
def _tool_results(messages) -> list[dict]:
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


def _trigger_payload(messages) -> dict:
    """The event payload, recovered from the instruction the runtime assembled."""
    text = messages[0]["content"]
    marker = "Event payload: "
    if marker not in text:
        return {}
    try:
        return json.loads(text.split(marker, 1)[1].split("\n", 1)[0])
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return {}


def grounded_engineer(*, turn, messages, **_):
    """Turn 0 reads the tyres and the setup; turn 1 answers from what they said."""
    if turn == 0:
        return tool_use_turn(
            [("get_tyre_trend", {}), ("get_setup_snapshot", {}), ("get_car_health", {})]
        )
    payload = _trigger_payload(messages)
    tyres, setup, health = {}, {}, {}
    for result in _tool_results(messages):
        if "temp_trend_c_per_lap" in result:
            tyres = result
        elif "thresholds" in result:
            health = result
        elif "available" in result:
            setup = result

    trends = tyres.get("temp_trend_c_per_lap") or {}
    hottest = max(trends, key=lambda c: trends[c]) if trends else None
    setup_ok = bool(setup.get("available"))

    if payload.get("measure") in ("oil_temp", "water_temp", "engine_warning", "tow"):
        value = payload.get("value")
        return EngineerCall(
            priority=RadioPriority.ADVISORY,
            spoken_text=(
                f"Oil is {value} degrees, over the {payload.get('threshold')} limit. Ease it."
                if value is not None
                else "Warning light is showing. Watch the temperatures."
            ),
            detail_text=(
                f"car_health_warning: {payload.get('measure')} at {value}, threshold "
                f"{payload.get('threshold')}. Water {health.get('water_temp')}."
            ),
            finding="car_health",
            affected=["oil" if payload.get("measure") == "oil_temp" else "engine"],
            trend="worsening",
            driver_action="Short-shift and lift on the straights.",
            confidence="high",
            rationale="Reported straight from get_car_health and the trigger payload.",
        )

    if payload.get("issue") in ("lockup", "wheelspin", "offtrack"):
        return EngineerCall(
            priority=RadioPriority.ADVISORY,
            spoken_text=(
                f"{payload['occurrences']} lockups at {payload['corner']} - "
                "fronts are taking it."
            ),
            detail_text=(
                f"recurring_issue: {payload['occurrences']} {payload['issue']} events at "
                f"{payload['corner']} across laps {payload['laps']}, worst slip "
                f"{payload.get('worst_slip')}. LF trend "
                f"{trends.get('LF')} C per lap over {tyres.get('stint_laps')} stint laps."
            ),
            finding="brake_lockup",
            corner=payload["corner"],
            affected=["LF", "RF"],
            trend="worsening",
            driver_action="Roll the brake off earlier there.",
            setup_note=(
                "Brake bias is 54.0% on the sheet; a click rearward next stop."
                if setup_ok
                else None
            ),
            confidence="high",
            rationale="Three repeats at one corner, from the deterministic aggregate.",
        )

    return EngineerCall(
        priority=RadioPriority.ADVISORY,
        spoken_text=(
            f"{hottest} climbing {trends.get(hottest)} degrees a lap."
            if hottest
            else "Tyres look settled."
        ),
        detail_text=(
            f"tyre trends {trends} over {tyres.get('stint_laps')} stint laps; "
            f"temps {tyres.get('temps_c')}."
        ),
        finding="tyre_temps" if hottest else "none",
        affected=[hottest] if hottest else [],
        trend="worsening" if hottest else "stable",
        driver_action="Give them a lap to come back." if hottest else None,
        setup_note=None,
        confidence="medium",
        rationale="Trend read straight from get_tyre_trend.",
    )


def grounded_spotter(*, turn, messages, **_):
    if turn == 0:
        return tool_use_turn([("get_standings_around_player", {"window": 3})])
    payload = _trigger_payload(messages)
    standings = next(
        (r for r in _tool_results(messages) if "gap_basis" in r), {}
    )
    if payload.get("to") in ("yellow", "red"):
        return SpotterCall(
            priority=RadioPriority.CRITICAL,
            spoken_text="Yellow, yellow. Slow down.",
            detail_text=f"flag_change to {payload.get('to')}; active {payload.get('active')}.",
            threat="hazard",
            side="unknown",
            action="lift",
            confidence="high",
        )
    car = payload.get("car_idx")
    gap = payload.get("gap")
    return SpotterCall(
        priority=RadioPriority.ADVISORY,
        spoken_text=f"Car {car} {payload.get('side', 'behind')}, {gap} seconds.",
        detail_text=(
            f"traffic from the event: car {car} at {gap}s. "
            f"gap_basis {standings.get('gap_basis')}."
        ),
        threat="car_closing",
        side=payload.get("side", "unknown"),
        car_idx=car,
        action="hold_line",
        confidence="high" if standings.get("gap_basis") else "low",
    )


def grounded_coach(*, turn, messages, **_):
    if turn == 0:
        return tool_use_turn([("get_corner_detail", {})])
    payload = _trigger_payload(messages)
    detail = next((r for r in _tool_results(messages) if "telemetry" in r), {})
    trace = (detail.get("telemetry") or {}) if detail else {}
    corner = trace.get("corner") or {}
    if trace.get("available"):
        return CoachCall(
            priority=RadioPriority.ADVISORY,
            spoken_text=(
                f"{corner['label']} minimum is {corner['min_speed_kmh']}. Carry more speed."
            ),
            detail_text=(
                f"Layer-1 corner {corner['label']} at {corner['distance_m']} m, minimum "
                f"{corner['min_speed_kmh']} km/h against a reference of "
                f"{corner['ref_min_speed_kmh']} km/h on lap {trace['ref_lap']}."
            ),
            theme="braking",
            corner=corner["label"],
            cue="Trail the brake later and carry the speed to the apex.",
            from_telemetry=True,
            confidence="high",
            rationale="Read from the deterministic corner analysis of the stored lap.",
        )
    return CoachCall(
        priority=RadioPriority.ADVISORY,
        spoken_text="Same corner keeps catching you. Brake a touch earlier.",
        detail_text=f"Pattern only, no corner trace: {payload}.",
        theme="braking",
        corner=payload.get("corner"),
        cue="Brake a touch earlier there.",
        from_telemetry=False,
        confidence="low",
        rationale="No stored telemetry, so this is the event pattern alone.",
    )


def fabricating_engineer(*, turn, **_):
    """An engineer that invents a tyre temperature nothing supports."""
    return EngineerCall(
        priority=RadioPriority.CRITICAL,
        spoken_text="Right front is 143.7 degrees, twenty over the window.",
        detail_text="Pressure has gone to 218.4 kPa on that corner.",
        finding="tyre_temps",
        corner="C08",
        affected=["RF"],
        trend="worsening",
        confidence="high",
        rationale="Invented figures, none of which came from a tool.",
    )


def dispatch(*, agent, **kwargs):
    return {
        "strategist": grounded_strategist,
        "vehicle_engineer": grounded_engineer,
        "spotter": grounded_spotter,
        "coach": grounded_coach,
    }[agent](agent=agent, **kwargs)


# --------------------------------------------------------------------------
async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rtv-smoke-roles-"))
    try:
        return await _run(tmp)
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except OSError:  # pragma: no cover - Windows file locking
            pass


async def _run(tmp: Path) -> int:
    # ---- a real store, so get_corner_detail reaches the real Layer-1 --------
    parquet = tmp / "parquet"
    parquet.mkdir(parents=True, exist_ok=True)
    db = Database(tmp / "telemetry.duckdb")
    writer = TelemetryWriter(db, parquet)
    repo = Repository(db, parquet)
    seed_scenario_session(writer, SESSION_ID, SPEC)
    coaching = CoachingService(repo)

    catalog = scenario_catalog(SPEC)
    engine = RaceStateEngine(source="replay", config=CONFIG)
    engine.bind_catalog(catalog, session_id=SESSION_ID)
    engine.set_session_info(scenario_session_info(SPEC))

    provider = ScriptedProvider(dispatch)
    feed = RadioFeed()
    orch = PitwallOrchestrator(
        engine,
        provider,
        list(AGENT_REGISTRY),
        feed=feed,
        tool_config={"pit_lane_loss_s": 25.0, "standings_window": 3},
        tool_extras={"coaching": coaching, "repo": repo, "session_id": SESSION_ID},
        clock=None,
    )

    # ---- 1: routing ------------------------------------------------------
    wakes: dict[str, list[str]] = {name: [] for name in orch.runtimes}
    messages = []
    emitted = []
    for frame in scenario_frames(SPEC):
        seen = len(engine.bus.history())
        engine.on_frame(frame, catalog)
        for event in engine.bus.history()[seen:]:
            for message in await orch.handle_event(event, engine.snapshot()):
                wakes[message.agent].append(event.key)
                messages.append(message)
        emitted.extend(feed.pump(frame.session_time))
    events = engine.bus.history()

    print("=== replay ===")
    print(f"  {len(events)} race events, {len(messages)} radio messages")

    print("\n=== radio feed ===")
    for message in emitted:
        print(
            f"  t={message.session_time:7.2f}s [{message.priority.value:<8}] "
            f"{message.agent:<16} {message.spoken_text}"
        )
        print(f"           tools={message.tools_used} grounded={message.grounded}")

    print("\n=== assertions ===")
    for agent, expected in EXPECTED_WAKES.items():
        assert wakes[agent] == expected, f"{agent}: expected {expected}, got {wakes[agent]}"
        print(f"  {agent:<16} woke on {' -> '.join(expected)}")

    # ---- 2: the aggregation is what saves the money ----------------------
    lockups = [e for e in events if e.key == "lockup"]
    recurrences = [e for e in events if e.key == "recurring_issue"]
    assert len(lockups) == 3, f"expected 3 scripted lockups, got {len(lockups)}"
    assert len(recurrences) == 1, f"expected 1 aggregate, got {len(recurrences)}"
    woke_on_singles = [k for ks in wakes.values() for k in ks if k == "lockup"]
    assert not woke_on_singles, "an agent was woken by a single lockup"
    print(
        f"  {len(lockups)} single lockups woke nobody; the one aggregated "
        f"recurring_issue woke {sum(1 for ks in wakes.values() if 'recurring_issue' in ks)} agents"
    )

    # ---- 3: Layer-1 really was reached -----------------------------------
    coach_calls = [m for m in messages if m.agent == COACH.name]
    assert coach_calls, "the coach never spoke"
    traced = [m for m in coach_calls if m.data.get("from_telemetry")]
    assert traced, "get_corner_detail never returned a stored corner trace"
    assert all("get_corner_detail" in m.tools_used for m in coach_calls)
    assert all(m.grounded for m in coach_calls), [m.ungrounded for m in coach_calls]
    print(
        f"  coach read Layer-1 corner {traced[-1].data['corner']} from session "
        f"{SESSION_ID} and every figure it quoted validated"
    )

    # ---- 4: the setup snapshot really read the session-info YAML ---------
    setup = engine.setup_snapshot()
    assert setup["available"], setup
    assert "BrakePressureBias" in json.dumps(setup["setup"]), setup
    engineer_calls = [m for m in messages if m.agent == VEHICLE_ENGINEER.name]
    assert engineer_calls, "the vehicle engineer never spoke"
    assert any("get_setup_snapshot" in m.tools_used for m in engineer_calls)
    assert all(m.grounded for m in engineer_calls), [m.ungrounded for m in engineer_calls]
    print(f"  setup snapshot read from session info ({len(setup['setup'])} sections)")

    # ---- 5: tool scoping is a permission, not a suggestion --------------
    spotter_tools = {t.name for t in SPOTTER.tools}
    assert "get_fuel_projection" not in spotter_tools
    assert "get_setup_snapshot" not in spotter_tools
    slice_ = SPOTTER.state_slice(engine.snapshot())
    assert "fuel" not in slice_ and "tyres" not in slice_, sorted(slice_)
    spotter_calls = [m for m in messages if m.agent == SPOTTER.name]
    assert spotter_calls and all(m.grounded for m in spotter_calls)
    print(
        f"  spotter is scoped to {sorted(spotter_tools)} and is shown "
        f"{sorted(slice_)} - it cannot quote fuel or tyres at all"
    )

    # ---- 6: an invented tyre temperature is caught in-loop --------------
    liar = PitwallOrchestrator(
        engine,
        ScriptedProvider(fabricating_engineer),
        [VEHICLE_ENGINEER],
        feed=RadioFeed(),
        tool_config={"corner_buckets": CONFIG.corner_buckets},
        clock=None,
    )
    recurrence = recurrences[0]
    out = await liar.handle_event(recurrence, engine.snapshot())
    assert out, "the fabricating engineer produced nothing at all"
    bad = out[0]
    assert bad.refused, "an invented tyre temperature was allowed onto the radio"
    assert bad.priority is RadioPriority.INFO
    assert "143.7" in bad.ungrounded and "218.4" in bad.ungrounded, bad.ungrounded
    print(
        f"  fabricated figures caught in-loop {sorted(bad.ungrounded)} -> "
        f'grounded refusal: "{bad.spoken_text}"'
    )

    # ---- 7: four agents, one channel -------------------------------------
    spoke = {m.agent for m in messages}
    assert spoke == set(EXPECTED_WAKES), spoke
    criticals = [m for m in messages if m.priority is RadioPriority.CRITICAL]
    aired = {m.seq for m in emitted}
    missing = [m for m in criticals if m.seq not in aired]
    assert not missing, f"{len(missing)} critical calls never aired"
    over = [m for m in messages if len(m.spoken_text.split()) > 25]
    assert not over, f"{len(over)} calls were too long for radio"
    print(
        f"  {len(spoke)} agents merged onto one channel: {feed.published} published, "
        f"{feed.superseded} superseded, {len(emitted)} aired, all "
        f"{len(criticals)} critical calls among them"
    )

    db.close()
    print("\nPitwall role agents smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
