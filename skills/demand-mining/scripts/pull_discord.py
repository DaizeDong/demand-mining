#!/usr/bin/env python3
"""Deterministic Discord collection for demand-mining (the live tap).

Reads the wired product's Discord channels via the bot token (REST history, Message Content Intent
required) and emits a REDACTED corpus the SKILL.md extraction layer turns into demand candidates.
Privacy-first: every message is scrubbed by redact.py and the author id is HMAC-pseudonymized BEFORE
it is written, so raw PII never leaves this step (Architecture: redact-on-ingest, always first).

Config-driven, no args needed for the daily run:
  * channels + token come from the companion config (registry.json product[0].discord_channels /
    .discord_token_ref, resolved via lib.find_config_dir). No secret is ever printed.
  * default window is the last `--since-hours` (72) of messages, enough for the cross-day dedup to
    RESURFACE/SUPPRESS; `--full` backfills the entire history (one-time).

Usage:
  python pull_discord.py                 # last 72h -> corpus on stdout
  python pull_discord.py --since-hours 48 --out corpus.json
  python pull_discord.py --full          # entire history (backfill)
Bots/webhooks and empty messages are skipped (not demand signal). 403/404 channels are skipped with
a note (a channel the bot was not granted read access to), never a hard failure.
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

from lib import find_config_dir, now_utc, parse_ts
from redact import pseudonymize, redact

API = "https://discord.com/api/v10"
_HARD_CAP = 60000  # runaway backstop per channel; real pulls exhaust well before this


def _load_wiring():
    """Return (channels, bot_token). channels = [{'id','name'}]. Raises with an init hint if the
    live tap is not wired (never silently degrades to reading nothing)."""
    d = find_config_dir()
    if not d:
        raise SystemExit("pull_discord: no config dir (set DEMAND_MINING_CONFIG); tap not wired.")
    reg = json.loads((d / "registry.json").read_text(encoding="utf-8-sig"))
    prod = (reg.get("products") or [{}])[0]
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
    for attempt in range(6):
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + attempt)
                continue
            if e.code in (403, 404):
                return "FORBIDDEN"
            raise
        except Exception:
            time.sleep(1 + attempt)
    return []


def pull(channels, token, since_hours=72.0, full=False):
    cutoff = None if full else (now_utc() - timedelta(hours=float(since_hours)))
    corpus, stats = {}, {}
    for c in channels:
        name, cid = c.get("name", c.get("id")), c["id"]
        msgs, before, stop = [], None, False
        while len(msgs) < _HARD_CAP and not stop:
            batch = _get(cid, token, before)
            if batch == "FORBIDDEN":
                stats[name] = {"forbidden": True}
                break
            if not batch:
                break
            for m in batch:
                ts = m.get("timestamp") or m.get("ts")
                if cutoff is not None and ts:
                    try:
                        if parse_ts(ts) < cutoff:
                            stop = True
                            break
                    except Exception:
                        pass
                msgs.append(m)
            before = batch[-1]["id"]
            if len(batch) < 100:
                break
            time.sleep(0.3)
        if stats.get(name, {}).get("forbidden"):
            continue
        clean = []
        for m in msgs:
            a = m.get("author") or {}
            if a.get("bot") or m.get("webhook_id"):
                continue
            body = (m.get("content") or "").strip()
            if not body:
                continue
            clean.append({
                "author": pseudonymize(str(a.get("id", ""))),
                "text": redact(body)["redacted"],
                "ts": m.get("timestamp") or m.get("ts"),
                "reply_to": (m.get("referenced_message") or {}).get("id"),
            })
        clean.reverse()  # chronological
        corpus[name] = clean
        stats[name] = {"raw": len(msgs), "human_text": len(clean)}
    return {"stats": stats, "channels": corpus}


def main() -> int:
    ap = argparse.ArgumentParser(description="demand-mining live Discord tap (redacted corpus)")
    ap.add_argument("--since-hours", type=float, default=72.0)
    ap.add_argument("--full", action="store_true", help="backfill entire history (ignore window)")
    ap.add_argument("--out", default=None, help="write corpus JSON here (default: stdout)")
    args = ap.parse_args()
    channels, token = _load_wiring()
    data = pull(channels, token, since_hours=args.since_hours, full=args.full)
    text = json.dumps(data, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        tot = sum(s.get("human_text", 0) for s in data["stats"].values())
        sys.stderr.write(f"pull_discord: {tot} redacted messages -> {args.out}\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
