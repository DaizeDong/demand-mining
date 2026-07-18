<#
Register the Windows Scheduled Task `DemandMiningDaemon`: the always-on live-tap gateway daemon,
supervised by daemon_supervisor.py (restart-on-crash) and launched windowless via pythonw.exe.

  powershell -ExecutionPolicy Bypass -File register-daemon-task.ps1 -ConfigDir C:\path\demand-mining-config [-Mode shadow] [-Python C:\path\python.exe]

Modes: shadow (default, safe: capture + admin dashboard, never posts in the community),
       live (also replies/reacts), dry (log only). Flip to live by re-running with -Mode live.

Unregister:  Unregister-ScheduledTask -TaskName DemandMiningDaemon -Confirm:$false

Design notes (each is a scar):
 * Trigger is AtLogOn, NOT a repetition trigger. A Repetition with a Duration silently dies after
   ~24h (NextRun goes blank); AtLogOn has no such trap and also restarts after a reboot.
 * ExecutionTimeLimit is 0 (unlimited). The default kills a task after 3 days, which would take a
   24/7 daemon down every third day.
 * RestartOnFailure is a backstop for the rare case the SUPERVISOR itself dies; the supervisor
   already restarts the daemon on ordinary crashes.
#>
param(
  [Parameter(Mandatory = $true)][string]$ConfigDir,
  [ValidateSet("shadow", "live", "dry")][string]$Mode = "shadow",
  [string]$Python = ""
)
$ErrorActionPreference = "Stop"

$supervisor = Join-Path $PSScriptRoot "daemon_supervisor.py"
if (-not (Test-Path $supervisor)) { throw "daemon_supervisor.py not found next to this script" }
if (-not (Test-Path $ConfigDir))  { throw "ConfigDir not found: $ConfigDir" }

# resolve python.exe, then derive the windowless pythonw.exe next to it for the task action
if (-not $Python) {
  $c = Get-Command python -ErrorAction SilentlyContinue
  if (-not $c) { throw "python not found; pass -Python <abs path to python.exe>" }
  $Python = $c.Source
}
$pythonw = $Python -replace "python\.exe$", "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $Python }  # fall back to python.exe if no pythonw

$argline = "`"$supervisor`" --config-dir `"$ConfigDir`" --python `"$Python`" --mode $Mode"
$action  = New-ScheduledTaskAction -Execute $pythonw -Argument $argline

$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew `
  -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
# Explicit current-user principal (Interactive, Limited run level): without it the CIM registration
# can hit "Access is denied" from a non-elevated shell for a logon-triggered task.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "DemandMiningDaemon" -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal `
  -Description "demand-mining: always-on live demand-tap gateway ($Mode mode)" -Force | Out-Null

Write-Host "Registered DemandMiningDaemon (mode=$Mode)."
Write-Host "  launcher: $pythonw $supervisor"
Write-Host "  config  : $ConfigDir"
Write-Host "Start now:  Start-ScheduledTask -TaskName DemandMiningDaemon"
