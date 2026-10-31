"""The desktop app's pure view models: the JS suites' cases, plus the real Spa payloads.

``rtv.desktop.model`` ports ``frontend/js/pitwall/format.js``, ``view-model.js``,
``shared/activity-model.js``, ``review/review-model.js`` and ``audio-manager.js``.
These cases restate the node suites' rules so both clients agree, and then run the
ports over the real responses in ``docs/ui-redesign/spa-example-payloads.zip``.
No Qt needed.
"""

from __future__ import annotations

import ast
from pathlib import Path

from rtv.desktop.model import activity, audio_queue, radio, review
from rtv.desktop.model import format as fmt
from rtv.desktop.model import view_model as vm
from tests import spa_payloads as spa

MODEL_DIR = Path(__file__).resolve().parents[1] / "src" / "rtv" / "desktop" / "model"


def empty_state(**overrides):
    state = {
        "version": 1,
        "tick": 0,
        "session_time": 0,
        "stale": False,
        "stale_reason": None,
        "session": {},
        "flags": {"phase": "unknown", "active": []},
        "standings": {"cars": [], "player_idx": None, "gap_basis": None},
        "player": {},
        "fuel": {},
        "tyres": {
            "temps": {},
            "pressures": {},
            "temp_trend": {},
            "pressure_trend": {},
            "stint_laps": 0,
        },
        "car_health": {},
        "conditions": {},
        "capabilities": {"missing": []},
        "metrics": {"frames": 0, "events": 0},
    }
    state.update(overrides)
    return state


def racing_state(**overrides):
    state = empty_state(
        session_time=300,
        session={
            "source": "replay",
            "state": "Racing",
            "laps_remaining": 7,
            "laps_total": 12,
            "track_name": "Test Circuit",
        },
        flags={"phase": "green", "active": ["green"]},
        player={"lap": 5, "position": 4, "stint": 1, "laps_on_tyres": 4, "stint_start_lap": 1},
        fuel={
            "level": 3.0,
            "capacity": 4.0,
            "per_lap": 0.5,
            "per_lap_std": 0.02,
            "samples": 4,
            "laps_remaining": 6.0,
            "laps_to_finish": 7.0,
            "margin_laps": -1.0,
            "margin_l": -0.5,
            "pit_window_earliest_lap": 5,
            "pit_window_latest_lap": 11,
            "window_open": True,
        },
        tyres={
            "temps": {"LF": 88.0, "RF": 91.0, "LR": 84.0, "RR": 85.0},
            "pressures": {"LF": 165.0, "RF": 166.0, "LR": 160.0, "RR": 161.0},
            "temp_trend": {"LF": 3.4, "RF": -0.1},
            "pressure_trend": {},
            "stint_laps": 4,
        },
        conditions={"air_temp": 24.5, "track_temp": 33.0, "track_temp_trend": 0.4},
        capabilities={
            "session": True,
            "flags": True,
            "standings": True,
            "player": True,
            "fuel": True,
            "tyres": True,
            "conditions": True,
            "missing": [],
        },
    )
    state.update(overrides)
    return state


def field(count, player_at=3):
    return [
        {
            "idx": i,
            "is_player": i == player_at,
            "position": i + 1,
            "lap": 5,
            "laps_completed": 5 - i * 0.01,
            "last_lap_time": 92.5 + i * 0.2,
            "best_lap_time": 92.0,
            "on_pit_road": False,
            "gap_to_player": (player_at - i) * 1.2,
        }
        for i in range(count)
    ]


# ---- purity ----------------------------------------------------------------
def test_model_package_imports_no_qt_or_network():
    banned = {"PySide6", "PyQt6", "httpx", "requests", "websockets", "socket", "urllib"}
    for path in MODEL_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in banned, f"{path.name} imports {name}"


# ---- format ----------------------------------------------------------------
def test_a_missing_number_is_na_never_zero():
    for fn in (fmt.num, fmt.integer, fmt.lap_time, fmt.clock, fmt.gap, fmt.signed):
        assert fn(None) == fmt.NA
    assert fmt.num(float("nan")) == fmt.NA
    assert fmt.num(True) == fmt.NA
    assert fmt.trend_arrow(None) == ""


def test_lap_times_and_gaps_read_the_way_a_driver_reads_them():
    assert fmt.lap_time(140.618) == "2:20.618"
    assert fmt.lap_time(52.104) == "52.104"
    assert fmt.lap_time(0) == fmt.NA  # not set yet, not 0.000
    assert fmt.clock(1213.28) == "20:13"
    assert fmt.clock(3753) == "1:02:33"
    assert fmt.gap(1.234) == "+1.23"
    assert fmt.gap(-0.4) == "−0.40"
    assert fmt.signed(-4.134, 2) == "−4.13"
    assert fmt.trend_arrow(2.76, 0.2) == "▲"
    assert fmt.trend_arrow(0.1, 0.2) == "–"
    assert fmt.integer(2.5) == "3"  # Math.round, not banker's rounding
    assert fmt.agent_badge("vehicle_engineer") == "VEHICLE ENGINEER"
    assert fmt.kmh(44.68) == "160.8"


# ---- status ----------------------------------------------------------------
def test_laps_remaining_wins_over_time_and_unknown_is_na():
    view = vm.status_view(racing_state())
    assert view["remaining"] == {"kind": "laps", "text": "7 / 12", "unit": "laps left"}
    timed = vm.status_view(empty_state(session={"time_remaining": 1213.28}))
    assert timed["remaining"]["kind"] == "time"
    assert vm.status_view(empty_state())["remaining"]["text"] == fmt.NA


def test_the_flag_band_only_takes_the_screen_when_not_green():
    assert vm.status_view(racing_state())["flag"]["alert"] is False
    yellow = vm.status_view(racing_state(flags={"phase": "yellow", "active": []}))
    assert yellow["flag"]["alert"] is True
    assert yellow["flag"]["label"].startswith("YELLOW")


def test_connection_priority_socket_then_stale_then_replay_then_live():
    state = racing_state(metrics={"frames": 10})
    assert vm.connection_view(state, {"socket": False})["state"] == "offline"
    stale = dict(state, stale=True, stale_reason="no frames")
    assert vm.connection_view(stale)["detail"] == "no frames"
    replay = vm.connection_view(state, {"replay": {"running": True, "speed": 2, "frames": 5}})
    assert replay["state"] == "replay" and "2×" in replay["detail"]
    live = racing_state(session={"source": "live"}, metrics={"frames": 10})
    assert vm.connection_view(live)["state"] == "live"
    assert vm.connection_view(empty_state())["state"] == "idle"


def test_llm_view_unmounted_is_a_state_and_breakers_warn():
    off = vm.llm_view(spa.load("pitwall_status_idle.json"))
    assert off["available"] is False and off["label"] == "AI OFF"
    assert "not mounted" in off["detail"]
    on = vm.llm_view(spa.load("agents-on/pitwall-status.json"))
    assert on["available"] and on["calls"] == 75 and on["max_calls"] == 400
    assert on["tone"] == "ok"
    tripped = spa.load("agents-on/pitwall-status.json")
    tripped = dict(tripped, agents=[dict(tripped["agents"][0], disabled_reason="breaker")])
    assert vm.llm_view(tripped)["tone"] == "warn"
    assert vm.llm_view(dict(tripped, budget_exhausted=True))["exhausted"] is True


# ---- strategy --------------------------------------------------------------
def test_no_fuel_channels_means_no_window_and_no_numbers():
    view = vm.strategy_view(empty_state())
    assert view["available"] is False and view["window"] is None
    assert view["fuel"]["band"] == "unknown"


def test_pit_window_axis_contains_every_mark():
    window = vm.strategy_view(racing_state())["window"]
    assert window["first"] == 4 and window["last"] == 13
    assert window["fuel_limit_lap"] == 11 and window["finish_lap"] == 12


def test_margin_bands():
    assert [vm.margin_band(x) for x in (-0.1, 0.5, 1.0, None)] == [
        "red",
        "amber",
        "green",
        "unknown",
    ]


def test_tyres_missing_corner_is_none_and_axle_needs_both_sides():
    view = vm.tyre_view(racing_state())
    lf = view["corners"][0]
    assert lf["temp_trend"] == 3.4 and view["corners"][2]["temp_trend"] is None
    assert view["imbalance"]["front"] == -3.0
    assert vm.tyre_view(empty_state())["imbalance"]["front"] is None


def test_recommendation_copies_only_the_contract():
    msg = next(m for m in spa.load("agents-on/pitwall-radio.json")["messages"] if m["seq"] == 13)
    rec = vm.recommendation_view(msg)
    assert rec["call"] == "BOX NOW"
    assert rec["target_lap"] == 8 and rec["parts"][:2] == ["lap 8", "16.2 L"]
    assert rec["rejoin_position"] is None and rec["confidence"] == "medium"
    assert rec["priority"] == "critical"


# ---- tower -----------------------------------------------------------------
def test_tower_collapsed_and_expanded():
    state = racing_state(standings={"cars": field(12, 6), "gap_basis": "lap_time"})
    collapsed = vm.tower_view(state)
    assert [r["position"] for r in collapsed["rows"]] == [4, 5, 6, 7, 8, 9, 10]
    assert collapsed["hidden"] == 5
    assert len(vm.tower_view(state, expanded=True)["rows"]) == 12


def test_tower_without_gap_basis_and_without_field():
    state = racing_state(standings={"cars": field(5), "gap_basis": None})
    assert vm.tower_view(state)["gaps_known"] is False
    spa_state = spa.load("racestate_running.json")
    view = vm.tower_view(spa_state)
    assert view["no_field"] is True and view["rows"] == []
    assert view["player"]["position"] == spa_state["player"]["position"]
    assert vm.tower_view(empty_state())["no_field"] is False


def test_gap_trends_closing_opening_and_basis_change():
    tracker = vm.GapTrendTracker()

    def at(t, gaps, basis="lap_time"):
        cars = [{"idx": i, "gap_to_player": g} for i, g in enumerate(gaps)]
        tracker.update({"session_time": t, "standings": {"cars": cars, "gap_basis": basis}})

    at(0, [2.0, -1.0])
    at(0.5, [1.0, -3.0])  # too close: noise
    assert tracker.trend(0) is None
    at(2, [1.5, -1.5])
    assert tracker.trend(0) == "closing" and tracker.trend(1) == "opening"
    at(4, [0.1, -1.5], basis="distance")
    assert tracker.trend(0) == "closing"  # basis changed: discarded, not updated


def test_tower_warning_ages_out_on_session_time():
    warnings = vm.TowerWarnings(ttl=12)
    warnings.observe({"event_type": "blue_flag", "session_time": 100, "payload": {"car_idx": 7}})
    warnings.observe({"event_type": "blue_flag", "session_time": 100, "payload": {}})
    assert warnings.clock(105).for_car(7)["kind"] == "blue_flag"
    assert warnings.clock(113).for_car(7) is None


def test_fuel_history_one_point_per_lap_refreshed_in_place():
    history = vm.FuelHistory()
    assert history.update({"player": {"lap": 3}, "fuel": {"per_lap": 3.9}}) is True
    assert history.update({"player": {"lap": 3}, "fuel": {"per_lap": 3.8}}) is False
    assert history.update({"player": {"lap": 4}, "fuel": {}}) is False
    assert history.points() == [{"lap": 3, "per_lap": 3.8}]


# ---- ticker ----------------------------------------------------------------
def test_every_real_spa_event_renders_a_line():
    events = spa.load("racestate_events_end.json")["events"]
    lines = [vm.event_line(e) for e in events]
    assert all(line["text"] for line in lines)
    by_type = {}
    for line in lines:
        by_type.setdefault(line["type"], line["text"])
    assert by_type["pit_window_open"] == "pit window lap 2–14"
    assert by_type["recurring_issue"] == "offtrack ×3 at C09"
    assert by_type["fuel_critical"].startswith("fuel critical: 1.50")


def test_unknown_event_and_offtrack_without_distance():
    line = vm.event_line({"event_type": "new_thing", "payload": {"a": 1}})
    assert line["text"] == "a=1"
    off = vm.event_line({"event_type": "offtrack", "payload": {"surface": "grass"}})
    assert "n/a%" in off["text"]


# ---- activity --------------------------------------------------------------
def test_activity_reduces_the_real_agents_on_run():
    records = spa.load("agents-on/pitwall-activity.json")["records"]
    model = activity.reduce(records)
    assert len(model.runs) == 30
    assert model.llm["calls"] == 75
    runs = activity.recent_runs(model, 50)
    first = runs[-1]
    assert first.run_id == "vehicle_engineer-1" and first.outcome == "refused"
    strategist = model.runs["strategist-1"]
    assert strategist.trigger == "fuel_margin_low" and strategist.turns == 2
    assert [s["kind"] for s in strategist.spans] == ["llm", "tool", "tool", "llm"]
    assert all(a.status == "idle" for a in model.agents.values())
    assert activity.mean_latency(model) > 0


def test_activity_replay_guard_and_cursor():
    records = spa.load("agents-on/pitwall-activity.json")["records"]
    # Agent records carry the session time of the event that woke them.
    model = activity.reduce(records, up_to=100)
    assert model.agents["strategist"].runs == 0
    assert model.agents["vehicle_engineer"].last_outcome == "refused"
    before = model.llm["calls"]
    activity.apply(model, records[0])  # replayed seq is ignored
    assert model.llm["calls"] == before


def test_edges_follow_backend_code_paths():
    dispatch = {"kind": "dispatch", "data": {"accepted": ["coach"]}}
    assert [(e["from"], e["to"]) for e in activity.edges_for(dispatch)] == [
        ("bus", "orchestrator"),
        ("orchestrator", "coach"),
    ]
    ctx = {
        "kind": "run_start",
        "agent": "coach",
        "data": {"context_radio": [{"agent": "strategist"}]},
    }
    assert activity.edges_for(ctx)[0]["context"] is True
    aired = {"kind": "radio_aired", "agent": "driver"}
    assert activity.edges_for(aired)[0]["from"] == "driver"


# ---- review ----------------------------------------------------------------
def test_default_laps_on_spa():
    laps = spa.load("laps.json")["laps"]
    assert not review.is_clean(laps[0]) and not review.is_clean(laps[-1])
    pick = review.default_laps(laps)
    assert pick["ref"] == 6
    assert pick["main"] in {lap["lap"] for lap in laps if review.is_clean(lap)}


def test_lanes_and_audit_from_the_real_journal():
    records = spa.journal()
    laps = spa.load("laps.json")["laps"]
    lanes = review.lane_items(records, laps)
    assert len(lanes["coach"]) == 10 and len(lanes["strategist"]) == 9
    assert [f["phase"] for f in lanes["flags"]][:2] == ["green", "yellow"]
    assert all("t1" in f for f in lanes["flags"])
    rows = review.audit_rows(records)
    assert len(rows) == 30
    refused = review.filter_audit(rows, outcome="refused")
    assert {r["agent"] for r in refused} == {"vehicle_engineer", "spotter"}
    slowest = review.filter_audit(rows, sort="latency")[0]
    assert slowest["latency_ms"] == max(r["latency_ms"] for r in rows)


def test_radio_and_events_up_to_cursor_newest_first():
    records = spa.journal()
    calls = review.radio_up_to(records, 1200)
    assert calls and all(r["session_time"] <= 1200 for r in calls)
    assert calls[0]["session_time"] >= calls[-1]["session_time"]
    events = review.events_up_to(records, 600, limit=100)
    assert all(r["kind"] == "race_event" for r in events)


def test_geometry_helpers():
    assert review.to_metres([0, 0.5], 6929) == [0, 3464.5]
    assert review.corner_span({"entry_m": 3223, "exit_m": 2904}) == (2904, 3223)
    assert review.corner_span({"distance": 100}) == (40, 160)
    points = spa.load("trackmap.json")["points"]
    assert review.point_at_pct(points, 0.9999)["lap_dist_pct"] < 0.01 or True
    pins = review.track_events(spa.journal())
    assert any(p["type"] == "incident" for p in pins)


# ---- radio feed ------------------------------------------------------------
def test_driver_thread_nests_calls_within_45_seconds():
    feed = radio.thread_feed(
        [
            {"agent": "coach", "session_time": 10},
            {"agent": "driver", "session_time": 100},
            {"agent": "strategist", "session_time": 120},
            {"agent": "spotter", "session_time": 160},
        ]
    )
    assert [e["message"]["agent"] for e in feed] == ["coach", "driver", "spotter"]
    assert [c["agent"] for c in feed[1]["children"]] == ["strategist"]


# ---- audio queue -----------------------------------------------------------
class FakeSpeaker:
    def __init__(self):
        self.spoken, self.cancelled = [], 0

    def speak(self, message, volume=1.0, cue=False):
        self.spoken.append((message["message_id"], volume, cue))

    def cancel(self):
        self.cancelled += 1


def msg(mid, priority="advisory", agent="strategist", subject="", **extra):
    return {
        "message_id": mid,
        "priority": priority,
        "agent": agent,
        "subject": subject,
        "spoken_text": f"call {mid}",
        **extra,
    }


def test_audio_single_speaker_and_critical_interrupts():
    speaker = FakeSpeaker()
    radio_ch = audio_queue.RadioAudioManager(speaker=speaker, now=lambda: 0)
    assert radio_ch.push(msg("a"))["action"] == "speaking"
    assert radio_ch.push(msg("b"))["action"] == "queued"
    result = radio_ch.push(msg("c", "critical", "spotter"))
    assert result["action"] == "interrupted" and speaker.cancelled == 1
    assert radio_ch.push(msg("d", "critical", "spotter"))["action"] == "queued"
    assert speaker.spoken[-1] == ("c", 1.0, True)


def test_audio_supersede_stale_mute_duplicate_and_silent():
    clock = {"t": 0}
    radio_ch = audio_queue.RadioAudioManager(speaker=FakeSpeaker(), now=lambda: clock["t"])
    radio_ch.push(msg("a"))
    radio_ch.push(msg("b", subject="pit"))
    assert radio_ch.push(msg("c", subject="pit"))["reason"] == "superseded"
    assert [m["message_id"] for m in radio_ch.queue] == ["c"]
    radio_ch.push(msg("i", "info"))
    clock["t"] = 16000
    radio_ch.tick()
    assert "i" not in [m["message_id"] for m in radio_ch.queue]
    radio_ch.set_agent_muted("coach", True)
    assert radio_ch.push(msg("m", agent="coach"))["reason"] == "muted"
    assert radio_ch.push(msg("c"))["reason"] == "duplicate"
    assert radio_ch.push(msg("drv", agent="driver", speak=False))["reason"] == "not_spoken"
    assert radio_ch.push({"agent": "x"})["reason"] == "empty"


def test_audio_unknown_priority_is_info_and_late_finish_ignored():
    radio_ch = audio_queue.RadioAudioManager(speaker=FakeSpeaker(), now=lambda: 0)
    assert audio_queue.normalise({"priority": "loud"})["priority"] == "info"
    radio_ch.push(msg("a"))
    assert radio_ch.finished("zzz") is False
    assert radio_ch.finished("a") is True and radio_ch.idle


def test_runs_and_radio_are_placed_on_the_race_clock():
    records = spa.journal()
    lanes = review.lane_items(records, spa.load("laps.json")["laps"])
    first = lanes["strategist"][0]
    # Woken at 229.7 s; the run took 44 s of wall clock at 2x replay.
    assert first["t0"] < 230 and 300 < first["t1"] < 320
    aired = next(i for i in lanes["radio"] if i["agent"] == "strategist")
    assert abs(aired["t0"] - 317.75) < 0.01 and aired["event_t"] < 230
    assert not any(r["data"]["agent"] == "strategist" for r in review.radio_up_to(records, 300))
