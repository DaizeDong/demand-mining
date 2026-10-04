# Roadmap

Current: **v0.7.1**

## Current implementation

Release history is preserved in [CHANGELOG.md](CHANGELOG.md). Its Unreleased section records
later changes on main without inventing another release.

- The Discord REST tap and gateway daemon collect demand-specific feedback. Daemon modes separate
  dry execution, direct replies in shadow mode and authorized community activity in live mode.
- Installed `llmcall` proposes candidates. The local deterministic tail binds observations,
  scores and deduplicates candidates, applies evidence and privacy gates, and prepares the digest.
- PRIVATE companion verification precedes initialization and real DATA writes. Pool, digest and
  ledger operations preserve evidence and expose failures; a successful process alone is not a delivery receipt.
- RICE, Opportunity and WSJF describe different ranking questions; Kano describes need type.
  The scores are reproducible calculations over supplied evidence, not calibrated predictions by themselves.
- Synthetic checks cover local contracts. Live Discord access, scheduler operation, relay delivery,
  implicit-demand recall and community calibration require their own evidence.

## Deferred integrations and validation

- Collect competitor changelog differences and connect daily-hotspots / market-intel research
  to observed corroboration and urgency, instead of relying on a model-supplied competitor status.
- Connect the reserved product-code root to implementation locations for ranked demands.
- Measure redaction and intent/Kano quality on authorized evaluation material; structured redaction
  does not guarantee removal of names or sensitive free-form prose.
- Expand implicit-demand and false-merge regression cases and measure realized reach/effort
  before applying calibration changes. Keep candidate development separate from held-out evaluation.

These are remaining capabilities or acceptance requirements, not descriptions of an active deployment.
