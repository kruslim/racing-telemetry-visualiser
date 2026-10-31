# PyInstaller spec for RTV Desktop: one windowed RTV.exe with the server inside.
#
# Build with scripts\build_desktop.ps1 (it installs PyInstaller into the venv).
# The web frontend is bundled too, so the embedded server still serves the browser
# pages (e.g. /radio.html on a tablet) exactly as run.ps1 does.
# ruff: noqa
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

# rtv imports a lot lazily (the agent layer, coaching, optional extras), so pull in
# every submodule explicitly; uvicorn picks its loop/protocol modules by name.
hidden = (
    collect_submodules("rtv")
    + collect_submodules("uvicorn")
    + collect_submodules("irsdk")
    + ["duckdb", "pyarrow", "orjson", "yaml", "websockets", "anthropic", "httpx"]
)
hidden = [h for h in hidden if not h.startswith("rtv.desktop.tests")]

datas = [(str(ROOT / "frontend"), "frontend")]
datas += collect_data_files("rtv")

a = Analysis(
    [str(ROOT / "packaging" / "rtv_desktop_entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PySide6.Qt3DCore",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick",
              "PySide6.QtQml", "PySide6.QtPdf", "PySide6.QtCharts", "PySide6.QtDataVisualization"],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = ROOT / "packaging" / "rtv.ico"
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RTV",
    console=False,
    icon=str(icon) if icon.exists() else None,
    upx=False,
    runtime_tmpdir=None,
)
