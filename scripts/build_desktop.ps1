# Build dist\RTV.exe: one windowed executable with the RTV server embedded.
#
# Usage:
#   .\scripts\build_desktop.ps1            # installs PyInstaller + the desktop extra, builds
#   .\scripts\build_desktop.ps1 -NoInstall # build with what the venv already has
#
# The exe keeps its data in %LOCALAPPDATA%\RTV\data unless RTV_DATA_DIR is set. The
# pitwall agents still need RTV_PITWALL_AGENTS=true and KIMI_CODE_API_KEY in the
# environment, exactly as for run.ps1.
#
param([switch]$NoInstall)

# Native tools (uv, PyInstaller) write progress to stderr, which Windows PowerShell
# turns into errors under "Stop"; check exit codes instead.
$ErrorActionPreference = "Continue"
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "No .venv: create it first (see CLAUDE.md, Commands)." }

if (-not $NoInstall) {
    uv pip install --python $python -e "$root[desktop,ai]" "pyinstaller>=6.6"
    if ($LASTEXITCODE -ne 0) { throw "Installing the build dependencies failed ($LASTEXITCODE)." }
}

& $python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build\pyinstaller") `
    (Join-Path $root "packaging\rtv-desktop.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)." }
Write-Host "Built $(Join-Path $root 'dist\RTV.exe')"
