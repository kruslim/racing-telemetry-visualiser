"""PostToolUse hook: tidy a Python file Claude just edited.

Reads the hook event (JSON) on stdin. Imports are always sorted (ruff's `I` rules, which
the repo enforces). `ruff format` runs only on new files and on files whose committed
version is already formatted, so editing one of the many unformatted files doesn't
rewrite all of it. No full `ruff check --fix`: it would delete an import added one edit
before its first use. Lint errors are left for `ruff check .`.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

RUFF = [sys.executable, "-m", "ruff"]


def was_formatted(path: Path) -> bool:
    """True if the file is new to git or its HEAD version is already ruff-formatted."""
    head = subprocess.run(
        ["git", "-C", str(path.parent), "show", f"HEAD:./{path.name}"],
        capture_output=True,
        check=False,
    )
    if head.returncode != 0:
        return True
    check = subprocess.run(
        [*RUFF, "format", "--check", "--quiet", "--stdin-filename", str(path), "-"],
        input=head.stdout,
        capture_output=True,
        check=False,
    )
    return check.returncode == 0


def main() -> None:
    event = json.load(sys.stdin)
    path = Path(event.get("tool_input", {}).get("file_path", ""))
    if path.suffix != ".py" or not path.is_file() or importlib.util.find_spec("ruff") is None:
        return
    if was_formatted(path):
        subprocess.run([*RUFF, "format", "--quiet", str(path)], check=False)
    subprocess.run([*RUFF, "check", "--quiet", "--fix", "--select", "I", str(path)], check=False)


if __name__ == "__main__":
    main()
