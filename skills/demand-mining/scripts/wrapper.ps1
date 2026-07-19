<#
demand-mining headless EOD wrapper for the Windows Task Scheduler.

ABSOLUTE python/claude paths (Task Scheduler PATH is minimal, a bare `python` half-runs and
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
  # Skill orchestration goes through the resilient runner: cc (hosted gateway) -> claude-direct
  # (claude.ai subscription, gateway env unset, independent of the gateway) + retry (the gateway 530s recover) +
  # notify. A single dead transport no longer fails the run. The runner owns the native-stderr
  # ErrorActionPreference dance internally, so it is NOT needed here.
  $prompt = "Run the demand-mining skill EOD now: redact + read today's Discord demand signals, recover intent + JTBD, dedup into the need pool, score the three axes, brainstorm Quick-win/Big-bet iteration directions, deliver the ranked headlines digest to Discord, and archive."
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.local\agent-runner.ps1" -Prompt $prompt -Log $log -Stream "demand-mining"
  $rc = $LASTEXITCODE
  "[$(Get-Date -Format o)] demand-mining EOD end rc=$rc" | Tee-Object -FilePath $log -Append
  if ($rc -ne 0) { Notify-Abort "EOD agent failed rc=$rc (cc + claude-direct both; see $log)" }

  # ---- commit + push the day's demand pool + digest to the PRIVATE companion repo ----
  # Best-effort durability/sync of the private archive (the demand pool is DATA, it lives ONLY in the
  # private companion repo, never a public tree). A push failure must NOT fail the run (the headlines
  # already delivered). origin is the ssh-alias remote (git@daizedong:) for unattended auth;
  # --rebase --autostash absorbs drift. Only pool/ is committed, other local changes stay the user's,
  # and secrets/ is .gitignored so it is never staged.
  if ($rc -eq 0 -and $ConfigDir -and (Test-Path (Join-Path $ConfigDir '.git'))) {
    try {
      Push-Location $ConfigDir
      $ErrorActionPreference = 'Continue'
      & git add pool/ *>> $log
      & git diff --cached --quiet
      if ($LASTEXITCODE -ne 0) {
        & git commit -m "data: demand pool + digest $(Get-Date -Format 'yyyy-MM-dd')" *>> $log
        & git pull --rebase --autostash origin master *>> $log
        & git push origin master *>> $log
        $pushRc = $LASTEXITCODE
        "[$(Get-Date -Format o)] archive push rc=$pushRc" | Tee-Object -FilePath $log -Append
        if ($pushRc -ne 0) { Notify-Abort "archive push failed rc=$pushRc (pool backup may lag; see $log)" }
      } else {
        "[$(Get-Date -Format o)] archive: nothing to commit" | Tee-Object -FilePath $log -Append
      }
      $ErrorActionPreference = 'Stop'
      Pop-Location
    } catch {
      $ErrorActionPreference = 'Stop'
      try { Pop-Location } catch {}
      "[$(Get-Date -Format o)] archive push exception: $($_.Exception.Message)" | Tee-Object -FilePath $log -Append
    }
  }
  exit $rc
}
catch {
  Notify-Abort $_.Exception.Message
  throw
}
