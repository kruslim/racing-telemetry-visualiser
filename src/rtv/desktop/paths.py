"""Where a frozen ``RTV.exe`` finds its files and keeps its data.

From source, nothing changes: the repo's ``frontend/`` and ``./data`` as usual. In
the PyInstaller one-file build the bundled ``frontend/`` is unpacked under
``sys._MEIPASS``, and data goes to ``%LOCALAPPDATA%\\RTV\\data`` (the exe may be run
from anywhere) unless ``RTV_DATA_DIR`` says otherwise.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def default_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "RTV" / "data"


def prepare_environment(data_dir: str | None = None) -> None:
    """Set the env the server reads, before ``rtv.main`` is imported."""
    if data_dir:
        os.environ["RTV_DATA_DIR"] = data_dir
    elif frozen() and "RTV_DATA_DIR" not in os.environ:
        os.environ["RTV_DATA_DIR"] = str(default_data_dir())
    bundle = bundle_dir()
    if bundle and "RTV_FRONTEND_DIR" not in os.environ and (bundle / "frontend").is_dir():
        os.environ["RTV_FRONTEND_DIR"] = str(bundle / "frontend")
    target = os.environ.get("RTV_DATA_DIR")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
    _ensure_streams(Path(target) if target else Path.cwd())


def _ensure_streams(log_dir: Path) -> None:
    """A windowed exe has no stdout/stderr; logging to ``None`` kills the server thread.

    Send both to ``rtv-desktop.log`` beside the data instead, which is also where to
    look when the window says the server did not start.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = open(log_dir / "rtv-desktop.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
