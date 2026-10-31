"""The live view's new frontend pieces: shipped, served, wired, and tested.

The pure parts (activity model, timeline geometry) are asserted in JavaScript by
frontend/js/shared/shared.test.js; this file runs that suite when node is on PATH
and checks the static contracts a build step would otherwise catch.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

pytest.importorskip("fastapi")

from rtv.main import FRONTEND_DIR  # noqa: E402

JS_DIR = FRONTEND_DIR / "js"
NEW_MODULES = [
    *sorted((JS_DIR / "shared").glob("*.js")),
    *sorted((JS_DIR / "live").glob("*.js")),
]


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


@pytest.mark.parametrize(
    "relative",
    [
        "css/live.css",
        "js/shared/activity-model.js",
        "js/shared/timeline.js",
        "js/shared/flow-graph.js",
        "js/live/runs-panel.js",
        "js/live/telemetry-strip.js",
        "js/live/onboard-panel.js",
    ],
)
def test_the_live_view_ships_and_serves_the_file(client, relative):
    assert (FRONTEND_DIR / relative).is_file()
    assert client.get(f"/{relative}").status_code == 200


@pytest.mark.parametrize("module", NEW_MODULES, ids=lambda p: p.name)
def test_every_new_import_resolves_to_a_file(module):
    for specifier in _imports(module.read_text("utf-8")):
        assert specifier.startswith(("./", "../")), f"{module.name}: bare import {specifier}"
        assert (module.parent / specifier).resolve().is_file(), f"{module.name}: {specifier}"


@pytest.mark.parametrize("module", NEW_MODULES, ids=lambda p: p.name)
def test_no_innerhtml_in_the_new_modules(module):
    assert "innerHTML" not in module.read_text("utf-8"), module.name


def test_the_activity_model_is_pure():
    """The review rebuilds state from recorded records with this module."""
    source = (JS_DIR / "shared" / "activity-model.js").read_text("utf-8")
    assert _imports(source) == []
    for forbidden in ("document.", "window.", "fetch(", "WebSocket"):
        assert forbidden not in source, forbidden


def test_every_data_role_the_new_panels_query_exists_on_the_page():
    markup = (FRONTEND_DIR / "index.html").read_text("utf-8")
    present = set(re.findall(r"""data-role=["']([\w-]+)["']""", markup))
    wanted = set()
    for module in [JS_DIR / "pitwall-app.js", *NEW_MODULES]:
        wanted |= set(
            re.findall(r"""\[data-role=["']([\w-]+)["']\]""", module.read_text("utf-8"))
        )
    assert wanted, "the live view stopped using data-role -- update this test"
    assert wanted <= present, f"missing from index.html: {sorted(wanted - present)}"


def test_the_live_view_offers_no_synthetic_race():
    source = (JS_DIR / "pitwall-app.js").read_text("utf-8")
    assert "'scenario'" not in source


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on PATH (optional)")
def test_the_shared_component_suite_passes():
    result = subprocess.run(
        [shutil.which("node"), str(JS_DIR / "run-shared-tests.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"\b(\d+)/\1 passed", result.stdout), result.stdout
