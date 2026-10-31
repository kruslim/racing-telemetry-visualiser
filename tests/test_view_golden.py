"""The shared view golden file: the desktop models and the web view models agree.

``tests/fixtures/view_golden/`` is ``payload -> expected view fields`` built from the
Spa example payloads (``scripts/build_view_golden.py``). The Python ports in
``rtv.desktop.model`` must pass every case here, and the JS modules must pass the same
file under node (``frontend/js/run-golden-tests.mjs``). Each expected value holds the
fields both clients share; a client may carry extra fields of its own.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests import view_golden

ROOT = Path(__file__).resolve().parents[1]
INPUTS, CASES = view_golden.load_golden()


def test_the_golden_file_covers_every_shared_module():
    modules = {case["module"] for _, case in CASES}
    assert modules == {"format", "view_model", "activity", "review"}
    assert len(CASES) > 200


@pytest.mark.parametrize("case", [c for _, c in CASES], ids=[f"{f}:{c['name']}" for f, c in CASES])
def test_the_desktop_model_matches_the_golden_file(case):
    actual = view_golden.neutral(view_golden.call(case, INPUTS))
    assert view_golden.mismatch(case["expect"], actual) is None


def test_the_comparison_is_not_vacuous():
    """A wrong value, a missing key and a wrong length all fail."""
    assert view_golden.mismatch({"a": 1}, {"a": 2})
    assert view_golden.mismatch({"a": 1}, {})
    assert view_golden.mismatch([1, 2], [1])
    assert view_golden.mismatch("n/a", None)
    assert view_golden.mismatch(True, 1)
    assert view_golden.mismatch({"a": 1}, {"a": 1.0, "extra": 3}) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on PATH (optional)")
def test_the_web_view_models_match_the_golden_file():
    result = subprocess.run(
        [shutil.which("node"), str(ROOT / "frontend" / "js" / "run-golden-tests.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
