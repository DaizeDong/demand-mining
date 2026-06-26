"""Security guard — NO real owner PII may ever live in any tracked file of this PUBLIC template.

demand-mining handles user chat / demand data, so a real phone/email/name accidentally pasted into
a fixture is a privacy leak the moment the repo is published. This guard fails-closed: it scans
every tracked source file for a denylist of the maintainer's REAL identifiers and asserts zero hits.

The denylist needles are stored REVERSED in this file so the guard itself never contains a verbatim
real-PII token (otherwise this very file would trip the scan it performs). `DaizeDong` / `Daize Dong`
are deliberately NOT on the denylist: they are the public MIT/authorship attribution (plugin.json
author, LICENSE, install URLs), which is intended attribution, not a leak.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Stored reversed; de-obfuscated at runtime so this source carries no verbatim real-PII token.
_REVERSED_NEEDLES = [
    "4368603",          # real phone local digits (contiguous)
    "9102gnodzd",       # real email local-part #1
    "365804573ydnas",   # real email local-part #2
    "niWtcejbO",        # real employer
    "nospmeK",          # real address (apartment)
    "sregtuR",          # real affiliation
    "yawatacsiP",       # real city
    "rellefakcoR",      # real street
]
DENYLIST = [n[::-1] for n in _REVERSED_NEEDLES]

# the real phone is also written with separators in places; normalise digit runs before matching it.
_DIGITS = re.compile(r"\D+")


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True,
                             text=True, encoding="utf-8", timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return [REPO_ROOT / line for line in out.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    # fallback: walk the tree (skip vcs / caches / sandbox)
    skip = {".git", ".sie", "__pycache__", ".pytest_cache", ".venv", "venv"}
    return [p for p in REPO_ROOT.rglob("*")
            if p.is_file() and not (set(p.relative_to(REPO_ROOT).parts) & skip)]


def test_no_real_pii_in_repo():
    phone_digits = DENYLIST[0]
    hits: list[str] = []
    for fp in _tracked_files():
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = fp.relative_to(REPO_ROOT).as_posix()
        for needle in DENYLIST[1:]:
            if needle in text:
                hits.append(f"{rel}: contains real-PII token {needle!r}")
        # phone: match across any separators (spaces/dashes/parens) by digit-stripping the line
        for ln, line in enumerate(text.splitlines(), 1):
            if phone_digits in _DIGITS.sub("", line):
                hits.append(f"{rel}:{ln}: contains real phone digits")
    assert not hits, "real owner PII found in tracked files:\n" + "\n".join(hits)
