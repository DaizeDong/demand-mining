#!/usr/bin/env python3
"""Doctor for demand-mining's companion config (config-spec E3). Resolves the config dir via the
documented discovery order (identical to scripts/lib.py:find_config_dir), validates it against the
contract in CONFIG.md, and prints PASS/FAIL per check naming exactly what is missing.
Exit 0 = ready, 1 = not ready, 2 = usage error.

Discovery order (config-spec E2):
  1. $DEMAND_MINING_CONFIG   2. ~/.demand-mining-config/   3. ~/.config/demand-mining-config/

Accepts BOTH supported layouts:
  * per-product: registry.json{schema_version, skill, products:[{slug}]} -> products/<slug>/...
  * flat:        <dir>/priority.json (or watchlist.json) [+ taxonomy.json]
The registry variant is products[]; tools[]/entries[] are also accepted for forward-compat.

Usage:
  python verify_config.py [--skill <name>] [--config-dir <dir>]
Stdlib only. Never echoes secret values (only presence).
"""
import argparse
import json
import os
import sys

DEFAULT_SKILL = "demand-mining"
PASS, FAIL = "PASS", "FAIL"


def env_var(skill):
    return skill.upper().replace("-", "_") + "_CONFIG"


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


def discover(skill, override):
    if override:
        return os.path.abspath(os.path.expanduser(override)), "explicit (--config-dir)"
    val = os.environ.get(env_var(skill))
    if val and os.path.isdir(os.path.expanduser(val)):
        return os.path.abspath(os.path.expanduser(val)), "env:%s" % env_var(skill)
    for d in (os.path.expanduser("~/.%s-config" % skill),
              os.path.expanduser("~/.config/%s-config" % skill)):
        if os.path.isdir(d):
            return d, "default:%s" % d
    return None, None


def main():
    ap = argparse.ArgumentParser(description="Validate demand-mining's companion config.")
    ap.add_argument("--skill", default=None)
    ap.add_argument("--config-dir", default=None)
    a = ap.parse_args()

    skill = a.skill or detect_skill() or DEFAULT_SKILL

    cfg, how = discover(skill, a.config_dir)
    print("Config doctor for skill '%s'" % skill)
    print("Discovery env var: %s" % env_var(skill))
    if not cfg:
        print("  [%s] config located -> none found." % FAIL)
        print("       Set %s=<dir> or run: python scripts/init_config.py"
              % env_var(skill))
        return 1
    print("  resolved via %s -> %s" % (how, cfg))
    print("-" * 60)

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))

    check("config dir exists", os.path.isdir(cfg))

    reg = os.path.join(cfg, "registry.json")
    has_reg = os.path.isfile(reg)
    flat_priority = (os.path.isfile(os.path.join(cfg, "priority.json")) or
                     os.path.isfile(os.path.join(cfg, "watchlist.json")))

    if has_reg:
        try:
            with open(reg, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            check("registry.json valid JSON", True)
            check("schema_version == 1", data.get("schema_version") == 1,
                  "got %r" % data.get("schema_version"))
            items = data.get("products", data.get("tools", data.get("entries")))
            check("products[]/tools[]/entries[] is a list", isinstance(items, list),
                  "type %s" % type(items).__name__)
            # each registered product should have a dir (warn-level via FAIL naming the slug)
            if isinstance(items, list):
                for it in items:
                    slug = (it or {}).get("slug")
                    if slug:
                        pdir = os.path.join(cfg, "products", slug)
                        check("product '%s' dir present" % slug, os.path.isdir(pdir),
                              "missing products/%s/ (run init_config.py --product %s)"
                              % (slug, slug))
        except Exception as e:
            check("registry.json valid JSON", False, str(e))
    else:
        # flat layout is valid too; if neither registry nor flat priority, config is empty.
        check("layout present (registry.json OR priority.json/watchlist.json)", flat_priority,
              "neither registry.json nor priority.json/watchlist.json found")

    check("secrets/ dir present", os.path.isdir(os.path.join(cfg, "secrets")))

    gi = os.path.join(cfg, ".gitignore")
    gi_ok = os.path.isfile(gi)
    check(".gitignore present", gi_ok)
    if gi_ok:
        txt = open(gi, "r", encoding="utf-8", errors="replace").read()
        check(".gitignore blocks secrets (secrets/* + *.env)",
              "secrets/" in txt and "*.env" in txt)

    # self-contained check (E5): no absolute-path leakage in committed config files.
    leak = []
    scan = ["registry.json", ".gitignore", os.path.join("secrets", "README.md"),
            "priority.json", "taxonomy.json"]
    for rel in scan:
        p = os.path.join(cfg, rel)
        if os.path.isfile(p):
            t = open(p, "r", encoding="utf-8", errors="replace").read()
            if any(s in t for s in ("C:\\", "C:/", "/home/", "/Users/", "/root/")):
                leak.append(rel)
    check("self-contained (no hardcoded absolute paths)", not leak, "leaks in %s" % leak)

    n_fail = sum(1 for _, ok, _ in results if not ok)
    for nm, ok, detail in results:
        line = "  [%s] %s" % (PASS if ok else FAIL, nm)
        if detail and not ok:
            line += "  -> %s" % detail
        print(line)
    print("-" * 60)
    if n_fail:
        print("NOT READY: %d check(s) failed. Fix the above (or re-run init_config.py)." % n_fail)
        return 1
    print("READY: config at %s conforms. Tune products/<slug>/priority.json to override defaults."
          % cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
