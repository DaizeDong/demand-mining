<# Scheduled EOD uses installed llmcall routing and the local deterministic finalizer. #>
param(
  [string]$Python = "",
  [string]$ConfigDir = "",
  [string]$LogDir = ""
)
$ErrorActionPreference = "Stop"
$env:GIT_OPTIONAL_LOCKS = '0'
if ($Python) {
  if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
  }
} else {
  $command = Get-Command python -ErrorAction Stop
  $Python = $command.Source
}
$caller = Join-Path $PSScriptRoot 'scheduled.py'
$arguments = @('-X', 'utf8', '-B', $caller)
if ($ConfigDir) { $arguments += @('--config-dir', $ConfigDir) }
if ($LogDir) { $arguments += @('--log-dir', $LogDir) }
& $Python @arguments
$rc = $LASTEXITCODE
if ($rc -ne 0) { Write-Error "demand-mining EOD incomplete (exit $rc); inspect the private caller state" -ErrorAction Continue }
exit $rc
