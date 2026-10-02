#!/usr/bin/env python3
"""Deterministic Discord collection for demand-mining (the live tap).

Reads the wired product's Discord channels via the bot token (REST history, Message Content Intent
required) and emits a REDACTED corpus the SKILL.md extraction layer turns into demand candidates.
Privacy-first: every message is scrubbed by redact.py and the author id is HMAC-pseudonymized BEFORE
it is written, so raw PII never leaves this step (Architecture: redact-on-ingest, always first).

Config-driven, no args needed for the daily run:
  * channels + token come from the selected companion product (registry.json discord_channels /
    .discord_token_ref, resolved via lib.find_config_dir). No secret is ever printed.
  * default window is the last `--since-hours` (72) of messages, enough for the cross-day dedup to
    RESURFACE/SUPPRESS; `--full` backfills the entire history (one-time).

Usage:
  python pull_discord.py                 # last 72h -> corpus on stdout
  python pull_discord.py --since-hours 48 --out corpus.json
  python pull_discord.py --full          # entire history (backfill)
Bots/webhooks and empty messages are skipped. Inaccessible channels, malformed responses and
incomplete pagination fail the required collection.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta

from lib import find_config_dir, load_config, now_utc, parse_ts
from redact import pseudonymize, redact, safe_text
from data_safety import atomic_bytes, require_private

API = "https://discord.com/api/v10"
_HARD_CAP = 60000  # runaway backstop per channel; real pulls exhaust well before this


def _load_wiring():
    """Return (channels, bot_token). channels = [{'id','name'}]. Raises with an init hint if the
    live tap is not wired (never silently degrades to reading nothing)."""
    d = find_config_dir()
    if not d:
        raise SystemExit("pull_discord: no config dir (set DEMAND_MINING_CONFIG); tap not wired.")
    reg = json.loads((d / "registry.json").read_text(encoding="utf-8-sig"))
    selected = os.environ.get("DEMAND_MINING_PRODUCT") or load_config().get("product_id")
    products = reg.get("products") or []
    prod = next((item for item in products if item.get("slug") == selected), None) if selected else next(iter(products), None)
    if prod is None:
        raise ValueError("collection product is not registered")
    chans = prod.get("discord_channels") or []
    ref = prod.get("discord_token_ref")
    if not chans or not ref:
        raise SystemExit("pull_discord: product has no discord_channels/discord_token_ref; "
                         "wire the live tap first (see registry.json).")
    tok_path = (d / ref) if not os.path.isabs(ref) else __import__("pathlib").Path(ref)
    token = tok_path.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"pull_discord: empty token at {tok_path}; write the bot token there.")
    return chans, token


def _get(cid, token, before=None):
    u = f"{API}/channels/{cid}/messages?limit=100" + (f"&before={before}" if before else "")
    req = urllib.request.Request(u, headers={"Authorization": f"Bot {token}",
                                             "User-Agent": "demand-mining-tap/1.0"})
    last_error = None
    for attempt in range(6):
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + attempt)
                continue
            if e.code in (403, 404):
                raise RuntimeError(f"required collection source unavailable (HTTP {e.code})") from e
            raise
        except (OSError, ValueError) as exc:
            last_error = exc
            time.sleep(1 + attempt)
    raise RuntimeError("collection retries exhausted") from last_error


def pull(channels, token, since_hours=72.0, full=False, source_window=None):
    if not isinstance(channels, list) or not channels:
        raise ValueError("collection requires at least one declared source")
    cutoff = (parse_ts(source_window["start"]) if source_window else
              None if full else (now_utc() - timedelta(hours=float(since_hours))))
    end = parse_ts(source_window["end"]) if source_window else None
    corpus, stats = {}, {}
    for c in channels:
        raw_name, cid = str(c.get("name", c.get("id"))), c["id"]
        name = safe_text(raw_name)
        if name != raw_name or name in corpus:
            name += "-" + pseudonymize(str(cid))[2:10]
        msgs, before, stop = [], None, False
        while len(msgs) < _HARD_CAP and not stop:
            batch = _get(cid, token, before)
            if batch == "FORBIDDEN":
                raise RuntimeError(f"required collection source forbidden: {name}")
            if not isinstance(batch, list):
                raise ValueError(f"collection source returned a non-list: {name}")
            if not batch:
                break
            for m in batch:
                ts = m.get("timestamp") or m.get("ts")
                if not ts:
                    raise ValueError(f"collection source has a missing timestamp: {name}")
                timestamp = parse_ts(ts)
                if end is not None and timestamp >= end:
                    continue
                if cutoff is not None and ts:
                    if timestamp < cutoff:
                        stop = True
                        break
                msgs.append(m)
            cursor = batch[-1]["id"]
            if cursor == before:
                raise ValueError(f"collection pagination cursor did not advance: {name}")
            before = cursor
            if len(batch) < 100:
                break
            time.sleep(0.3)
        if len(msgs) >= _HARD_CAP and not stop:
            raise RuntimeError(f"collection cap reached before completion: {name}")
        clean = []
        for m in msgs:
            a = m.get("author") or {}
            if a.get("bot") or m.get("webhook_id"):
                continue
            body = (m.get("content") or "").strip()
            if not body:
                continue
            if not m.get("id") or not a.get("id"):
                raise ValueError("collection observation lacks message or author identity")
            clean.append({
                "observation_id": pseudonymize("discord-message:" + str(cid) + ":" + str(m["id"])),
                "source_id": pseudonymize("discord-channel:" + str(cid)),
                "author_hash": pseudonymize(str(a.get("id", ""))),
                "text": safe_text(body),
                "ts": m.get("timestamp") or m.get("ts"),
                "reply_to": pseudonymize(str(m["referenced_message"]["id"]))
                    if (m.get("referenced_message") or {}).get("id") else None,
            })
        clean.reverse()  # chronological
        corpus[name] = clean
        stats[name] = {"raw": len(msgs), "human_text": len(clean)}
    return {"stats": stats, "channels": corpus, "collection": {"status": "complete",
            "source_window": source_window, "source_count": len(channels)}}


def main() -> int:
    ap = argparse.ArgumentParser(description="demand-mining live Discord tap (redacted corpus)")
    ap.add_argument("--since-hours", type=float, default=72.0)
    ap.add_argument("--full", action="store_true", help="backfill entire history (ignore window)")
    ap.add_argument("--out", default=None, help="write corpus JSON here (default: stdout)")
    args = ap.parse_args()
    if args.out:
        require_private(args.out)
    channels, token = _load_wiring()
    data = pull(channels, token, since_hours=args.since_hours, full=args.full)
    text = json.dumps(data, ensure_ascii=False)
    if args.out:
        atomic_bytes(args.out, text.encode("utf-8"))
        tot = sum(s.get("human_text", 0) for s in data["stats"].values())
        sys.stderr.write(f"pull_discord: {tot} redacted messages -> {args.out}\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
