"""Fetch the real telemetry fixtures the real-data tests run on.

The files are too large to commit, so they live under ``tests/fixtures/real/``
(gitignored) and each one is checked against a pinned sha256. Tests that need a
fixture skip when it is absent, so the default suite stays offline.

    python scripts/fetch_fixtures.py                    # download what is missing
    python scripts/fetch_fixtures.py --from file.ibt    # use a local copy instead
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "real"


@dataclass(frozen=True)
class Fixture:
    name: str
    sha256: str
    size: int
    url: str
    about: str


FIXTURES = (
    Fixture(
        name="audir8lmsevo2gt3_spa_race.ibt",
        sha256="754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36",
        size=162_304_117,
        url=(
            "https://media.githubusercontent.com/media/SVappsLAB/iRacingTelemetrySDK/"
            "main/Sdk/tests/SmokeTests/data/ibt/audir8lmsevo2gt3_spa%20up.ibt"
        ),
        about=(
            "Audi R8 LMS EVO II GT3, Spa GP Pit, official iRacing race (~42 min, 19 laps, "
            "one pit stop). From SVappsLAB/iRacingTelemetrySDK, Apache-2.0; the driver "
            "in the file is that repo's author."
        ),
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ok(path: Path, fixture: Fixture) -> bool:
    return path.is_file() and path.stat().st_size == fixture.size and (
        _sha256(path) == fixture.sha256
    )


def fetch(fixture: Fixture, source: Path | None = None) -> Path:
    target = FIXTURE_DIR / fixture.name
    if _ok(target, fixture):
        print(f"ok       {fixture.name}")
        return target
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    if source is not None:
        print(f"copying  {source} -> {target}")
        shutil.copyfile(source, partial)
    else:
        print(f"fetching {fixture.name} ({fixture.size / 1e6:.0f} MB)")
        with urllib.request.urlopen(fixture.url) as response, partial.open("wb") as fh:
            shutil.copyfileobj(response, fh, length=1 << 20)
    if not _ok(partial, fixture):
        partial.unlink(missing_ok=True)
        raise SystemExit(f"{fixture.name}: size or sha256 does not match the pinned fixture")
    partial.replace(target)
    print(f"ok       {fixture.name}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", type=Path, help="Local copy of the .ibt.")
    args = parser.parse_args()
    for fixture in FIXTURES:
        fetch(fixture, args.source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
