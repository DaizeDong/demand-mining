#!/usr/bin/env python3
"""Privacy core, redact-on-ingest (Acceptance Gate T6). Stdlib only, PURE, deterministic.

This is the load-bearing privacy guarantee, enforced in code BEFORE any text reaches an LLM,
embedding, or the need pool. The architecture's hard rule: redaction must happen *before* the
model ever sees the message, otherwise the PII has already leaked. So `redact()` is called as the
first step of ingest in run.py, on every raw message, and only its output flows downstream.

Layers (cost-ascending; Tier1/Tier2 are pure-stdlib and always on):
  * Tier1, deterministic regex + checksum: emails, phones, credit cards (Luhn-verified),
            Discord user-id / @handle / invite link, URLs, IPs.
  * Tier2, entropy: long high-entropy tokens (API keys / secrets) → [SECRET_n].
  * Local name/address patterns plus visible review holds. General NER remains unchecked;
    privacy_coverage() names the covered forms and the remaining limitations.

Two anti-patterns this file exists to kill:
  1. Unified placeholders that COLLAPSE distinct entities (one "[EMAIL]" for two addresses loses who
     said what). We mint UNIQUE, stable-within-a-message placeholders: [EMAIL_1], [PHONE_2]...
     Conservative name/address patterns use the same placeholder mechanism.
  2. A consistent author pseudonym that is reversible. `pseudonymize()` = HMAC-SHA256(salt, id):
     same person → same token across messages (a real clustering signal) but not invertible. The
     salt is read from secrets/env at call time and NEVER hardcoded or echoed; salt-in-repo would
     make the pseudonym as good as plaintext.

The need pool stores ONLY redacted, distilled items, never raw conversation. See run.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sys
import unicodedata

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Confusable dot/at punctuation that NFKC does NOT fold (ideographic full stops etc.). Mapped to
# ASCII BEFORE NFKC so full-width / homoglyph obfuscation cannot smuggle structured PII past the
# Tier-1 regexes (e.g. bob@host。com, ｊｏｈｎ＠ｅｖｉｌ．ｃｏｍ). NFKC handles the U+FF00 full-width block.
_CONFUSABLE_PUNCT = str.maketrans({"。": ".", "｡": ".", "︒": ".", "﹒": ".", "･": ".", "‧": "."})


def _normalize(text: str) -> str:
    """Canonicalize text so obfuscated structured PII is matchable: fold confusable dots, then NFKC
    (full-width -> ASCII). Applied only inside redact()/has_pii(); the redacted output is the
    normalized form (acceptable for the distilled pool; CJK content is unchanged by NFKC)."""
    return unicodedata.normalize("NFKC", (text or "").translate(_CONFUSABLE_PUNCT))

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
# IPv6, full 8-group form OR any "::"-compressed form (architecture Tier1 lists "IPs"). Guarded in
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

# A bare ISO calendar date (YYYY-MM-DD) and a pure run of 4-digit years look like a loose phone
# (8+ digits joined by '-'/space) but are NEVER contact numbers. The phone substituter skips them so
# a date header or a '2020-2026' range is not mislabeled [PHONE_*], which would also make the
# fail-closed has_pii() gate abort an otherwise-clean digest. A real phone survives both guards.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Local, conservative coverage. These rules supplement the structural scanner;
# unrecognized personal-context spans are held instead of sent to a model.
_STREET = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z][\w'-]*\s+){1,5}"
    r"(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|court|ct|way|boulevard|blvd)\b"
    r"(?:[.,]?\s+(?:apt|apartment|suite|unit|#)\s*[\w-]+)?", re.I)
_NAME = re.compile(r"\b[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?(?:\s+[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?){1,3}\b")
_NAME_CONTEXT = re.compile(
    r"(?i)(?:my name is|name\s*:|contact person\s*:|named)\s+"
    r"([a-z][a-z'-]+(?:\s+[a-z][a-z'-]+){0,2})(?=\s*(?:[.!?;\n]|$))")
_CJK_PERSON = re.compile(
    r"(?:我叫|姓名[：:]|联系人[：:])\s*([\u3400-\u9fff]{2,4})(?=\s*(?:[。！？；\n]|$))")
_CJK_ADDRESS = re.compile(
    r"(?:地址[：:]|住址[：:]|住在)\s*[^。！？\n]{2,100}(?=\s*(?:[。！？\n]|$))")
_PERSON_CUES = re.compile(
    r"(?i)\b(?:"
    r"(?:my|his|her|their|our|your)\s+(?:(?:full|legal|real|first|last|given|family)\s+)?name\b"
    r"|(?:full|legal|real|given|family|contact)\s+name\s*(?:is\b|[:=])"
    r"|name\s*[:=]"
    r"|(?:home|street|mailing|postal|residential|delivery)\s+address\b"
    r"|(?:my|his|her|their|our|your)\s+address\s*(?:is\b|[:=])"
    r"|(?:lives?|resides?)\s+(?:at|in)\b"
    r"|contact person\b|(?:person|user|contact)\s+named\b)"
    r"|(?:我叫|(?:我的?)?名字(?:是|为|為|[：:])|姓名(?:是|为|為|[：:])"
    r"|联系人[：:]|住址|(?:联系|联络|送货|收货|邮寄|家庭)?地址(?:是|为|為|[：:])|住在)")
_PRODUCT_WORDS = set("api csv json sql ui ux oauth github discord acme acmecorp widget "
                     "product export import dark light mode quick win big bet core workflow "
                     "demand mining daily summary new update tier backlog performance "
                     "please add retry support first last all time opportunity rice kano wsjf "
                     "google drive microsoft teams error message project alpha beta "
                     "keyboard shortcut shortcuts file files upload uploads download downloads batch "
                     "native mobile desktop notifications notification account accounts login logout "
                     "sign in out password reset search results filters filter sorting sort bulk "
                     "data report reports dashboard settings preferences performance history sync "
                     "billing payment invoice invoices support forum feedback channel thread "
                     "save saved connection connections button buttons retry retries add edit delete "
                     "create status progress view preview private public team workspace access "
                     "control controls rate limit limits user users invite invitation concurrent workers".split())


def privacy_coverage():
    return {"covered": ["email", "phone", "payment_card", "url", "ip", "handle",
                        "discord_id", "secret_token", "latin_name_spans", "street_address_patterns"],
            "uncovered": ["arbitrary_person_names", "unrecognized_address_formats"],
            "unchecked": ["general_local_NER"],
            "policy": "local redaction; unsupported personal context is held for review"}


def _is_year_run(v: str) -> bool:
    """True if v is nothing but 4-digit calendar years (1900-2099) joined by phone-ish separators ,
    e.g. '2020-2026', '2019 2020 2021 2022'. Such a value is a date range/list in prose, not a phone;
    a real number's groups (area 3 / exchange 3 / line 4) are not all 4-digit years, so it is kept."""
    groups = re.findall(r"\d+", v)
    return bool(groups) and all(len(g) == 4 and 1900 <= int(g) <= 2099 for g in groups)


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
    text = _normalize(text or "")  # fold full-width/homoglyph obfuscation before Tier-1 matching

    def bump(k):
        found[k] = found.get(k, 0) + 1

    def local_match(kind, value):
        bump(kind)
        return mint.get(kind, value)

    text = _STREET.sub(lambda m: local_match("ADDRESS", m.group()), text)
    text = _CJK_ADDRESS.sub(lambda m: local_match("ADDRESS", m.group()), text)
    text = _CJK_PERSON.sub(lambda m: local_match("PERSON", m.group()), text)
    text = _NAME_CONTEXT.sub(lambda m: local_match("PERSON", m.group()), text)
    cue_spans = [(match.start(), match.end()) for match in _PERSON_CUES.finditer(text)]

    def named_span(match):
        # Keep the cue visible when title casing joins it to an unsupported
        # value. Redacting the cue alone would hide that value from the hold.
        if any(start < match.end() and match.start() < end for start, end in cue_spans):
            return match.group()
        words = re.findall(r"[a-z]+", match.group().lower())
        if all(word in _PRODUCT_WORDS for word in words):
            return match.group()
        return local_match("PERSON", match.group())

    text = _NAME.sub(named_span, text)
    review_required = False
    generated = [placeholder for kind in ("PERSON", "ADDRESS")
                 for placeholder in mint._by_type.get(kind, {}).values()]
    for cue in _PERSON_CUES.finditer(text):
        tail = re.split(r"[.!?;。！？；\n]", text[cue.end():], maxsplit=1)[0]
        tail = re.sub(r"(?i)^\s*(?:is\b|at\b|in\b|:|=|是|为|為)\s*", "", tail)
        for placeholder in generated:
            tail = tail.replace(placeholder, "")
        if re.search(r"\w", tail):
            review_required = True
            break
    if review_required:
        # The boundary sees an explicit hold marker, never the unchecked text.
        text = "[PRIVACY_REVIEW_REQUIRED]"
        bump("PRIVACY_REVIEW")

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
        # date/year-safe: an ISO date or a pure year range/list is never a phone (see _ISO_DATE /
        # _is_year_run), skip so a date header / '2020-2026' is not flagged by redact()/has_pii().
        if _ISO_DATE.fullmatch(v) or _is_year_run(v):
            return v
        if len(re.sub(r"\D", "", v)) >= 8:
            bump("PHONE"); return mint.get("PHONE", re.sub(r"\D", "", v))
        return v
    text = _PHONE.sub(sub_phone, text)

    def sub_ipv6(m):
        v = m.group(0)
        # require a real "::" or the full 8-group form, and at least one hex digit, so a bare
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
    return {"redacted": text, "placeholders": placeholders, "found": found,
            "review_required": review_required}


class PrivacyReviewRequired(ValueError):
    """An unsupported personal-context span was held locally."""


def safe_text(text):
    result = redact(str(text or ""))
    if result["review_required"]:
        raise PrivacyReviewRequired("privacy review required: unsupported person/address text")
    return result["redacted"]


def safe_data(value):
    """Scrub all content-bearing nested fields, including proposed metadata."""
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, list):
        return [safe_data(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"user_id", "author", "author_hash"}:
                if not isinstance(item, (str, int)) or isinstance(item, bool):
                    raise ValueError("invalid author identity type")
                identity = str(item)
                author = identity if re.fullmatch(r"u_[0-9a-f]{16}", identity) else pseudonymize(identity)
                if "author_hash" in result and result["author_hash"] != author:
                    raise ValueError("conflicting author identity")
                result["author_hash"] = author
            elif key in {"observation_id", "source_id"} and isinstance(item, str) and re.fullmatch(
                    r"(?:u_[0-9a-f]{16}|obs_[0-9a-f]{64})", item):
                # Typed product identities survive repeated screening. Their spelling
                # remains subject to ordinary redaction everywhere in free text.
                result[key] = item
            elif key in {"ts", "timestamp", "first_seen", "last_seen"} and isinstance(item, str):
                from lib import parse_ts
                parse_ts(item)
                result[key] = item
            else:
                result[safe_text(str(key))] = safe_data(item)
        return result
    return value


# --------------------------------------------------------------------------- pseudonyms

def _stable_salt() -> bytes | None:
    """Resolve the same configured source for preflight and pseudonymization."""
    value = os.environ.get("DEMAND_MINING_PSEUDONYM_SALT")
    if value is not None:
        if not value.strip():
            raise ValueError("configured pseudonym salt is empty")
        return value.encode("utf-8")
    from lib import find_config_dir
    directory = find_config_dir()
    if directory is None:
        return None
    path = directory / "secrets/pseudonym_hmac_salt"
    if not path.is_file():
        return None
    value = path.read_bytes().strip()
    if not value:
        raise ValueError("configured pseudonym salt is empty")
    return value


def _load_salt() -> bytes:
    """Use configured stable bytes, or an explicit offline-only ephemeral salt."""
    value = _stable_salt()
    if value is not None:
        return value
    if os.environ.get("DEMAND_MINING_DRYRUN") == "1":
        return os.urandom(32)
    raise ValueError("real collection needs a configured stable pseudonym salt")


_EPHEMERAL_SALT = None


def ensure_stable_salt():
    global _EPHEMERAL_SALT
    value = _stable_salt()
    if value is None:
        raise ValueError("real collection needs a configured stable pseudonym salt")
    _EPHEMERAL_SALT = value


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
    """Cheap egress check (DLP): True if any Tier1/Tier2 pattern still matches, used fail-closed
    before anything leaves the machine (push, delegation query). A True here BLOCKS egress."""
    r = redact(text or "")
    return bool(r["found"])


def main() -> int:
    """CLI: stdin {text, user_id?} → {redacted, found, placeholders, author_pseudo?}."""
    data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig", "replace") or "{}")
    out = redact(data.get("text", ""))
    if data.get("user_id"):
        out["author_pseudo"] = pseudonymize(data["user_id"])
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
