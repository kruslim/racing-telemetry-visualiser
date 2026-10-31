"""Real API responses from the Spa race, read from the UI brief's payload zip.

``docs/ui-redesign/spa-example-payloads.zip`` holds responses captured from the
app (PR #11). Some files wrap the body as ``{"status", "body"}``; ``load`` unwraps
them. Test-only: nothing at runtime reads this.
"""

from __future__ import annotations

import json
import zipfile
from functools import cache
from pathlib import Path

ZIP = Path(__file__).resolve().parents[1] / "docs" / "ui-redesign" / "spa-example-payloads.zip"


@cache
def _raw(name: str) -> bytes:
    with zipfile.ZipFile(ZIP) as archive:
        return archive.read(f"spa-payloads/{name}")


def load(name: str):
    data = json.loads(_raw(name))
    if isinstance(data, dict) and set(data) == {"status", "body"}:
        return data["body"]
    return data


def journal(name: str = "agents-on/20261031T004037Z-01-replay.jsonl") -> list[dict]:
    """The agents-on race journal, one record per line."""
    return [json.loads(line) for line in _raw(name).splitlines() if line.strip()]
