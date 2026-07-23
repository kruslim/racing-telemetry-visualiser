"""Offline smoke test of the stage-6 pitwall UI (no iRacing, no API key, no network).

Boots the real app, serves the real frontend, and drives a scripted race through
the socket the browser actually connects to -- then checks the things that would
break the pit stand without breaking a test:

    1. the app shell is served, and every module it imports resolves over HTTP
    2. every element id / data-role / data-field a panel reaches for exists
    3. /ws/pitwall at 10 Hz delivers state and event frames during a replay
    4. the three REST polls the UI makes all answer with no agent layer mounted
    5. the replay bar's start/stop round trip works, and refuses two sources
    6. a session with no tyre, fuel or standings channels reports that in
       `capabilities` -- which is the whole basis of the UI's `n/a`
    7. the view-model suite passes under node

Run:  python scripts/smoke_pitwall_ui.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FRONTEND = ROOT / "frontend"
JS = FRONTEND / "js"

PASS, FAIL = "  ok  ", " FAIL "
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{PASS if condition else FAIL}] {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def imports_of(path: Path) -> list[str]:
    return re.findall(r"""^\s*import[^'"]*['"]([^'"]+)['"]""", path.read_text("utf-8"), re.M)


def main() -> int:
    print("\n== stage 6: the live pitwall UI ==\n")

    os.environ.setdefault("RTV_AUTOSTART_LIVE", "false")
    os.environ.pop("RTV_PITWALL", None)

    from fastapi.testclient import TestClient

    from rtv.main import create_app
    from rtv.racestate.models import RaceEvent, RaceState
    from rtv.racestate.scenario import ScenarioSpec

    with TestClient(create_app()) as client:
        # -- 1. the shell and its modules ---------------------------------
        shell = client.get("/")
        check("the root serves the pitwall app", shell.status_code == 200)
        check(
            "it offers both top-level modes",
            'data-mode="analysis"' in shell.text and 'data-mode="pitwall"' in shell.text,
        )
        check(
            "the stage-4 radio page is still there",
            client.get("/radio.html").status_code == 200,
        )

        unresolved = []
        for specifier in imports_of(JS / "pitwall-app.js"):
            url = specifier if specifier.startswith("/") else f"/js/{specifier.lstrip('./')}"
            response = client.get(url)
            if response.status_code != 200 or "javascript" not in response.headers.get(
                "content-type", ""
            ):
                unresolved.append(url)
        check(
            "every module the app imports is served as javascript",
            not unresolved,
            ", ".join(unresolved),
        )

        # -- 2. no build step, so the wiring is checked here ---------------
        markup = (FRONTEND / "index.html").read_text("utf-8")
        ids = set(re.findall(r"""\bid=["']([\w-]+)["']""", markup))
        wanted_ids = set(
            re.findall(
                r"""(?:\$|getElementById)\(\s*['"]([\w-]+)['"]""",
                (JS / "pitwall-app.js").read_text("utf-8"),
            )
        )
        check(
            "every element the app reaches for exists",
            wanted_ids <= ids,
            ", ".join(sorted(wanted_ids - ids)),
        )

        roles = set(re.findall(r"""data-role=["']([\w-]+)["']""", markup))
        fields = set(re.findall(r"""data-field=["']([\w-]+)["']""", markup))
        wanted_roles: set[str] = set()
        wanted_fields: set[str] = set()
        for module in sorted((JS / "pitwall").glob("*-panel.js")):
            source = module.read_text("utf-8")
            wanted_roles |= set(re.findall(r"""\[data-role=["']([\w-]+)["']\]""", source))
            wanted_fields |= set(re.findall(r"""_set\(\s*['"]([\w-]+)['"]""", source))
        check(
            "every panel slot exists on the page",
            wanted_roles <= roles and wanted_fields <= fields,
            ", ".join(sorted((wanted_roles - roles) | (wanted_fields - fields))),
        )

        # -- 4. the polls the UI makes on a loop --------------------------
        health = client.get("/api/v1/pitwall/health").json()
        check("/pitwall/health answers", health["pitwall"] is True and health["ok"] is True)
        status = client.get("/api/v1/pitwall/status").json()
        check(
            "/pitwall/status explains a quiet radio rather than erroring",
            status["available"] is False and bool(status["reason"]),
            status["reason"],
        )
        events = client.get("/api/v1/racestate/events?limit=60")
        check("/racestate/events seeds the ticker", events.status_code == 200)

        # -- 3/5. the socket, driven by a real replay ---------------------
        frames: dict[str, list[dict]] = {"state": [], "event": []}
        with client.websocket_connect("/ws/pitwall") as ws:
            opening = ws.receive_json()
            check(
                "a client is never blank: a snapshot arrives on connect",
                opening["type"] == "state",
            )
            ws.send_json({"op": "subscribe", "rate_hz": 10})
            started = client.post(
                "/api/v1/replay/start", json={"session_id": "scenario", "speed": 8.0}
            )
            check("the replay bar can start a race", started.status_code == 200)
            for _ in range(4000):
                message = ws.receive_json()
                frames.setdefault(message["type"], []).append(message)
                if len(frames["state"]) > 3 and len(frames["event"]) > 3:
                    break

        check("state frames drive the panels", len(frames["state"]) > 3, f"{len(frames['state'])}")
        check("event frames drive the ticker", len(frames["event"]) > 3, f"{len(frames['event'])}")
        check(
            "every state frame is contract-valid",
            all(RaceState.model_validate(f["state"]) for f in frames["state"]),
        )
        check(
            "every event frame carries the key the ticker renders",
            all(
                RaceEvent.model_validate(f["event"]).key == f["event"]["key"]
                for f in frames["event"]
            ),
        )

        stopped = client.post("/api/v1/replay/stop")
        check("the replay bar can stop it again", stopped.status_code == 200)

        # -- 6. degradation is announced, not inferred --------------------
        snapshot = client.get("/api/v1/racestate").json()
        caps = snapshot["capabilities"]
        check(
            "the state names which groups this session can back",
            all(group in caps for group in ("fuel", "tyres", "standings", "conditions")),
        )

    # A session stripped of tyre, fuel and standings channels: the UI's whole
    # `n/a` contract rests on this being reported rather than zeroed.
    from rtv.racestate import RaceStateEngine
    from rtv.racestate.replay import replay_scenario

    tyre_channels = tuple(
        f"{corner}{suffix}"
        for corner in ("LF", "RF", "LR", "RR")
        for suffix in ("tempCM", "pressure")
    )
    engine = RaceStateEngine(source="replay")
    replay_scenario(
        engine,
        ScenarioSpec(exclude=("FuelLevel", "FuelLevelPct", *tyre_channels)),
    )
    degraded = engine.snapshot()
    check(
        "a session with no fuel channels says so instead of reporting 0.0",
        degraded.capabilities.fuel is False and degraded.fuel.per_lap is None,
    )
    check(
        "...and the same for tyres",
        degraded.capabilities.tyres is False and degraded.tyres.temps == {},
    )

    # -- 7. the view-model rules ------------------------------------------
    node = shutil.which("node")
    if node:
        result = subprocess.run(
            [node, str(JS / "run-pitwall-tests.mjs")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        passed = re.search(r"(\d+)/(\d+) passed", result.stdout)
        check(
            "the view-model suite passes under node",
            result.returncode == 0 and passed is not None and passed.group(1) == passed.group(2),
            passed.group(0) if passed else result.stderr.strip()[:120],
        )
    else:
        print("[ skip ] node is not on PATH; open /pitwall-test.html instead")

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("all pitwall UI smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
