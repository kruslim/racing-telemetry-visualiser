"""Build the shared view golden file from the Spa example payloads.

    python scripts/build_view_golden.py          write tests/fixtures/view_golden/
    python scripts/build_view_golden.py --check  only report, write nothing

Each case is one view-model call on real Spa payloads (``tests/spa_payloads.py``)
or a small edge case. The script runs every case through the Python desktop models
and, under node, through the JS view models, and keeps as the expected value the
fields both sides return identically. It fails, and writes nothing, when the two
sides disagree on a shared field or one side throws: that is a drift to fix in the
code, not in the golden file. Fields only one side returns are listed, not kept.

Regenerate after an intended view change, then review the diff like any test change.
Test-only: nothing at runtime reads the golden file.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import spa_payloads as spa  # noqa: E402
from tests import view_golden  # noqa: E402

RUNNER = ROOT / "frontend" / "js" / "run-golden-tests.mjs"

# The journal slice: the first ten minutes of the agents-on race. State records keep
# only their clock fields, which is all the view models read from them.
JOURNAL_UNTIL_S = 600.0
STATE_EVERY_S = 30.0
# Every fifth map point is still ~280 points round Spa; enough for nearest-point cases.
TRACK_POINT_STRIDE = 5


# Task B1: the same slice as the radio would now record it. The strategist's real
# "box this lap" (computed 229.7 s, aired 317.8 s) airs tagged late, and the coach's
# real hairpin cue is dropped at its air time as no longer true. Test data only.
LATE_ID = "fd8c4e03a6c93cb1"
EXPIRED_ID = "386c95aadfe737d5"
EXPIRED_REASON = "lap 3 has passed (now lap 4)"
# Task B3: an engineer "box now" queued at the same yellow is replaced by the
# strategist's real critical box call when that is published. Test data only.
SUPERSEDED_BY_ID = "13dfdf18da914f37"
SUPERSEDED_ID = "b3e0c2d9f1a7e440"
SUPERSEDED_REASON = "replaced by the strategist's critical pit_stop call"
SUPERSEDED_AT = 540.0


def _air_journal(journal: list[dict]) -> list[dict]:
    out = []
    aired = {
        (r.get("data") or {}).get("message_id"): r["session_time"]
        for r in journal
        if r["kind"] == "radio_aired"
    }
    for rec in journal:
        rec = copy.deepcopy(rec)
        data = rec.get("data") or {}
        mid = data.get("message_id")
        if rec["kind"] == "radio_aired" and mid == EXPIRED_ID:
            out.append({**rec, "kind": "radio_expired", "data": {
                **data, "reason": EXPIRED_REASON, "age_s": round(rec["session_time"] - 367.7, 1),
            }})
            continue
        if rec["kind"] == "radio_aired" and mid == LATE_ID:
            out.append({**rec, "kind": "radio_late", "data": {**data, "late_by_s": 88.1,
                                                                "max_age_s": 30.0}})
            rec["data"] = {**data, "late": True, "late_by_s": 88.1}
        if rec["kind"] == "radio" and mid == LATE_ID:
            data.update(
                late=True,
                late_by_s=round(aired[mid] - data["session_time"], 1),
                max_age_s=30.0,
                air_session_time=aired[mid],
                spoken_text="Late call: " + data["spoken_text"],
            )
        if rec["kind"] == "radio" and mid == EXPIRED_ID:
            data.update(
                expired=True,
                expired_reason=EXPIRED_REASON,
                speak=False,
                air_session_time=aired[mid],
            )
        out.append(rec)
    template = next(
        r for r in out if r["kind"] == "radio" and r["data"]["message_id"] == SUPERSEDED_BY_ID
    )
    seq = max(r.get("seq") or 0 for r in out)
    engineer = copy.deepcopy(template)
    engineer["seq"] = seq + 1
    engineer["agent"] = "vehicle_engineer"
    engineer["data"].update(
        agent="vehicle_engineer",
        priority="advisory",
        subject="damage_stop",
        spoken_text="Box this lap, the front-left is losing pressure.",
        detail_text="Front-left pressure falling.",
        data={"finding": "damage", "pit_request": "box_now"},
        message_id=SUPERSEDED_ID,
        superseded=True,
        superseded_by=SUPERSEDED_BY_ID,
        superseded_reason=SUPERSEDED_REASON,
        speak=False,
        air_session_time=SUPERSEDED_AT,
    )
    out.append(engineer)
    out.append({
        **{k: template.get(k) for k in ("wall_ms", "tick", "event_key")},
        "seq": seq + 2,
        "session_time": SUPERSEDED_AT,
        "kind": "radio_superseded",
        "agent": "vehicle_engineer",
        "data": {
            "agent": "vehicle_engineer", "message_id": SUPERSEDED_ID, "priority": "advisory",
            "subject": "damage_stop", "by_message_id": SUPERSEDED_BY_ID,
            "by_agent": "strategist", "by_priority": "critical", "rule": "critical",
            "group": "pit_stop", "reason": SUPERSEDED_REASON,
        },
    })
    return out


def _refusal_records() -> list[dict]:
    """One coach run the validator refused twice, as the journal records it (A3)."""
    base = {"agent": "coach", "run_id": "coach-1", "event_key": "offtrack", "tick": 201300}
    detail = "27.74091339111328 km/h (no speed figure in the data matches)"
    return [
        {**base, "seq": 1, "wall_ms": 1000, "session_time": 367.7, "kind": "run_start",
         "data": {"model": "kimi-k3"}},
        {**base, "seq": 2, "wall_ms": 1100, "session_time": 367.7, "kind": "validate",
         "data": {"ok": False, "unsupported": ["4.66", "4.6610630306531675"],
                  "unsupported_detail": [detail, "4.6610630306531675 (no figure in the data "
                                                 "matches)"],
                  "word_count": 17, "repaired": False}},
        {**base, "seq": 3, "wall_ms": 1200, "session_time": 367.7, "kind": "validate",
         "data": {"ok": False, "unsupported": ["4.66"], "unsupported_detail": [detail],
                  "word_count": 15, "repaired": True}},
        {**base, "seq": 4, "wall_ms": 1300, "session_time": 367.7, "kind": "run_end",
         "data": {"outcome": "refused", "latency_ms": 12000.5}},
    ]


def _inputs() -> dict:
    running = spa.load("racestate_running.json")
    journal = []
    last_state = None
    for rec in spa.journal():
        t = rec.get("session_time")
        if t is not None and t > JOURNAL_UNTIL_S:
            continue
        if rec["kind"] == "state":
            if last_state is not None and t - last_state < STATE_EVERY_S:
                continue
            last_state = t
            rec = {k: rec.get(k) for k in ("seq", "wall_ms", "session_time", "tick", "kind")}
            rec["data"] = {}
        journal.append(rec)

    status_on = spa.load("agents-on/pitwall-status.json")
    tripped = copy.deepcopy(status_on)
    tripped["budget_exhausted"] = True
    tripped["enabled"] = False
    tripped["agents"][0]["degraded"] = True

    # A field around the player for the tower, built from the running state's own
    # player so the numbers stay Spa's; .ibt replays carry no CarIdx field.
    field_state = copy.deepcopy(running)
    player = field_state["player"]
    cars = []
    for i, (pos, gap) in enumerate(
        [
            (1, -20.4),
            (2, -11.0),
            (3, -4.2),
            (4, -1.1),
            (5, None),
            (6, 0.9),
            (7, 3.3),
            (8, 7.75),
            (9, 12.0),
            (10, 30.5),
        ]
    ):
        is_player = gap is None
        cars.append(
            {
                "idx": 30 if is_player else 40 - i,
                "position": pos,
                "lap": player["lap"],
                "laps_completed": player["lap"] - 1,
                "last_lap_time": player["last_lap_time"] + i * 0.1,
                "best_lap_time": player["best_lap_time"] if is_player else 0,
                "gap_to_player": 0.0 if is_player else gap,
                "is_player": is_player,
                "on_pit_road": pos == 9,
                "track_surface": "on_track",
            }
        )
    field_state["standings"] = {
        "cars": cars,
        "player_idx": 30,
        "gap_basis": "reference_lap",
        "lap_length_m": 6930.0,
        "reference_lap_time": player["best_lap_time"],
    }
    field_state["capabilities"] = {**field_state["capabilities"], "standings": True}
    field_later = copy.deepcopy(field_state)
    field_later["session_time"] += 4.0
    for car in field_later["standings"]["cars"]:
        if car["idx"] == 37:  # P4 closing
            car["gap_to_player"] = -0.5
        if car["idx"] == 35:  # P7 opening
            car["gap_to_player"] = 4.5

    return {
        "state_idle": spa.load("racestate_idle.json"),
        "state_running": running,
        "state_end": spa.load("racestate_end.json"),
        "state_field": field_state,
        "state_field_later": field_later,
        "status_idle": spa.load("pitwall_status_idle.json"),
        "status_on": status_on,
        "status_tripped": tripped,
        "radio": spa.load("agents-on/pitwall-radio.json")["messages"],
        "events": spa.load("racestate_events_end.json")["events"],
        "activity": spa.load("agents-on/pitwall-activity.json")["records"],
        "refusal": _refusal_records(),
        "laps": spa.load("laps.json")["laps"],
        "corners": spa.load("lap_findings.json")["corners"],
        "track_points": spa.load("trackmap.json")["points"][::TRACK_POINT_STRIDE],
        "journal": journal,
        "journal_air": _air_journal(journal),
        "replay_running": spa.load("replay_status_running.json"),
    }


def _ref(name: str) -> dict:
    return {"$input": name}


def _cases(inputs: dict) -> dict[str, list[dict]]:
    def case(module, name, fn, *args, **kwargs):
        out = {"name": name, "module": module, "fn": fn, "args": list(args)}
        if kwargs:
            out["kwargs"] = kwargs
        return out

    fmt = []
    for name, fn, args in [
        ("num of a real fuel level", "num", [5.137364864349365, 2]),
        ("num rounds a half up like toFixed", "num", [2.5, 0]),
        ("num rounds 0.125 up like toFixed", "num", [0.125, 2]),
        ("num of a negative half", "num", [-2.5, 0]),
        ("num of 1.005 (binary below the half)", "num", [1.005, 2]),
        ("num of negative zero", "num", [-0.0, 2]),
        ("num of a tiny negative", "num", [-0.0001, 2]),
        ("num of null", "num", [None]),
        ("num of a string", "num", ["3.2"]),
        ("num of a bool", "num", [True]),
        ("num with suffix", "num", [82.30874633789062, 1, " °C"]),
        ("integer rounds a half up", "integer", [2.5]),
        ("integer of a negative half", "integer", [-2.5]),
        ("integer of rpm", "integer", [1249.9979248046875, " rpm"]),
        ("lapTime of the best Spa lap", "lapTime", [140.61790466308594]),
        ("lapTime under a minute", "lapTime", [52.104]),
        ("lapTime that rounds to the minute", "lapTime", [59.9996]),
        ("lapTime of zero", "lapTime", [0]),
        ("clock of time remaining", "clock", [154.71901666026497]),
        ("clock with hours", "clock", [3753.2]),
        ("clock of a negative", "clock", [-1]),
        ("gap positive", "gap", [0.9]),
        ("gap negative uses a real minus", "gap", [-1.1]),
        ("gap zero", "gap", [0]),
        ("gap of null", "gap", [None]),
        ("signed fuel margin", "signed", [-0.706, 2]),
        ("signed with suffix", "signed", [1.25, 1, " L"]),
        ("trendArrow up", "trendArrow", [0.6, 0.5]),
        ("trendArrow inside the deadband", "trendArrow", [-0.4, 0.5]),
        ("trendArrow down", "trendArrow", [-0.51, 0.5]),
        ("trendArrow of null", "trendArrow", [None]),
        ("humanise an event key", "humanise", ["pit_window_open"]),
        ("humanise a dispatch key", "humanise", ["offtrack:lap:5"]),
        ("humanise null", "humanise", [None]),
        ("agentBadge", "agentBadge", ["vehicle_engineer"]),
        ("agentBadge of nothing", "agentBadge", [""]),
        ("stamp falls back to the session clock", "stamp", [None, 2554.2166666730686]),
        ("stamp with nothing", "stamp", [None, None]),
    ]:
        fmt.append(case("format", name, fn, *args))

    vm = []
    for state in ("state_idle", "state_running", "state_end", "state_field"):
        vm.append(case("view_model", f"statusView of {state}", "statusView", _ref(state)))
        vm.append(case("view_model", f"strategyView of {state}", "strategyView", _ref(state)))
        vm.append(case("view_model", f"tyreView of {state}", "tyreView", _ref(state)))
        vm.append(case("view_model", f"towerView of {state}", "towerView", _ref(state)))
    vm += [
        case(
            "view_model",
            "statusView with agents on",
            "statusView",
            _ref("state_running"),
            {"$opts": {"socket": True, "pitwall_status": _ref("status_on")}},
        ),
        case(
            "view_model",
            "connectionView with the socket down",
            "connectionView",
            _ref("state_running"),
            {"$opts": {"socket": False, "socket_detail": "code 1006"}},
        ),
        case(
            "view_model",
            "connectionView while a replay runs",
            "connectionView",
            _ref("state_running"),
            {"$opts": {"socket": True, "replay": _ref("replay_running")}},
        ),
        case(
            "view_model",
            "connectionView of a stale state",
            "connectionView",
            {"stale": True, "stale_reason": "no frames for 3 s"},
        ),
        case(
            "view_model",
            "connectionView of a live state",
            "connectionView",
            {"session": {"source": "live"}, "metrics": {"frames": 1234}},
        ),
        case("view_model", "connectionView of nothing", "connectionView", None),
        case("view_model", "llmView of nothing", "llmView", None),
        case("view_model", "llmView with agents off", "llmView", _ref("status_idle")),
        case("view_model", "llmView with agents on", "llmView", _ref("status_on")),
        case("view_model", "llmView tripped", "llmView", _ref("status_tripped")),
        case("view_model", "marginBand red", "marginBand", -0.706),
        case("view_model", "marginBand amber", "marginBand", 0.5),
        case("view_model", "marginBand green", "marginBand", 1.0),
        case("view_model", "marginBand unknown", "marginBand", None),
        case("view_model", "towerView expanded", "towerView", _ref("state_field"), expanded=True),
        case(
            "view_model",
            "towerView with trends and warnings",
            "towerView",
            _ref("state_field_later"),
            trends={"$trends": [_ref("state_field"), _ref("state_field_later")]},
            warnings={
                "$warnings": {
                    "events": [
                        {
                            "event_type": "traffic_close",
                            "session_time": inputs["state_field_later"]["session_time"] - 2,
                            "payload": {"car_idx": 37, "gap": -0.5, "side": "ahead"},
                        },
                        {
                            "event_type": "blue_flag",
                            "session_time": inputs["state_field_later"]["session_time"] - 30,
                            "payload": {"car_idx": 34, "gap": 3.3},
                        },
                    ],
                    "clock": inputs["state_field_later"]["session_time"],
                }
            },
        ),
        case(
            "view_model",
            "FuelHistory over the race states",
            "FuelHistory.points",
            [_ref("state_idle"), _ref("state_running"), _ref("state_running"), _ref("state_end")],
        ),
    ]
    for i, message in enumerate(inputs["radio"]):
        vm.append(
            case(
                "view_model",
                f"recommendationView of radio {i} ({message['agent']})",
                "recommendationView",
                {"$input": "radio", "index": i},
            )
        )
    vm.append(case("view_model", "recommendationView of nothing", "recommendationView", None))
    for i, event in enumerate(inputs["events"]):
        vm.append(
            case(
                "view_model",
                f"eventLine {i} ({event['event_type']})",
                "eventLine",
                {"$input": "events", "index": i},
            )
        )
    vm.append(
        case(
            "view_model",
            "eventLine of an unknown type",
            "eventLine",
            {"event_type": "new_kind", "severity": "advisory", "payload": {"a": 1, "b": "x"}},
        )
    )
    vm.append(case("view_model", "eventLine of nothing", "eventLine", None))

    act = [
        case("activity", "reduce the agents-on activity", "reduce", _ref("activity")),
        case("activity", "reduce up to 600 s", "reduce", _ref("activity"), 600),
        case("activity", "reduce the journal slice", "reduce", _ref("journal")),
        case("activity", "reduce the journal with a superseded call", "reduce",
             _ref("journal_air")),
        case("activity", "recentRuns", "recentRuns", {"$model": _ref("activity")}, 5),
        case("activity", "meanLatency", "meanLatency", {"$model": _ref("activity")}),
        case("activity", "meanLatency of nothing", "meanLatency", {"$model": []}),
    ]
    seen = set()
    for rec in inputs["activity"]:
        kind = rec["kind"]
        key = (kind, bool((rec.get("data") or {}).get("context_radio")))
        if key in seen:
            continue
        seen.add(key)
        act.append(case("activity", f"edgesFor {kind} #{rec['seq']}", "edgesFor", rec))
    act.append(
        case(
            "activity",
            "edgesFor a blocked dispatch",
            "edgesFor",
            {"kind": "dispatch", "data": {"blocked": True}},
        )
    )
    act.append(
        case(
            "activity",
            "edgesFor the driver's own call",
            "edgesFor",
            {"kind": "radio_aired", "agent": "driver", "data": {}},
        )
    )
    act.append(case("activity", "edgesFor nothing", "edgesFor", None))
    # Task A3: the validator's figures and reasons are shown rounded.
    for name, text in [
        ("a raw float", "withheld 4.66, 4.6610630306531675"),
        ("a reason", "27.74091339111328 km/h (no speed figure in the data matches)"),
        ("a lap time stays", "1:32.418 and 0.9674"),
        ("short decimals stay", "2.5 laps, 0.97 and 12"),
        ("a tiny negative", "-0.0001 s"),
        ("nothing", None),
    ]:
        act.append(case("activity", f"roundFigures of {name}", "roundFigures", text))
    act.append(case("activity", "reduce a refused run with reasons", "reduce", _ref("refusal")))

    rev = [
        case("review", "defaultLaps of the Spa laps", "defaultLaps", _ref("laps")),
        case("review", "defaultLaps of nothing", "defaultLaps", []),
        case("review", "lapAt mid-race", "lapAt", _ref("laps"), 1200.0),
        case("review", "lapAt before the start", "lapAt", _ref("laps"), 0.0),
        case("review", "bounds from laps", "bounds", None, _ref("laps")),
        case("review", "bounds of nothing", "bounds", None, []),
        case("review", "laneItems of the journal", "laneItems", _ref("journal"), _ref("laps")),
        case("review", "auditRows of the journal", "auditRows", _ref("journal")),
        case("review", "auditRows of a refused run with reasons", "auditRows", _ref("refusal")),
        case("review", "radioUpTo 400 s", "radioUpTo", _ref("journal"), 400.0, 5),
        case("review", "radioUpTo everything", "radioUpTo", _ref("journal"), None),
        case("review", "eventsUpTo 300 s", "eventsUpTo", _ref("journal"), 300.0),
        case("review", "laneItems with late and dropped calls", "laneItems",
             _ref("journal_air"), _ref("laps")),
        case("review", "radioUpTo with late and dropped calls", "radioUpTo",
             _ref("journal_air"), None),
        case("review", "radioUpTo 420 s, before the dropped call's air time", "radioUpTo",
             _ref("journal_air"), 420.0),
        case("review", "airBadge of a late call", "airBadge",
             {"late": True, "late_by_s": 88.1}),
        case("review", "airBadge rounds a half up", "airBadge",
             {"late": True, "late_by_s": 2.5}),
        case("review", "airBadge of a late call without its age", "airBadge", {"late": True}),
        case("review", "airBadge of a dropped call", "airBadge",
             {"expired": True, "expired_reason": EXPIRED_REASON}),
        case("review", "airBadge of a dropped call without a reason", "airBadge",
             {"expired": True}),
        case("review", "airBadge of a superseded call", "airBadge",
             {"superseded": True, "superseded_reason": SUPERSEDED_REASON}),
        case("review", "airBadge of a superseded call without a reason", "airBadge",
             {"superseded": True}),
        case("review", "radioUpTo 530 s, before the superseded call was dropped", "radioUpTo",
             _ref("journal_air"), 530.0),
        case("review", "airBadge of an ordinary call", "airBadge", {"late": False}),
        case("review", "airBadge of nothing", "airBadge", None),
        case("review", "toMetres", "toMetres", [0, 0.25, 0.5, 1], 6930.0),
        case("review", "toMetres without a length", "toMetres", [0, 0.5], 0),
        case("review", "cornerSpan without bounds", "cornerSpan", {"distance": 400.0}),
        case("review", "cornerSpan of nothing", "cornerSpan", None),
        case("review", "trackEvents of the journal", "trackEvents", _ref("journal")),
        case("review", "pointAtPct mid-lap", "pointAtPct", _ref("track_points"), 0.5),
        case("review", "pointAtPct across the line", "pointAtPct", _ref("track_points"), 0.9999),
    ]
    for lap in inputs["laps"][:3] + [{"is_valid": True, "lap_time": 0}]:
        rev.append(case("review", f"isClean lap {lap.get('lap')}", "isClean", lap))
    for corner in inputs["corners"][:3]:
        rev.append(case("review", f"cornerSpan {corner['label']}", "cornerSpan", corner))
    for agent, outcome, sort in [
        ("", "", "time"),
        ("strategist", "", "latency"),
        ("", "radio", "calls"),
        ("", "unfinished", "time"),
    ]:
        rev.append(
            case(
                "review",
                f"filterAudit agent={agent or 'any'} outcome={outcome or 'any'} sort={sort}",
                "filterAudit",
                {"$auditRows": _ref("journal")},
                agent=agent,
                outcome=outcome,
                sort=sort,
            )
        )
    return {"format": fmt, "view_model": vm, "activity": act, "review": rev}


def _node_outputs(inputs: dict, cases: list[dict]) -> dict:
    node = shutil.which("node")
    if node is None:
        sys.exit("node is not on PATH; the JS side cannot be checked")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps({"inputs": inputs, "cases": cases}), "utf-8")
        out = subprocess.run(
            [node, str(RUNNER), "--emit", str(path)], capture_output=True, text=True, check=True
        )
    return json.loads(out.stdout)


def _common(py, js, path, problems, one_sided):
    """The fields both sides return identically; differences go to ``problems``."""
    if isinstance(py, dict) and isinstance(js, dict):
        out = {}
        for key in py.keys() | js.keys():
            if key not in js or key not in py:
                one_sided.add(f"{path}.{key} ({'python' if key in py else 'js'} only)")
                continue
            out[key] = _common(py[key], js[key], f"{path}.{key}", problems, one_sided)
        return {k: out[k] for k in py if k in out}
    if isinstance(py, list) and isinstance(js, list) and len(py) == len(js):
        return [
            _common(p, j, f"{path}[{i}]", problems, one_sided)
            for i, (p, j) in enumerate(zip(py, js, strict=True))
        ]
    if view_golden.mismatch(py, js, path) is not None:
        problems.append(f"{path}: python {json.dumps(py)[:200]} vs js {json.dumps(js)[:200]}")
    return py


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report only, write nothing")
    args = parser.parse_args()

    inputs = _inputs()
    groups = _cases(inputs)
    flat = [c for group in groups.values() for c in group]
    js_out = _node_outputs(inputs, flat)

    problems: list[str] = []
    one_sided: set[str] = set()
    expected: dict[str, object] = {}
    for c in flat:
        name = f"{c['module']}: {c['name']}"
        try:
            py = view_golden.neutral(view_golden.call(c, inputs))
        except Exception as err:  # noqa: BLE001 - reported, not hidden
            problems.append(f"{name}: python threw {err!r}")
            continue
        js = js_out[c["name"]]
        if not js["ok"]:
            problems.append(f"{name}: js threw {js['error'].splitlines()[0]}")
            continue
        expected[c["name"]] = _common(py, js["value"], name, problems, one_sided)

    for line in sorted(one_sided):
        print(f"one side only: {line}")
    if problems:
        print(f"\n{len(problems)} disagreements:")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"{len(flat)} cases agree.")
    if args.check:
        return 0

    out_dir = ROOT / "tests" / "fixtures" / "view_golden"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "inputs.json").write_text(json.dumps(inputs, indent=None) + "\n", "utf-8")
    for module, group in groups.items():
        cases = [{**c, "expect": expected[c["name"]]} for c in group]
        (out_dir / f"{module}.json").write_text(
            json.dumps({"module": module, "cases": cases}, indent=1, ensure_ascii=False) + "\n",
            "utf-8",
        )
    print(f"wrote {out_dir.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
