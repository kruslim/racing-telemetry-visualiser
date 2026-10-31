"""The post-race review page: shipped, served, wired, and its pure parts tested.

The grouping logic (timeline lanes, agent audit, default laps) is asserted in
JavaScript by frontend/js/review/review-model.test.js; this file runs that suite when
node is on PATH and checks the static contracts a build step would otherwise catch.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

pytest.importorskip("fastapi")

from rtv.main import FRONTEND_DIR  # noqa: E402

JS_DIR = FRONTEND_DIR / "js"
PAGE = FRONTEND_DIR / "review" / "index.html"
MODULES = sorted((JS_DIR / "review").glob("*.js"))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path_factory.mktemp("data")))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _imports(source: str) -> list[str]:
    return re.findall(r"""^\s*import[^'"]*['"]([^'"]+)['"]""", source, re.M)


def test_the_review_is_served_at_review(client):
    r = client.get("/review/")
    assert r.status_code == 200
    assert "review-app.js" in r.text


def test_the_old_analysis_app_is_gone(client):
    assert not (FRONTEND_DIR / "app").exists()
    assert client.get("/app/").status_code == 404


@pytest.mark.parametrize(
    "relative",
    [
        "css/review.css",
        "js/review/review-app.js",
        "js/review/review-model.js",
        "js/review/charts.js",
    ],
)
def test_the_review_ships_and_serves_the_file(client, relative):
    assert (FRONTEND_DIR / relative).is_file()
    assert client.get(f"/{relative}").status_code == 200


def test_every_asset_the_page_links_exists():
    markup = PAGE.read_text("utf-8")
    links = re.findall(r"""(?:href|src)=["']([^"':]+\.(?:css|js))["']""", markup)
    assert links
    for path in links:
        # Relative, so the same page works at /review/ and under the Pages subpath.
        assert not path.startswith("/"), path
        assert (PAGE.parent / path).resolve().is_file(), path


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_every_import_resolves_to_a_file(module):
    for specifier in _imports(module.read_text("utf-8")):
        assert specifier.startswith(("./", "../")), f"{module.name}: bare import {specifier}"
        assert (module.parent / specifier).resolve().is_file(), f"{module.name}: {specifier}"


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_innerhtml(module):
    assert "innerHTML" not in module.read_text("utf-8"), module.name


def test_the_review_model_is_pure():
    source = (JS_DIR / "review" / "review-model.js").read_text("utf-8")
    assert _imports(source) == ["../shared/activity-model.js"]
    for forbidden in ("document.", "window.", "fetch(", "WebSocket"):
        assert forbidden not in source, forbidden


def test_every_id_and_data_role_the_app_queries_exists_on_the_page():
    markup = PAGE.read_text("utf-8")
    roles = set(re.findall(r"""data-role=["']([\w-]+)["']""", markup))
    ids = set(re.findall(r"""\bid=["']([\w-]+)["']""", markup))
    app = (JS_DIR / "review" / "review-app.js").read_text("utf-8")
    runs = (JS_DIR / "live" / "runs-panel.js").read_text("utf-8")
    wanted_roles = set(re.findall(r"""\bq\(['"]([\w-]+)['"]\)""", app))
    wanted_roles |= set(re.findall(r"""\[data-role=["']([\w-]+)["']\]""", runs))
    wanted_ids = set(re.findall(r"""\$\(['"]([\w-]+)['"]\)""", app))
    assert wanted_roles and wanted_ids, "the review stopped using q()/$() -- update this test"
    assert wanted_roles <= roles, f"missing data-role: {sorted(wanted_roles - roles)}"
    assert wanted_ids <= ids, f"missing id: {sorted(wanted_ids - ids)}"


def test_the_review_never_replays_a_synthetic_race_or_reads_storage():
    for module in MODULES:
        source = module.read_text("utf-8")
        assert "'scenario'" not in source, module.name
        assert "localStorage" not in source and "sessionStorage" not in source, module.name


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on PATH (optional)")
def test_the_review_model_suite_passes():
    result = subprocess.run(
        [shutil.which("node"), str(JS_DIR / "run-review-tests.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"\b(\d+)/\1 passed", result.stdout), result.stdout
