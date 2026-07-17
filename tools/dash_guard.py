#!/usr/bin/env python3
"""dash_guard: flag / fix en-dash and em-dash used as prose in a public repo.

House rule (user global): published prose carries NO en/em dash. The ASCII hyphen `-` is left
ALONE (it is code syntax: identifiers, flags, file names, versions, URLs, ranges in code), so this
guard only touches the en-dash U+2013, em-dash U+2014 and horizontal bar U+2015. None of those
three ever appear in code SYNTAX, so every occurrence outside a code span is prose and is a target.

Modes (exactly one action):
  --check  (default) print every offending file:line; exit 1 if any (pre-commit / CI gate)
  --fix              rewrite the offending files in place

Target set:
  --staged           the git staged text blobs (pre-commit hook)
  --tree   (default) every git-tracked text file
  paths...           explicit files (overrides the set)

Markdown safety: fenced ``` code blocks and inline `code` spans are skipped, so a dash shown as a
literal example survives. In every other text file each en/em dash is treated as prose.

Replacement (deterministic):
  spaced   ` — ` / ` – `                 -> ", "   (appositive / aside; never grammatically wrong)
  ASCII range  A–B  (word char both sides) -> "A to B"  (e.g. T1–T9, 2020–2026)
  any leftover run  —— / – / ―           -> ","
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_DASHES = "–—―"          # – — ―
_DASH_RE = re.compile(f"[{_DASHES}]")
_TEXT_EXT = {".md", ".markdown", ".py", ".ps1", ".txt", ".json", ".yml", ".yaml", ".toml",
             ".cfg", ".ini", ".js", ".ts", ".sh", ".rst"}

_SPACED = re.compile(rf"\s+[{_DASHES}]+\s+")
_RANGE = re.compile(rf"([A-Za-z0-9])[{_DASHES}]+([A-Za-z0-9])")
_RUN = re.compile(rf"[{_DASHES}]+")


def fix_prose(s: str) -> str:
    """Replace en/em dashes in one prose segment. Order matters: spaced separators first (they
    become ', '), then ASCII ranges ('A to B'), then any leftover dash run collapses to a comma."""
    s = _SPACED.sub(", ", s)
    s = _RANGE.sub(r"\1 to \2", s)
    s = _RUN.sub(",", s)
    return s


def _split_md_code(line: str, in_fence: bool):
    """Yield (segment, is_code) for a markdown line, protecting inline `code`. `in_fence` marks a
    line inside a ``` fenced block (entirely code). Returns (segments, new_in_fence)."""
    stripped = line.lstrip()
    if stripped.startswith("```") or stripped.startswith("~~~"):
        return [(line, True)], (not in_fence)
    if in_fence:
        return [(line, True)], True
    # protect inline code spans (`...`)
    segs, is_code = [], False
    for i, part in enumerate(re.split(r"(`[^`]*`)", line)):
        segs.append((part, part.startswith("`") and part.endswith("`") and len(part) >= 2))
    return segs, False


_ALLOW = "dash-guard: allow"       # a line carrying this marker is left untouched (rare legit dash)


def process_text(text: str, is_md: bool):
    """Return (new_text, hits) where hits = list of (lineno, original_line). Skips code spans in md."""
    out_lines, hits, in_fence = [], [], False
    for lineno, line in enumerate(text.split("\n"), 1):
        if _ALLOW in line:
            out_lines.append(line)
            continue
        if is_md:
            segs, in_fence = _split_md_code(line, in_fence)
            new_parts, changed = [], False
            for part, is_code in segs:
                if is_code:
                    new_parts.append(part)
                else:
                    fixed = fix_prose(part)
                    if fixed != part:
                        changed = True
                    new_parts.append(fixed)
            if changed:
                hits.append((lineno, line))
            out_lines.append("".join(new_parts))
        else:
            fixed = fix_prose(line)
            if fixed != line:
                hits.append((lineno, line))
            out_lines.append(fixed)
    return "\n".join(out_lines), hits


def _git(repo, *a):
    r = subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 else ""


def _tracked(repo):
    return [f for f in _git(repo, "ls-files").splitlines() if os.path.splitext(f)[1].lower() in _TEXT_EXT]


def _staged(repo):
    out = _git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACM")
    return [f for f in out.splitlines() if os.path.splitext(f)[1].lower() in _TEXT_EXT]


def main() -> int:
    ap = argparse.ArgumentParser(description="en/em dash guard for public repo prose")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--fix", action="store_true", help="rewrite offending files in place")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true")
    g.add_argument("--tree", action="store_true")
    ap.add_argument("paths", nargs="*", help="explicit files (overrides --staged/--tree)")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    if args.paths:
        files = args.paths
    elif args.staged:
        files = _staged(repo)
    else:
        files = _tracked(repo)

    total = 0
    changed_files = 0
    _self = {"dash_guard.py", "test_dash_guard.py"}   # the guard's own source carries the dash set
    for rel in files:
        path = rel if os.path.isabs(rel) else os.path.join(repo, rel)
        if not os.path.isfile(path) or os.path.basename(path) in _self:
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        if not _DASH_RE.search(text):
            continue
        is_md = os.path.splitext(path)[1].lower() in (".md", ".markdown", ".rst")
        new_text, hits = process_text(text, is_md)
        if not hits:
            continue
        total += len(hits)
        if args.fix:
            if new_text != text:
                open(path, "w", encoding="utf-8", newline="\n").write(new_text)
                changed_files += 1
                print(f"fixed {len(hits):3} {os.path.relpath(path, repo)}")
        else:
            for lineno, line in hits:
                print(f"{os.path.relpath(path, repo)}:{lineno}: {line.strip()[:100]}")

    if args.fix:
        print(f"dash_guard: fixed {total} line(s) across {changed_files} file(s)")
        return 0
    if total:
        print(f"dash_guard: {total} prose en/em dash(es) found (run with --fix)", file=sys.stderr)
        return 1
    print("dash_guard: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
