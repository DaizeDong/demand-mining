# demand-mining, Config

`demand-mining` is **config-bearing**: every tunable (RICE weights, score thresholds, Kano map,
taxonomy, push limits, retention) and every secret (pseudonym HMAC salt, Discord bot credentials)
lives in a **separate, private companion config repo** you create, never in this public skill repo.
This file is the authoritative config contract (config-spec E1). It is the canonical source the code
(`skills/demand-mining/scripts/lib.py`) and the doctor (`scripts/verify_config.py`) agree on.

> **Domain variant note (E1).** The reference config-spec registry shape is `tools[]`/`entries[]`.
> demand-mining is product-centric, so its companion repo uses a **`products[]`** registry instead,
> one tunable profile per tracked product. This is a deliberate, documented variant; `verify_config.py`
> accepts `products[]` (and also `tools[]`/`entries[]` for forward-compat). Everything else matches the
> spec (Mode B secrets, env-var discovery, deterministic init).

## Discovery convention (E2)

`DEMAND_MINING_CONFIG` selects the companion root; `DEMAND_MINING_CONFIG_DIR` is its lower-priority alias. `DEMAND_MINING_DATA_DIR` may select the supported DATA directory in that same companion. Conflicting CONFIG and DATA selections, empty selectors and explicit missing paths fail before any write. With no explicit selector, the pinned Guards resolver checks a proven sibling companion, `~/.demand-mining-config`, then its legacy `~/.demand-mining-data` convention. There is no separate XDG search. Settings and DATA share this selection. `--config-dir` on the doctor selects one companion in isolation from inherited selectors; restore normal environment selection before running the product. Runtime state lives at `<companion>/pool`; DATA_DIR must name that exact pool directory.

## Layouts (two supported)

**(A) Per-product**, repo root holds `registry.json`; each product gets its own dir:

```
<config-dir>/
  registry.json                 # { schema_version, skill, products:[{slug}] }
  products/<slug>/priority.json # scoring / push / delegation / privacy overrides
  products/<slug>/taxonomy.json # taxonomy[] override (optional)
  competitors.json              # competitor watchlist (delegation lane 3, optional)
  secrets/                      # selected PRIVATE credential backup policy
```

`DEMAND_MINING_PRODUCT` selects a registered slug. Without it, `load_config()` selects the
first registered product. A missing selected directory is an error; it does not skip to another product.

**(B) Flat**, keep one product's files at the companion root selected by `$DEMAND_MINING_CONFIG` (no registry). Runtime DATA still lives in that companion's `pool/`:

```
<config-dir>/
  priority.json        # or watchlist.json
  taxonomy.json        # optional
```

## Schema, `registry.json` (per-product layout), E1

| Field            | Type            | Required | Example                              |
| ---------------- | --------------- | -------- | ------------------------------------ |
| `schema_version` | int             | yes      | `1`                                  |
| `skill`          | str             | yes      | `"demand-mining"`                    |
| `products`       | array of object | yes      | `[{ "slug": "acme-app" }]`           |
| `products[].slug`| str (kebab)     | yes      | `"acme-app"` → `products/acme-app/`  |

## Schema, `products/<slug>/priority.json` (tunable surface), E1

Scheduled and live runs require a nonempty `product_id` and an IANA `timezone` (for example `UTC`). The
initializer fills the selected product slug and UTC; review both before the doctor. Other tunables
below deep-merge over `DEFAULT_CONFIG`.

Scheduled delivery and dedup require `SCHEDULE_DB_PATH` or `ledger.db_path` pointing to an existing,
nonempty PRIVATE schedule-reminder store. Initialize it with schedule-reminder first.
Standalone `push_card.py` delivery instead uses the configured product identity and its
PRIVATE `pool/card-deliveries/` state. It needs no ledger or timezone and cannot infer the
product from the card. Its stable identity, replay and reconciliation rules are described in
[the runtime contract](docs/runtime-contract.md).
Discord collection additionally needs the selected registry product's `discord_channels` and
`discord_token_ref`, an accessible credential and the Message Content Intent. A stable pseudonym
salt must come from `DEMAND_MINING_PSEUDONYM_SALT` or the selected companion's secret file.
The config doctor checks product identity and schema; scheduled preflight checks its selected
collection/delivery dependencies without making a model call. See docs/runtime-contract.md.

| Key                              | Type                  | Example / default                                            |
| -------------------------------- | --------------------- | ------------------------------------------------------------ |
| `schema_version`                 | int                   | `1`                                                          |
| `taxonomy[]`                     | array of object       | `{id, label, weight:float, keywords:[str], enabled:bool}`    |
| `focus_topics`                   | array of str          | `["activation friction", "competitor switch"]`               |
| `exclude`                        | array of str          | `["airdrop giveaway", "nsfw"]` (hard mutes)                  |
| `scoring.rice_weights`           | object of float       | `{reach:1.0, impact:1.0, confidence:1.0, effort:1.0}`        |
| `scoring.impact_anchors`         | object of float       | `{massive:3.0, high:2.0, medium:1.0, low:0.5, minimal:0.25}` |
| `scoring.confidence_map`         | object of float       | 4 anchored bands `1.0 / 0.8 / 0.5 / 0.3`                     |
| `scoring.min_independent_sources`| int                   | `2`                                                          |
| `scoring.effort_min`             | float                 | `0.5` (clamp floor; anti small-divisor)                      |
| `scoring.effort_tbd_default`     | float                 | `2.0` (neutral when un-estimated)                            |
| `scoring.opportunity_importance_max` / `opportunity_satisfaction_max` | float | `10.0` / `10.0`             |
| `scoring.urgency_fibonacci`      | array of int          | `[1,2,3,5,8,13]`                                             |
| `scoring.time_criticality_anchors`| object of int        | `{competitor_shipped:13, competitor_building:8, ...}`        |
| `scoring.kano_levels`            | array of str          | `["must_be","performance","delighter","indifferent","reverse"]` |
| `scoring.kano_must_be_to_tier0`  | bool                  | `true`                                                       |
| `scoring.urgency_score`          | object of int         | `{should:1, need:2, blocking:3}`                             |
| `scoring.segment_score`          | object of int         | `{free:1, pro:2, team:3, enterprise:4}`                      |
| `scoring.dedup_cosine_threshold` | float                 | `0.83`                                                       |
| `scoring.dedup_simhash_hamming`  | int                   | `3`                                                          |
| `scoring.candidate_merge_band`   | array[float,float]    | `[0.78, 0.83]` (boundary → human review)                     |
| `scoring.min_score_to_archive`   | int                   | `40`                                                         |
| `scoring.min_score_to_push`      | int                   | `70`                                                         |
| `scoring.flagship_score`         | int                   | `80`                                                         |
| `scoring.tier_bands`             | object of int         | `{tier1:80, tier2:60, backlog:40}`                           |
| `scoring.resurface_score_jump`   | int                   | `15`                                                         |
| `scoring.resurface_velocity_jump`| float                 | `5.0`                                                        |
| `scoring.lookback_days`          | int                   | `30`                                                         |
| `scoring.samples_cap`            | int                   | `30`                                                         |
| `scoring.fading_quiet_days`      | int                   | `5`                                                          |
| `scoring.override_budget`        | float                 | `0.2`                                                        |
| `scoring.golden_set_drift_band`  | int                   | `1`                                                          |
| `scoring.weight_regression`      | object of float       | `{max_tau:0.25, max_push_churn_frac:0.20, catastrophic_tau:0.6, catastrophic_churn_frac:0.5}` |
| `push`                           | object                | `{channel:"discord-relay", max_per_day:5}`                   |
| `delegation`                     | object                | `{market-intel:{enabled,scale,daily_cap}, daily-hotspots:{enabled,consume_archive}}` |
| `privacy`                        | object of int         | `{raw_retention_days:14, pseudo_map_retention_days:7}`       |

`taxonomy[]` (also valid as a standalone `taxonomy.json`), each track:

| Field      | Type         | Required | Example                                  |
| ---------- | ------------ | -------- | ---------------------------------------- |
| `id`       | str          | yes      | `"performance"`                          |
| `label`    | str          | yes      | `"Performance / reliability"`            |
| `weight`   | float        | yes      | `1.1`                                    |
| `keywords` | array of str | yes      | `["slow","crash","timeout","卡","崩"]`   |
| `enabled`  | bool         | yes      | `true`                                   |

`competitors.json` (optional), competitor watchlist consumed by delegation lane 3; free-form list of
competitor records (slug/name/url) the SKILL's deep-dive layer reads.

## Secrets, Mode B (E6)

The companion is separate and PRIVATE. The template defaults to Mode B: ignored secrets
need a separate protected backup and must be restored before use. A selected Mode A may version
credentials in verified PRIVATE Git and restore them from that private history. Declare the mode
in `secrets/README.md` and align its ignore rules. Neither mode permits credentials in public source
or printed output. Secrets used by demand-mining:

| Secret                  | Where                                                                    | Notes                                              |
| ----------------------- | ------------------------------------------------------------------------ | -------------------------------------------------- |
| Pseudonym HMAC salt     | env `DEMAND_MINING_PSEUDONYM_SALT` **or** `secrets/pseudonym_hmac_salt`  | drives `redact.py` HMAC pseudonyms; never log it.  |
| Discord bot credentials | shared via the `auto-support` single-bot relay (`push.channel`)          | not stored here when the shared relay supplies it. |
| Product code root path  | `@DEFERRED`                                                              | reserved; not required for the EOD pipeline today. |

## Clock seam (deterministic replay)

`lib.py:now_utc()` honours `$DEMAND_MINING_NOW` / `$SCHEDULE_NOW` (ISO-8601) so tests and replays are
deterministic. Not a config field, an env override for reproducibility.

## First-time setup (E3)

Create or clone the private companion repository, configure its origin, and prepare the
verified PRIVATE visibility receipt before running `init_config.py`. A plain directory is
not sufficient. The exact destination and transport checks are documented in
[Configuration and DATA](docs/runtime-contract.md#configuration-and-data).

```bash
# 1. After preparing the private repository, stamp its config skeleton:
python scripts/init_config.py            # -> ~/.demand-mining-config/
#    add a starter product profile in one shot:
python scripts/init_config.py --product acme-app

# 2. Point the skill at it (skip if you used the default path):
export DEMAND_MINING_CONFIG=~/.demand-mining-config

# 3. Fill secrets + per-product priority.json, then confirm it is ready:
python scripts/verify_config.py          # doctor: PASS/FAIL per check
python skills/demand-mining/scripts/scheduled.py --preflight  # current interpreter and adapters
```

## Switching between two configs (hot-swap), E5

A config dir is self-contained (no hardcoded absolute paths). Keep as many as you like and switch by
repointing the env var, no other change:

```bash
export DEMAND_MINING_CONFIG=~/configs/work       # config A
export DEMAND_MINING_CONFIG=~/configs/personal   # config B — same skill, different state
```

Verify the swap: `init_config.py --out ~/configs/work` and `--out ~/configs/personal`, run
`verify_config.py --config-dir <each>`, then flip `$DEMAND_MINING_CONFIG`, both must verify READY.
