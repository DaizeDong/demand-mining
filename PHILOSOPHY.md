# demand-mining — Design Philosophy

> One test governs every change: **does it fix the framing, or just patch a symptom?**

demand-mining is a *thin orchestration skill* for turning a shipped product's messy community
signal into a ranked iteration plan — without leaking a user's PII, without re-building an engine,
and without the usual demand-mining self-deceptions (loudest-wins, feature-factory, vote=truth).
Five root-cause principles produced every concrete decision in this repo.

## P1 — LLM proposes, a deterministic gate disposes

- **Symptom patch:** trust the model to rank demands and "remember" not to push duplicates.
- **Root cause:** judgement (reading intent, JTBD, proposing a score) is the model's strength;
  *ruling* (what merges, what ships, what gets pushed) must be reproducible and fail-closed.
- **Decision:** `score.py` aggregates with pure functions (byte-identical reruns), `dedup.py` rules
  NEW/SUPPRESS/RESURFACE, `verify_gate.py` blocks unfit cards. The model never makes a final call.

## P2 — Privacy is a code boundary, not a prompt promise

- **Symptom patch:** instruct the model "please don't store PII."
- **Root cause:** once the model sees PII it has leaked; a prompt cannot un-see it.
- **Decision:** `redact.py` runs *before* any model/embedding/pool write (Tier1 regex+Luhn, Tier2
  entropy, unique non-collapsing placeholders, HMAC author pseudonyms with a gitignored salt). The
  pool stores only redacted, distilled items; an egress DLP wall re-checks before anything leaves.

## P3 — Own the seam, delegate every engine

- **Symptom patch:** add a Discord listener here, a hotspot collector there, a competitor scraper too.
- **Root cause:** each of those is an engine another skill already owns; re-building them is sprawl
  and double-maintenance.
- **Decision:** share auto-support's bot (no 2nd listener), consume daily-hotspots' archive (no
  re-fan-out), gate-delegate market-intel (it refuses monitoring), use the schedule-reminder base as
  the pool (CLI-only). demand-mining owns only the cadence and the seam between them.

## P4 — Three orthogonal axes, never one opaque number

- **Symptom patch:** blend everything into a single 0-100 "priority" score.
- **Root cause:** "how strong", "do-first", and "how soon" are different questions; merging them
  hides the trade-off and lets a tiny Effort denominator explode the rank.
- **Decision:** RICE (order, clamped Effort, mechanical Confidence), Opportunity/ODI (strength),
  WSJF (urgency, competitor-just-shipped = highest), with a Kano floor (must-be missing → Tier0).
  Tiers are argued as bands, never single points (anti false-precision).

## P5 — Honest emptiness over filler; the implicit demand is the prize

- **Symptom patch:** always produce a digest of "top demands" so the radar looks busy.
- **Root cause:** loudest-wins and feature-factory optimize for output volume, not truth; the real
  value is the demand a user did *not* say out loud.
- **Decision:** every suggestion needs ≥1 internal evidence or it is blocked; an empty day prints
  "今日无合格新需求"; intensity counts distinct authors (anti-stuffing); JTBD's Anxiety/Habit forces
  are double-tracked as the implicit pool, never dropped; verbatim grounding rejects ungrounded
  extractions (omission ≈ 2× fabrication).
