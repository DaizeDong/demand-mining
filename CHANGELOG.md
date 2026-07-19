# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## [0.4.1] - 2026-07-18
### Fixed
- **Shadow mode no longer silences direct @/DM replies.** The daemon had one gate for all community
  output, so shadow (meant to hold back UNPROMPTED channel auto-replies while you review) also
  swallowed replies to users who @-mentioned or DMed the bot, which just looks broken. Split into
  three gates: `post_direct` (user-initiated @/DM reply; fires in shadow + live), `post_community`
  (unprompted channel auto-reply + reaction on a detected demand; live only), `post_display` (admin
  dashboard; any mode but dry). The reply was always generated fine (the cost chain worked); it was
  only suppressed. `tests/test_bot_modes.py` guards the gate matrix.
- **Reply language follows the user.** The reply prompt now answers in the user's own language
  (English default), instead of occasionally answering an English message in Chinese.

## [0.4.0] - 2026-07-17
Live demand-tap daemon: a persistent gateway service for the interactive half.

### Added
- **`scripts/demand_bot.py`** a discord.py gateway daemon that stays connected (Message Content
  Intent) and, in real time: replies to @-mentions and DMs; buffers monitored-channel messages and
  every `--interval` s runs a cheap regex pre-filter then a background LLM (cc, claude cost chain)
  batch-classifier over the survivors. A high-confidence demand gets a short "logged for the team"
  reply + a bookmark reaction; a low-confidence one gets the reaction only; both upsert into the
  demand pool. Every body is redacted and the author pseudonymized BEFORE the pool or an LLM sees it.
  `--dry-run` logs every action without posting (safe first run against a live server). Bot output is
  dash-normalized (house rule). Requires `discord.py`; uninstall `aiodns` if the gateway cannot
  resolve DNS (this environment needs the threaded resolver).
- **`scripts/demand_pool.py`** the persistent demand pool (`pool/demands.jsonl`): upsert with
  dedup-on-canonical-key (a recurrence bumps last_seen, unions authors so reach = distinct users, and
  merges evidence, then re-scores), plus status (new/ack/planned/shipped/wontfix) and a ranked view
  the daemon renders to an admin display channel. Pure file IO, lock + atomic replace.

### Changed
- The interactive half moves from the daily batch to a continuous service; the daily `DemandMiningEOD`
  becomes the periodic deep re-rank/dedup pass over what the daemon captured live.

## [0.3.0] - 2026-07-17
Live Discord tap: deterministic, config-driven collection from a product's community.

### Added
- **`scripts/pull_discord.py`** the collection path (SKILL.md step 0). It reads the wired product's
  Discord channels via the bot token (REST history, Message Content Intent required; channels +
  `discord_token_ref` come from the companion `registry.json`), and emits a REDACTED corpus:
  every message is scrubbed by `redact.py` and the author id is HMAC-pseudonymized BEFORE it is
  written, so raw PII never leaves this step. Bots/webhooks/empty are skipped; 403/404 channels are
  skipped with a note. Daily run pulls the last `--since-hours` (72); `--full` backfills once. The
  token is never printed, and an unwired tap exits with an init hint (never silently reads nothing).

### Changed
- SKILL.md documents the deterministic collection step; the model no longer reads Discord directly.
- Scoring floors are now a per-product calibration surface: a small community where `reach` equals
  the real distinct-author count needs lower `min_score_to_archive` / `min_score_to_push` than the
  defaults, tuned in the companion config, not here.

## [0.2.1] - 2026-07-16
House style: no en/em dash in published prose, enforced by design.

### Changed
- De-dashed the repo (docs, source comments, output strings): every en/em dash becomes a comma
  or "to". The ASCII hyphen in identifiers, flags, versions, URLs and code is left untouched.
- `digest._inline` normalizes any en/em/bar dash to a comma at render time, so an LLM-supplied
  demand field can never carry a dash into the pushed headlines. The dash set is written as `\u`
  escapes so this source file itself stays dash-free.

### Added
- `tools/dash_guard.py` plus a `dash-guard` CI workflow: prose dashes fail the build. Markdown
  code spans are exempt, and a `dash-guard: allow` line marker permits a rare legitimate dash.

## [0.2.0] - 2026-07-16
Consolidated 'headlines' delivery (ported from `daily-hotspots`, privacy-adapted).

### Changed
- **Delivery model: ONE ranked headlines digest/day, not a Discord embed per demand.** The old
  per-card push (`push_card.push_card` in a loop) plus a second full-markdown push was noisy and
  duplicative. `run.py` now marks the pushable demands as shown (no per-card network call) and
  delivers a single `digest.build_headlines` message: the top `push.max_per_day` (5) archivable
  demands ranked by tier+score, each `**N.【立即·刚需】标题**` (领域 tag = urgency·need-type) + a
  human prose summary (`why` + 建议 `recommendation`, sentence-boundary trimmed) + a
  `grade final_score · RICE=rice_raw · N证据` meta line. Thin days honestly show fewer; an all-cut day
  prints the honest 空日 line. The full markdown (every field + evidence) remains the archived digest.

### Privacy
- **The headline carries NO url and NO @handle, a deliberate divergence from daily-hotspots.** This
  skill mines PRIVATE conversation and redacts at ingest; `push_card.deliver`'s `has_pii` gate is
  fail-closed and aborts on any url/handle. So evidence links stay private and the digest is pointed
  at by a **plain-text** hint (`私有归档 <year>/<file>.md`), never a clickable link.
- **Phone matcher no longer flags calendar dates or year ranges** (shared privacy-core fix, kept
  byte-synced with the `daily-hotspots` sibling): `2026-07-15`, `2020-2026`, `2019 2020 2021` are
  never contact numbers, so `redact()`/`has_pii()` skip them. This ALSO fixes a latent bug where the
  EOD digest's own `YYYY-MM-DD` header tripped `has_pii` and would abort the push on a real run. A
  real phone in the same text is still redacted. Guards: `_ISO_DATE` + `_is_year_run`.

### Ops
- `wrapper.ps1` now commits + pushes `pool/` (demand pool + digests) to the private companion repo
  via the `git@daizedong:` ssh-alias remote after a successful run (best-effort; a push failure never
  fails the run; `secrets/` is `.gitignore`d). Also applies the `$ErrorActionPreference='Continue'`
  fix around the native `claude -p` call so a stderr line can no longer masquerade as a FALSE abort.

### Tests
- `test_gate_digest.py`: +7 headlines tests (tier ranking, 领域 tag, cap + overflow note, cut-noise
  exclusion, honest empty day, why+建议 prose join, injection neutralization, and, critically, that
  the message carries no url and passes the fail-closed `has_pii` egress gate).

## [0.1.2] - 2026-07-06
### Security / privacy
- **Redactor: NFKC hardening.** Full-width / ideographic-dot obfuscated emails (e.g. bob@host。com,
  ｊｏｈｎ＠ｅｖｉｌ．ｃｏｍ, realistic for a CJK product) bypassed Tier-1 regexes and the has_pii DLP.
  redact()/has_pii() now NFKC-normalize + fold confusable dots before matching, so obfuscated
  structured PII is caught. Plain CJK text is not over-redacted.
- **Honesty fix (no over-claim).** SKILL.md advertised a `[PERSON_1]` placeholder + "stores only
  redacted, never raw chat", but names/addresses are the unwired Tier3 NER hook (v0.2). Docs now
  state the true scope: structured PII (email/phone/card/secret/id/url/ip/handle) is stripped;
  **names/addresses are NOT yet redacted, keep them out of ingest** until apply_ner is wired.
### Fixed / added
- **T7 catch-up entry** (`run.py --catch-up`): the tested `catch_up_digests` backfill was invoked by
  nothing; now reachable (idempotent, reads no stdin) for the cron/orchestration layer.
- Removed dead `lib.age_hours` (zero callers).
- Regression tests `tests/test_redact_unicode_and_catchup.py` (+5). 85 passed.

## [0.1.1] - 2026-06-27
### Changed
- **Discord egress unified through Agent Center relay**: pushes now prefer schedule-reminder's
  `relay.py send --stream demand` (per-stream identity in the Agent Center server) when the base
  is installed, and **fall back to the Big Brother relay (send.py) when it is not**, fully
  pluggable, no behaviour change when the base is absent. Existing env/arg overrides still win.

## [0.1.0] - 2026-06-25
### Added
- Initial release, offline skeleton (skill-smith build #6).
- `redact.py`: redact-on-ingest (Tier1 regex+Luhn, Tier2 entropy, unique non-collapsing
  placeholders, HMAC author pseudonyms, egress `has_pii` DLP).
- `extract.py`: 8-label intent normalization, demand-vs-noise gate, JTBD four-force completeness,
  verbatim grounding (reject ungrounded), dual-track explicit/implicit unit builder.
- `dedup.py`: two-gate need-pool dedup (cosine ∧ simhash + subject agreement + candidate-merge
  band), NEW/SUPPRESS/RESURFACE evolution, distinct-author intensity merge, schedule-reminder
  base client (source=demand-mining, ext x_demand_mining_*).
- `score.py`: three orthogonal axes (RICE / Opportunity-ODI / WSJF) + Kano tier floor +
  deterministic 2D tiering + bounded reproducible final score + weight-regression gate.
- `verify_gate.py`: schema + ≥1-internal-evidence + egress DLP, fail-closed; no-filler batch gate.
- `digest.py`: EOD Quick-win/Big-bet split, iteration-direction queue, idempotent digest item,
  bounded catch-up; honest empty day.
- `run.py`: full deterministic pipeline orchestrator (`--dry-run` / `--no-ledger`).
- `wrapper.ps1` + `register-task.ps1`: OS Task Scheduler headless EOD (no CronCreate).
- 7 reference shards (privacy / extract / delegation / scoring / dedup-pool / eod-brainstorm /
  cron-setup); 56 tests (T1-T7).
