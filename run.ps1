# Launch the Racing Telemetry Visualiser backend.
#
# Usage:
#   .\run.ps1                 # start the API server
#   .\run.ps1 -Reload         # dev mode with auto-reload (poller guarded against reloader child)
#
param(
    [switch]$Reload,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

# Prefer the local virtualenv if present.
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $python = $venvPython
} else {
    $python = "python"
}

$reloadArg = ""
if ($Reload) { $reloadArg = "--reload" }

& $python -m uvicorn rtv.main:app --app-dir (Join-Path $PSScriptRoot "src") `
    --host $BindHost --port $Port --workers 1 $reloadArg
