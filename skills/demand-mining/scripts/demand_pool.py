#!/usr/bin/env python3
"""Persistent demand pool for the live-tap daemon. A JSONL of distilled demands the community bot
maintains in real time (add / merge-on-recurrence / status) and renders to the admin display channel.

One line per demand:
  {canonical_key, title, summary, why, recommendation, taxonomy_track, kano, tier, final_score,
   grade, rice, reach, impact_label, independent_source_count, evidence[], status, first_seen,
   last_seen, source, authors[]}

Merge rule (dedup): a new observation whose canonical_key matches an existing demand does NOT create
a row, it bumps last_seen, unions authors (reach = distinct authors), and appends up to N evidence
snippets, then re-scores. A genuinely new subject appends a new row. Stores ONLY redacted/distilled
data (the daemon redacts before it ever calls this). Pure file IO, no network.
"""
from __future__ import annotations

import json
import os
import threading

from lib import iso, now_utc

_LOCK = threading.Lock()
_MAX_EVIDENCE = 8
_STATUSES = ("new", "ack", "planned", "shipped", "wontfix", "duplicate")


def pool_path(config_dir) -> str:
    return os.path.join(str(config_dir), "pool", "demands.jsonl")


def load(path: str) -> list:
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def _atomic_write(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _distinct_authors(rows_authors) -> list:
    seen, out = set(), []
    for a in rows_authors:
        h = a.get("author_hash") if isinstance(a, dict) else a
        if h and h not in seen:
            seen.add(h)
            out.append({"author_hash": h} if not isinstance(a, dict) else a)
    return out


def upsert(path: str, demand: dict, rescore=None) -> tuple[str, dict]:
    """Insert a new demand or merge into an existing one by canonical_key. Returns (action, row)
    where action is 'new' or 'merged'. `rescore(row)` (optional) recomputes final_score/grade/tier/
    reach after the merge. Thread + process safe via a lock + atomic replace."""
    ck = demand.get("canonical_key")
    now = iso(now_utc())
    with _LOCK:
        rows = load(path)
        idx = next((i for i, r in enumerate(rows) if r.get("canonical_key") == ck), None)
        if idx is None:
            row = dict(demand)
            row.setdefault("status", "new")
            row.setdefault("first_seen", now)
            row["last_seen"] = now
            row["authors"] = _distinct_authors(demand.get("authors", []))
            row["reach"] = max(int(demand.get("reach", 0) or 0), len(row["authors"]))
            if rescore:
                row = rescore(row)
            rows.append(row)
            _atomic_write(path, rows)
            return "new", row
        row = rows[idx]
        row["last_seen"] = now
        row["authors"] = _distinct_authors((row.get("authors") or []) + (demand.get("authors") or []))
        row["reach"] = max(int(row.get("reach", 0) or 0), len(row["authors"]))
        ev = (row.get("evidence") or []) + (demand.get("evidence") or [])
        seen, dedup = set(), []
        for e in ev:
            k = (e.get("redacted_snippet") or "")[:120]
            if k and k not in seen:
                seen.add(k)
                dedup.append(e)
        row["evidence"] = dedup[:_MAX_EVIDENCE]
        # keep the richer title/summary if the incoming one is longer/nonempty
        for f in ("summary", "why", "recommendation", "taxonomy_track", "kano"):
            if demand.get(f) and not row.get(f):
                row[f] = demand[f]
        if rescore:
            row = rescore(row)
        rows[idx] = row
        _atomic_write(path, rows)
        return "merged", row


def set_status(path: str, canonical_key: str, status: str) -> bool:
    if status not in _STATUSES:
        raise ValueError(f"bad status {status}")
    with _LOCK:
        rows = load(path)
        for r in rows:
            if r.get("canonical_key") == canonical_key:
                r["status"] = status
                r["last_seen"] = iso(now_utc())
                _atomic_write(path, rows)
                return True
    return False


def ranked(path: str, exclude_status=("shipped", "wontfix", "duplicate")) -> list:
    rows = [r for r in load(path) if r.get("status") not in exclude_status]
    return sorted(rows, key=lambda r: -float(r.get("final_score", 0) or 0))
