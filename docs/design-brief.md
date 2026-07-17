# Design Brief, demand-mining

> Produced by skill-smith Step 0 (research-first). Full architecture lives in the build workspace
> `CodesResearch/_skill-builds/06-demand-mining/ARCHITECTURE.md` (7-lane research synthesis,
> 2026-06-25). This brief is the auditable rationale summary.

## Best references (match-or-beat)

- **daily-hotspots**, the structural twin (thin orchestration over the schedule-reminder base +
  LLM-proposes/gate-disposes + tiered Discord push). demand-mining forks its skeleton.
- **JTBD four-forces** (Push/Pull/Anxiety/Habit), the demand lens; Anxiety/Habit = implicit prize.
- **RICE** (Intercom), **Opportunity/ODI** (Ulwick), **WSJF** (SAFe), **Kano**, the four
  prioritization frameworks, kept orthogonal.
- **ABSA / opinion-unit** extraction + **verbatim grounding** (DeTAILS), the anti-hallucination frame.
- **Microsoft Presidio**, local-only PII redaction (Tier3, v0.2).

## Frontier ideas incorporated

- Mechanical Confidence (source-tier × cross-validation) so "≥2 independent sources" is a number,
  not a vibe; Effort clamped against small-divisor explosions.
- Distinct-author intensity with no time decay (keeps long-standing strong needs); velocity is a
  separate urgency input (competitor-just-shipped = highest WSJF).
- Cross-source triangulation: internal Discord + daily-hotspots + competitor gap merge on one
  canonical_key.
- Unique non-collapsing placeholders + HMAC pseudonyms with a gitignored salt.

## Anti-patterns avoided (each → a guardrail/lint)

2nd Discord bot · re-collecting hotspots · daily deep competitor calls · feature-factory ·
loudest-wins/vote=truth · single-signal dedup · keyword chitchat filtering · time-window slicing ·
raw-text-in-pool / collapsed placeholders / salt-in-repo · intensity time-decay / per-mention
stuffing · bypassing the base DB · RICE-as-law ignoring Kano · CronCreate · skipping the library budget.

## Proof bar (eval signal for the gate + self-evolve)

T1 extraction recall (implicit not dropped, verbatim grounding 100%) · T2 dedup correctness (double
gate, no false merge) · T3 reproducible ranking (byte-identical, anchored drift) · T4 EOD completeness
(count-conserving, honest empty day) · T5 base round-trip (idempotent UPSERT, source isolation) ·
T6 redaction (no PII, unique placeholders, stable pseudonym) · T7 cross-day catch-up (no double-send).

## Scope & focus (one job, ≤3 modules)

One job: **daily user-demand → ranked iteration plan for a shipped product.** Modules:
(1) ingest+extract (redact/extract), (2) pool+score (dedup/score), (3) EOD+deliver (verify/digest/push).
Everything heavier is delegated.
