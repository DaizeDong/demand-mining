# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## [0.1.0] - 2026-06-25
### Added
- Initial release — offline skeleton (skill-smith build #6).
- `redact.py`: redact-on-ingest (Tier1 regex+Luhn, Tier2 entropy, unique non-collapsing
  placeholders, HMAC author pseudonyms, egress `has_pii` DLP).
- `extract.py`: 8-label intent normalization, demand-vs-noise gate, JTBD four-force completeness,
  verbatim grounding (reject ungrounded), dual-track explicit/implicit unit builder.
- `dedup.py`: two-gate need-pool dedup (cosine ∧ simhash + subject agreement + candidate-merge
  band), NEW/SUPPRESS/RESURFACE evolution, distinct-author intensity merge, schedule-reminder
  base client (source=demand-mining, ext x_demand_mining_*).
- `score.py`: three orthogonal axes (RICE / Opportunity-ODI / WSJF) + Kano tier floor +
  deterministic 2D tiering + bounded reproducible final score + weight-regression gate.
- `verify_gate.py`: schema + ≥1-internal-evidence + egress DLP, fail-closed; no-filler batch gate.
- `digest.py`: EOD Quick-win/Big-bet split, iteration-direction queue, idempotent digest item,
  bounded catch-up; honest empty day.
- `run.py`: full deterministic pipeline orchestrator (`--dry-run` / `--no-ledger`).
- `wrapper.ps1` + `register-task.ps1`: OS Task Scheduler headless EOD (no CronCreate).
- 7 reference shards (privacy / extract / delegation / scoring / dedup-pool / eod-brainstorm /
  cron-setup); 56 tests (T1-T7).
