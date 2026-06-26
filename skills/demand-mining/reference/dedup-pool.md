# dedup-pool — need pool, cross-day dedup, evolution (Step 5)

Backend = the **schedule-reminder base** (frozen `api_version 1.0.0`): subprocess via `reminder.py
--json` only. **Never** read the `.db`, build SQL, or put it on OneDrive (WAL corruption) — local
NTFS only. `scripts/dedup.py` is the pool layer.

## Demand → base item mapping

| base field | demand semantics |
|---|---|
| `kind` | **always `task`** (an iteration candidate is executable; never `event`) |
| `title` | redacted one-line canonical demand (no PII) |
| `state` | `pending` (new) / `doing` (scheduled) / `done` (shipped) / `blocked` (needs clarify) / `cancelled` (merged/rejected) |
| `priority` | 1 (highest) for Tier0, else from RICE final band |
| `source`/`actor` | `demand-mining` |
| `idempotency_key` | `demand-mining:` + `canonical_key` (UPSERT = cross-day idempotency) |
| `ext.x_demand_mining_*` | the demand-only namespace (MUST-PRESERVE round-trip) |

ext fields: `canonical_key, cluster_id, intensity, distinct_author_count, mention_count, authors[]
(HMAC only), source_set, rice{}, opportunity_score, urgency_wsjf, tier, kano, velocity,
competitor_status/ref, external_corroboration, first/last_seen, push_count, samples[], evidence[]
(redacted snippets only)`. **Vectors never enter ext/base** (row bloat) → local sidecar; ext keeps
only `cluster_id` for reverse lookup.

## Two-gate dedup (forbid single-signal merges, anti-pattern #9)

1. **Message-level exact** — message_id / content hash filters re-posts.
2. **Semantic cluster (double gate)** — same demand iff **cosine ≥ 0.83 AND simhash Hamming ≤ 3**
   AND entity overlap AND subject agreement (`dedup.match_existing`). Boundary band 0.78-0.83 →
   `candidate-merge` (human review, **never** auto-merge; surfaced as a gap). Pure semantic alone
   false-merges "same words, different need"; pure string-match misses rewrites.
3. **canonical_key idempotent UPSERT** — match a cluster centroid → `add` same key UPSERTs
   (mention++, intensity accrues, evidence appended, recency updated); miss → new. Periodic offline
   recluster (HDBSCAN/agglomerative) guards centroid drift.
4. **Cross-source triangulation** — internal + external signals extract the same entities → same
   `canonical_key` → merge with attribution, `frequency++`, never re-file.

## Intensity (need-weight, anti-stuffing)

`intensity = Σ_distinct(urgency{should=1,need=2,blocking=3} + segment{free=1..enterprise=4}) +
distinct_author_count`, accumulated per **distinct `author_hash`** (`lib.intensity` +
`dedup.merge_authors`). One loud user repeating only bumps `mention_count`, never intensity. **No
time decay** (keep long-standing strong needs); time-sensitivity is the separate `velocity`.

## Cross-day evolution (free, from base events)

The base's events audit stream is the evolution history. `dedup.decide` → **NEW** (no match →
score+create) / **SUPPRESS** (recurs, small delta, no new origin → count, don't re-push) /
**RESURFACE** (new external corroboration / competitor shipped / urgency jump / new origin crossing
≥2 / score jump ≥ threshold → evolution UPDATE card). 1-origin candidates → explicit `below_sources`
gap (never silent). 5-day silence → auto `doing`→`done`. Watermark written **only after** a full
successful run (atomic); next run `since=last_run-5min` + UPSERT = at-least-once + dedupe.
