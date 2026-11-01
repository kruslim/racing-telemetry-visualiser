"""Ask the pitwall a scripted list of driver questions at fixed laps of a running replay.

Task C1's live check. Start the server with the agents on, start a replay
(``POST /api/v1/replay/start``), then run this against it. At each scripted lap
it posts the question to ``/api/v1/pitwall/driver-message`` and waits for the
answer on the radio (the message whose ``in_reply_to`` is the transcript's id).

    python scripts/ask_during_replay.py --url http://127.0.0.1:8000 --out answers.json

Writes every question with its route, the answer, who answered it (template or
model) and the latency, both wall clock (POST to answer on the radio) and session
time (the replay's clock, which runs at the replay speed). With agents on, the
model-answered questions use the Kimi quota.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

#: (lap, question). A mix of template lookups and questions an agent must judge.
QUESTIONS: list[tuple[int, str]] = [
    (2, "How many laps of fuel do I have?"),
    (3, "What's the gap behind?"),
    (4, "What was my last lap?"),
    (5, "How are the tyres holding up?"),
    (6, "Where am I losing time?"),
    (7, "Tyre temps?"),
    (8, "What's the plan for our stop?"),
    (10, "What position am I in?"),
    (12, "Anyone close enough to attack me?"),
    (14, "How many laps to go?"),
]


def _call(url: str, body: dict | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _find_answer(api: str, qid: str) -> dict | None:
    radio = _call(f"{api}/pitwall/radio?limit=200")
    for message in radio["messages"] + radio["pending"]:
        if message.get("in_reply_to") == qid:
            return message
    return None


def run(base: str, questions: list[tuple[int, str]], timeout_s: float) -> list[dict]:
    api = base.rstrip("/") + "/api/v1"
    results: list[dict] = []
    for lap, text in questions:
        while True:
            replay = _call(f"{api}/replay/status")
            state = _call(f"{api}/racestate")
            now_lap = (state.get("player") or {}).get("lap") or 0
            if now_lap >= lap or not replay.get("running"):
                break
            time.sleep(0.5)
        if not replay.get("running"):
            print(f"replay ended before lap {lap}; stopping", file=sys.stderr)
            break
        asked_at = time.monotonic()
        body = _call(f"{api}/pitwall/driver-message", {"text": text, "source": "text"})
        transcript = body["message"]
        qid = transcript["message_id"]
        answer = None
        while answer is None and time.monotonic() - asked_at < timeout_s:
            answer = _find_answer(api, qid)
            if answer is None:
                time.sleep(0.25)
        wall = round(time.monotonic() - asked_at, 2) if answer else None
        row = {
            "lap": now_lap,
            "question": text,
            "route": body.get("question"),
            "asked_session_time": transcript.get("session_time"),
            "answer": answer.get("spoken_text") if answer else None,
            "agent": answer.get("agent") if answer else None,
            "source": answer.get("source") if answer else None,
            "grounded": answer.get("grounded") if answer else None,
            "refused": answer.get("refused") if answer else None,
            "latency_wall_s": wall,
            "latency_session_s": (
                round(answer["session_time"] - transcript["session_time"], 2)
                if answer and answer.get("session_time") is not None
                else None
            ),
            "message_id": answer.get("message_id") if answer else None,
        }
        results.append(row)
        print(
            f"L{now_lap:>2} {text!r} -> {row['agent']}/{row['source']} "
            f"({wall} s): {row['answer']}"
        )
    return results


def markdown(rows: list[dict]) -> str:
    lines = [
        "| lap | question | route | answered by | answer | latency (wall) |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        route = r["route"] or {}
        how = f"{route.get('owner')} / {route.get('intent') or 'agent'}"
        who = f"{r['agent']} ({r['source']})" if r["agent"] else "no answer"
        latency = f"{r['latency_wall_s']} s" if r["latency_wall_s"] is not None else "-"
        answer = (r["answer"] or "-").replace("|", "/")
        lines.append(f"| {r['lap']} | {r['question']} | {how} | {who} | {answer} | {latency} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=90.0, help="Seconds to wait per answer.")
    parser.add_argument("--out", type=Path, help="Write the answers as JSON (and a .md table).")
    args = parser.parse_args(argv)
    rows = run(args.url, QUESTIONS, args.timeout)
    if args.out:
        args.out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        args.out.with_suffix(".md").write_text(markdown(rows), encoding="utf-8")
    print(markdown(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
