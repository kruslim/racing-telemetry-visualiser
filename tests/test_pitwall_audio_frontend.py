"""The frontend half, checked from Python.

The audio-discipline rules themselves are asserted in JavaScript, where they
live: ``frontend/js/audio-manager.test.js`` runs in-page from
``frontend/audio-test.html`` and headlessly under node. This module covers the
three things Python *can* own honestly:

1. FastAPI actually serves the files (a page that 404s is not a frontend);
2. the frontend's offline voice table has not drifted from the backend's;
3. the house rules hold -- no storage APIs, no build step, no frameworks.

Plus the JS suite itself when a ``node`` binary happens to be on PATH. It is not
required: the repo has no JS toolchain and this stage did not add one.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from rtv.main import FRONTEND_DIR  # noqa: E402
from rtv.pitwall.tts import DEFAULT_VOICES, FALLBACK_VOICE  # noqa: E402

JS_DIR = FRONTEND_DIR / "js"
EXPECTED_FILES = [
    "index.html",
    "radio.html",
    "audio-test.html",
    "css/pitwall.css",
    "js/audio-manager.js",
    "js/audio-manager.test.js",
    "js/radio-voices.js",
    "js/speech.js",
    "js/pitwall-radio.js",
    "js/run-audio-tests.mjs",
]


def strip_comments(source: str) -> str:
    """Code only. These checks are about what the files *do*, not what they say.

    Naive on purpose: block comments, then whole-line ``//`` and ``*`` lines. It
    never touches a ``//`` inside a string, which is what a smarter version would
    get wrong on the first URL it met.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    kept = [
        line
        for line in source.splitlines()
        if not line.lstrip().startswith(("//", "*"))
    ]
    return "\n".join(kept)


def frontend_sources() -> list[Path]:
    return [
        path
        for path in FRONTEND_DIR.rglob("*")
        if path.is_file() and path.suffix in {".js", ".mjs", ".html", ".css"}
    ]


# --------------------------------------------------------------------------
# the files exist and are served
# --------------------------------------------------------------------------
@pytest.mark.parametrize("relative", EXPECTED_FILES)
def test_the_frontend_ships_the_file(relative):
    assert (FRONTEND_DIR / relative).is_file()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RTV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RTV_AUTOSTART_LIVE", "false")
    from rtv.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from rtv.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_the_root_serves_the_frontend(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Pitwall radio" in response.text


@pytest.mark.parametrize(
    "path,needle",
    [
        ("/radio.html", "pitwall-radio.js"),
        ("/audio-test.html", "audio-manager.test.js"),
        ("/js/audio-manager.js", "RadioAudioManager"),
        ("/js/speech.js", "speechSynthesis"),
        ("/css/pitwall.css", "--critical"),
    ],
)
def test_static_pages_and_modules_are_served(client, path, needle):
    response = client.get(path)
    assert response.status_code == 200
    assert needle in response.text


def test_javascript_is_served_as_javascript(client):
    """A module served as text/plain does not import; browsers are strict here."""
    content_type = client.get("/js/audio-manager.js").headers["content-type"]
    assert "javascript" in content_type


def test_mounting_the_frontend_did_not_shadow_the_api(client):
    """The v1 surface has to be reachable with a catch-all mount on '/'."""
    assert client.get("/api/v1/health").json()["status"] == "ok"
    assert client.get("/api/v1/sessions").status_code == 200
    assert client.get("/api/v1/racestate").status_code == 200
    # A genuinely missing API path is still FastAPI's 404, not the file server's.
    assert client.get("/api/v1/nope").status_code == 404
    # And a non-GET to a path nobody serves is 404, not the static handler's 405.
    assert client.post("/api/v1/nope", json={}).status_code == 404
    assert client.post("/radio.html").status_code == 404


# --------------------------------------------------------------------------
# the two voice tables agree
# --------------------------------------------------------------------------
def parse_default_voices_js() -> dict:
    """``radio-voices.js`` is deliberately strict JSON so this can be exact."""
    source = (JS_DIR / "radio-voices.js").read_text(encoding="utf-8")
    match = re.search(r"export const DEFAULT_VOICES = (\{.*?\n\});", source, re.S)
    assert match, "could not find the DEFAULT_VOICES literal"
    return json.loads(match.group(1))


def test_the_browser_voice_table_matches_the_backend_one():
    js = parse_default_voices_js()
    for agent, profile in DEFAULT_VOICES.items():
        assert agent in js, f"{agent} is missing from radio-voices.js"
        assert js[agent] == profile.to_api(), agent


def test_the_browser_table_has_a_fallback_for_an_agent_nobody_gave_a_voice():
    assert parse_default_voices_js()["default"] == FALLBACK_VOICE.to_api()


def test_every_role_is_distinguishable_by_ear_in_the_browser_table_too():
    js = parse_default_voices_js()
    signatures = {
        agent: (js[agent]["rate"], js[agent]["pitch"], js[agent]["lang"])
        for agent in ("strategist", "vehicle_engineer", "spotter", "coach")
    }
    assert len(set(signatures.values())) == 4


# --------------------------------------------------------------------------
# house rules
# --------------------------------------------------------------------------
def test_no_storage_apis_anywhere_in_the_frontend():
    """Mutes and volume live in the URL. The spec is explicit; so is this."""
    offenders = [
        path.name
        for path in frontend_sources()
        if re.search(
            r"\b(localStorage|sessionStorage|indexedDB)\b",
            strip_comments(path.read_text("utf-8")),
        )
    ]
    assert offenders == []


def test_no_build_step_and_no_frameworks():
    for marker in ("package.json", "node_modules", "vite.config.js", "webpack.config.js"):
        assert not (FRONTEND_DIR / marker).exists()
    for path in frontend_sources():
        text = strip_comments(path.read_text("utf-8"))
        assert "require(" not in text, path.name
        assert "cdn.jsdelivr" not in text and "unpkg.com" not in text, path.name


def test_the_audio_manager_is_pure_logic():
    """Its whole point: the queue rules must be testable without a browser."""
    source = strip_comments((JS_DIR / "audio-manager.js").read_text("utf-8"))
    for forbidden in ("speechSynthesis", "SpeechSynthesisUtterance", "document.", "window."):
        assert forbidden not in source, f"{forbidden} leaked into the pure module"
    assert "setTimeout" not in source and "setInterval" not in source


def test_the_self_test_page_runs_the_same_module_the_app_does():
    page = (FRONTEND_DIR / "audio-test.html").read_text("utf-8")
    assert "/js/audio-manager.test.js" in page
    assert "runAudioTests" in page
    assert "__RTV_AUDIO_TEST__" in page, "the page must publish a machine-readable result"


@pytest.mark.parametrize(
    "script,page",
    [("js/pitwall-radio.js", "radio.html"), ("audio-test.html", "audio-test.html")],
)
def test_every_element_the_script_reaches_for_exists_on_its_page(script, page):
    """No build step means no compiler; a typo'd id is a silent dead control."""
    source = (FRONTEND_DIR / script).read_text("utf-8")
    markup = (FRONTEND_DIR / page).read_text("utf-8")
    wanted = set(re.findall(r"""(?:\$|getElementById)\(\s*['"]([\w-]+)['"]""", source))
    assert wanted, "no element lookups found -- the pattern has drifted"
    present = set(re.findall(r"""\bid=["']([\w-]+)["']""", markup))
    assert wanted <= present, f"missing from {page}: {sorted(wanted - present)}"


def test_push_to_talk_is_feature_detected_and_posts_to_the_documented_endpoint():
    speech = (JS_DIR / "speech.js").read_text("utf-8")
    assert "webkitSpeechRecognition" in speech
    radio = (JS_DIR / "pitwall-radio.js").read_text("utf-8")
    assert "/pitwall/driver-message" in radio
    assert "ptt-row" in radio and "hidden = true" in radio, "hidden where unsupported"


# --------------------------------------------------------------------------
# the JS suite, when node is available
# --------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("node") is None, reason="no node on PATH (optional)")
def test_the_javascript_audio_discipline_suite_passes():
    result = subprocess.run(
        [shutil.which("node"), str(JS_DIR / "run-audio-tests.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    # A suite that silently stopped asserting things would also "pass".
    assert re.search(r"\b(\d+)/\1 passed", result.stdout), result.stdout
    assert int(re.search(r"(\d+)/\d+ passed", result.stdout).group(1)) >= 15
