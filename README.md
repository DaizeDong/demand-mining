# demand-mining

Daily user-demand mining + competitor/hotspot tracking + EOD brainstorm + RICE/Kano quantified iteration ranking, for a shipped product.

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-v0.1%20offline%20skeleton-green?style=flat)](ROADMAP.md)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#languages)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.1.2-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ Read this first — the design philosophy

**LLM proposes, a deterministic gate disposes — and the gate guards privacy first.** A shipped
product's user signal is messy, sensitive, and easy to mis-rank. So every judgement call (reading a
Discord session, recovering intent + the Job-To-Be-Done, proposing a score) is the model's, but
every *ruling* — what counts as a demand, what merges, what ships, what gets pushed — is made by a
pure Python gate that fails closed. And before the model ever sees a message, `redact.py` strips the
PII. The need pool stores only redacted, distilled demand items, never raw conversation.

This is the orchestration product `market-intel` reserved and `daily-hotspots`' twin: it owns the
*seam* (cadence, pool, scoring, delivery) and **delegates every engine** — it never re-implements
search, verification, the Discord listener, or the hotspot fan-out.

📜 **[Read the full design philosophy -> PHILOSOPHY.md](PHILOSOPHY.md)**

---

## What it is (and isn't)

**Is:** a daily demand radar for one shipped product. It ingests product-community signal (Discord),
extracts real demands (explicit *and* implicit, JTBD-grounded), pools them with cross-day dedup and
distinct-author intensity, ranks them on three orthogonal axes (RICE for order, Opportunity for
strength, WSJF for urgency, Kano for nature), and emits an EOD brainstorm with a prioritized
iteration-direction queue.

**Isn't:** a second Discord bot (it shares auto-support's listener), a hotspot collector (it consumes
daily-hotspots), a competitor research engine (it gates-delegates to market-intel), or a database
(the need pool is the schedule-reminder base, CLI-only). It is a thin seam, not an engine.

## Install

```
/plugin install github:DaizeDong/demand-mining
```

Or clone manually:

```bash
git clone https://github.com/DaizeDong/demand-mining.git ~/.claude/plugins/demand-mining
```

## Quick start

```bash
# offline preview — runs the full deterministic tail with no writes, no network
python skills/demand-mining/scripts/run.py --in candidates.json --dry-run --no-ledger

# real EOD (headless, via the scheduler wrapper)
powershell -ExecutionPolicy Bypass -File skills/demand-mining/scripts/register-task.ps1 -Time 21:53
```

`candidates.json` is a list of candidate demand clusters (the SKILL's LLM layer produces them from
the live Discord + external fan-out); the gate runs redact → score → dedup → verify → push → pool →
digest → watermark.

## How to invoke

Trigger words: **需求挖掘 · demand mining · 迭代建议 · EOD 汇总**, or the daily scheduled run.

## Example output

An EOD digest with an iteration-direction queue, each line showing all three axes:

```
1. [tier0/immediate] reliably export my data — final 78 · RICE(R=6,I=3,C=1.0,E=2)=9 ·
   Opp=16(intensity 12, 4 人) · WSJF=4.8 · Kano=must_be · 竞品 competitorX · 证据×3
```

plus Quick-win / Big-bet pools. On a quiet day it honestly prints `今日无合格新需求`.

## Config

`demand-mining` is **config-bearing** — it reads per-product tunables (RICE weights, thresholds, Kano
map, taxonomy, push limits) and secrets (pseudonym HMAC salt, Discord creds) from a **separate,
private** companion config repo. Full contract: [CONFIG.md](CONFIG.md). Absent → built-in
`scripts/lib.py:DEFAULT_CONFIG`.

- **Mount (discovery order):** `$DEMAND_MINING_CONFIG` → `~/.demand-mining-config/` →
  `~/.config/demand-mining-config/`. First that exists wins; absent = runs on defaults.
- **First time:**
  ```bash
  python scripts/init_config.py --product <slug>  # stamp skeleton (deterministic)
  export DEMAND_MINING_CONFIG=~/.demand-mining-config                   # or pass --out <dir>
  python scripts/verify_config.py                  # doctor: PASS/FAIL, names gaps
  ```
- **Switch configs (hot-swap):** point the env var at another config dir — configs are self-contained,
  no other change: `export DEMAND_MINING_CONFIG=~/configs/work` ↔ `~/configs/personal`.
- **Secrets:** Mode B — `secrets/*` is gitignored and never enters git; back up out-of-band. The
  pseudonym salt may instead come from `$DEMAND_MINING_PSEUDONYM_SALT`.

## Limitations

- v0.1 is an **offline skeleton**: the deterministic tail (redact/extract/dedup/score/gate/digest)
  is real and tested; the live Discord tap + real secrets + competitor changelog diff land in v0.2
  (see ROADMAP). The product code root and Discord bot wiring are `@DEFERRED` until provided.
- Implicit-demand recall is the hard part; it improves by adding adversarial fixtures over time.
- Kano is an LLM proxy (no survey) — calibrated on real community samples after go-live.

## Languages

English (`README.md`, authoritative) · 中文 (`README_CN.md`)

## Roadmap · Contributing · License

See [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE) (MIT).
