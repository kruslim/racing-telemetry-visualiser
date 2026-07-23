# run_pitwall.ps1 - headless staged build of RTV v2 pitwall
# Run from repo root, on a dedicated branch, with the venv activated.
# ASCII only by design: Windows PowerShell 5.1 mangles non-ASCII in scripts.

$ErrorActionPreference = "Stop"

if (-not (Test-Path "prompts")) {
    Write-Host "No .\prompts folder found. Run this from the repo root." -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path "prompts\done" | Out-Null

$prompts = Get-ChildItem -Path "prompts" -Filter "0*.md" | Sort-Object Name

if ($prompts.Count -eq 0) {
    Write-Host "All stages already completed (prompts folder is empty)." -ForegroundColor Green
    exit 0
}

Write-Host ("Stages to run: " + $prompts.Count) -ForegroundColor Cyan

foreach ($f in $prompts) {
    Write-Host ""
    Write-Host ("=== [{0}] starting ===" -f $f.Name) -ForegroundColor Cyan

    $promptText = Get-Content -Raw $f.FullName
    claude -p $promptText --dangerously-skip-permissions

    if ($LASTEXITCODE -ne 0) {
        Write-Host ("claude exited non-zero on {0} - stopping." -f $f.Name) -ForegroundColor Red
        Write-Host "If this was a 401: run 'claude' then /login, and re-run this script." -ForegroundColor Yellow
        exit 1
    }

    Write-Host ("=== [{0}] verifying with pytest ===" -f $f.Name) -ForegroundColor Cyan
    pytest -q

    if ($LASTEXITCODE -ne 0) {
        Write-Host ("pytest FAILED after {0} - stopping." -f $f.Name) -ForegroundColor Red
        Write-Host "Fix interactively, then re-run this script (completed stages are skipped)." -ForegroundColor Yellow
        exit 1
    }

    git add -A
    git commit -m ("pitwall: " + $f.BaseName)

    Move-Item -Path $f.FullName -Destination ("prompts\done\" + $f.Name) -Force
    git add -A
    git commit -m ("pitwall: mark " + $f.BaseName + " done") --allow-empty

    Write-Host ("=== [{0}] committed ===" -f $f.Name) -ForegroundColor Green
}

Write-Host ""
Write-Host "All stages complete. See docs/PITWALL.md for the demo path." -ForegroundColor Green
