# Avito capture helper (§7-clean): opens all your tracked Avito listings/searches
# as tabs in a real Chrome, lets YOU load/solve them as a human, then reads the
# already-loaded pages and records prices. No automated requests to Avito.
#
# Usage (from anywhere):  powershell -ExecutionPolicy Bypass -File scripts\avito-grab.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = Join-Path $root "venv\Scripts\python.exe" }
if (-not (Test-Path $py)) { $py = "python" }
$profileDir = Join-Path $root "data\chrome-grab"

# --- locate Chrome ---
$chrome = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { $chrome = (Get-Command chrome.exe -ErrorAction SilentlyContinue).Source }
if (-not $chrome) { Write-Error "Chrome not found - edit the path at the top of this script."; exit 1 }

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

# A visible countdown that returns early if Enter is pressed. Works whether the
# console is interactive (attended) or not (unattended dashboard Refresh): if the
# key API is unavailable it simply counts down the full time.
function Wait-OrEnter([int]$seconds, [string]$label) {
    $canRead = $true
    for ($i = $seconds; $i -gt 0; $i--) {
        Write-Host -NoNewline ("`r{0} {1,3} s ... (press Enter now)   " -f $label, $i)
        for ($t = 0; $t -lt 10; $t++) {
            if ($canRead) {
                try {
                    if ([Console]::KeyAvailable -and [Console]::ReadKey($true).Key -eq 'Enter') {
                        Write-Host ""; return
                    }
                } catch { $canRead = $false }   # non-interactive: stop polling keys
            }
            Start-Sleep -Milliseconds 100
        }
    }
    Write-Host ""
}

# Auto-continue after AVITO_GRAB_LOAD_TIME seconds (default 40) so an unattended
# Refresh finishes on its own; pressing Enter reads the tabs sooner.
$wait = 40
if ($env:AVITO_GRAB_LOAD_TIME -and ($env:AVITO_GRAB_LOAD_TIME -as [int])) {
    $wait = [int]$env:AVITO_GRAB_LOAD_TIME
}
Wait-OrEnter $wait "Reading the tabs in"

& $py -m src.main grab

# Keep this window open so the grab result above stays visible (it auto-closes).
Write-Host ""
Wait-OrEnter 30 "Closing this window in"