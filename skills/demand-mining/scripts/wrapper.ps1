<#
demand-mining headless EOD wrapper for the Windows Task Scheduler.

ABSOLUTE python/claude paths (Task Scheduler PATH is minimal — a bare `python` half-runs and
silently fails), fail-fast preflight, notify-on-abort. It does NOT use the in-session CronCreate
tool (session-only = wrong primitive).

Register once with register-task.ps1 (off-:00, e.g. 21:53). It invokes `claude -p` headless so the
SKILL orchestration (redact -> read Discord sessions -> extract -> external lanes) runs, then the
deterministic run.py disposes (score/dedup/gate/push/pool/digest/watermark).

Env it sets for the run:
  DEMAND_MINING_CONFIG   (if a companion repo path is given; carries secrets/pseudonym_hmac_salt)
  SCHEDULE_DB_PATH       (local NTFS ledger db; never OneDrive/network = WAL corruption)
#>
param(
  [string]$Python = "",
  [string]$ConfigDir = "",
  [string]$LogDir = "$env:USERPROFILE\.demand-mining-logs"
)
$ErrorActionPreference = "Stop"

function Resolve-Python {
  param([string]$p)
  if ($p -and (Test-Path $p)) { return $p }
  $c = (Get-Command python -ErrorAction SilentlyContinue)
  if ($c) { return $c.Source }
  throw "python not found; pass -Python <abs path>"
}

function Notify-Abort {
  param([string]$msg)
  $relay = "$env:USERPROFILE\.local\relay.py"
  if (Test-Path $relay) {
    try { & $script:py $relay "[demand-mining] ABORT: $msg" | Out-Null } catch {}
  }
}

try {
  $script:py = Resolve-Python $Python
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $stamp = Get-Date -Format "yyyy-MM-dd"
  $log = Join-Path $LogDir "eod-$stamp.log"

  $claude = (Get-Command claude -ErrorAction SilentlyContinue)
  if (-not $claude) { Notify-Abort "claude CLI not on PATH"; throw "claude CLI missing" }

  if ($ConfigDir) { $env:DEMAND_MINING_CONFIG = $ConfigDir }
  if (-not $env:SCHEDULE_DB_PATH) {
    $env:SCHEDULE_DB_PATH = "$env:USERPROFILE\.schedule-reminder\schedule.db"
  }

  "[$(Get-Date -Format o)] demand-mining EOD start (py=$script:py)" | Tee-Object -FilePath $log -Append
  & $claude.Source -p "Run the demand-mining skill EOD now: redact + read today's Discord demand signals, recover intent + JTBD, dedup into the need pool, score the three axes, brainstorm Quick-win/Big-bet iteration directions, push the digest to Discord, and archive." --dangerously-skip-permissions *>> $log
  $rc = $LASTEXITCODE
  "[$(Get-Date -Format o)] demand-mining EOD end rc=$rc" | Tee-Object -FilePath $log -Append
  if ($rc -ne 0) { Notify-Abort "claude -p exited rc=$rc (see $log)" }
  exit $rc
}
catch {
  Notify-Abort $_.Exception.Message
  throw
}
