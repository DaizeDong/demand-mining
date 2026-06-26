#!/usr/bin/env python3
"""Initialize a spec-conformant companion config repo for demand-mining (config-spec E3/E4).

Generic + deterministic. Stamps an empty, conformant config skeleton using the skill's actual
**per-product** layout (registry.json -> products[] -> products/<slug>/{priority,taxonomy}.json),
Mode B secrets gitignored. Re-running with the same args produces byte-identical output —
template-driven, no interactive divergence (E4). Stdlib only; never writes/echoes secrets.

Discovery convention (mirrors scripts/lib.py:find_config_dir, also in CONFIG.md E2). The config dir
resolves from, in order:
  1. $DEMAND_MINING_CONFIG
  2. ~/.demand-mining-config/        (dotfile fallback)
  3. ~/.config/demand-mining-config/ (XDG fallback)

Usage:
  python init_config.py [--skill <name>] [--out <dir>] [--product <slug>] [--mode B] [--force]

--skill    skill name; default auto-detected from the nearest .claude-plugin/plugin.json,
           else "demand-mining".
--out      target dir; default = ~/.<skill>-config/.
--product  if given, also stamps products/<slug>/{priority.json,taxonomy.json} starter overrides
           and registers the slug in registry.json (deterministic minimal content).
"""
import argparse
import json
import os
import sys

DEFAULT_SKILL = "demand-mining"

GITIGNORE = """\
# Secrets gate (config-spec E6 / Mode B) — real values never enter git.
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
# secrets/ — Mode B (gitignored)

Real secret values live here and are **gitignored** (see ../.gitignore). They never enter git.
Back them up out-of-band (cloud sync / encrypted drive). Restore on a new machine by copying the
files back into this directory, then re-running `scripts/verify_config.py`.

Active storage mode: **B** (gitignored + out-of-band backup). Files MUST be UTF-8 without BOM.

demand-mining secrets:
- `pseudonym_hmac_salt` — HMAC salt driving redact.py pseudonyms (or set env
  DEMAND_MINING_PSEUDONYM_SALT instead). Never log or echo it.
- Discord bot credentials — normally supplied by the shared `auto-support` relay; only place a
  per-config override here if you are NOT using the shared bot.
"""

# Deterministic minimal starter overrides (kept tiny on purpose: they only demonstrate shape;
# everything unset deep-merges from lib.py:DEFAULT_CONFIG). Byte-stable across runs (E4).
STARTER_PRIORITY = {
    "schema_version": 1,
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
    out = a.out or default_dir(skill)
    out = os.path.abspath(os.path.expanduser(out))
    slug = a.product

    print("Init config for skill '%s' (mode %s) at %s" % (skill, a.mode, out))
    print("Discovery env var: %s  (fallback %s)" % (env_var(skill), default_dir(skill)))

    # registry.json — per-product variant; deterministic, no machine-specific content (E4/E5).
    registry = {"schema_version": 1, "skill": skill,
                "products": ([{"slug": slug}] if slug else [])}
    write(os.path.join(out, "registry.json"), dumps(registry), a.force)
    write(os.path.join(out, ".gitignore"), GITIGNORE, a.force)
    write(os.path.join(out, "products", ".gitkeep"), "", a.force)
    write(os.path.join(out, "secrets", "README.md"), SECRETS_README, a.force)
    write(os.path.join(out, "secrets", ".gitkeep"), "", a.force)

    if slug:
        pd = os.path.join(out, "products", slug)
        write(os.path.join(pd, "priority.json"), dumps(STARTER_PRIORITY), a.force)
        write(os.path.join(pd, "taxonomy.json"), dumps(STARTER_TAXONOMY), a.force)

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
