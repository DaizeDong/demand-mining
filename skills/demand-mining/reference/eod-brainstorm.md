# eod-brainstorm, EOD digest + structured brainstorm + tiered push (Step 6)

The daily close pipeline's deterministic tail (`scripts/digest.py` + `verify_gate.py` +
`push_card.py`).

## EOD five-stage pipeline

`① aggregate (internal Discord delta + explicitly supplied external evidence) → ② dedup/
cluster (merge synonyms, record mention count + source distribution) → ③ per-demand three-axis score
(temp0 rubric) → ④ 2D tiering + layering → ⑤ brainstorm + iteration directions in order`.

## Structured brainstorm (gate-bound, not free-form)

`digest.split_pools` + `digest.iteration_queue`:
1. Theme clustering (today's demands into 3-6 themes).
2. **Internal × external crossing** (internal pain ⨯ external trend/competitor gap = opportunity;
   supplied through manual daily-hotspots or market-intel research; automatic feeds are deferred).
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

The daily finalizer delivers one ranked headlines message with up to `push.max_per_day` entries
(default 5). Tier0 retains priority and can remain eligible below the normal score floor;
other cards use the configured push floor (default 70, flagship 80). Push-grade admission still
checks independent-source requirements. CUT and Kano indifferent/reverse cards do not consume
headline capacity. The scheduled finalizer writes the full Markdown digest in PRIVATE storage at
`pool/runs/<run-key>/archive/digests/YYYY/YYYY-MM-DD.md` and registers an idempotent
schedule-reminder item. Use `result.digest_path` to locate the artifact and the run's bound
`manifest.json` to verify it.
Headlines contain no URL or handle; they point to the private digest with a plain-text hint.

Standalone `push_card.py` is a separate, explicitly invoked delivery path. It validates Discord
embed limits (6000 characters, 25 fields, 1024 characters per value) and applies egress DLP.
Its product/card/new-or-update identity and confirmed receipt suppress retries independently of
scheduled digests and bot summaries. See [the runtime contract](../../../docs/runtime-contract.md#handoff-and-completion)
before sending or reconciling a card. An empty daily result is valid only after collection and
classification complete; a blocked card or failed handoff does not constitute an empty day.
