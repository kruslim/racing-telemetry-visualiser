# run_pitwall.ps1 — headless staged build of RTV v2 pitwall (Windows / PowerShell)
# Run from the repo root, on a dedicated branch, with the venv activated.

$ErrorActionPreference = "Stop"

$prompts = Get-ChildItem -Path "prompts" -Filter "0*.md" | Sort-Object Name

foreach ($f in $prompts) {
    Write-Host "`n=== [$($f.Name)] starting ===" -ForegroundColor Cyan

    $promptText = Get-Content -Raw $f.FullName
    claude -p $promptText --dangerously-skip-permissions
    if ($LASTEXITCODE -ne 0) {
        Write-Host "claude exited non-zero on $($f.Name) — stopping." -ForegroundColor Red
        exit 1
    }

    # Gate: the stage is only accepted if the full suite is green.
    Write-Host "=== [$($f.Name)] verifying with pytest ===" -ForegroundColor Cyan
    pytest -q
    if ($LASTEXITCODE -ne 0) {
        Write-Host "pytest FAILED after $($f.Name) — stopping. Fix or re-run this stage." -ForegroundColor Red
        exit 1
    }

    git add -A
    git commit -m "pitwall: $($f.BaseName)"
    Write-Host "=== [$($f.Name)] committed ===" -ForegroundColor Green
}

Write-Host "`nAll stages complete. Run the demo path from docs/PITWALL.md." -ForegroundColor Green
