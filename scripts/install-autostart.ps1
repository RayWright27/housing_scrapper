# Register a Windows Scheduled Task that starts Realty Tracker at logon: one
# process serving the dashboard AND running the background CIAN scheduler
# (CLAUDE.md §9a/§16.7, Decision A). "At log on" beats a Startup-folder shortcut
# - it restarts on failure and runs without a console cluttering your desktop.
#
# Run once (from anywhere):
#   powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
# Remove:
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1
#
# This changes a Windows setting on your machine - review it before running.

$ErrorActionPreference = "Stop"
$TaskName = "RealtyTracker"
$root = Split-Path $PSScriptRoot -Parent

# Locate the venv interpreter. Prefer windowless pythonw.exe so no console window
# lingers for the long-running server; fall back to python.exe.
$candidates = @(
    (Join-Path $root "venv\Scripts\pythonw.exe"),
    (Join-Path $root ".venv\Scripts\pythonw.exe"),
    (Join-Path $root "venv\Scripts\python.exe"),
    (Join-Path $root ".venv\Scripts\python.exe")
)
$exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) {
    Write-Error "No venv Python found under venv\ or .venv\. Create it first (CLAUDE.md §11)."
    exit 1
}

$action = New-ScheduledTaskAction -Execute $exe -Argument "-m src.main serve" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
# Start when the machine catches up after being off; restart a few times if the
# process ever exits; no execution time limit (it is meant to run all day).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive

# Idempotent: replace any existing registration.
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Realty Tracker: dashboard + background CIAN scheduler (starts at logon)." | Out-Null

Write-Host "Registered scheduled task '$TaskName'."
Write-Host "  runs:  $exe -m src.main serve"
Write-Host "  in:    $root"
Write-Host ""
Write-Host "Start it now without logging out:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Then open the dashboard (default http://127.0.0.1:8000) and check the"
Write-Host "scheduler pill in the header for its heartbeat."
Write-Host "Remove with:  powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1"
