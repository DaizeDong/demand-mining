# Roadmap

Current: **v0.1.2**

## v0.1.2 (current), offline skeleton

- Deterministic tail real + tested (56 tests): redact-on-ingest, demand extraction + verbatim
  grounding, need pool (two-gate dedup + distinct-author intensity + NEW/SUPPRESS/RESURFACE) over
  the schedule-reminder base, three-axis reproducible scoring (RICE/Opportunity/WSJF/Kano), verify
  gate + egress DLP, EOD digest (Quick-win/Big-bet + iteration queue) + bounded catch-up, tiered push.
- `run.py --dry-run --no-ledger` runs the full offline pipeline; T5 round-trips the real base.

## v0.2, real wiring (DEFERRED unlocks)

- Bind the product root `.claude/` + the auto-support demand tap (shared Discord listener).
- Real secrets (Discord token reset, HMAC salt) + canary red-team of the egress DLP.
- Benchmark redaction precision + intent/Kano accuracy on real community samples (NER thresholds by
  entity type so product names/terms are not mis-killed).

## v0.3, sister-skill closed loop

- Consume daily-hotspots `opportunities.jsonl`; gated market-intel deep-dive; competitor changelog
  diff end-to-end driving WSJF urgency.

## v0.4, self-evolve gate

- Wire the self-evolve regression gate (implicit recall is the death-spot, grow implicit fixtures);
  calibrate the Kano proxy; library budget_check on every description change.

## Always, test headroom

- Each new mis-judge / false-merge / PII-leak class → a new adversarial fixture in the matching T.
- Golden-set quarterly backfill of realized reach/effort → auto-applied correction factors.
