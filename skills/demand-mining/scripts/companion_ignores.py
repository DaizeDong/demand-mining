#!/usr/bin/env python3
"""Report declared versioned artifacts that the companion's Git ignore rules hide.

Write admission (guards storage_contract.authorize_artifact_write) refuses any artifact
that is not declared transient when Git ignores its destination. So an ignore rule that
covers a versioned artifact is not a cosmetic backup gap: the producer of that artifact
fails at its first write. That is how DemandMiningDaemon died on every start while the
companion ignored pool/** without re-including pool/logs/ (the supervisor could not
admit its own log). This check finds that class of mismatch before a producer does.

Each declared pattern is probed with representative paths: '*' becomes one segment, '**'
becomes both one segment and a nested file two levels down (an ignore rule can re-include only
the first level), '?' becomes '0' and a character class becomes its first member. Transient and
retired artifacts are skipped (admission ignores the first and refuses the second), and
so are the exemptions below, each with its reason.

usage: companion_ignores.py --companion DIR [--contract storage.contract.json]
Exit 0 when nothing declared is hidden, 1 when something is, 2 when the check could not run.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from no_console import no_window_kwargs

DEFAULT_CONTRACT = Path(__file__).resolve().parents[3] / "storage.contract.json"

# Declared but deliberately ignored, and never written through admission, so ignoring
# them cannot refuse a producer. Anything added here needs the same two facts.
EXEMPT = {
    # Mode B keeps credential values out of the companion history (scripts/init_config.py).
    "credentials": "Mode B credential values; placed by the operator, never through admission",
    # Legacy staging names. data_safety.atomic_bytes now stages under .staging/atomic-*.tmp
    # (declared transient); these names only describe leftovers of older writers.
    "pool_atomic_staging_0": "legacy staging name; atomic_bytes stages under .staging/",
    "pool_atomic_staging_1": "legacy staging name; atomic_bytes stages under .staging/",
}


# What '**' expands to in the probes: one segment, and a file two levels below it. Probing
# only the first level passed a companion that re-included pool/logs/* but not deeper paths.
DEEP_SAMPLES = ("x", "a/b.log")


def _sample_segment(segment: str) -> str:
    segment = re.sub(r"\[!?(.)[^\]]*\]", r"\1", segment)
    return segment.replace("*", "x").replace("?", "0")


def sample_paths(pattern: str) -> list[str]:
    """Concrete paths that the segment glob `pattern` matches, at every probed depth."""
    paths = [""]
    for segment in pattern.split("/"):
        options = DEEP_SAMPLES if segment == "**" else (_sample_segment(segment),)
        paths = [prefix + ("/" if prefix else "") + option for prefix in paths for option in options]
    return paths


def sample_path(pattern: str) -> str:
    """The shallowest concrete path that the segment glob `pattern` matches."""
    return sample_paths(pattern)[0]


def versioned_artifacts(contract: dict) -> list[tuple[str, str]]:
    """(artifact_id, sample path) for every probe of every artifact admission requires to be
    unignored; an artifact whose pattern contains '**' contributes one row per probed depth."""
    rows = []
    for artifact in contract["artifacts"]:
        if artifact.get("persistence", "versioned") == "transient":
            continue
        if artifact["retention_rule"]["class"] == "retired":
            continue
        if artifact["artifact_id"] in EXEMPT:
            continue
        rows.extend((artifact["artifact_id"], path)
                    for path in sample_paths(artifact["path_pattern"]))
    return rows


def ignored_versioned(companion: Path, contract: dict) -> list[tuple[str, str]]:
    """The declared versioned artifacts whose sample path Git ignores in `companion`."""
    rows = versioned_artifacts(contract)
    if not rows:
        raise ValueError("storage contract declares no versioned artifact to check")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    # Bytes and NUL separators: a text-mode pipe on Windows turns "\n" into "\r\n", and Git
    # then checks "path\r", which nothing ignores, so every probe would silently pass.
    result = subprocess.run(
        ["git", "-C", str(companion), "check-ignore", "--no-index", "--stdin", "-z"],
        input=b"".join(path.encode("utf-8") + b"\0" for _, path in rows),
        capture_output=True, env=env, **no_window_kwargs())
    if result.returncode not in (0, 1):
        raise RuntimeError("git check-ignore failed: "
                           + result.stderr.decode("utf-8", "replace").strip())
    hidden = {item.decode("utf-8") for item in result.stdout.split(b"\0") if item}
    return [(artifact_id, path) for artifact_id, path in rows if path in hidden]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--companion", required=True, help="PRIVATE companion worktree root")
    ap.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    args = ap.parse_args(argv)
    try:
        with open(args.contract, encoding="utf-8") as stream:
            contract = json.load(stream)
        hidden = ignored_versioned(Path(args.companion), contract)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"NOT CHECKED: {exc}", file=sys.stderr)
        return 2
    for artifact_id, path in hidden:
        print(f"IGNORED versioned artifact {artifact_id}: {path}")
    checked = len({artifact_id for artifact_id, _ in versioned_artifacts(contract)})
    if hidden:
        print(f"FAIL: {len(hidden)} of {checked} declared versioned artifacts are git-ignored; "
              "their producers will be refused at the first write")
        return 1
    print(f"OK: {checked} declared versioned artifacts checked, none git-ignored "
          f"(exempt: {', '.join(sorted(EXEMPT))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
