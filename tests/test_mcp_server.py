"""Unit tests for the MCP server's transform logic (no live backend, no network).

The HTTP pass-through is covered by the API/endpoint tests; here we stub the backend
call and check the parts of the MCP layer that actually transform data: field trimming,
corner-window slicing, and error passthrough.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("mcp")

_PATH = Path(__file__).resolve().parents[1] / "mcp_server" / "telemetry_coach.py"
_spec = importlib.util.spec_from_file_location("telemetry_coach", _PATH)
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)


def _stub(monkeypatch, payload):
    calls = {}

    async def fake_get(path, params=None):
        calls["path"] = path
        calls["params"] = params
        return payload

    monkeypatch.setattr(tc, "_get", fake_get)
    return calls


def test_list_sessions_trims_fields(monkeypatch):
    _stub(monkeypatch, {"sessions": [
        {"session_id": "s1", "car_id": "ir18", "track_name": "Spa", "label": "Me",
         "kind": "ibt", "track_id": "spa", "started_at": 1.0, "internal": "drop-me"},
    ]})
    out = asyncio.run(tc.list_sessions())
    assert out[0]["session_id"] == "s1"
    assert "internal" not in out[0]  # only the documented fields survive


def test_get_lap_findings_passes_params(monkeypatch):
    calls = _stub(monkeypatch, {"corners": [], "chief": {}})
    asyncio.run(tc.get_lap_findings("s1", 5, 3))
    assert calls["path"] == "/coaching/lap-findings"
    assert calls["params"] == {"session_id": "s1", "main_lap": 5, "ref_lap": 3}


def test_get_corner_detail_slices_window_and_drops_none(monkeypatch):
    xs = [i / 10 for i in range(11)]  # 0.0 .. 1.0
    ys = [None if i == 7 else float(i) for i in range(11)]
    _stub(monkeypatch, {"x_values": xs, "laps": {"4": ys}})
    out = asyncio.run(
        tc.get_corner_detail("s1", 4, center_pct=0.7, name="Speed", width_pct=0.25)
    )
    pcts = [p["pct"] for p in out["points"]]
    # window ~[0.575, 0.825] minus the None at 0.7
    assert pcts == [0.6, 0.8]
    assert all(p["value"] is not None for p in out["points"])


def test_error_passthrough(monkeypatch):
    _stub(monkeypatch, {"error": 404, "detail": "no such session", "path": "/sessions"})
    out = asyncio.run(tc.list_laps("ghost"))
    assert out == [{"error": 404, "detail": "no such session", "path": "/sessions"}]


def test_all_tools_registered():
    tools = asyncio.run(tc.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "list_sessions", "list_laps", "get_lap_findings",
        "compare_channel", "get_corner_detail", "get_session_info",
    }
    # every tool carries a description (drives Claude's tool selection)
    assert all(t.description for t in tools)
