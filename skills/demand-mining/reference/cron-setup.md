# cron-setup — scheduling the daily EOD run (Step 7)

**Never `CronCreate`** — an in-session cron dies with the session (wrong primitive). The correct
chain on Windows:

`Windows Task Scheduler (off-:00, e.g. 21:53 to avoid top-of-hour congestion) → wrapper.ps1
(absolute python/claude paths + fail-fast preflight + non-zero exit → Discord relay alert) →
claude -p '<run demand-mining EOD>'` (headless).

## Register the task

```powershell
# from the skill's scripts/ dir
powershell -ExecutionPolicy Bypass -File register-task.ps1 -Time 21:53
```

`register-task.ps1` registers `DemandMiningEOD` at the chosen off-:00 time, pointing at
`wrapper.ps1`. `wrapper.ps1` resolves absolute interpreter paths, preflights (config dir reachable?
relay present? base DB writable?), runs the headless EOD, and on any non-zero exit pushes a Discord
alert via `the notifier` so a silent failure is impossible.

## Idempotency + catch-up

The EOD digest is an idempotent schedule-reminder item (`idempotency_key=demand-mining:digest:<date>`)
— a re-run / backfill never double-sends. After the machine sleeps, the next run uses
`since=last_run_at-5min` + fingerprint UPSERT = at-least-once + dedupe. `digest.catch_up_digests`
backfills the most-recent missed days, **bounded** (an overslept laptop never floods the channel).

## Folding into an existing daily summary

If the product already has a "每日总结" routine, expose the demand-mining EOD block to it (don't
start a competing channel). Otherwise the skill pushes its own digest via the relay.

## Deployment form

Deploy as a plugin into the product root `.claude/` (mirrors auto-support), carrying
`templates/settings.json.template` (`permissions.deny` + a PreToolUse hook). The privacy/secret
boundary is enforced by the **deterministic layer** (permissions.deny + PreToolUse hook + stdlib
guardrails in redact.py) **outside** the prompt — not by SKILL.md text promises.
