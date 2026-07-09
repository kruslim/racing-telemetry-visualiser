"""MCP server exposing the telemetry backend as coaching tools.

This is Layer 2 of the coaching design: a thin wrapper over the REST API so you can
drive Q&A, lap, and stint coaching by *chatting* in Claude Desktop or Claude Code on
your existing Claude subscription — no per-token API bill. Claude itself orchestrates
the tool calls (the single-agent loop); the deterministic ~20-finding model from
``/coaching/lap-findings`` is what keeps raw 60 Hz telemetry out of the context window.

Run the backend first (``.\\run.ps1``), then register this server.

Claude Code:
    claude mcp add telemetry-coach -- \\
        "D:/Personal Projects/Racing Telemetry Visualiser/.venv/Scripts/python.exe" \\
        "D:/Personal Projects/Racing Telemetry Visualiser/mcp_server/telemetry_coach.py"

Claude Desktop (claude_desktop_config.json -> "mcpServers"):
    "telemetry-coach": {
      "command": "D:/Personal Projects/Racing Telemetry Visualiser/.venv/Scripts/python.exe",
      "args": ["D:/Personal Projects/Racing Telemetry Visualiser/mcp_server/telemetry_coach.py"]
    }

Then ask, e.g.: "Review my lap 5 against my fastest lap" or "Why am I slow in turn 4?".

Install deps:  pip install -e ".[mcp]"
Override the backend URL with RTV_API_BASE (default http://127.0.0.1:8000/api/v1).
"""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = os.environ.get("RTV_API_BASE", "http://127.0.0.1:8000/api/v1")

mcp = FastMCP("telemetry-coach")
_client = httpx.AsyncClient(base_url=API_BASE, timeout=60.0)


async def _get(path: str, params: dict | None = None) -> dict | list:
    resp = await _client.get(path, params=params)
    if resp.status_code >= 400:
        # Surface a clean message to the model instead of raising.
        return {"error": resp.status_code, "detail": resp.text[:300], "path": path}
    return resp.json()


@mcp.tool()
async def list_sessions(limit: int = 20) -> list[dict]:
    """List recorded telemetry sessions (most recent first).

    Call this first to discover which sessions exist and get their ``session_id``,
    car, track and label before any other coaching tool.
    """
    body = await _get("/sessions", {"limit": limit})
    if isinstance(body, dict) and "error" in body:
        return [body]
    sessions = body.get("sessions", []) if isinstance(body, dict) else []
    keep = ("session_id", "kind", "car_id", "track_id", "track_name", "label", "started_at")
    return [{k: s.get(k) for k in keep} for s in sessions]


@mcp.tool()
async def list_laps(session_id: str) -> list[dict]:
    """List the laps in a session with their lap times and validity flags.

    Call this to choose a 'main' lap to coach and a faster 'reference' lap to compare
    against (typically the session's fastest valid lap, or an imported alien lap).
    """
    body = await _get(f"/sessions/{session_id}/laps")
    if isinstance(body, dict) and "error" in body:
        return [body]
    return body.get("laps", []) if isinstance(body, dict) else []


@mcp.tool()
async def get_lap_findings(session_id: str, main_lap: int, ref_lap: int) -> dict:
    """Get the deterministic corner-by-corner coaching findings for one lap vs a reference.

    THIS IS THE PRIMARY TOOL — call it whenever the driver asks to review a lap, compare
    against a reference/fastest/alien lap, find where they are losing time, or what to work
    on. It returns ~20 compact findings (no raw telemetry): per-corner min-speed deltas,
    brake points, throttle application, gear, grip and lock-ups, each with apportioned time
    loss; plus sector deltas and a 'chief' summary listing the top-3 priority corners and
    total time available. Base your coaching on these numbers — do not invent figures.
    """
    return await _get(
        "/coaching/lap-findings",
        {"session_id": session_id, "main_lap": main_lap, "ref_lap": ref_lap},
    )


@mcp.tool()
async def compare_channel(
    session_id: str, name: str, laps: list[int], grid: int = 200
) -> dict:
    """Compare one raw channel (e.g. Speed, Throttle, Brake, Gear) across laps, aligned on
    lap distance (0..1).

    Use this only when ``get_lap_findings`` is not enough and you need the actual numbers —
    e.g. to confirm a trace shape. Keep ``grid`` small (100-300) to stay token-light;
    the values are returned per lap at ``grid`` evenly spaced points along the lap.
    """
    body = await _get(
        f"/sessions/{session_id}/compare",
        {"name": name, "laps": ",".join(str(x) for x in laps), "grid": grid},
    )
    return body if isinstance(body, dict) else {"result": body}


@mcp.tool()
async def get_corner_detail(
    session_id: str,
    lap: int,
    center_pct: float,
    name: str = "Speed",
    width_pct: float = 0.06,
    grid: int = 400,
) -> dict:
    """Zoom into one channel over a single corner for a single lap.

    Give ``center_pct`` as the corner's position along the lap (0..1 — divide a finding's
    ``distance`` by ``lap_length_m``). Returns just the window around the corner (a few
    dozen points), so it is cheap. Use it to inspect exactly what happened at a corner the
    findings flagged — e.g. the brake/speed trace through turn 4.
    """
    body = await _get(
        f"/sessions/{session_id}/compare",
        {"name": name, "laps": str(lap), "grid": grid},
    )
    if not isinstance(body, dict) or "x_values" not in body:
        return body if isinstance(body, dict) else {"result": body}
    xs = body["x_values"]
    ys = body.get("laps", {}).get(str(lap), [])
    lo, hi = center_pct - width_pct / 2, center_pct + width_pct / 2
    window = [
        {"pct": round(x, 4), "value": y}
        for x, y in zip(xs, ys, strict=False)
        if lo <= x <= hi and y is not None
    ]
    return {"session_id": session_id, "lap": lap, "channel": name,
            "center_pct": center_pct, "points": window}


@mcp.tool()
async def get_session_info(session_id: str) -> dict:
    """Get the parsed iRacing session-info (track, car, conditions) for context.

    Useful for grounding advice in the actual car/track when the driver asks general
    setup or technique questions.
    """
    body = await _get(f"/sessions/{session_id}/info")
    return body if isinstance(body, dict) else {"result": body}


if __name__ == "__main__":
    mcp.run()  # stdio transport (what Claude Desktop / Claude Code expect)
