# privacy, redact-on-ingest (Step 1, runs FIRST, always)

The load-bearing guarantee, enforced in code (`scripts/redact.py`), not in a prompt promise.
**Redaction must happen before any LLM / embedding / pool write touches a message**, once the
model has seen PII, it has leaked. So run.py redacts every raw message first; only the output flows.

## Layers (cost-ascending; Tier1/Tier2 always on, pure stdlib)

| Tier | Catches | How |
|---|---|---|
| 1 regex+checksum | email, phone, credit card (Luhn), Discord id/`@handle`/invite, URL, IPv4 | `redact.redact()` |
| 2 entropy | API keys / long high-entropy tokens → `[SECRET_n]` | Shannon entropy ≥3.5 + mixed alnum |
| 3 NER (opt, v0.2) | person names / addresses | Presidio **local-only**, never a 3rd-party PII API |

## Two anti-patterns this kills

1. **Collapsed placeholders.** A unified `[PERSON]` for two people loses who-said-what. We mint
   **unique, co-reference-stable** placeholders: `[EMAIL_1]`/`[EMAIL_2]`; the same value twice in
   one message reuses one placeholder. (Tested: `test_redact.py::test_unique_placeholders_no_collapse`.)
2. **Reversible / committed pseudonyms.** `pseudonymize(user_id) = HMAC-SHA256(salt, id)[:16]` ,
   same person → same token (a real clustering signal), not invertible. The salt is read from
   `DEMAND_MINING_PSEUDONYM_SALT` env or `secrets/pseudonym_hmac_salt` (gitignored, Mode B) at call
   time, **never hardcoded or echoed**. Salt-in-repo = pseudonym-in-clear.

## Egress DLP (fail-closed, the second wall)

`redact.has_pii()` re-scans any user-visible string before it leaves the machine. `verify_gate.py`
blocks a card with residual PII; `push_card.py` aborts a send with residual PII. So even a model
slip cannot reach Discord or a delegated web query.

## Pool storage rule + retention

The need pool stores **only distilled, redacted demand items**, canonical job/pain + redacted
evidence snippet + msg pointer + HMAC pseudonym. **Never raw conversation.** Retention (config
`privacy`): Tier0 raw 7-30 days cron-purged; pool long-lived; pseudo-map (if persisted) short-TTL +
encrypted. Right-to-erasure = forward-delete every evidence row by `author_hash` (no reverse table).

## Delegation hygiene

Queries handed to market-intel / web carry only non-private topics (feature/competitor name) ,
never a user's raw words. That is both a privacy rule and a prompt-injection defense.
