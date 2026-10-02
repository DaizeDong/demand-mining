#!/usr/bin/env python3
"""Persistent demand pool for the live-tap daemon. A JSONL of distilled demands the community bot
maintains in real time (add / merge-on-recurrence / status) and renders to the admin display channel.

One line per demand:
  {product_id, canonical_key, title, summary, why, recommendation, taxonomy_track, kano, tier, final_score,
   grade, rice, reach, impact_label, independent_source_count, evidence[], status, first_seen,
   last_seen, source, authors[]}

Merge rule (dedup): a new observation whose product_id and canonical_key match an existing demand does NOT create
a row, it bumps last_seen, unions authors (reach = distinct authors), and appends up to N evidence
snippets, then re-scores. A genuinely new subject appends a new row. Stores ONLY redacted/distilled
data (the daemon redacts before it ever calls this). Pure file IO, no network.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time

from lib import iso, now_utc, merge_observations

try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None
try:
    import fcntl  # POSIX
except ImportError:
    fcntl = None

_LOCK = threading.Lock()
_MAX_EVIDENCE = 8
_STATUSES = ("new", "ack", "planned", "shipped", "wontfix", "duplicate")


def pool_path(config_dir) -> str:
    from data_safety import require_private
    override = os.environ.get("DEMAND_MINING_DATA_DIR")
    if override is not None and not override.strip():
        raise ValueError("DEMAND_MINING_DATA_DIR is empty")
    base = override if override is not None else os.path.join(str(config_dir), "pool")
    return require_private(os.path.join(base, "demands.jsonl"))["path"]


@contextlib.contextmanager
def _file_lock(path: str):
    from data_safety import file_lock
    with file_lock(path + ".lock"):
        yield


def _normalize_stored_unknown_authors(row):
    """Adapt identified legacy observations without deriving new history."""
    for field in ("evidence", "observation_index"):
        items = row.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or "author_hash" not in item:
                continue
            identity = item.get("observation_id")
            if (item["author_hash"] is None
                    and "author" not in item and "user_id" not in item
                    and isinstance(identity, str)
                    and re.fullmatch(r"(?:u_[0-9a-f]{16}|obs_[0-9a-f]{64})", identity)):
                del item["author_hash"]


def _read_rows(path: str) -> list:
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"pool contains a non-object record: {path}")
                _normalize_stored_unknown_authors(value)
                out.append(value)
    return out


def _product_id(value):
    from redact import safe_data
    if not isinstance(value, str) or not value.strip():
        raise ValueError("live pool needs an explicit product identity")
    if safe_data(value) != value:
        raise ValueError("product identity cannot change during privacy screening")
    return value


def load(path: str, *, product_id=None) -> list:
    """Return only explicitly attributed rows for the selected product."""
    product = _product_id(product_id)
    return [row for row in _read_rows(path) if row.get("product_id") == product]


def _check_binding(row, product, canonical_key):
    if not isinstance(row, dict) or row.get("product_id") != product or row.get("canonical_key") != canonical_key:
        raise ValueError("rescore changed the live demand identity")
    return row


def _atomic_write(path: str, rows: list) -> None:
    from data_safety import atomic_bytes
    from redact import safe_data
    content = "".join(json.dumps(safe_data(row), ensure_ascii=False) + "\n" for row in rows)
    atomic_bytes(path, content.encode("utf-8"))


def _distinct_authors(rows_authors) -> list:
    seen, out = set(), []
    for a in rows_authors:
        h = a.get("author_hash") if isinstance(a, dict) else a
        if h and h not in seen:
            seen.add(h)
            out.append({"author_hash": h} if not isinstance(a, dict) else a)
    return out


def upsert(path: str, demand: dict, rescore=None) -> tuple[str, dict]:
    """Insert or merge a demand by product_id and canonical_key. Returns (action, row)
    where action is 'new' or 'merged'. `rescore(row)` (optional) recomputes final_score/grade/tier/
    reach after the merge. Thread + process safe via a lock + atomic replace."""
    from redact import safe_data
    product = _product_id(demand.get("product_id"))
    demand = safe_data(demand)
    if demand.get("product_id") != product:
        raise ValueError("product identity cannot change during privacy screening")
    ck = demand.get("canonical_key")
    if not isinstance(ck, str) or not ck:
        raise ValueError("pool demand requires canonical_key")
    now = iso(now_utc())
    with _LOCK, _file_lock(path):
        rows = _read_rows(path)
        idx = next((i for i, r in enumerate(rows)
                    if r.get("product_id") == product and r.get("canonical_key") == ck), None)
        if idx is None:
            row = dict(demand)
            row.setdefault("status", "new")
            row.setdefault("first_seen", now)
            row["last_seen"] = now
            row.update(merge_observations(demand, evidence_cap=_MAX_EVIDENCE))
            if rescore:
                row = rescore(row)
            _check_binding(row, product, ck)
            rows.append(row)
            _atomic_write(path, rows)
            return "new", row
        row = rows[idx]
        row["last_seen"] = now
        row.update(merge_observations(row, demand, evidence_cap=_MAX_EVIDENCE))
        # keep the richer title/summary if the incoming one is longer/nonempty
        for f in ("summary", "why", "recommendation", "taxonomy_track", "kano"):
            if demand.get(f) and not row.get(f):
                row[f] = demand[f]
        if rescore:
            row = rescore(row)
        _check_binding(row, product, ck)
        rows[idx] = row
        _atomic_write(path, rows)
        return "merged", row


def set_status(path: str, canonical_key: str, status: str, *, product_id=None) -> bool:
    product = _product_id(product_id)
    if status not in _STATUSES:
        raise ValueError(f"bad status {status}")
    with _LOCK, _file_lock(path):
        rows = _read_rows(path)
        for r in rows:
            if r.get("product_id") == product and r.get("canonical_key") == canonical_key:
                r["status"] = status
                r["last_seen"] = iso(now_utc())
                _atomic_write(path, rows)
                return True
    return False


def ranked(path: str, exclude_status=("shipped", "wontfix", "duplicate"), *, product_id=None) -> list:
    rows = [r for r in load(path, product_id=product_id) if r.get("status") not in exclude_status]
    return sorted(rows, key=lambda r: -float(r.get("final_score", 0) or 0))


def attribute_legacy(path: str, canonical_key: str, *, product_id=None) -> bool:
    """Explicitly attribute one reviewed legacy row; refuse ambiguous or conflicting history."""
    product = _product_id(product_id)
    with _LOCK, _file_lock(path):
        rows = _read_rows(path)
        legacy = [row for row in rows if row.get("canonical_key") == canonical_key
                  and row.get("product_id") in (None, "")]
        if not legacy:
            return False
        if len(legacy) != 1 or any(row.get("canonical_key") == canonical_key
                                  and row.get("product_id") == product for row in rows):
            raise ValueError("legacy attribution conflicts with existing demand history")
        legacy[0]["product_id"] = product
        _atomic_write(path, rows)
        return True
