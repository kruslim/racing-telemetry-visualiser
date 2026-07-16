"""API smoke tests via FastAPI TestClient (no iRacing required)."""

from __future__ import annotations

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("fastapi")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    # These smoke tests assert behaviour on an empty store — keep demo seeding off.
    monkeypatch.setenv("RTV_SEED_DEMO", "false")
    # Fresh settings + app per test.
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_live_status_disconnected(client):
    r = client.get("/api/v1/live/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "disconnected"
    assert body["running"] is False


def test_variables_404_when_empty(client):
    r = client.get("/api/v1/variables")
    assert r.status_code == 404


def test_sessions_empty(client):
    r = client.get("/api/v1/sessions")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_import_discover_lists_only_ibt(client, tmp_path):
    folder = tmp_path / "ibt"
    folder.mkdir()
    (folder / "a.ibt").write_bytes(b"")
    (folder / "b.ibt").write_bytes(b"")
    (folder / "notes.txt").write_text("ignore me")

    r = client.get("/api/v1/import/discover", params={"dir": str(folder)})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    names = {f["name"] for f in body["files"]}
    assert names == {"a.ibt", "b.ibt"}
    assert all("path" in f and "size" in f and "modified" in f for f in body["files"])


def test_import_discover_missing_dir_is_empty(client, tmp_path):
    r = client.get("/api/v1/import/discover", params={"dir": str(tmp_path / "nope")})
    assert r.status_code == 200
    assert r.json()["files"] == []


def test_import_missing_file_reports_error(client):
    r = client.post("/api/v1/import", json={"path": "Z:/does/not/exist.ibt"})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    # Poll until the background job resolves.
    import time

    status = {"status": "pending"}
    for _ in range(100):
        status = client.get(f"/api/v1/import/{job_id}").json()
        if status["status"] in ("error", "complete"):
            break
        time.sleep(0.05)
    assert status["status"] == "error"
    assert status["error"]
