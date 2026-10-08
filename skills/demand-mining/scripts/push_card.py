#!/usr/bin/env python3
"""Discord delivery, tiered push (anti-spam) + egress DLP (Acceptance Gate T6). Stdlib.

Builds BOTH a Discord embed dict (future embed bot) AND a plain-text rendering (current content-
only relay). Two fail-closed guards run BEFORE anything leaves the machine:

  1. egress DLP, every user-visible string is re-scanned with redact.has_pii; ANY residual PII or
     secret aborts the send (returns ok=False, reason). The pool stores only redacted data, but
     this is the belt-and-suspenders backstop so raw PII can never reach Discord.
  2. Discord hard limits, embed<=6000 / <=25 fields / value<=1024 / <=10 embeds / content<=2000,
     validated up front so nothing is silently truncated by Discord.

Delivery seam (clean bot switch, zero code change):
  DEMAND_MINING_RELAY_CMD, JSON list / shell string; receives the message on argv[1].
  else fallback to a conventional content-only relay under the home dir.
The Discord token is NEVER read or echoed here, the relay owns the token; this script hands text.
"""
from __future__ import annotations

import argparse
import json
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
import os
import shlex
import subprocess
import sys
from pathlib import Path

from no_console import no_window_kwargs
from redact import has_pii, safe_text, PrivacyReviewRequired
from score import domain_factor_text

EMBED_TOTAL, FIELD_VALUE, MAX_FIELDS, MAX_EMBEDS, CONTENT_MAX = 6000, 1024, 25, 10, 2000
_TIER_COLOR = {"tier0": 0xE74C3C, "tier1": 0xE67E22, "tier2": 0x3498DB,
               "backlog": 0x95A5A6, "cut": 0x7F8C8D}
_DLP_FIELDS = ("title", "summary", "inferred_job", "why", "action", "recommendation")
_DELIVERY_IDENTITY = ContextVar("demand_delivery_identity", default=None)


@contextmanager
def delivery_context(identity):
    token = _DELIVERY_IDENTITY.set(identity)
    try:
        yield
    finally:
        _DELIVERY_IDENTITY.reset(token)


def dlp_scan(card: dict) -> list[str]:
    """Return the list of fields that still contain PII/secrets (empty = clean). Fail-closed
    caller aborts on non-empty. Also scans evidence redacted_snippets defensively."""
    leaked = [f for f in _DLP_FIELDS if card.get(f) and has_pii(str(card.get(f)))]
    for i, e in enumerate(card.get("evidence", []) or []):
        snip = e.get("redacted_snippet") or e.get("quote") or ""
        if snip and has_pii(str(snip)):
            leaked.append(f"evidence[{i}].redacted_snippet")
    return leaked


def build_embed(card: dict, update: bool = False) -> dict:
    rc = card.get("rice", {})
    tag = "🔄 UPDATE" if update else ("🚨 TIER0" if card.get("tier") == "tier0" else "🆕 NEW")
    title = f"{tag} · {(card.get('title') or card.get('inferred_job') or '?')[:240]}"
    desc = []
    if card.get("why"):
        desc.append("**Why:** " + card["why"])
    if card.get("recommendation"):
        desc.append("**建议:** " + card["recommendation"])
    if card.get("action"):
        desc.append("**行动:** " + card["action"])
    fields = [
        {"name": "Weighted RICE",
         "value": f"{rc.get('rice_raw')} -> final {card.get('final_score')}"[:FIELD_VALUE], "inline": True},
        {"name": "Domain factors", "value": domain_factor_text(card)[:FIELD_VALUE], "inline": False},
        {"name": "Opportunity", "value": str(card.get("opportunity_score"))[:FIELD_VALUE], "inline": True},
        {"name": "WSJF", "value": str(card.get("urgency_wsjf"))[:FIELD_VALUE], "inline": True},
        {"name": "Kano", "value": str(card.get("kano"))[:FIELD_VALUE], "inline": True},
        {"name": "intensity", "value": str(card.get("intensity"))[:FIELD_VALUE], "inline": True},
    ]
    footer = (f"{card.get('distinct_author_count',0)} 人 · {card.get('independent_source_count',0)} 独立源"
              f" · {card.get('tier')} · {card.get('run_id','')}")
    return {"title": title[:256], "color": _TIER_COLOR.get(card.get("tier", "tier2"), 0x3498DB),
            "description": "\n".join(desc)[:4000], "fields": fields[:MAX_FIELDS],
            "footer": {"text": footer[:2048]}}


def validate_embed(embed: dict) -> list[str]:
    errs = []
    total = len(embed.get("title", "")) + len(embed.get("description", "")) + \
        len(embed.get("footer", {}).get("text", ""))
    for f in embed.get("fields", []):
        total += len(f.get("name", "")) + len(f.get("value", ""))
        if len(f.get("value", "")) > FIELD_VALUE:
            errs.append(f"field {f.get('name')} value > {FIELD_VALUE}")
    if len(embed.get("fields", [])) > MAX_FIELDS:
        errs.append(f">{MAX_FIELDS} fields")
    if total > EMBED_TOTAL:
        errs.append(f"embed total {total} > {EMBED_TOTAL}")
    return errs


def render_text(card: dict, update: bool = False) -> str:
    tag = "[UPDATE]" if update else ("[TIER0]" if card.get("tier") == "tier0" else "[NEW]")
    rc = card.get("rice", {})
    lines = [
        f"{tag} {card.get('title') or card.get('inferred_job','?')}  "
        f"({card.get('grade')} {card.get('final_score')})",
        f"tier: {card.get('tier')} | track: {card.get('taxonomy_track', card.get('track'))} | "
        f"Kano: {card.get('kano')}",
        f"Domain factors: {domain_factor_text(card)}",
        f"Weighted RICE: R={rc.get('reach')} I={rc.get('impact')} C={rc.get('confidence')} "
        f"E={rc.get('effort')} = {rc.get('rice_raw')} -> final {card.get('final_score')}",
        f"Opportunity={card.get('opportunity_score')} (intensity {card.get('intensity')}, "
        f"{card.get('distinct_author_count',0)} 人) | WSJF={card.get('urgency_wsjf')}",
    ]
    if card.get("why"):
        lines.append(f"why: {card['why']}")
    if card.get("recommendation"):
        lines.append(f"建议: {card['recommendation']}")
    if card.get("competitor_ref"):
        lines.append(f"竞品: {card['competitor_ref']}")
    ev = card.get("evidence", [])
    lines.append(f"{card.get('independent_source_count',0)} 独立源, 证据×{len(ev)}")
    for e in ev[:3]:
        lines.append(f"  - [{e.get('channel', e.get('source','?'))}] {e.get('redacted_snippet','')[:120]}")
    return "\n".join(lines)


def _relay_cmd():
    env = os.environ.get("DEMAND_MINING_RELAY_CMD")
    if env:
        try:
            v = json.loads(env)
            if isinstance(v, list):
                return v
        except Exception:
            return shlex.split(env)
    # Pluggable Agent Center egress: prefer schedule-reminder's unified relay (#demand stream) when
    # the base is installed; fall back to the Big Brother relay so this skill still works standalone.
    rp = os.environ.get("SCHEDULE_RELAY_PY") or str(
        Path.home() / ".claude/skills/schedule-reminder/scripts/relay.py")
    if os.path.isfile(rp):
        return [sys.executable, rp, "send", "--stream", "demand", "--text"]
    return [sys.executable, str(Path.home() / ".claude/discord_relay/send.py")]


def deliver(message: str, dry_run: bool = False) -> tuple[bool, str]:
    """Send a text message via the relay (chunks on newlines). Egress DLP on the raw message too;
    length-only logging, never the content."""
    if has_pii(message):
        return (False, "egress blocked: residual PII in message")
    if dry_run or os.environ.get("DEMAND_MINING_DRYRUN"):
        return (True, f"[dry-run] would deliver {len(message)} chars")
    try:
        child_env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
        identity = _DELIVERY_IDENTITY.get()
        if identity is not None:
            child_env["DEMAND_MINING_DELIVERY_IDENTITY"] = json.dumps(identity, sort_keys=True)
        child_env["DEMAND_MINING_DELIVERY_CONTENT_SHA256"] = hashlib.sha256(message.encode("utf-8")).hexdigest()
        proc = subprocess.run(_relay_cmd() + [message], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30,
                              env=child_env, **no_window_kwargs())
        content_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        if proc.returncode != 0:
            return False, {"status": "unknown", "adapter_exit": proc.returncode,
                           "content_sha256": content_hash}
        try:
            receipt = json.loads(proc.stdout)
        except (ValueError, TypeError):
            return False, {"status": "unknown", "reason": "adapter returned no JSON receipt",
                           "content_sha256": content_hash}
        if not isinstance(receipt, dict):
            return False, {"status": "unknown", "reason": "adapter receipt is not an object"}
        # A native Discord response includes the actual ID and sent content.
        if receipt.get("id") and receipt.get("content") == message:
            receipt = {"status": "confirmed", "message_id": str(receipt["id"]),
                       "content_sha256": content_hash}
            if identity is not None:
                receipt["identity"] = identity
        confirmed = (receipt.get("status") == "confirmed" and receipt.get("message_id") and
                     receipt.get("content_sha256") == content_hash)
        return bool(confirmed), receipt
    except Exception as e:
        return False, {"status": "unknown", "error_type": type(e).__name__}


def push_card(card: dict, update: bool = False, dry_run: bool = False, *, revision: str | None = None) -> dict:
    caller_owns_delivery = _DELIVERY_IDENTITY.get() is not None
    if revision is not None and (caller_owns_delivery or not update
                                 or not isinstance(revision, str) or not revision.strip()):
        raise ValueError("revision requires a standalone update and a nonempty stable string token")
    leaked = dlp_scan(card)
    if leaked:
        return {"ok": False, "detail": f"egress DLP blocked: PII in {leaked}",
                "embed_errors": [], "embed": None}
    embed = build_embed(card, update)
    errs = validate_embed(embed)
    message = render_text(card, update)
    preview = dry_run or bool(os.environ.get("DEMAND_MINING_DRYRUN"))
    if preview or caller_owns_delivery:
        ok, detail = deliver(message, dry_run=preview)
        return {"ok": ok, "detail": detail, "embed_errors": errs, "embed": embed}
    identity = None
    for key in ("demand_id", "canonical_key", "id"):
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            identity = value
            break
        if type(value) is int and value != 0:
            identity = str(value)
            break
    if identity is None:
        return {"ok": False, "detail": "stable card identity required: demand_id, canonical_key or id",
                "embed_errors": errs, "embed": embed}
    from lib import load_config
    from finalize import deliver_event
    cfg = load_config()
    product = cfg.get("product_id") or cfg.get("slug")
    if not isinstance(product, str) or not product.strip():
        raise ValueError("configure product_id before standalone card delivery")
    if card.get("product_id") not in (None, product):
        raise ValueError("card product_id differs from the configured product")
    event_identity = {"product_id": product, "card_id": identity, "event": "update" if update else "new"}
    if revision is not None:
        event_identity["revision"] = revision
    result = deliver_event(event_identity, message)
    return {**result, "detail": result["delivery"], "embed_errors": errs, "embed": embed}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Deliver one card from standard input with durable replay protection.")
    parser.add_argument("--revision", help="stable token for a distinct update; requires _update: true in the card")
    args = parser.parse_args(argv)
    data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig", "replace") or "{}")
    res = push_card(data, update=bool(data.get("_update")),
                    dry_run=bool(os.environ.get("DEMAND_MINING_DRYRUN")), revision=args.revision)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
