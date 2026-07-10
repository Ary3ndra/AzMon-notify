# -- one-click launch (PowerShell) ---------------------------------------
# Run with:  .\run.ps1
# (Right-click > "Run with PowerShell" also works; double-click in Explorer
#  opens .ps1 files in an editor by default, it will NOT execute this.)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Fail($msg) {
    Write-Host "[!] $msg" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Fail "Azure CLI not found. Install it from https://aka.ms/installazurecli, then run: az login"
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail "Python not found in PATH. Install Python 3.11+ from https://www.python.org/downloads/ (tick 'Add to PATH')."
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual env..."
    python -m venv .venv
    if (-not (Test-Path $venvPython)) {
        Fail "Virtual env creation failed."
    }
}

Write-Host "Installing dependencies..."
& $venvPython -m pip install -q --upgrade pip
& $venvPython -m pip install -q -r requirements.txt

# Always verify the Azure CLI session before launching - DefaultAzureCredential
# reuses it. If not signed in (or the token is stale/expired), prompt az login
# and retry up to 3 times before giving up. Sign-in is the user's choice; this
# just makes sure a usable session exists so polling won't fail on auth.
Write-Host "Checking Azure CLI sign-in..."
$account = $null
for ($try = 1; $try -le 3; $try++) {
    $account = az account show 2>$null | ConvertFrom-Json
    if ($account) { break }
    Write-Host "Not signed in (or session expired) - launching az login... (attempt $try/3)" -ForegroundColor Yellow
    az login | Out-Null
}
if (-not $account) {
    Fail "Azure sign-in failed after 3 attempts. Run 'az login' manually, then re-run this script."
}
Write-Host "Signed in as $($account.user.name)  (tenant $($account.tenantId))" -ForegroundColor Green

$ip = (Get-NetIPConfiguration -ErrorAction SilentlyContinue |
    Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
    Select-Object -First 1 -ExpandProperty IPv4Address |
    Select-Object -First 1 -ExpandProperty IPAddress)

Write-Host ""
Write-Host "  Console:  http://localhost:8000"
if ($ip) {
    Write-Host "  Phone:    http://${ip}:8000   (same Wi-Fi)"
}
Write-Host ""

$env:AZMON_CONFIG = "config.yaml"
& $venvPython -m azmon_notify.web.app

Read-Host "Press Enter to close"