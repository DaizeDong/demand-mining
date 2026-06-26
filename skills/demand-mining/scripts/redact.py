#!/usr/bin/env python3
"""Privacy core — redact-on-ingest (Acceptance Gate T6). Stdlib only, PURE, deterministic.

This is the load-bearing privacy guarantee, enforced in code BEFORE any text reaches an LLM,
embedding, or the need pool. The architecture's hard rule: redaction must happen *before* the
model ever sees the message, otherwise the PII has already leaked. So `redact()` is called as the
first step of ingest in run.py, on every raw message, and only its output flows downstream.

Layers (cost-ascending; Tier1/Tier2 are pure-stdlib and always on):
  * Tier1 — deterministic regex + checksum: emails, phones, credit cards (Luhn-verified),
            Discord user-id / @handle / invite link, URLs, IPs.
  * Tier2 — entropy: long high-entropy tokens (API keys / secrets) → [SECRET_n].
  * Tier3 — NER (Presidio, LOCAL-only, never a third-party PII API) for names/addresses: a hook
            point (apply_ner) the skill can wire in v0.2; absent => Tier1/2 still redact.

Two anti-patterns this file exists to kill:
  1. Unified placeholders that COLLAPSE distinct entities ("[PERSON]" for two people loses who
     said what). We mint UNIQUE, stable-within-a-message placeholders: [PERSON_1], [EMAIL_2]...
  2. A consistent author pseudonym that is reversible. `pseudonymize()` = HMAC-SHA256(salt, id):
     same person → same token across messages (a real clustering signal) but not invertible. The
     salt is read from secrets/env at call time and NEVER hardcoded or echoed; salt-in-repo would
     make the pseudonym as good as plaintext.

The need pool stores ONLY redacted, distilled items — never raw conversation. See run.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --------------------------------------------------------------------------- Tier-1 patterns

# Order matters: more specific patterns first so an email is not partly eaten by the URL rule.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_DISCORD_MENTION = re.compile(r"<@!?(\d{15,21})>")            # <@123...> / <@!123...>
_DISCORD_ID = re.compile(r"\b\d{17,20}\b")                    # bare snowflake (user/channel id)
_INVITE = re.compile(r"\b(?:https?://)?(?:discord\.gg|discord(?:app)?\.com/invite)/\S+",
                     re.IGNORECASE)
_URL = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_HANDLE = re.compile(r"(?<![\w/])@([A-Za-z0-9_]{2,32})\b")    # @handle (not an email local-part)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# IPv6 — full 8-group form OR any "::"-compressed form (architecture Tier1 lists "IPs"). Guarded in
# the substituter so plain decimal times/ratios (colons but no "::" and not 8 hex groups) are never
# eaten. Lookaround stops partial matches inside larger word/colon runs.
_IPV6 = re.compile(
    r"(?<![\w:.])(?:"
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"                                   # 8 full groups
    r"|(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{0,4}::(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{0,4}"  # :: compressed
    r")(?![\w:.])")
# phone: loose international-ish; validated by digit count to avoid eating ordinary numbers
_PHONE = re.compile(r"(?<!\w)(\+?\d[\d\s().-]{7,}\d)(?!\w)")
_CCARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
# high-entropy token (Tier2): a long run of base64/hex-ish chars with no spaces
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def _luhn_ok(num: str) -> bool:
    ds = [int(c) for c in re.sub(r"\D", "", num)]
    if not (13 <= len(ds) <= 19):
        return False
    s, alt = 0, False
    for d in reversed(ds):
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        s += d
        alt = not alt
    return s % 10 == 0


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    from collections import Counter
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


class _Minter:
    """Mints unique, stable-within-a-call placeholders per entity TYPE and per distinct VALUE.
    The same value seen twice in one message gets the same placeholder (preserves co-reference);
    two different values get [TYPE_1] / [TYPE_2] (never collapsed)."""

    def __init__(self):
        self._by_type: dict[str, dict[str, str]] = {}

    def get(self, kind: str, value: str) -> str:
        table = self._by_type.setdefault(kind, {})
        if value not in table:
            table[value] = f"[{kind}_{len(table) + 1}]"
        return table[value]


def redact(text: str, salt: bytes | None = None) -> dict:
    """Redact one message. PURE (no clock/network). Returns:
        {redacted: str, placeholders: {placeholder: type}, found: {type: count}}
    `salt` only affects pseudonymize() (handles), not the structural redaction. Email is redacted
    before @handle so an email local-part is never mistaken for a handle."""
    found: dict[str, int] = {}
    mint = _Minter()

    def bump(k):
        found[k] = found.get(k, 0) + 1

    # 1) invite links (before generic URL), 2) emails, 3) discord mentions/ids, 4) urls,
    # 5) credit cards (Luhn), 6) phones, 7) ipv4, 8) handles, 9) Tier2 secret tokens.
    def sub_invite(m):
        bump("INVITE"); return mint.get("INVITE", m.group(0))
    text = _INVITE.sub(sub_invite, text or "")

    def sub_email(m):
        bump("EMAIL"); return mint.get("EMAIL", m.group(0))
    text = _EMAIL.sub(sub_email, text)

    def sub_mention(m):
        bump("DISCORD_ID"); return mint.get("DISCORD_ID", m.group(1))
    text = _DISCORD_MENTION.sub(sub_mention, text)

    def sub_url(m):
        bump("URL"); return mint.get("URL", m.group(0))
    text = _URL.sub(sub_url, text)

    def sub_cc(m):
        v = m.group(0)
        if _luhn_ok(v):
            bump("CARD"); return mint.get("CARD", re.sub(r"\D", "", v))
        return v
    text = _CCARD.sub(sub_cc, text)

    def sub_phone(m):
        v = m.group(1)
        if len(re.sub(r"\D", "", v)) >= 8:
            bump("PHONE"); return mint.get("PHONE", re.sub(r"\D", "", v))
        return v
    text = _PHONE.sub(sub_phone, text)

    def sub_ipv6(m):
        v = m.group(0)
        # require a real "::" or the full 8-group form, and at least one hex digit — so a bare
        # "::" or a decimal time/ratio is left untouched (fail-safe against over-redaction).
        if "::" not in v and v.count(":") != 7:
            return v
        if not re.search(r"[0-9A-Fa-f]", v):
            return v
        bump("IP"); return mint.get("IP", v)
    text = _IPV6.sub(sub_ipv6, text)

    def sub_ip(m):
        bump("IP"); return mint.get("IP", m.group(0))
    text = _IPV4.sub(sub_ip, text)

    def sub_handle(m):
        bump("HANDLE"); return mint.get("HANDLE", m.group(1))
    text = _HANDLE.sub(sub_handle, text)

    def sub_id(m):
        bump("DISCORD_ID"); return mint.get("DISCORD_ID", m.group(0))
    text = _DISCORD_ID.sub(sub_id, text)

    def sub_token(m):
        v = m.group(0)
        if _entropy(v) >= 3.5 and any(c.isdigit() for c in v) and any(c.isalpha() for c in v):
            bump("SECRET"); return mint.get("SECRET", v)
        return v
    text = _TOKEN.sub(sub_token, text)

    placeholders = {ph: kind for kind, table in mint._by_type.items() for ph in table.values()}
    return {"redacted": text, "placeholders": placeholders, "found": found}


# --------------------------------------------------------------------------- pseudonyms

def _load_salt() -> bytes:
    """Salt discovery (NEVER hardcoded; salt-in-repo == pseudonym-in-clear). Order:
      1) DEMAND_MINING_PSEUDONYM_SALT env (raw value),
      2) the companion repo's secrets/pseudonym_hmac_salt file (gitignored, Mode B),
      3) a process-ephemeral random salt (tests/offline; pseudonyms then NOT cross-run-stable).
    The value is read but never logged/echoed."""
    v = os.environ.get("DEMAND_MINING_PSEUDONYM_SALT")
    if v:
        return v.encode("utf-8")
    d = os.environ.get("DEMAND_MINING_CONFIG")
    if d:
        p = os.path.join(os.path.expanduser(d), "secrets", "pseudonym_hmac_salt")
        try:
            if os.path.isfile(p):
                return open(p, "rb").read().strip()
        except Exception:
            pass
    # ephemeral: stable within ONE process run only (good enough for offline tests/--dry-run)
    return os.urandom(32)


_EPHEMERAL_SALT = None


def pseudonymize(user_id: str, salt: bytes | None = None) -> str:
    """author_pseudo = HMAC-SHA256(salt, user_id)[:16]. Same person → same token (a clustering
    signal); not invertible (no reverse table). right-to-erasure = forward-delete by this hash."""
    global _EPHEMERAL_SALT
    if salt is None:
        if _EPHEMERAL_SALT is None:
            _EPHEMERAL_SALT = _load_salt()
        salt = _EPHEMERAL_SALT
    mac = hmac.new(salt, (user_id or "").encode("utf-8"), hashlib.sha256).hexdigest()
    return "u_" + mac[:16]


def has_pii(text: str) -> bool:
    """Cheap egress check (DLP): True if any Tier1/Tier2 pattern still matches — used fail-closed
    before anything leaves the machine (push, delegation query). A True here BLOCKS egress."""
    r = redact(text or "")
    return bool(r["found"])


def main() -> int:
    """CLI: stdin {text, user_id?} → {redacted, found, placeholders, author_pseudo?}."""
    data = json.loads(sys.stdin.read() or "{}")
    out = redact(data.get("text", ""))
    if data.get("user_id"):
        out["author_pseudo"] = pseudonymize(data["user_id"])
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
