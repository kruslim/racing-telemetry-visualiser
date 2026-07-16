"""The /coaching/chat endpoint: annotation resolution + end-to-end with a scripted model."""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")

from rtv.api import routes_coaching as rc
from rtv.coaching.agent.contracts import CoachingAnswer


# --------------------------------------------------------------------------- unit
def _answer(**over) -> CoachingAnswer:
    base = dict(
        headline="You're losing most time braking into T3.",
        priorities=[{"corner": "T3", "why": "brake 8 m early", "gain_s": 0.42}],
        one_lap_focus="Trail the brake deeper into T3.",
        claims=[{
            "statement": "T3 costs the most.",
            "corner": "T3",
            "citations": [{"corner": "T3", "figure": "net_dt", "value": 0.42, "unit": "s"}],
            "confidence": "high",
        }],
        corners_examined=["T3", "T7"],
        could_not_determine=[],
        session_id="s", main_lap=5, ref_lap=1,
    )
    base.update(over)
    return CoachingAnswer.model_validate(base)


_FINDINGS = {
    "lap_length_m": 4000.0,
    "corners": [
        {"label": "T3", "distance": 800.0, "net_dt": 0.42},
        {"label": "T7", "distance": 2400.0, "net_dt": 0.15},
    ],
}


def test_resolve_annotations_maps_corners_to_distances():
    anns = rc._resolve_annotations(_FINDINGS, _answer())
    assert len(anns) == 1  # T3 once (priority wins over the claim)
    a = anns[0]
    assert a.corner == "T3"
    assert a.distance_m == 800.0
    assert abs(a.lap_dist_pct - 0.2) < 1e-6
    assert a.severity == "high"       # 0.42s
    assert a.kind == "priority"
    assert "brake" in a.note.lower()


def test_resolve_annotations_includes_claim_only_corners():
    ans = _answer(
        priorities=[{"corner": "T3", "why": "late apex", "gain_s": 0.42}],
        claims=[
            {"statement": "T3 loses most.", "corner": "T3",
             "citations": [{"corner": "T3", "figure": "net_dt", "value": 0.42, "unit": "s"}],
             "confidence": "high"},
            {"statement": "T7 a touch slow.", "corner": "T7",
             "citations": [{"corner": "T7", "figure": "net_dt", "value": 0.15, "unit": "s"}],
             "confidence": "medium"},
        ],
        corners_examined=["T3", "T7"],
    )
    anns = rc._resolve_annotations(_FINDINGS, ans)
    labels = {a.label: a for a in anns}
    assert set(labels) == {"T3", "T7"}
    assert labels["T7"].kind == "claim"
    assert labels["T7"].distance_m == 2400.0
    # sorted by magnitude of time available, T3 first
    assert anns[0].label == "T3"


def test_answer_markdown_mentions_priorities():
    md = rc._answer_markdown(_answer())
    assert "T3" in md and "brake 8 m early" in md and "Next-lap focus" in md


# --------------------------------------------------------------- integration
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    monkeypatch.setenv("RTV_SEED_DEMO", "true")           # seed the demo session
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")   # pass the 503 guard
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def test_chat_503_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_SEED_DEMO", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    with TestClient(create_app()) as c:
        r = c.post("/api/v1/coaching/chat",
                   json={"session_id": "x", "main_lap": 1, "ref_lap": 0, "question": "hi"})
    assert r.status_code == 503


def _demo_laps(client):
    js = client.get("/api/v1/sessions").json()
    sid = js["sessions"][0]["session_id"]
    laps = [l["lap"] for l in client.get(f"/api/v1/sessions/{sid}/laps").json()["laps"]]
    return sid, laps


def test_chat_answer_with_scripted_model(client, make_model, ai, monkeypatch):
    """Full path: endpoint → run_coach (real provider over the seeded store) → annotations."""
    sid, laps = _demo_laps(client)
    main, ref = laps[4], laps[0]

    # Pull the real findings so the scripted answer cites a grounded figure.
    findings = client.get("/api/v1/coaching/lap-findings",
                          params={"session_id": sid, "main_lap": main, "ref_lap": ref}).json()
    corner = max(findings["corners"], key=lambda c: c["net_dt"])
    label, net_dt = corner["label"], round(corner["net_dt"], 3)

    payload = {
        "headline": f"Most time is in {label}.",
        "priorities": [{"corner": label, "why": "carry more entry speed", "gain_s": net_dt}],
        "one_lap_focus": f"Attack {label}.",
        "claims": [{
            "statement": f"{label} is the biggest loss.",
            "corner": label,
            "citations": [{"corner": label, "figure": "net_dt", "value": net_dt, "unit": "s"}],
            "confidence": "high",
        }],
        "corners_examined": [c["label"] for c in findings["corners"]],
        "could_not_determine": [],
    }
    model = make_model([
        ai("get_lap_findings", {"session_id": sid, "main_lap": main, "ref_lap": ref}),
        ai("submit_coaching", payload),
    ])
    monkeypatch.setattr(rc, "_build_model", lambda *_a, **_k: model)

    r = client.post("/api/v1/coaching/chat",
                    json={"session_id": sid, "main_lap": main, "ref_lap": ref,
                          "question": "Where am I losing time?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "answer"
    assert body["headline"] == payload["headline"]
    # The coached corner is resolved to a graph annotation with a distance.
    labels = {a["label"]: a for a in body["annotations"]}
    assert label in labels
    assert labels[label]["distance_m"] > 0
    assert 0.0 <= labels[label]["lap_dist_pct"] <= 1.0


def test_chat_refusal_with_scripted_model(client, make_model, ai, monkeypatch):
    sid, laps = _demo_laps(client)
    model = make_model([
        ai("list_available_channels", {"session_id": sid}),
        ai("refuse", {
            "reason": "channel_not_captured",
            "channels_required": ["TyreTempLF"],
            "suggestion": "Enable tyre-temperature logging.",
        }),
    ])
    monkeypatch.setattr(rc, "_build_model", lambda *_a, **_k: model)

    r = client.post("/api/v1/coaching/chat",
                    json={"session_id": sid, "main_lap": laps[1], "ref_lap": laps[0],
                          "question": "What were my tyre temps?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "refusal"
    assert body["refusal"]["reason"] == "channel_not_captured"
    assert body["annotations"] == []
