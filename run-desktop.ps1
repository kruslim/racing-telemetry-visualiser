# Launch RTV Desktop, the native Qt app (the server runs embedded in it).
#
# Usage:
#   .\run-desktop.ps1                                  # embedded server, free port
#   .\run-desktop.ps1 -Server http://127.0.0.1:8000    # attach to a running .\run.ps1
#   .\run-desktop.ps1 -DataDir D:\rtv-data             # keep the store elsewhere
#
# Needs the desktop extra once:
#   uv pip install --python .venv\Scripts\python.exe -e ".[desktop]"
#
param(
    [string]$Server = "",
    [int]$Port = 0,
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $python = $venvPython
} else {
    $python = "python"
}

$env:PYTHONPATH = Join-Path $PSScriptRoot "src"
$cliArgs = @()
if ($Server) { $cliArgs += @("--server", $Server) }
if ($Port -gt 0) { $cliArgs += @("--port", $Port) }
if ($DataDir) { $cliArgs += @("--data-dir", $DataDir) }

& $python -m rtv.desktop @cliArgs
