#!/usr/bin/env python3
"""Initialize synthetic configuration templates for demand-mining.

Selection and required fields are defined in CONFIG.md and config.contract.json.
Explicit CLI paths isolate environment selection. Runtime uses the same pinned Guards
companion discovery; invalid selectors never fall through to another companion.
"""
import argparse
import json
import os
import sys

DEFAULT_SKILL = "demand-mining"

GITIGNORE = """\
# Secrets gate (config-spec E6 / Mode B), Mode B excludes credential values from this backup.
secrets/*
!secrets/README.md
!secrets/.gitkeep
*.env
!*.env.template
!env.template
*.pseudo-map
pseudonym_hmac_salt
claude.json
.claude.json
*credentials*.json
*.key
*.pem
!*.key.template
!*.pem.template
"""

SECRETS_README = """\
# secrets/, Mode B (gitignored)

Real secret values live here and are **gitignored** (see ../.gitignore). This is the default Mode B policy, not a prohibition on verified PRIVATE backup.
Back them up out-of-band under Mode B. An explicitly selected Mode A may instead version them only in a verified PRIVATE repository and restore from that history. Restore on a new machine by copying the
files back into this directory, then re-running `scripts/verify_config.py`.

Active storage mode: **B** (gitignored + out-of-band backup). Files MUST be UTF-8 without BOM.

demand-mining secrets:
- `pseudonym_hmac_salt`, HMAC salt driving redact.py pseudonyms (or set env
  DEMAND_MINING_PSEUDONYM_SALT instead). Never log or echo it.
- Discord bot credentials, normally supplied by the shared `auto-support` relay; only place a
  per-config override here if you are NOT using the shared bot.
"""

# Deterministic minimal starter overrides (kept tiny on purpose: they only demonstrate shape;
# everything unset deep-merges from lib.py:DEFAULT_CONFIG). Byte-stable across runs (E4).
STARTER_PRIORITY = {
    "schema_version": 1,
    "timezone": "UTC",
    "privacy": {"raw_retention_days": 14, "pseudo_map_retention_days": 7},
    "focus_topics": ["activation friction", "competitor switch"],
    "scoring": {"min_score_to_push": 70, "flagship_score": 80},
    "push": {"channel": "discord-relay", "max_per_day": 5},
}
STARTER_TAXONOMY = {
    "taxonomy": [
        {"id": "core-workflow", "label": "Core workflow", "weight": 1.1,
         "keywords": ["workflow", "flow", "step", "process"], "enabled": True},
        {"id": "other", "label": "Other / uncategorized", "weight": 0.8,
         "keywords": [], "enabled": True},
    ]
}


def env_var(skill):
    return skill.upper().replace("-", "_") + "_CONFIG"


def default_dir(skill):
    return os.path.expanduser("~/.%s-config" % skill)


def detect_skill():
    starts = [os.getcwd(), os.path.dirname(os.path.abspath(__file__))]
    for start in starts:
        d = start
        for _ in range(6):
            pj = os.path.join(d, ".claude-plugin", "plugin.json")
            if os.path.isfile(pj):
                try:
                    with open(pj, "r", encoding="utf-8") as f:
                        return json.load(f).get("name")
                except Exception:
                    pass
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
    return None


def write(path, content, force):
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "skills", "demand-mining", "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from data_safety import require_private
    require_private(path)
    if os.path.exists(path) and not force:
        print("  SKIP (exists): %s" % path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print("  wrote: %s" % path)


def dumps(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Stamp a spec-conformant demand-mining config repo.")
    ap.add_argument("--skill", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--product", default=None, help="optional product slug to scaffold")
    ap.add_argument("--mode", default="B", choices=["A", "B"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    skill = a.skill or detect_skill() or DEFAULT_SKILL
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "skills", "demand-mining", "scripts"))
    from config_paths import companion_root
    out = a.out or companion_root() or default_dir(skill)
    out = os.path.abspath(os.path.expanduser(out))
    slug = a.product
    if slug and not __import__("re").fullmatch(r"[a-zA-Z0-9_-]+", slug):
        raise ValueError("product slug must contain only letters, digits, hyphen or underscore")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "skills", "demand-mining", "scripts"))
    from data_safety import require_private
    require_private(out)

    print("Init config for skill '%s' (mode %s) at %s" % (skill, a.mode, out))
    print("Discovery env var: %s  (fallback %s)" % (env_var(skill), default_dir(skill)))

    # registry.json, per-product variant; deterministic, no machine-specific content (E4/E5).
    registry = {"schema_version": 1, "skill": skill,
                "products": ([{"slug": slug}] if slug else []), "mode": a.mode}
    write(os.path.join(out, "registry.json"), dumps(registry), a.force)
    ignore = GITIGNORE if a.mode == "B" else "# Mode A: version credentials only in this verified PRIVATE companion.\n/.staging/\n.demand-backup-index-*\n"
    write(os.path.join(out, ".gitignore"), ignore, a.force)
    write(os.path.join(out, "products", ".gitkeep"), "", a.force)
    policy = SECRETS_README if a.mode == "B" else (
        "# Private credential backup\n\nActive storage mode: A\n\n"
        "Version credentials only in the verified PRIVATE companion. Restore from its history; "
        "rotated credentials must be renewed with their provider. Never print secret values.\n")
    write(os.path.join(out, "secrets", "README.md"), policy, a.force)
    write(os.path.join(out, "secrets", ".gitkeep"), "", a.force)

    if slug:
        pd = os.path.join(out, "products", slug)
        write(os.path.join(pd, "priority.json"), dumps(dict(STARTER_PRIORITY, product_id=slug)), a.force)
        write(os.path.join(pd, "taxonomy.json"), dumps(dict(STARTER_TAXONOMY, slug=slug)), a.force)

    print("\nNext:")
    if not slug:
        print("  1) Add a product:  python %s --product <slug>" %
              os.path.relpath(os.path.abspath(__file__)))
    print("  2) Edit products/<slug>/priority.json to tune RICE/Kano/thresholds (deep-merged).")
    print("  3) Put real secrets in secrets/ (gitignored) or set DEMAND_MINING_PSEUDONYM_SALT.")
    print("  4) export %s=%s" % (env_var(skill), out))
    print("  5) python scripts/verify_config.py   # doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
