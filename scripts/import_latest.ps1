# Find the newest iRacing .ibt telemetry file and import it into the
# running Racing Telemetry Visualiser backend.
#
# Usage (server must already be running, e.g. via .\run.ps1):
#   .\scripts\import_latest.ps1
#   .\scripts\import_latest.ps1 -ApiBase http://127.0.0.1:8000/api/v1
#   .\scripts\import_latest.ps1 -TelemetryDir "C:\path\to\telemetry"
#
param(
    [string]$ApiBase = "http://127.0.0.1:8000/api/v1",
    [string]$TelemetryDir = (Join-Path $env:USERPROFILE "Documents\iRacing\telemetry")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $TelemetryDir)) {
    throw "Telemetry directory not found: $TelemetryDir"
}

$latest = Get-ChildItem -Path $TelemetryDir -Filter *.ibt -File |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $latest) {
    throw "No .ibt files found in $TelemetryDir"
}
Write-Host "Importing: $($latest.FullName)  ($([math]::Round($latest.Length/1MB,1)) MB)"

# pathlib on the server accepts forward slashes; avoids JSON backslash-escape issues.
$path = $latest.FullName -replace '\\', '/'
$body = @{ path = $path } | ConvertTo-Json -Compress

$job = Invoke-RestMethod -Method Post -Uri "$ApiBase/import" -ContentType "application/json" -Body $body
Write-Host "Job $($job.job_id): $($job.status)"

# Poll until the import finishes.
while ($true) {
    Start-Sleep -Seconds 2
    $st = Invoke-RestMethod -Method Get -Uri "$ApiBase/import/$($job.job_id)"
    $pct = [math]::Round(($st.progress * 100), 0)
    Write-Host ("  {0,-9} {1,3}%" -f $st.status, $pct)
    if ($st.status -eq "complete") {
        Write-Host "Done. session_id = $($st.session_id)" -ForegroundColor Green
        break
    }
    if ($st.status -eq "error") {
        throw "Import failed: $($st.error)"
    }
}
