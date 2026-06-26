<#
Register the Windows Scheduled Task `DemandMiningEOD` (off-:00, default 21:53, to avoid herd).
Idempotent: re-running updates the action. Pass -ConfigDir to bind a per-product companion config repo.

  powershell -ExecutionPolicy Bypass -File register-task.ps1 [-ConfigDir C:\path\demand-mining-config]

Unregister:  Unregister-ScheduledTask -TaskName DemandMiningEOD -Confirm:$false
#>
param(
  [string]$ConfigDir = "",
  [string]$Time = "21:53",
  [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$wrapper = Join-Path $PSScriptRoot "wrapper.ps1"
if (-not (Test-Path $wrapper)) { throw "wrapper.ps1 not found next to this script" }

$argline = "-ExecutionPolicy Bypass -NoProfile -File `"$wrapper`""
if ($Python)    { $argline += " -Python `"$Python`"" }
if ($ConfigDir) { $argline += " -ConfigDir `"$ConfigDir`"" }

$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argline
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "DemandMiningEOD" -Action $action -Trigger $trigger `
  -Settings $settings -Description "demand-mining: daily EOD user-demand radar + iteration ranking" -Force | Out-Null
Write-Host "Registered DemandMiningEOD at $Time daily. Wrapper: $wrapper"
