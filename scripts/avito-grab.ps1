# Avito capture helper (§7-clean): opens all your tracked Avito listings/searches
# as tabs in a real Chrome, lets YOU load/solve them as a human, then reads the
# already-loaded pages and records prices. No automated requests to Avito.
#
# Usage (from anywhere):  powershell -ExecutionPolicy Bypass -File scripts\avito-grab.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$profileDir = Join-Path $root "data\chrome-grab"

# --- locate Chrome ---
$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { $chrome = (Get-Command chrome.exe -ErrorAction SilentlyContinue).Source }
if (-not $chrome) { Write-Error "Chrome not found — edit the path at the top of this script."; exit 1 }

# --- gather the Avito URLs we already track ---
$urls = @(& $py -m src.main urls --source avito) | Where-Object { $_ -and $_.Trim() }
if ($urls.Count -eq 0) {
    Write-Host "No Avito URLs tracked yet. Add some first (dashboard, or: python -m src.main add --source avito --url ...)."
    exit 0
}

Write-Host "Opening $($urls.Count) Avito tab(s) in Chrome (debug profile: $profileDir)..."
$chromeArgs = @("--remote-debugging-port=9222", "--user-data-dir=$profileDir") + $urls
Start-Process -FilePath $chrome -ArgumentList $chromeArgs

Write-Host ""
Write-Host "Let every tab finish loading (solve any challenge by hand if one appears)."
Read-Host "Then press Enter here to read the tabs and record prices" | Out-Null

& $py -m src.main grab
Write-Host ""
Write-Host "Done. You can close the Chrome window."
