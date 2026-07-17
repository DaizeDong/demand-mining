# eod-brainstorm, EOD digest + structured brainstorm + tiered push (Step 6)

The daily close pipeline's deterministic tail (`scripts/digest.py` + `verify_gate.py` +
`push_card.py`).

## EOD five-stage pipeline

`① aggregate (internal Discord delta + external daily-hotspots/market-intel archives) → ② dedup/
cluster (merge synonyms, record mention count + source distribution) → ③ per-demand three-axis score
(temp0 rubric) → ④ 2D tiering + layering → ⑤ brainstorm + iteration directions in order`.

## Structured brainstorm (gate-bound, not free-form)

`digest.split_pools` + `digest.iteration_queue`:
1. Theme clustering (today's demands into 3-6 themes).
2. **Internal × external crossing** (internal pain ⨯ external trend/competitor gap = opportunity;
   fed by daily-hotspots + market-intel).
3. Each candidate direction → three-axis score → order.
4. **Quick-win** (high demand / low effort, Kano Performance/Must-be) vs **Big-bet** (high impact /
   low confidence, Kano Delighter), two pools, each Top-N.
5. Trend: which demands are heating (frequency/recency up) vs yesterday's pool diff.

## verify_gate (fail-closed)

Every iteration suggestion carries **≥1 internal evidence** {channel, redacted_snippet, ts} (ideally
+1 external) or `verify_gate.validate_card` BLOCKS it as an explicit gap, never no-evidence filler.
Push-grade cards also need ≥2 independent sources. **Egress DLP**: any residual PII blocks the card.
Empty day → honest "今日无合格新需求", never filler.

## Iteration-direction queue (the deliverable)

Each entry exposes all three axes so the call is auditable: `{canonical demand, RICE detail,
Opportunity + intensity + distinct_authors, Kano band, 7/30-day velocity, linked competitor/hotspot
signal, evidence count, horizon (this-week/this-month/quarter/backlog), order number}`. Ordered by
tier rank → final_score desc → canonical_key (replay-safe tie-break).

## Tiered push (anti-spam)

Tier0 (must-be missing) pushes immediately regardless of score; otherwise `final ≥70` (flagship ≥80)
AND distinct ORIGIN ≥2 → single card now (≤5/day); the rest aggregate into the digest. `push_card`
validates Discord hard limits (embed ≤6000 / fields ≤25 / value ≤1024) and runs egress DLP before
sending. The digest is one markdown artifact, both delivered to Discord and written to
`pool/digests/YYYY/YYYY-MM-DD.md`, registered as an idempotent schedule-reminder item.
