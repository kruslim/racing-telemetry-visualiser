"""PyInstaller entry point for ``RTV.exe``: the same ``main`` as ``rtv-desktop``."""

from __future__ import annotations

from rtv.desktop.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
