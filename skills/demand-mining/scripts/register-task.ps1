<#
Register the Windows Scheduled Task `DemandMiningEOD` (off-:00, default 21:53, to avoid herd).
Idempotent: re-running updates the action. Pass -ConfigDir to bind a per-product companion config repo.

  powershell -ExecutionPolicy Bypass -File register-task.ps1 [-ConfigDir C:\path\demand-mining-config]

Unregister:  Unregister-ScheduledTask -TaskName DemandMiningEOD -Confirm:$false
#>
param(
  [string]$ConfigDir = "",
  [string]$Time = "21:53",
  [string]$Python = "",
  [string]$TaskContextScript = $env:TASK_CONSOLE_CONTEXT_SCRIPT
)
$ErrorActionPreference = "Stop"
$wrapper = Join-Path $PSScriptRoot "wrapper.ps1"
if (-not (Test-Path $wrapper)) { throw "wrapper.ps1 not found next to this script" }

function ConvertTo-WindowsArgument([string]$Value) {
  if ($Value -match '[\x00\r\n]') { throw 'Invalid process argument' }
  # CommandLineToArgvW: double backslashes before quotes and the closing quote.
  $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
  $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
  return '"' + $escaped + '"'
}
$arguments = @('-ExecutionPolicy','Bypass','-NoProfile','-File',$wrapper,'-Scheduled')
if ($Python)    { $arguments += @('-Python',$Python) }
if ($ConfigDir) { $arguments += @('-ConfigDir',$ConfigDir) }
if ($TaskContextScript) { $arguments += @('-TaskContextScript',$TaskContextScript) }
$argline = ($arguments | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' '

$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argline
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "DemandMiningEOD" -Action $action -Trigger $trigger `
  -Settings $settings -Description "demand-mining: daily EOD user-demand radar + iteration ranking" -Force | Out-Null
Write-Host "Registered DemandMiningEOD at $Time daily. Wrapper: $wrapper"
