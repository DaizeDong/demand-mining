# Demand Mining storage

Real configuration, observations, run artifacts and credentials belong in a
versioned PRIVATE companion repository. They must never be written inside this
public source repository, including ignored paths. Runtime writers verify the
actual destination and its Git transport before writing; an unknown or PUBLIC
destination fails. See [CONFIG.md](CONFIG.md) for configuration discovery and
[the runtime contract](docs/runtime-contract.md#configuration-and-data) for
destination admission.

`storage.contract.json` inventories paths relative to the companion root. It
describes the default layout: configuration at the root, durable runtime state
under `pool/`, and temporary collection material under `data/`. The default
runtime destination is `<config>/pool`; `DEMAND_MINING_DATA_DIR` can select a
different verified PRIVATE destination. A custom layout needs its own reviewed
path declarations. The contract does not classify arbitrary legacy files by
their extension, age or presence in a private repository.

## What each area preserves

| Companion path | Purpose and retention |
| --- | --- |
| `registry.json`, flat or per-product priority/taxonomy files, `competitors.json` | Current product identity, collection wiring and policy. Preserve the versions needed to interpret pending work. |
| `secrets/` | Configured credentials and the stable HMAC salt. Preserve according to the selected private credential backup policy. The salt is not an expiring pseudo-map. |
| `pool/demands.jsonl` | Product-bound canonical demands and recorded observation identities. No automatic age deletion. |
| `pool/runs/`, `pool/scheduled/`, `pool/implicit/` | Plans, redacted evidence, manifests, delivery receipts, cursors, caller state and recovery markers. Preserve while delivery, retries, replay prevention or selected final evidence depends on them. |
| `pool/digests/`, `pool/.daily-summary-*.json` | Redacted final digests and live-summary delivery state. Preserve required evidence and unresolved outcomes. |
| `pool/.last_summary` | Legacy summary marker. Preserve during migration; a date alone does not prove delivery. |
| `pool/logs/` | Current diagnostic logs. Coordinate rotation with the writer and retain incident evidence; the corpus TTL does not rotate logs. |
| `data/corpus*.json`, `data/raw/`, `data/chunks/`, `data/eod2_chunks/`, `data/eod3_chunks/` | Temporary collection material eligible for the raw retention limit. |
| `data/manual-corpus/` | Manually supplied collection material. Requires explicit artifact registration before automatic expiry. |
| `data/pseudo-maps/` | Optional externally supplied pseudonym maps. Requires explicit registration before automatic expiry. The built-in HMAC pseudonymizer does not create reverse maps. |
| `pool/retention/` | Registration metadata, one current cleanup plan, one latest receipt and the maintenance lock. Preserve current control state; do not accumulate daily cleanup reports. |

The schedule-reminder database selected by `SCHEDULE_DB_PATH` or `ledger.db_path`
is an existing shared ledger. Demand does not create a replacement or own its
retention. Preserve it and its transactional sidecars under the ledger owner's
recovery procedure. If it resides inside this companion, add its exact path to
the companion's reviewed storage declarations before inventory acceptance.

## Temporary collection retention

`skills/demand-mining/scripts/retention.py` enforces
`privacy.raw_retention_days` (default **14 days**) and
`privacy.pseudo_map_retention_days` (default **7 days**). Both settings must be
positive integers. Cleanup selects only declared temporary paths and does not
inspect corpus contents to infer whether an unknown file is safe to remove.

Declared corpus and chunk files use their registered capture time when available;
legacy files in those declared areas use their file modification time. Manual
corpora and optional maps require registration that identifies their kind,
capture time and exact file bytes. Unregistered files stay for review.
An `authors*.json` filename is not proof that a file is a reverse map.

The retention runtime is standalone. It uses the existing Guards-backed PRIVATE
destination checks and binds cleanup to exact plan bytes, file metadata and
SHA256. Windows cleanup removes individually checked leaves through native
PowerShell. Symbolic links, junctions and hardlinks fail the safety checks.
A changed file requires a new plan.

The `plan` command is read-only. `enforce` stores one `current-plan.json` and one
`latest-receipt.json` under `pool/retention/`. The `register` command takes an
explicit `--path`, `--kind` and `--captured-at` Unix timestamp; registration binds
that artifact's identity before it becomes eligible for expiry.

The scheduled CLI runs retention before collection and holds the activity lock
through finalization. Pending recovery defers cleanup while allowing the caller
to resume its saved run. `pull_discord.py --out` uses the same lock and registers
outputs in known raw areas, including `data/manual-corpus/`. An explicitly chosen
PRIVATE output elsewhere remains outside automatic TTL; move it to a managed
area and register it when retention should apply.

Cleanup coordinates with runtime locks and defers when a run or checkpoint still
needs recovery. An existing zero-byte `.lock` file does not establish activity;
the operating-system lock does. Ledger state, roster-review material, active
delivery state and unknown historical files are not temporary corpus merely
because they are old. The runtime retention command reports deferred and manual
review cases; it must not describe them as successfully purged.

These limits govern working-tree files. Removing a tracked file leaves earlier
bytes in private Git history and backups. This implementation does not provide
history rewriting, cryptographic erasure or encryption at rest. A separate
review is required when an erasure request includes retained history or backups.

## Inventory and restoration

The shared `skill-smith/scripts/storage_contract.py` checker reads this source
contract and inventories the selected PRIVATE companion. Passing its schema
validation establishes only that the contract is well formed. Inventory also
has to report no undeclared paths, ambiguous matches or boundary errors before
the companion can be called covered. The contract lists audited legacy script,
snapshot and diagnostic filename families as retirement candidates. That
classification does not establish inactivity: an exact removal plan still needs
to check old task bindings, pending recovery and unique final evidence. The exact root
`tracking.json` is selected collection policy and is checked by the current
configuration verifier. Preserve its reviewed sources and disabled-source
decisions while collection or interpretation depends on them. Unknown author
files and undeclared product metadata still require individual review; neither
is a TTL target. A product metadata file or saved registry pointer does not prove
its configured product directory exists or that collection is deployed.

Restore the private repository, selected credentials and shared ledger before
enabling writers. Verify the destination's current PRIVATE proof, then run
`scripts/verify_config.py` and `scheduled.py --preflight`. Reconcile pending
delivery and checkpoint state before a new collection. Restore the HMAC salt
with the pool that used it so author identities remain stable. A completed local
test or an old receipt does not establish a successful current delivery.
