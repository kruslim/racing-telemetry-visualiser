"""The static Pages export: scripts/export_session.py and scripts/build_pages.py.

The exporter writes REST payloads to the file layout that
frontend/js/review/static-data.js reads (its node suite pins the same paths), and
build_pages.py copies the review next to the export. The data here is the generated
test session (tests only); the published export is made from the real Spa run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parents[1]
RUN = "20260101T000000Z-01-replay"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _journal(path: Path, session_id: str) -> None:
    def rec(seq, kind, t, data, agent=None):
        return {
            "seq": seq, "wall_ms": 1_000_000 + int((t or 0) * 1000), "session_time": t,
            "tick": None if t is None else int(t * 60), "kind": kind, "agent": agent,
            "run_id": None, "event_key": None, "data": data,
        }

    records = [
        rec(1, "lifecycle", None, {"phase": "start", "source": "replay",
                                   "session_id": session_id, "agents": []}),
        rec(2, "state", 10.0, {"player": {"lap": 1}}),
        rec(3, "radio", 12.0, {"message_id": "m1", "spoken_text": "Box this lap."},
            agent="strategist"),
        rec(4, "state", 20.0, {"player": {"lap": 2}}),
        rec(5, "lifecycle", None, {"phase": "end", "reason": "done"}),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    from evals.synthetic_session import SYNTHETIC_SESSION_ID, seed_synthetic_session

    from rtv.config import get_settings
    from rtv.store.duck import Database
    from rtv.store.writer import TelemetryWriter

    tmp = tmp_path_factory.mktemp("export")
    data = tmp / "data"
    data.mkdir()
    db = Database(data / "telemetry.duckdb")
    seed_synthetic_session(TelemetryWriter(db, data / "parquet"))
    db.close()
    sid = SYNTHETIC_SESSION_ID
    _journal(data / "sessions" / sid / "runs" / f"{RUN}.jsonl", sid)

    exporter = _script("export_session")
    mp = pytest.MonkeyPatch()
    for key in ("RTV_DATA_DIR", "RTV_AUTOSTART_LIVE", "RTV_PITWALL_AGENTS", "RTV_VISION_MODE"):
        mp.setenv(key, "")  # the exporter sets these; undo restores them
    try:
        out = tmp / "docs" / "data" / "test"
        manifest = exporter.export(data, sid, out)
    finally:
        mp.undo()
        get_settings.cache_clear()
    return tmp / "docs", out, sid, manifest


def test_the_export_follows_the_static_layout(exported):
    _, out, sid, manifest = exported
    d = out / "sessions" / sid
    for rel in (
        "laps.json",
        "pitwall-runs.json",
        "summary/none.json",
        f"summary/{RUN}.json",
        f"pitwall-log/{RUN}.json",
        f"pitwall-log/{RUN}-state.json",
    ):
        assert (d / rel).is_file(), rel
    sessions = json.loads((out / "sessions.json").read_text("utf-8"))
    assert [s["session_id"] for s in sessions["sessions"]] == [sid]
    assert sessions["sessions"][0]["source_path"] is None  # no local paths published
    assert json.loads((d / "pitwall-runs.json").read_text("utf-8"))["recording"] is None

    laps = manifest["laps"]
    assert laps and manifest["pairs"] > 0
    for lap in laps:
        assert (d / "trackmap" / f"{lap}.json").is_file()
        for name in manifest["channels"]:
            body = json.loads((d / "compare" / name / f"{lap}.json").read_text("utf-8"))
            assert list(body["laps"]) == [str(lap)]
    main, ref = manifest["pair_laps"][:2]
    assert (d / "lap-findings" / f"{main}-{ref}.json").is_file()
    assert (d / "delta" / f"{main}-{ref}.json").is_file()


def test_the_log_leaves_state_out_and_the_state_file_holds_only_state(exported):
    _, out, sid, _ = exported
    d = out / "sessions" / sid / "pitwall-log"
    log = json.loads((d / f"{RUN}.json").read_text("utf-8"))["records"]
    state = json.loads((d / f"{RUN}-state.json").read_text("utf-8"))["records"]
    assert "state" not in {r["kind"] for r in log} and "radio" in {r["kind"] for r in log}
    assert {r["kind"] for r in state} == {"state"} and len(state) == 2


def test_the_journal_names_its_session(tmp_path):
    exporter = _script("export_session")
    _journal(tmp_path / "run.jsonl", "abc-123")
    assert exporter.journal_session_id(tmp_path / "run.jsonl") == "abc-123"


def test_the_exporter_will_not_clear_a_folder_it_did_not_write(tmp_path):
    exporter = _script("export_session")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "notes.md").write_text("mine")
    with pytest.raises(SystemExit):
        exporter.export(tmp_path / "data", "x", tmp_path / "out")
    assert (tmp_path / "out" / "notes.md").is_file()


def test_build_pages_copies_the_review_and_points_index_at_the_export(exported):
    docs, *_ = exported
    pages = _script("build_pages")
    copied = pages.build(ROOT / "frontend", docs, "test")
    assert "review/index.html" in copied
    assert "js/review/static-data.js" in copied and "css/review.css" in copied
    assert not any(name.endswith(".test.js") for name in copied)
    for rel in copied:
        assert (docs / rel).read_bytes() == (ROOT / "frontend" / rel).read_bytes(), rel
    index = (docs / "index.html").read_text("utf-8")
    assert "review/?static=../data/test/" in index
    assert (docs / ".nojekyll").is_file()


def test_build_pages_refuses_without_an_export(tmp_path):
    pages = _script("build_pages")
    with pytest.raises(SystemExit):
        pages.build(ROOT / "frontend", tmp_path / "docs")


def test_the_published_copy_matches_frontend():
    """docs/ must be rebuilt (scripts/build_pages.py) in the same change as frontend/."""
    docs = ROOT / "docs"
    if not (docs / "review" / "index.html").is_file():
        pytest.skip("no Pages copy in docs/")
    pages = _script("build_pages")
    page = ROOT / "frontend" / pages.PAGE
    assets = pages.page_assets(page)
    files = [page, *[a for a in assets if a.suffix == ".css"], *pages.module_graph(assets)]
    for path in files:
        rel = path.resolve().relative_to((ROOT / "frontend").resolve())
        assert (docs / rel).read_bytes() == path.read_bytes(), f"docs/{rel} is stale"
