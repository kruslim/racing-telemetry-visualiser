"""The stage-6 pitwall UI, checked from Python.

Same division of labour stage 4 settled on. The rules that turn a ``RaceState``
into something on screen are pure JavaScript and are asserted there
(``frontend/js/pitwall/view-model.test.js``, in-page via
``frontend/pitwall-test.html`` or headlessly under node). What Python owns is
everything a missing build step would otherwise let rot silently:

1. FastAPI serves every file the app imports, and the import graph resolves;
2. every element id, ``data-role`` and ``data-field`` a module reaches for
   exists on the page it reaches for it on;
3. the app talks to endpoints that exist, at a rate the socket allows;
4. the house rules still hold across the files this stage added.

The node suite runs here too when a ``node`` binary happens to be on PATH -- it
is not required, exactly as in stage 4.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from rtv.main import FRONTEND_DIR  # noqa: E402

JS_DIR = FRONTEND_DIR / "js"
PITWALL_JS = JS_DIR / "pitwall"

NEW_FILES = [
    "index.html",
    "pitwall-test.html",
    "css/app.css",
    "js/pitwall-app.js",
    "js/run-pitwall-tests.mjs",
    "js/pitwall/format.js",
    "js/pitwall/view-model.js",
    "js/pitwall/view-model.test.js",
    "js/pitwall/dom.js",
    "js/pitwall/status-panel.js",
    "js/pitwall/strategy-panel.js",
    "js/pitwall/tower-panel.js",
    "js/pitwall/radio-panel.js",
    "js/pitwall/ticker-panel.js",
]

#: Every module the pitwall app pulls in, directly or otherwise.
APP_MODULES = [
    JS_DIR / "pitwall-app.js",
    *sorted(PITWALL_JS.glob("*.js")),
]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path_factory.mktemp("data")))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    os.environ.pop("RTV_PITWALL", None)

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


# --------------------------------------------------------------------------
# the files exist and are served
# --------------------------------------------------------------------------
@pytest.mark.parametrize("relative", NEW_FILES)
def test_the_pitwall_ui_ships_the_file(relative):
    assert (FRONTEND_DIR / relative).is_file()


@pytest.mark.parametrize(
    "path,needle",
    [
        ("/", "mode-switch"),
        ("/css/app.css", "pitwall-grid"),
        ("/js/pitwall-app.js", "/ws/pitwall"),
        ("/js/pitwall/view-model.js", "strategyView"),
        ("/js/pitwall/format.js", "n/a"),
        ("/pitwall-test.html", "view-model.test.js"),
    ],
)
def test_the_pitwall_ui_is_served(client, path, needle):
    response = client.get(path)
    assert response.status_code == 200
    assert needle in response.text


def test_the_root_is_the_pitwall_app_with_both_modes(client):
    body = client.get("/").text
    assert 'data-mode="analysis"' in body
    assert 'data-mode="pitwall"' in body
    assert 'id="mode-pitwall"' in body and 'id="mode-analysis"' in body
    assert "/js/pitwall-app.js" in body
    # The v1 analysis worksheets are a separate app; the mode must not pretend
    # to be them.
    assert "analysis-url" in body


def test_the_stage_four_pages_still_work(client):
    """Stage 6 replaced the placeholder index; it must not have taken the rest."""
    for path in ("/radio.html", "/audio-test.html", "/js/pitwall-radio.js"):
        assert client.get(path).status_code == 200, path


# --------------------------------------------------------------------------
# the import graph resolves (there is no bundler to catch a typo)
# --------------------------------------------------------------------------
def _imports(path: Path) -> list[str]:
    source = path.read_text("utf-8")
    return re.findall(r"""^\s*import[^'"]*['"]([^'"]+)['"]""", source, re.M)


@pytest.mark.parametrize("module", APP_MODULES, ids=lambda p: p.name)
def test_every_import_resolves_to_a_file_on_disk(module):
    for specifier in _imports(module):
        assert specifier.startswith(("./", "../", "/")), (
            f"{module.name} imports a bare specifier {specifier!r} -- that needs a "
            "bundler, and this project does not have one"
        )
        target = (
            (FRONTEND_DIR / specifier.lstrip("/"))
            if specifier.startswith("/")
            else (module.parent / specifier)
        ).resolve()
        assert target.is_file(), f"{module.name} imports {specifier}, which is not a file"


def test_the_app_module_is_served_for_every_import_it_makes(client):
    for specifier in _imports(JS_DIR / "pitwall-app.js"):
        url = specifier if specifier.startswith("/") else f"/js/{specifier.lstrip('./')}"
        response = client.get(url)
        assert response.status_code == 200, url
        assert "javascript" in response.headers["content-type"], url


# --------------------------------------------------------------------------
# every element the scripts reach for exists
# --------------------------------------------------------------------------
def test_every_element_id_the_app_reaches_for_exists_on_the_page():
    source = (JS_DIR / "pitwall-app.js").read_text("utf-8")
    markup = (FRONTEND_DIR / "index.html").read_text("utf-8")
    wanted = set(re.findall(r"""(?:\$|getElementById)\(\s*['"]([\w-]+)['"]""", source))
    assert wanted, "no element lookups found -- the pattern has drifted"
    present = set(re.findall(r"""\bid=["']([\w-]+)["']""", markup))
    assert wanted <= present, f"missing from index.html: {sorted(wanted - present)}"


def test_every_data_role_a_panel_queries_exists_on_the_page():
    markup = (FRONTEND_DIR / "index.html").read_text("utf-8")
    present = set(re.findall(r"""data-role=["']([\w-]+)["']""", markup))
    wanted = set()
    for module in PITWALL_JS.glob("*-panel.js"):
        wanted |= set(
            re.findall(r"""\[data-role=["']([\w-]+)["']\]""", module.read_text("utf-8"))
        )
    assert wanted, "the panels stopped using data-role -- update this test"
    assert wanted <= present, f"missing from index.html: {sorted(wanted - present)}"


def test_every_data_field_a_panel_writes_exists_on_the_page():
    """`_set('fuel-margin', ...)` against a `data-field` that is not there is a
    silent no-op -- exactly the class of dead control a build step would catch."""
    markup = (FRONTEND_DIR / "index.html").read_text("utf-8")
    present = set(re.findall(r"""data-field=["']([\w-]+)["']""", markup))
    wanted = set()
    for module in PITWALL_JS.glob("*-panel.js"):
        wanted |= set(re.findall(r"""_set\(\s*['"]([\w-]+)['"]""", module.read_text("utf-8")))
    assert wanted, "the panels stopped using _set -- update this test"
    assert wanted <= present, f"missing from index.html: {sorted(wanted - present)}"


# --------------------------------------------------------------------------
# it talks to endpoints that exist
# --------------------------------------------------------------------------
def test_the_app_only_calls_documented_endpoints(client):
    source = (JS_DIR / "pitwall-app.js").read_text("utf-8")
    called = set(re.findall(r"""\$\{API\}(/[\w/-]+)""", source))
    assert called, "the API call pattern has drifted"
    routes = {route.path for route in client.app.routes}
    for path in called:
        assert f"/api/v1{path}" in routes, f"the UI calls {path}, which is not a route"


def test_the_socket_subscribes_within_the_render_ceiling():
    """Asking for more state frames than the app can paint is pure bandwidth."""
    source = (JS_DIR / "pitwall-app.js").read_text("utf-8")
    rate = re.search(r"rate_hz:\s*([\d.]+)", source)
    assert rate, "the subscribe frame no longer names a rate"
    ceiling = re.search(r"RENDER_MS\s*=\s*(\d+)", source)
    assert ceiling, "the render ceiling is no longer a named constant"
    assert float(rate.group(1)) <= 1000.0 / float(ceiling.group(1))


def test_the_ui_never_reaches_past_the_api_for_state():
    """No second source of truth: state comes off the socket, not from guesses."""
    source = (JS_DIR / "pitwall/view-model.js").read_text("utf-8")
    for forbidden in ("fetch(", "document.", "window.", "setTimeout", "setInterval"):
        assert forbidden not in source, f"{forbidden} leaked into the pure view model"


def test_the_pure_modules_import_nothing_impure():
    for name in ("format.js", "view-model.js"):
        source = (PITWALL_JS / name).read_text("utf-8")
        for specifier in _imports(PITWALL_JS / name):
            assert specifier in ("./format.js",), f"{name} imports {specifier}"
        assert "getContext" not in source


# --------------------------------------------------------------------------
# the degradation contract the UI is built on
# --------------------------------------------------------------------------
def test_the_ui_is_told_which_groups_are_unbacked(client):
    """`capabilities` is what makes `n/a` possible; assert the payload has it."""
    state = client.get("/api/v1/racestate").json()
    assert "capabilities" in state
    for group in ("fuel", "tyres", "standings", "conditions"):
        assert group in state["capabilities"]
    assert state["capabilities"]["missing"] == [] or isinstance(
        state["capabilities"]["missing"], list
    )


def test_the_status_endpoint_answers_even_with_no_agents(client):
    body = client.get("/api/v1/pitwall/status").json()
    assert body["available"] is False
    assert body["reason"]
    assert "known_agents" in body


# --------------------------------------------------------------------------
# house rules (the whole-frontend sweeps live in test_pitwall_audio_frontend)
# --------------------------------------------------------------------------
def test_the_new_pages_use_the_shared_stylesheet():
    for page in ("index.html", "pitwall-test.html"):
        assert "/css/pitwall.css" in (FRONTEND_DIR / page).read_text("utf-8"), page


def test_no_innerhtml_in_the_pitwall_panels():
    """Agent prose and session-info strings are not markup this app wrote."""
    from tests.test_pitwall_audio_frontend import strip_comments

    for module in APP_MODULES:
        assert "innerHTML" not in strip_comments(module.read_text("utf-8")), module.name


def test_the_self_test_page_runs_the_same_module_the_suite_does():
    page = (FRONTEND_DIR / "pitwall-test.html").read_text("utf-8")
    assert "/js/pitwall/view-model.test.js" in page
    assert "runPitwallTests" in page
    assert "__RTV_PITWALL_TEST__" in page


# --------------------------------------------------------------------------
# the JS suite, when node is available
# --------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("node") is None, reason="no node on PATH (optional)")
def test_the_javascript_view_model_suite_passes():
    result = subprocess.run(
        [shutil.which("node"), str(JS_DIR / "run-pitwall-tests.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    assert re.search(r"\b(\d+)/\1 passed", result.stdout), result.stdout
    assert int(re.search(r"(\d+)/\d+ passed", result.stdout).group(1)) >= 25
