---
name: demand-mining
description: "已发布产品每日用户需求挖掘+竞品/热点追踪+EOD 头脑风暴+RICE/Kano 量化迭代排序. Triggers: 需求挖掘, demand mining, 迭代建议, EOD 汇总."
allowed-tools: Read, Glob, Grep, Bash, Agent, Skill, WebSearch, WebFetch
---

# demand-mining

This skill analyzes feedback for a shipped product: Discord collection and redaction, JTBD
extraction, cross-day demand grouping, quantitative ranking, EOD review and authorized delivery.
The model proposes interpretations; `run.py` and `verify_gate.py` apply deterministic admission
rules. Read [PHILOSOPHY.md](../../PHILOSOPHY.md) for the rationale and privacy limits.

## When to use / when to stop

- **Fire**: the daily scheduled EOD run, or the user says 需求挖掘 / demand mining / 迭代建议 / EOD 汇总.
- **Stop & route**: a one-shot competitor research question → `market-intel` directly. Today's
  market opportunities (not user demands) → `daily-hotspots`. Improving this skill → `self-evolve`.
  "Does a skill exist for X" → `market-intel` ready-skills.

## Delegation map (never re-implement these)

| Deep job | Delegate to | Relationship |
|---|---|---|
| Discord listening layer | `demand_bot.py` and `pull_discord.py` | Implemented local ownership; shared-bot demand-tap integration is deferred. |
| Hotspots / public demand | **daily-hotspots** | Optional manual evidence; automatic ingestion is deferred. |
| Competitor deep-dive | **market-intel** | Optional user-requested research; automatic collection is deferred. |
| Demand pool / cross-day dedup / state | Local `demand_pool.py` + **schedule-reminder** CLI | Pool records stay in the companion; the shared ledger owns idempotency and completion. |

## Workflow (load one `reference/<shard>.md` per step)

0. **Collect the live tap (deterministic)**, `scripts/pull_discord.py`. It reads the wired product's
   Discord channels via the bot token (config: `registry.json` `discord_channels` + `discord_token_ref`;
   Message Content Intent required) and emits a REDACTED corpus. The scheduler freezes the window from the prior completed cutoff; direct manual collection accepts
   `--since-hours`, while `--full` is an explicit backfill. Bots/webhooks/empty are skipped. The token is never
   printed. An unwired tap exits with an initialization hint. This is the scheduled collection path;
   the daemon separately owns live collection. The model receives the collected redacted corpus.
1. **Redact-on-ingest (FIRST, always)**, `reference/privacy.md`. `pull_discord.py` already ran every
   raw message through `redact.py` (NFKC-normalized, so full-width/homoglyph obfuscation can't smuggle PII past it)
   BEFORE any LLM/embedding sees it: Tier1 regex+Luhn (email/phone/card/URL/IP/discord-id/handle),
   Tier2 entropy (secrets), unique placeholders (`[EMAIL_1]`/`[PHONE_2]`, never collapsed), HMAC
   author pseudonym. Local person/address patterns supplement these rules; unsupported personal
   context is visibly held for review. The doctor names remaining coverage gaps. Only redacted
   text flows downstream; the pool stores distilled items, never raw conversation.
2. **Extract demand**, `reference/extract.md`. Stage A: classification using 8 intent labels (context
   LLM, NOT keyword chitchat filtering). Stage B: session disentanglement by thread/reference
   chains (never time-window slicing) → JTBD four forces (Anxiety/Habit = the implicit goldmine) →
   three-layer translation (literal→job→emotion; never排期 the literal feature) → opinion-unit
   extraction → **verbatim grounding** (`extract.py`: a quote not locatable in the redacted source
   is REJECTED, omission ≈ 2× fabrication). Dual-track: explicit pool + implicit pool.
3. **Optional external evidence**, `reference/delegation.md`. Automatic hotspot, public-demand
   and competitor feeds remain deferred. For an explicitly requested manual investigation, use
   the relevant sibling skill and preserve independent provenance before including evidence.
   Existing demand extraction and daemon operation do not depend on those integrations.
4. **Score (three orthogonal axes, reproducible)**, `reference/scoring.md`. At **temperature 0**
   with anchored rubric samples, propose each axis's inputs; `score.py` (pure) disposes them:
   **RICE** (ordering; Confidence = mechanical source-tier×cross-validation, Effort clamped) ·
   **Opportunity/ODI** (demand strength) · **WSJF** (urgency; competitor-just-shipped = highest) ·
   **Kano** gate (must-be missing → Tier0, score-decoupled). 2D tier matrix; argue bands not points.
5. **Need pool + cross-day evolution**, `reference/dedup-pool.md`. `dedup.py` over the
   schedule-reminder base: exact canonical identity or guarded nonexact matching, with the
   0.78-0.83 candidate band reserved for review. Preserve product-scoped keys, distinct-author
   intensity without time decay, and NEW/SUPPRESS/RESURFACE state. Use the reference for the
   implemented similarity conditions.
6. **EOD digest + brainstorm**, `reference/eod-brainstorm.md`. `verify_gate.py` (≥1 internal
   evidence + egress DLP, fail-closed) → `digest.py` Quick-win/Big-bet split + iteration queue.
   Delivery is **one ranked 'headlines' message/day** (`digest.build_headlines`: top ≤5 push-eligible
   demands, each `**N.【立即·刚需】标题**` + 人话摘要(why+建议) + `grade final_score · RICE · N证据`),
   NOT a Discord embed per demand. The full markdown (every field + evidence) is the archived digest
   file, pointed at by a **plain-text** hint. Unlike daily-hotspots the headline carries **no url**:
   this skill mines private conversation and `push_card.deliver`'s `has_pii` gate aborts on any
   url/handle, so evidence stays private. Honest empty day.
7. **Schedule**, `reference/cron-setup.md`. OS Task Scheduler → `wrapper.ps1` → `scheduled.py` →
   installed `llmcall.call(prompt, mode="agent")` for candidates. The local finalizer owns current
   artifacts, confirmed delivery receipts and watermark state. Backup is separate and checked.
   See `../../docs/runtime-contract.md` for timezone, PRIVATE destinations and reconciliation.
   **Never CronCreate.**

**Fast path**, prepare candidate demand clusters as JSON, then let the gate run the deterministic tail:

```bash
python scripts/run.py --in candidates.json        # redact→score→dedup→gate→push→pool→digest→watermark
python scripts/run.py --in candidates.json --dry-run --no-ledger   # offline preview, no writes
```

## Hard rules (each maps to a guardrail; never violate)

1. **Privacy first.** redact-on-ingest runs before any model call; the pool stores redacted,
   distilled demand items + HMAC pseudonyms, never raw chat. Structured PII (email/phone/card/
   secret/id/url/ip/handle) is stripped fail-closed (NFKC-normalized against obfuscation). Local
   person/address patterns redact supported forms; unsupported personal context is held for review.
   Keep unique placeholders and a stable HMAC salt under the selected PRIVATE credential policy.
2. **Never send user words to a third party.** Delegated queries to market-intel / web carry only
   non-private topics (feature name, competitor name), never a user's raw message (privacy + injection).
3. **Job over feature.** Never排期 a literal feature ask; force an inferred JTBD job + 5-Whys.
   Implicit demand (Anxiety/Habit) is double-tracked, never dropped (loudest-wins is banned).
4. **Confidence is mechanical, Effort is clamped.** RICE Confidence = source-tier × cross-validation
   (≥2 independent = high); Effort floored (no small-divisor explosions). No hand-math, no LLM ranking.
5. **Every iteration suggestion carries ≥1 internal evidence** (ideally +1 external) or
   `verify_gate.py` BLOCKS it (no-filler). Honest empty day: "今日无合格新需求".
6. **Cross-day**: already-pushed demands SUPPRESS (count, don't re-push) unless a material change
   RESURFACEs them. Watermark is written **only after** the full run succeeds (atomic, at-least-once).
7. **Never** read the schedule-reminder DB directly / put it on OneDrive (WAL corruption), CLI +
   local NTFS only. Use the configured Discord collection owner; do not create an additional bot.
   Do not repeat the hotspots fan-out or use in-session CronCreate for persistent scheduling.

## Config

Use [CONFIG.md](../../CONFIG.md) for companion selection and required product fields.
`DEMAND_MINING_CONFIG` takes precedence over `DEMAND_MINING_CONFIG_DIR`, followed by shared
Guards discovery. `DEMAND_MINING_DATA_DIR` must select that companion's exact `pool/` directory.
Invalid explicit selections fail; there is no XDG or public-repository fallback.

Offline helpers may use `scripts/lib.py:DEFAULT_CONFIG`. Collection and writes require an
initialized, verified PRIVATE companion, product identity, timezone, selected ledger and stable
pseudonym salt as required by the chosen capability. Tune per-product policy in
`products/<slug>/priority.json`. Follow [DATA.md](../../DATA.md) for artifact ownership and
retention, and [the runtime contract](../../docs/runtime-contract.md) for recovery.

A real empty day requires completed collection and classification. Reuse the saved logical
identity, plan and receipt on retry. An uncertain send requires receipt reconciliation before
completion; backup failure remains separate from delivery success.

## Progressive loading

This `SKILL.md` is the only always-loaded file. Read `reference/<shard>.md` on demand, one per step.
Never read the whole `reference/` directory at once. All heavy logic lives in `scripts/` (tested:
`python -m pytest tests/`, T1 extract/grounding · T2 dedup · T3 reproducible scoring · T4 gate+EOD
· T5 base round-trip · T6 redaction/DLP · T7 cross-day catch-up).
