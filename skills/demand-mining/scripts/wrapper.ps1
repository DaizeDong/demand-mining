<# Scheduled EOD uses installed llmcall routing and the local deterministic finalizer.

   -Python names the interpreter, and the scheduled task passes it: register-task.ps1 resolves it
   once, when an operator registers the task, and bakes the absolute path into the action. Without
   -Python the wrapper falls back to `python` on PATH, but never to a Windows Store execution alias
   (...\WindowsApps\python.exe): under Task Scheduler that stub opens the Store or hangs instead of
   running anything, and an EOD run that silently does nothing is the failure this wrapper exists
   to surface. #>
param(
  [string]$Python = "",
  [string]$ConfigDir = "",
  [string]$LogDir = ""
)
$ErrorActionPreference = "Stop"
$env:GIT_OPTIONAL_LOCKS = '0'
function ConvertTo-WindowsArgument([string]$Value) {
  if ($Value -match '[\x00\r\n]') { throw 'Invalid process argument' }
  # CommandLineToArgvW: double backslashes before quotes and the closing quote.
  $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
  $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
  return '"' + $escaped + '"'
}
if (-not $Python) {
  $command = Get-Command python -ErrorAction Stop
  $Python = $command.Source
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
  throw "Python executable not found: $Python"
}
$Python = (Resolve-Path -LiteralPath $Python).Path
if ($Python -match '\\WindowsApps\\') {
  throw "python is the Windows Store alias ($Python); pass -Python <absolute python.exe> (re-run register-task.ps1 -Python ...)"
}
$caller = Join-Path $PSScriptRoot 'scheduled.py'
$arguments = @('-X', 'utf8', '-B', $caller)
if ($ConfigDir) { $arguments += @('--config-dir', $ConfigDir) }
if ($LogDir) { $arguments += @('--log-dir', $LogDir) }
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $Python
$startInfo.Arguments = ($arguments | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' '
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$callerProcess = [System.Diagnostics.Process]::Start($startInfo)
try {
  $stdoutCopy = $callerProcess.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput())
  $stderrCopy = $callerProcess.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError())
  $callerProcess.WaitForExit()
  $null = $stdoutCopy.GetAwaiter().GetResult()
  $null = $stderrCopy.GetAwaiter().GetResult()
  $rc = $callerProcess.ExitCode
} finally {
  $callerProcess.Dispose()
}
if ($rc -ne 0) { Write-Error "demand-mining EOD incomplete (exit $rc); inspect the private caller state" -ErrorAction Continue }
exit $rc
