# dedup-pool, need pool, cross-day dedup, evolution (Step 5)

Backend = the **schedule-reminder base** (frozen `api_version 1.0.0`): subprocess via `reminder.py
--json` only. **Never** read the `.db`, build SQL, or put it on OneDrive (WAL corruption), local
NTFS only. `scripts/dedup.py` is the pool layer.

## Demand → base item mapping

| base field | demand semantics |
|---|---|
| `kind` | **always `task`** (an iteration candidate is executable; never `event`) |
| `title` | redacted one-line canonical demand (no PII) |
| `state` | `pending` (new) / `doing` (scheduled) / `done` (shipped) / `blocked` (needs clarify) / `cancelled` (merged/rejected) |
| `priority` | 1 (highest) for Tier0, else from RICE final band |
| `source`/`actor` | `demand-mining` |
| `idempotency_key` | `demand-mining:product:` + SHA256(`product_id`) + `:demand:` + `canonical_key` |
| `ext.x_demand_mining_*` | the demand-only namespace (MUST-PRESERVE round-trip) |

ext fields: `canonical_key, cluster_id, intensity, distinct_author_count, mention_count, authors[]
(HMAC only), source_set, rice{}, opportunity_score, urgency_wsjf, tier, kano, velocity,
competitor_status/ref, external_corroboration, first/last_seen, push_count, samples[], evidence[]
(redacted snippets only)`. **Vectors never enter ext/base** (row bloat) → local sidecar; ext keeps
only `cluster_id` for reverse lookup.

## Two-gate dedup (forbid single-signal merges, anti-pattern #9)

Exact canonical-key matches preserve the existing demand identity. For a nonexact match,
`dedup.match_existing` requires subject agreement plus either two shared entities or one shared
entity in the same track. It then accepts a similarity signal outside the review band:
Jaccard similarity at least `dedup_cosine_threshold` (default 0.83), SimHash Hamming distance at
most 3, or Jaccard similarity from 0.45 to below the band's lower bound (default 0.78).
The historical `dedup_cosine_threshold` setting names a Jaccard calculation in this implementation.

The default 0.78 to below 0.83 band remains `candidate-merge` for human review and never merges
automatically. Entity and subject checks prevent similar wording about different needs from
merging. Cross-source evidence for the same product and demand preserves attribution under the
same canonical key. Periodic HDBSCAN/agglomerative reclustering remains a design proposal;
the shipped matcher does not run it. Ingest also preserves message-level observation identities
so replaying one message does not add another observation; see the runtime contract.

## Intensity (need-weight, anti-stuffing)

`intensity = Σ_distinct(urgency{should=1,need=2,blocking=3} + segment{free=1..enterprise=4}) +
distinct_author_count`, accumulated per **distinct `author_hash`** (`lib.intensity` +
`dedup.merge_authors`). One loud user repeating only bumps `mention_count`, never intensity. **No
time decay** (keep long-standing strong needs); time-sensitivity is the separate `velocity`.

## Cross-day evolution (free, from base events)

The base's events audit stream is the evolution history. `dedup.decide` → **NEW** (no match →
score+create) / **SUPPRESS** (recurs, small delta, no new origin → count, don't re-push) /
**RESURFACE** (new external corroboration / competitor shipped / urgency jump / new origin crossing
≥2 / score jump ≥ threshold → evolution UPDATE card). Single-origin cards that meet the push score
floor fail the independent-source check and appear in `blocked`; lower-score internal demands
remain subject to the archive floor and other admission checks. A five-day quiet period does not
establish that a product change shipped; the current demand runtime does not automatically mark
`doing` items `done` on that basis. Scheduled collection resumes from the prior completed cutoff,
and watermark advancement requires successful receipt-bound finalization. See the
[runtime contract](../../../docs/runtime-contract.md) for logical identity and interrupted runs.
