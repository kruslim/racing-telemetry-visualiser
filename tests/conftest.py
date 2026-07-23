"""Shared test guarantees.

The suite is required to run with no iRacing install, no network and no API key.
The first two are structural; the third is not, because a developer who has
``ANTHROPIC_API_KEY`` exported for the Layer-3 coach would otherwise have the
pitwall agent layer wire itself into every app fixture. So the key is removed for
the whole session: an offline suite that only passes on a machine without a key
is not an offline suite.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def _no_api_key() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    yield
    monkeypatch.undo()
