<#
Register the Windows Scheduled Task `DemandMiningEOD` (off-:00, default 21:53, to avoid herd).
Idempotent: re-running updates the action. Pass -ConfigDir to bind a per-product companion config repo.
The interpreter is fixed here, at registration: -Python, or else the `python` this shell resolves, is
written into the action as an absolute path, so the scheduled run never depends on the task's PATH.

  powershell -ExecutionPolicy Bypass -File register-task.ps1 [-ConfigDir C:\path\demand-mining-config] [-Python C:\path\python.exe]

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

function ConvertTo-WindowsArgument([string]$Value) {
  if ($Value -match '[\x00\r\n]') { throw 'Invalid process argument' }
  # CommandLineToArgvW: double backslashes before quotes and the closing quote.
  $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
  $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
  return '"' + $escaped + '"'
}
if (-not $Python) {
  $c = Get-Command python -ErrorAction SilentlyContinue
  if (-not $c) { throw "python not found; pass -Python <abs path to python.exe>" }
  $Python = $c.Source
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python executable not found: $Python" }
$Python = (Resolve-Path -LiteralPath $Python).Path
if ($Python -match '\\WindowsApps\\') { throw "refusing the Windows Store python alias ($Python); pass -Python <abs path to python.exe>" }
$arguments = @('-ExecutionPolicy','Bypass','-NoProfile','-File',$wrapper,'-Python',$Python)
if ($ConfigDir) { $arguments += @('-ConfigDir',$ConfigDir) }
$argline = ($arguments | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' '

$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argline
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "DemandMiningEOD" -Action $action -Trigger $trigger `
  -Settings $settings -Description "demand-mining: daily EOD user-demand radar + iteration ranking" -Force | Out-Null
Write-Host "Registered DemandMiningEOD at $Time daily. Wrapper: $wrapper  Python: $Python"
