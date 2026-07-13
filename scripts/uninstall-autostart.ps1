# Remove the Realty Tracker autostart task registered by install-autostart.ps1.
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1

$ErrorActionPreference = "Stop"
$TaskName = "RealtyTracker"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "No scheduled task '$TaskName' found - nothing to remove."
    exit 0
}
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'."
Write-Host "An already-running dashboard keeps going until you close it or log off;"
Write-Host "it simply will not start again at the next logon."
