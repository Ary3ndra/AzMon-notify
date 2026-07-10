# toast/ntfy/telegram only, no web UI
# Run with:  .\run_headless.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[!] .venv not found. Run .\run.ps1 once first to set it up." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

& $venvPython -m azmon_notify.main -c config.yaml

Read-Host "Press Enter to close"
