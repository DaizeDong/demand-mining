"""Security guard -- no real owner PII in any tracked file of this PUBLIC repo.

WHAT THIS FILE USED TO BE, AND WHY THAT MATTERS MORE THAN WHAT IT IS
-------------------------------------------------------------------
Until 2026-07-13 this file WAS the leak it claimed to prevent.

It carried eight of the maintainer's real identifiers -- phone, two email local-parts, employer,
apartment, affiliation, city, street -- stored REVERSED, each with a comment naming exactly what it
was, followed by a one-line decoder (`[n[::-1] for n in _REVERSED_NEEDLES]`). Its own docstring
explained that they were reversed *so that this file would not trip the scan it performs*.

Read that again. The author noticed the values would fail the scanner, and concluded that the
scanner should be evaded -- not that a public repo is no place for the values. The output was not a
guard. It was a labeled, machine-readable dossier with an excuse attached, sitting on public GitHub
for two and a half weeks, and it was strictly worse than the single leaked phone number it had been
written to prevent.

The general lesson, which is now iron law:

    A DENYLIST OF REAL IDENTIFIERS *IS* A PII DOCUMENT.
    Publishing it is not a defense against the leak. It is the leak, concentrated and annotated.

And the deeper one: a denylist is written by whoever leaked, so it can only ever contain what that
person already thought of. It cannot catch the vendor nobody anticipated -- and the 2026-07 audit
found exactly that.

WHAT IT IS NOW
--------------
It delegates to `tools/pii_guard.py`, which is built the other way round:

  * an ALLOWLIST -- anything real-world-shaped OUTSIDE the declared synthetic namespace
    (`*@example.com`, `555-*`, ZIP `10001`) is a finding, including identifiers nobody predicted.
    It needs no private data to work, so it is safe to publish and it works in CI.
  * an OPTIONAL private denylist, read at runtime from `~/.pii-denylist.json` -- OUTSIDE every
    repo, never committed, simply absent in CI and on any other machine. Structure still runs.

There are no needles in this file. There cannot be. That is the whole point.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD = REPO_ROOT / "tools" / "pii_guard.py"


def test_pii_guard_is_vendored() -> None:
    """The guard must exist. A repo that lost it is unguarded and does not know it."""
    assert GUARD.is_file(), (
        "tools/pii_guard.py is missing. Re-vendor it from your pii-guard master install."
    )


def test_no_real_pii_in_tree_or_history() -> None:
    """Tree AND history. Once PII is in a commit, editing the file is not a fix -- the commit is
    still on GitHub. That is how every leak in the 2026-07 audit survived being 'fixed'."""
    p = subprocess.run(
        [sys.executable, str(GUARD), "--tree", "--history"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert p.returncode == 0, "pii_guard found real private data:\n" + (p.stdout or "") + (p.stderr or "")


def test_data_boundary_holds() -> None:
    """No real-run output is git-tracked: this repo ships as an UNINITIALIZED TOOL.

    The scanner above is a backstop, not the primary control. It reads content and looks for things
    that SMELL private -- which is why it stayed green while sibling repos accumulated real stock
    positions and a log of real purchases, written there by the skills themselves on every run. A
    ticker with an entry price has no email in it. There is nothing to smell. Only the boundary
    catches that.
    """
    boundary = REPO_ROOT / "tools" / "data_boundary.py"
    if not boundary.is_file():
        return
    p = subprocess.run(
        [sys.executable, str(boundary)],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert p.returncode == 0, "data_boundary violation:\n" + (p.stdout or "") + (p.stderr or "")
