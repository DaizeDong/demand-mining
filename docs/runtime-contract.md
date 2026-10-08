# Runtime contract

The scheduled wrapper invokes `scheduled.py`. That caller collects and redacts
the source corpus, uses the installed `llmcall.call(prompt, mode="agent")` to
propose candidates, then calls the local deterministic finalizer. Model, routing,
timeouts and fallbacks belong to llmcall. Classification, independent audits and
replies use that same interface with only schema and independence constraints.
Scheduled preflight imports `llmcall.call` in its current Python interpreter and checks
that it accepts `call(prompt, mode="agent")`. Absence, a non-callable attribute or an
incompatible signature fails before readiness. This check makes no model call.

## Configuration and DATA

Each real run needs `product_id` and an explicitly configured IANA `timezone` in
the product priority configuration. Initialization writes `timezone: "UTC"`;
existing configurations must add a zone. Preview-only pure functions can use
UTC without configuration. The wrapper and runner use the same zone, including
DST boundaries. The logical identity is `{product_id, timezone, date,
source_window}`. A first scheduled collection starts at local midnight and ends
at a frozen current UTC cutoff. Later daily runs start at the prior completed
cutoff, including messages that arrived after yesterday's scheduled run. An
explicit window can carry start/end; cursor-only identities are supported by the
finalizer but rejected by the Discord scheduler until a cursor collector exists.
Attempts have separate random IDs. A successful run durably records its source
window with the watermark; changing a window creates a different logical run.
Legacy direct candidate callers also freeze their implicit window in private
`implicit/<product and zone hash>/active.json`. Same-input retries on that local
day reuse the cutoff, unfinished runs resume across midnight, and a later day's
run starts at the prior completed cutoff. An unfinished run refuses changed input;
callers needing an independent collection declare its window and identity.

Every persisted output must resolve to a Git repository with origin visibility
PRIVATE in the operator's verified local `~/.pii-guard/visibility.json` receipt.
The pinned Guards `prove_private_companion` API validates the complete transport
and requires a fresh `_refreshed` timestamp in that receipt.
PUBLIC, unknown and unmanaged destinations are rejected with the actual path.
The local visibility receipt format identifies canonical GitHub remotes; an
unresolved SSH alias or another host is not inferred to have that identity. Every
origin push URL must point to that same verified private repository.
Admission checks every configured remote's resolved fetch and push URLs, including
secondary remotes, and verifies PRIVATE visibility for every destination repository.
`remote.pushDefault`, `branch.*.pushRemote` and `branch.*.remote` must select a
configured named remote. Raw URL, local and unknown selectors are refused as
unproved; configure a named PRIVATE remote instead. Remote names retain their
case-sensitive identity. HTTPS permits the documented HTTP performance settings and
explicitly enabled certificate verification; unproved routing, proxy, header,
TLS and transport-command overrides are refused. `no_proxy` and the two Git
HTTP low-speed tuning variables remain supported. SSH requires the shared
guard's static configuration proof and refuses SSH command overrides. Git HTTP
settings do not govern an SSH connection. Visibility still comes from the
separate local verified receipt; this tool introduces no visibility API call.
Backup rechecks the configuration/environment fingerprint before its push, and
the Git launcher requires that current admission proof.

This applies to explicit archive/corpus/log paths, daemon output, run state,
receipts and the existing shared ledger. `SCHEDULE_DB_PATH` or `ledger.db_path`
must identify an existing, nonempty private shared store. Initialize that store
explicitly with schedule-reminder before attaching Demand; every ledger operation,
including reads and `init`, refuses a missing or empty database. No second
database is silently created. The tool does not query or change repository visibility automatically.
Explicit invalid config or DATA overrides never fall through to home defaults.
The nearest Git boundary owns a destination, including a nested private repository.
A malformed or aliased boundary cannot fall back to an outer private repository.
Runtime destinations refuse symbolic links, junctions and hardlinked files. Atomic
writes recheck the current proof immediately before replacing their destination.

### Fresh artifact admission

Every durable write and transient lock/staging creation calls the pinned Guards
artifact authorizer. It reloads the source storage contract, requires exactly one
active owner, checks the actual containing companion and proves its publication
routes PRIVATE with current Git configuration. Versioned artifacts must also be
eligible for Git tracking. A cached read/preflight transport proof never replaces
this fresh artifact proof, including before atomic replacement and log appends.
Backup pushes likewise obtain a fresh transport proof.

The older transport helper retains a bounded cache for non-writing preflight
callers. Its result alone does not authorize an artifact write. No ten-minute
admission window applies to the atomic writer or log append path. Fresh checks add
local Git-process overhead; offline regressions exercise that behavior but do not
establish live daemon throughput or latency.

### Windowless child processes

The daemon and its supervisor run under pythonw.exe, which has no console. On
Windows a console program started by such a process receives a new console, which
the default terminal shows as a window. Every subprocess launch in this skill
passes `CREATE_NO_WINDOW` (`no_console.no_window_kwargs`). The long-running entry
points (`daemon_supervisor.py`, `demand_bot.py`, `scheduled.py`) also install a
process-wide default when they start without a console, so launches made by
imported code, including the guard kit's Git calls, get the same flag. A caller
that explicitly requests a new console or a detached process keeps its choice. The
supervisor starts the daemon with the flag as well. With a console, children share
it and the default is not installed.

Initialize the private companion repository and its visibility receipt before
running `scripts/init_config.py`. Real DATA is versioned in that private repo.
`DEMAND_MINING_DATA_DIR` overrides the default `<config>/pool` destination.
Do not put real output inside the public tool tree, including ignored paths.

## Product ledger identity

Demand keys and watermarks include the product identity. Replays for one product
keep one key; another product can independently report the same demand. Reads
select only rows whose stored identity names the current product. Attributed
legacy rows remain readable, with new scoped rows taking precedence. Existing
rows without a product identity remain untouched and are excluded until their
ownership is explicitly reviewed; collection never guesses their product.
No automatic ledger migration runs.

The live JSONL pool may share a private DATA root across products, but each new
row carries the configured stable product_id (or slug). Merge identity is the
pair of product_id and canonical_key. load, ranked and set_status require
the selected product explicitly; direct feedback, passive collection and daily
summaries use the same binding. Missing identity fails before a pool read or
write. A rescorer cannot move a row to another product or canonical key.

Unattributed live rows remain excluded. Collection does not infer their owner.
After reviewing one row, a caller may explicitly use
demand_pool.attribute_legacy(path, canonical_key, product_id=product).
It changes only that row's product attribution, preserves its demand history and
other rows, and refuses multiple legacy matches or an existing target-product
row with the same canonical key. It returns false when no legacy match remains.
Keep the product identity stable when renaming the display name.

## Stable author pseudonyms

Preflight and pseudonymization use the same salt resolver: the explicit salt
environment value, then the secrets file in the explicitly configured or
default-discovered config directory. Empty or whitespace-only values are errors.
Real collection requires stable configured bytes; its preflight pins those bytes
for the process. Uninitialized offline helpers may opt into an ephemeral salt
with `DEMAND_MINING_DRYRUN=1`.

## Live classification recovery

The gateway owns one classification task and retains its pending batch separately
from newly arriving messages. A classifier or pool failure logs a pending state
and retries with exponential backoff counted in polling intervals: the first retry
is the next poll (90 s by default), then each wait doubles up to one hour. Only
state changes are logged: the first failure, a different error, recovery and
quarantine. Retries in between are silent. Completed observations leave the pending
queue before acknowledgment, so later failures do not replay completed work.
Reconnects reuse the active task. An unexpected task exit starts a replacement
with the same pending batch.

An observation the privacy screen keeps holding (`PrivacyReviewRequired`) is not
retried forever. After eight held attempts (about 2.6 hours at the default
interval) it moves to `pool/quarantine/` in the PRIVATE companion and the batch
continues. A hold during persistence quarantines the head observation; a hold
during classification cannot name one message, so the unclassified batch is held
together. Each record keeps the ingest-redacted text, pseudonymous identities, the
stage, any verdict or demand already computed, and `hold_source`. `pool` means the
observation screens clean on its own and the hold came from rows already stored in
`pool/demands.jsonl`, which every pool write screens again. Nothing is discarded:
the quarantine write uses the same PRIVATE admission as every other DATA write, and
when that admission is refused the observation stays pending and keeps backing off.
Replaying a record is an explicit operator step after review.

Direct mentions and DMs enter a separate pending map before context gathering.
Context, classification, persistence and reply-generation failures retain the
observation for the same supervised polling task to retry, with the same per-observation
backoff, change-only logging and privacy quarantine as passive collection. Direct handlers require complete
context: opener, reply-reference and history API failures propagate before partial context
can be cached or classified. The default context helper remains best-effort for display-only
callers. A successful stage
is reused on retry. Classified feedback is saved before reply generation; the
reply prompt receives the actual storage status and permits a logging
confirmation only after successful persistence. A non-demand reply receives
an explicit instruction that nothing was recorded.

The channel/message identity admits only one active direct handler at a time.
Completed persistence is not repeated when reply generation fails. The bot
records a reply attempt before awaiting the transport, so an exception with an
unknown remote outcome never triggers an automatic resend. Completed direct
observations also remain suppressed if a later admin activity note fails.
These pending maps and completion identities live in process memory. They do
not provide an inbox or replay suppression across process or machine failure,
and do not establish remote exactly-once delivery or model-language compliance.

## Same-batch canonical contributions

Exact canonical duplicates in one handoff are combined before scoring, dedup decisions,
pool mutations and headline selection. The first proposal supplies prose and other score
inputs; author contributions and distinct evidence are united in stable order. Reach,
source counts and mention counts use the largest declared count or observed union size,
so overlapping estimates are not added. Scoring uses those combined inputs. Each canonical
key gets one planned mutation and at most one headline. Existing bounded ledger evidence
samples remain bounded; the current cards and full digest retain the merged evidence.
Retries reuse the same durable plan and delivery receipt.

CUT and Kano indifferent/reverse cards are excluded before headline capacity is
allocated. The gate and renderer share the same rule, so proposed headlines,
planned push flags and confirmed push counts name the rendered selection. Tier0
retains its priority and remains eligible below the usual score floor.

## Handoff and completion

`run.process(candidates, cfg, ledger, dry_run, run_id, archive_dir)` keeps its
existing positional signature. Additive keyword fields are `identity`,
`attempt_id` and `collection`. It also accepts an object with those fields and
`candidates`. A collection record carries `status: "complete"`,
`classification: "complete"` and the exact `source_window`. Empty input without
that explicit evidence cannot complete a real run. Direct nonempty candidate
lists are treated as an explicit caller-supplied, completed candidate collection.

The private `runs/<identity hash>/` directory contains the immutable plan, actual
digest and card bytes, artifact manifest, state, adapter receipt and source
cursor. The manifest records actual paths, byte counts and SHA256 hashes. A
restart verifies those bytes and resumes the saved plan. Parse, collection,
classification, ledger, digest, lock and persistence errors are visible failures.
The finalizer must return successfully and the scheduled caller must complete
its own outcome; a model exit code or an old artifact cannot prove success.

After the scheduled caller has created its private `caller.json`, an exception
retains `status: "failed"` and `error_type` and adds `error_stage` and
`error_category`. Stages are `working_directory`, `handoff_read`,
`collection_config`, `collection`, `classification`, `handoff_write`, `ledger`,
`finalization`, `caller_state`, `manifest_verify`, `backup_delivery`,
`completion_state` and `backup_completion`. Failed backup objects carry the same
fields while retaining the existing `delivered_backup_pending` result.

Categories are `config_missing`, `config_invalid`, `config_io_error`, `timeout`,
`http_access_denied`, `http_source_unavailable`, `http_rate_limited`,
`http_server_error`, `http_error`, `transport_error`, `invalid_json`,
`io_error`, `llm_process_cleanup_failed`, `llm_timeout`,
`llm_policy_refusal`, `llm_not_installed`, `llm_budget_exhausted` and `unknown`.
Configuration categories apply to the collection-wiring stage. Transport
categories describe preserved exception types or numeric HTTP status, including
explicit causes; they do not reconstruct retries whose evidence was discarded.
LLM categories consume exact emitted `attempts[].reason` codes, with cleanup,
timeout, refusal, missing installation and budget in that priority order.
Unknown or absent codes remain `unknown`; empty attempts do not prove a
configuration or budget failure. These fields contain no exception message,
provider text, source content, token, path or traceback. The original exception
still propagates and restores the working directory. Preflight, initial state
writes and `BaseException` exits retain their existing behavior and may occur
before this failure record exists. Older records are not backfilled.

Delivery remains tuple-compatible: `(ok, detail)`. Confirmed adapters return a
detail object with `status: "confirmed"`, actual `message_id`, exact `identity`,
and the SHA256 of the sent UTF-8 content. A native Discord
response with matching `content` and an actual `id` is also recognized by the
relay adapter, which binds that actual response to its invocation identity.
Configured adapters receive `DEMAND_MINING_DELIVERY_IDENTITY` and
`DEMAND_MINING_DELIVERY_CONTENT_SHA256` and must return those bindings in their
receipt. An identity-less receipt or plain exit-zero string is insufficient.

The finalizer durably records unknown delivery before calling the adapter. An
exception, timeout or missing receipt remains `pending_reconciliation` and never
causes automatic resend. If interruption left a confirmed receipt on disk before
the state update, a restart validates its identity and content hash against the
saved manifest, then finishes bookkeeping without sending again. Invalid or
contradictory saved receipts fail visibly. `finalize.reconcile(state_path, actual_receipt)` records
verified receipt evidence; the restarted caller then finishes the watermark.
This method issues no send. Only durable current artifacts, manifest and receipt
permit watermark advancement. Exclusive process locks serialize overlapping
callers. Lock failure never permits an unlocked write.

The live bot's daily summary has its own product/zone-bound state and receipt.
`live.summary_hour` is a local hour in the configured zone; the old
`summary_hour_utc` key keeps its UTC-hour meaning when the local-hour key is absent.
Summary grouping converts observation timestamps to the same zone. Unknown
summary sends remain pending without automatic resend, including after a
restart. `DemandBot.reconcile_daily_summary(actual_receipt)` accepts confirmed
identity, message ID and content hash evidence. Legacy date-only markers are
kept but do not establish confirmed delivery.

Backup inspects staged changes using an isolated Git index, then uses Git's
path-limited `commit --only` so only declared run paths are committed and unrelated
staged edits survive. A repository lock serializes this skill's backups. Each Git result is
checked; a failure stops later backup steps. No pull/rebase/autostash affects
unrelated work. Delivery completion and backup status are separate. A failed
backup returns `delivered_backup_pending`; rerunning retries backup against the
same confirmed delivery without sending again. A local test with fake adapters
does not establish live delivery or remote backup persistence.
Declared backup paths must be regular files; Git treats every pathspec literally.
After backing up delivery artifacts, the caller persists its known completion
state and backs up those final bytes. Failure of that second checkpoint remains
`delivered_backup_pending`; success makes no further changes to backed files.
A private transient `checkpoint-pending.json` coordination marker keeps the
original identity resumable if the caller stops between those checkpoints,
including across midnight. Like the process lock, it is outside the declared
snapshot and is removed only after the final checkpoint succeeds. A pending
checkpoint cannot be bypassed by disabling backup.

## Privacy scope

Structural regex/checksum/entropy redaction remains enabled. Local conservative
rules also scrub Latin person-name spans and common street-address formats.
Unsupported personal-context spans are held with a visible privacy-review error
before model, pool or output boundaries. Nested candidate/evidence metadata is
scrubbed, and author IDs become HMAC pseudonyms. Harmless product language stays
usable. The config doctor explicitly lists covered, uncovered and unchecked
sensitive types; these rules do not constitute a general multilingual NER model.

The private Markdown archive includes the complete redacted fields and nested
evidence for each actionable demand, ordered by the same priority as the queue.
Literal JSON records preserve extension fields and keep embedded markup inside
the record. Delivered headlines retain their concise summary format.

Daemon and supervisor logs renew PRIVATE destination admission before every append, through the fresh artifact admission described under Configuration and DATA. The supervisor reads child stdout and stderr through a pipe and writes bounded binary chunks itself; children never inherit a log file handle. A refused log destination stops the current child and prevents restart. Direct --log-file uses the same append path, restores the prior output streams on exit, and records startup tracebacks only while the destination remains PRIVATE. Admission is checked when output is appended or a restart is attempted, not by an idle background visibility poll. The daily summary loop wakes every 30 minutes but proves its destination only when it is about to write: an already confirmed day or a send awaiting reconciliation is read without a proof, and a repeated summary state or failure is logged once.

Direct demand_bot CLI startup parses its real arguments and opens the validated private log before importing discord or llmcall. Missing optional dependencies retain their original import error in that log, including windowless execution where both streams began as None. Help and invalid arguments are handled before optional imports. Importing the module keeps its public functions and class available without parsing CLI arguments. PRIVATE admission is renewed for every append, and original streams are restored on exit.


## Observation conservation

The live tap and scheduled Discord collector pseudonymize channel and message
identities with the same namespaces. Replaying one message therefore keeps one
observation. The pool stores cumulative observation identities separately from
its eight display snippets. Reach counts distinct observed authors, independent
sources count distinct channels, and the internal confidence cluster counts
distinct internal authors. Repeated messages by one author do not create that
cluster. Legacy rows contribute only the authors and evidence they still retain;
missing historical corroboration is not reconstructed from old numeric claims.

Scheduled classification supplies proposed demand groupings and score estimates.
The caller binds every quoted span to the selected corpus channel, then derives
authors, origins and corroboration counts from the matched corpus observations.
Optional message identities and timestamps must match. Model count declarations
do not survive this binding. Empty or nontextual internal evidence blocks a card,
and a blocked card prevents real-run completion.

Every nonexact automatic duplicate match requires entity overlap and subject
agreement. The configured candidate-merge band remains review-only. Exact
canonical replay keeps its separate identity rule.


## Observation identity and external corroboration

Collected corpus rows use author_hash. Privacy screening also accepts the legacy author alias and raw user_id, normalizing both to author_hash. Existing u_ followed by 16 lowercase hexadecimal digits is already a pseudonym and remains unchanged. Conflicting author aliases are rejected. Author aliases require a string or a non-boolean integer; unsupported types are rejected before normalization, regardless of field order.

Only observation_id and source_id preserve the supported typed identity forms: u_ plus 16 lowercase hex digits, or the fallback obs_ plus 64 lowercase hex digits. The same spelling in prose receives normal privacy screening. This validates the identifier format; it does not prove the origin of arbitrary supplied metadata.

Every merge retains individually recorded historical authors, including authors without a surviving snippet. Numeric historical reach or mention claims do not create author records. Unknown attribution is represented by an absent author_hash in generated evidence and observation facts. Reading a stored pool converts a canonical null author_hash to absence only in evidence and observation_index entries with an existing typed observation_id (u_ plus 16 hexadecimal characters or obs_ plus 64 hexadecimal characters) and no competing author or user_id alias. This conversion applies to every stored row, including unrelated products, before any pool mutation; it preserves identities, authors, counts and all other fields. A read exposes the converted view without changing stored bytes. A successful upsert, status change or legacy attribution persists the conversion for the whole pool; a no-op or failed write leaves stored bytes unchanged. New demand input and rescore output still pass strict alias validation and cannot use this stored-data compatibility rule. An existing observation identity with unknown attribution stays unknown when later author records arrive. A legacy snippet without an observation identity can inherit an author only when its record has exactly one known author. The untrusted alias validator still rejects null and other unsupported types.

Current external corroboration is derived from evidence with an explicit external origin, a nonempty source and quote, and a valid timestamp. A model-supplied count alone cannot trigger resurfacing. Scheduled grounding discards supplied competitor and velocity claims because the internal corpus does not establish those events.
