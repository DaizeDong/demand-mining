# Privacy and temporary collection retention

Redaction runs before message text reaches a model, embedding, pool write or
output. `scripts/redact.py` provides the local checks; `run.py` and the live
collection pipeline apply them at their input boundaries.

## Local redaction

| Layer | Covered forms | Implementation |
| --- | --- | --- |
| Regex and checksum | Email, phone, Luhn-valid payment-card numbers, Discord IDs, handles, invites, URLs and IPv4 addresses | `redact.redact()` |
| Entropy | Long mixed high-entropy tokens that resemble API keys or secrets | Local entropy and token-shape checks |
| Conservative contextual patterns | Latin name spans and common street-address forms | Local redaction; unsupported personal context is held for review before model, pool or output use |

The doctor lists covered, uncovered and unchecked sensitive types. General
multilingual named-entity recognition remains unchecked. These checks do not
recognize every person, address or private fact.

Distinct detected values receive distinct placeholders within a message, such
as `[EMAIL_1]` and `[EMAIL_2]`. Repeated occurrences of the same value reuse its
placeholder. This preserves local references without retaining the original
value in the processed text.

## Stable author identity

`pseudonymize(user_id)` uses HMAC-SHA256 with a stable private salt and retains
16 hexadecimal digest characters. The same input and salt produce the same
author token. This supports observation deduplication; it is pseudonymization,
not proof that the retained evidence is anonymous.

The salt resolver reads `DEMAND_MINING_PSEUDONYM_SALT` first, then
`secrets/pseudonym_hmac_salt` in the configured PRIVATE companion. Real collection
requires stable configured bytes. The explicit offline dry-run mode can use an
ephemeral salt. Never publish or log the configured salt. Restoring or rotating
it requires attention to the author identities already stored in the pool.

The built-in pseudonymizer does not persist a reverse map. An optional map
supplied by another process is separate from the HMAC salt and needs explicit
retention registration. No map encryption is implemented here.

## Output checks

`redact.has_pii()` checks supported sensitive patterns before output.
`verify_gate.py` blocks a card with a detected residual value, and
`push_card.py` refuses a send with such a value. These checks are bounded by the
covered patterns above; a passing result is not a guarantee that all private
context has been removed.

Delegated market or web queries contain non-private topics such as product
features or competitor names. Do not send a user's raw conversation as a query.

## Storage and expiry

The need pool keeps distilled, redacted demands, evidence snippets, observation
pointers and HMAC author tokens. Raw conversation does not belong in the pool.
Real configuration and runtime DATA reside in a verified, versioned PRIVATE
companion, outside the public source repository.

`scripts/retention.py` implements the two `privacy` limits: raw collection
material defaults to **14 days**, and explicitly registered optional pseudo-maps
default to **7 days**. Declared raw areas cover `data/corpus*.json`, `data/raw/`,
`data/chunks/`, `data/eod2_chunks/` and `data/eod3_chunks/`. Manual material under
`data/manual-corpus/` and maps under `data/pseudo-maps/` require explicit
registration; unknown historical files remain for review.

The standalone retention runtime uses exact plans and SHA256 checks, with the
existing Guards-backed PRIVATE validation and runtime recovery/lock checks. It does
not age-delete the pool, shared ledger, run receipts, pending checkpoints or
the HMAC salt. The default interval is a runtime retention policy; this page
does not promise an independent cron job.

Deleting a working-tree file does not remove its private Git history or backups.
Neither encryption at rest nor full right-to-erasure handling is implemented by
this retention policy. An erasure request requires identifying the relevant
author's evidence and reviewing every retained copy, including history.

The source-root [DATA.md](../../../DATA.md) and
[storage.contract.json](../../../storage.contract.json) define artifact purposes,
retention boundaries and restoration requirements.
