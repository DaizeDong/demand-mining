#!/usr/bin/env python3
"""Persistent demand pool for the live-tap daemon. A JSONL of distilled demands the community bot
maintains in real time (add / merge-on-recurrence / status) and renders to the admin display channel.

One line per demand:
  {product_id, canonical_key, title, summary, why, recommendation, taxonomy_track, kano, tier, final_score,
   grade, rice, reach, impact_label, independent_source_count, evidence[], status, first_seen,
   last_seen, source, authors[]}

Merge rule (dedup): a new observation whose product_id and canonical_key match an existing demand does NOT create
a row, it bumps last_seen, unions authors (reach = distinct authors), and appends up to N evidence
snippets, then re-scores. A genuinely new subject appends a new row. The daemon redacts message
text before it reaches a model; the pool lives in the PRIVATE companion and does not screen its
rows again on write. Pure file IO, no network.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path

import machine_role
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


def pool_path() -> str:
    from data_safety import check_destination, data_root
    return check_destination(os.path.join(str(data_root()), "demands.jsonl"))["path"]


@contextlib.contextmanager
def _file_lock(path: str):
    from data_safety import file_lock
    with file_lock(path + ".lock"):
        yield


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
                out.append(value)
    return out


def _product_id(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("live pool needs an explicit product identity")
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
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
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
    machine_role.refuse_on_secondary()  # the pool is rewritten whole, so only the primary writes it
    product = _product_id(demand.get("product_id"))
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


def _commit_pool(root, rel: str, message: str) -> None:
    from data_safety import git
    if git(root, "diff", "--quiet", "HEAD", "--", rel, allowed=(0, 1)).returncode == 1:
        git(root, "commit", "--only", "-m", message, "--", rel)


_ABSENT = object()


def _rows_at(root, rev: str, rel: str) -> list:
    from data_safety import git
    shown = git(root, "show", f"{rev}:{rel}", allowed=(0, 128))
    if shown.returncode != 0:
        return []
    return [json.loads(line) for line in shown.stdout.splitlines() if line.strip()]


def _merge_rows(base: list, ours: list, theirs: list) -> list:
    """Three-way merge keyed by (product_id, canonical_key), field by field.

    A field the other machine changed and this one did not takes the other machine's value; a field
    both changed keeps this machine's value. Rows only the other machine has are appended."""
    key = lambda r: (r.get("product_id"), r.get("canonical_key"))
    base_by = {key(r): r for r in base}
    theirs_by = {key(r): r for r in theirs}
    out, seen = [], set()
    for row in ours:
        k = key(row)
        seen.add(k)
        other = theirs_by.get(k)
        if other is None or other == row:
            out.append(row)
            continue
        old = base_by.get(k, {})
        merged = dict(row)
        for field in set(other) | set(old):
            theirs_value, base_value = other.get(field, _ABSENT), old.get(field, _ABSENT)
            if theirs_value != base_value and row.get(field, _ABSENT) == base_value:
                if theirs_value is _ABSENT:
                    merged.pop(field, None)
                else:
                    merged[field] = theirs_value
        out.append(merged)
    out.extend(r for r in theirs if key(r) not in seen and key(r) not in base_by)
    return out


def _sync_pool(root, rel: str, path: str, change=None, message: str = "data: merge demand companion origin"):
    """Bring the fetched origin (FETCH_HEAD) into the companion and apply `change(rows)` on top.

    Caller holds _LOCK and the pool file lock and has just fetched. The pool file is merged row by row
    (never text-level), so neither machine's rows are dropped; any other file that conflicts aborts
    the merge and raises. Returns change's result (None without a change)."""
    from data_safety import git

    def ancestor(a, b):
        return git(root, "merge-base", "--is-ancestor", a, b, allowed=(0, 1)).returncode == 0

    merging = False
    if not ancestor("FETCH_HEAD", "HEAD"):
        # Rows this machine's daemon wrote but nobody committed yet go in first, so the merge sees them.
        _commit_pool(root, rel, "data: snapshot demand pool before merging origin")
        if ancestor("HEAD", "FETCH_HEAD"):
            git(root, "merge", "--ff-only", "FETCH_HEAD")
        else:
            base = git(root, "merge-base", "HEAD", "FETCH_HEAD").stdout.strip()
            rows = _merge_rows(_rows_at(root, base, rel), _rows_at(root, "HEAD", rel),
                               _rows_at(root, "FETCH_HEAD", rel))
            code = git(root, "merge", "--no-ff", "--no-commit", "FETCH_HEAD", allowed=(0, 1, 2, 128)).returncode
            conflicted = set(git(root, "diff", "--name-only", "--diff-filter=U").stdout.split())
            if code not in (0, 1) or conflicted - {rel}:
                git(root, "merge", "--abort", allowed=(0, 128))
                raise RuntimeError(f"could not merge origin into the demand companion {root}; resolve it by hand")
            merging = True
    if not merging:
        rows = _read_rows(path)
    result = change(rows) if change else None
    if merging or result:
        _atomic_write(path, rows)
    if merging:
        git(root, "add", "--", rel)
        git(root, "commit", "--no-edit", "-m", message)
    elif result:
        _commit_pool(root, rel, message)
    return result


def set_status(path: str, canonical_key: str, status: str, *, product_id=None) -> bool:
    """Owner-maintained status, allowed on either machine: change the row under the pool lock and
    commit only the pool file locally. Nothing is fetched or pushed here; the nightly companion sync
    rebases and pushes it, and the primary's EOD backup merges origin row by row on a rejected push."""
    from data_safety import check_destination, file_lock
    product = _product_id(product_id)
    if status not in _STATUSES:
        raise ValueError(f"bad status {status}")
    admission = check_destination(path)
    root = Path(admission["root"])
    rel = Path(admission["path"]).relative_to(root).as_posix()
    with file_lock(root / ".demand-backup.lock", timeout=300), _LOCK, _file_lock(path):
        rows = _read_rows(path)
        row = next((r for r in rows
                    if r.get("product_id") == product and r.get("canonical_key") == canonical_key), None)
        if row is None:
            return False
        row["status"] = status
        row["last_seen"] = iso(now_utc())
        _atomic_write(path, rows)
        _commit_pool(root, rel, f"data: set demand status to {status}")
    return True


def sync_origin(root) -> None:
    """Merge the companion's origin in (row by row for the pool). Caller holds the backup lock."""
    from data_safety import git
    path = pool_path()
    rel = Path(path).relative_to(Path(root)).as_posix()
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    git(root, "fetch", "origin", branch)
    with _LOCK, _file_lock(path):
        _sync_pool(root, rel, path)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Set the status of one demand in the live pool.")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("set-status", help="change one demand's status and commit it in the companion (no push)")
    cmd.add_argument("--product-id", required=True)
    cmd.add_argument("--key", required=True, help="canonical_key of the demand")
    cmd.add_argument("--status", required=True, choices=_STATUSES)
    cmd.add_argument("--pool", help="pool file (default: <companion>/pool/demands.jsonl)")
    args = parser.parse_args(argv)
    found = set_status(args.pool or pool_path(), args.key, args.status, product_id=args.product_id)
    print(json.dumps({"updated": found, "key": args.key, "status": args.status}))
    return 0 if found else 1


def ranked(path: str, exclude_status=("shipped", "wontfix", "duplicate"), *, product_id=None) -> list:
    rows = [r for r in load(path, product_id=product_id) if r.get("status") not in exclude_status]
    return sorted(rows, key=lambda r: -float(r.get("final_score", 0) or 0))


if __name__ == "__main__":
    raise SystemExit(main())
