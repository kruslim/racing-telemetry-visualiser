"""The 60-second demo: one command, a live pitwall, a voice.

    python scripts/demo_pitwall.py

Starts the real server, waits for it, opens the radio page in your browser, and
starts a replay of the scripted synthetic race. No iRacing, no API key, no
network. Press Ctrl-C to stop.

Why a script rather than "run these four commands": the demo has an ordering
constraint (the page must be open and the audio unlocked *before* the race
starts, or the first calls are missed) and a browser gesture requirement no
command line can satisfy. So the script sequences what it can and says plainly
what it cannot.

    --speed N     playback rate; 1.0 is real time, 4 is a brisk demo (default 4)
    --port N      bind port (default 8000)
    --no-browser  print the URL instead of opening it
    --agents      also mount the LLM agent layer (needs ANTHROPIC_API_KEY; this
                  is the only flag here that can spend money)
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BANNER = """
===========================================================================
  Racing Telemetry Visualiser -- v2 pitwall demo
===========================================================================
"""

STEPS = """
  1. The radio page is opening at {url}
  2. Press "Enable audio" once  (browsers make no sound before a gesture)
  3. The scripted race starts {when}

  What you should see and hear:
    - race events landing in the log as the engine detects them: a
      full-course yellow on lap 3, a front-axle lock-up on lap 4, the fuel
      window opening, a pit stop
    - a critical spotter shout cutting an advisory off mid-sentence
    - a pit call superseded by a newer one before it ever airs

  Also worth a look:
    {base}/                    the live pit stand: strategy, timing tower,
                               flag band and the deterministic event ticker,
                               all driven by this same replay
    {base}/audio-test.html     the 20 queue-discipline cases, in-page
    {base}/pitwall-test.html   the 34 view-model cases, in-page
    {base}/api/v1/pitwall/health   is anything still watching?
    {base}/docs                    the whole API

  Ctrl-C to stop.
===========================================================================
"""


def _wait_for_server(base: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/v1/health", timeout=1.0) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def _post(url: str, body: dict) -> dict:
    import json

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10.0) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speed", type=float, default=4.0)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--agents", action="store_true")
    parser.add_argument(
        "--delay",
        type=float,
        default=6.0,
        help="Seconds to wait after opening the page before starting the race, "
        "so there is time to press Enable audio.",
    )
    args = parser.parse_args()

    os.environ.setdefault("RTV_PITWALL", "true")
    os.environ.setdefault("RTV_AUTOSTART_LIVE", "false")
    if args.agents:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(
                "--agents needs ANTHROPIC_API_KEY. Without it the agent layer "
                "stays unmounted and the page plays the scripted radio instead."
            )
        os.environ["RTV_PITWALL_AGENTS"] = "true"

    base = f"http://{args.host}:{args.port}"
    url = f"{base}/radio.html"

    import uvicorn

    from rtv.config import get_settings
    from rtv.main import create_app

    get_settings.cache_clear()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(), host=args.host, port=args.port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, name="rtv-demo", daemon=True)
    thread.start()

    print(BANNER)
    if not _wait_for_server(base):
        print(f"The server did not come up on {base}. Is the port already in use?")
        return 1

    print(
        STEPS.format(
            url=url,
            base=base,
            when=f"in {args.delay:.0f}s, at {args.speed:g}x real time",
        )
    )
    if args.no_browser:
        print(f"  (open it yourself: {url})\n")
    else:
        webbrowser.open(url)

    time.sleep(args.delay)
    try:
        status = _post(
            f"{base}/api/v1/replay/start",
            {"session_id": "scenario", "speed": args.speed},
        )
        print(f"  Replay started: {status['total']} frames at {args.speed:g}x.\n")
    except Exception as exc:  # pragma: no cover - demo convenience
        print(f"  Could not start the replay: {exc}")
        print(f"  Start it from the page instead: {url}\n")

    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n  Stopping.")
        server.should_exit = True
        thread.join(timeout=5.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
