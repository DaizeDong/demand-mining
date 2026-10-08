#!/usr/bin/env python3
"""Validate the selected companion configuration for demand-mining.

Selection and required fields are defined in CONFIG.md and config.contract.json.
Explicit CLI paths isolate environment selection. Runtime uses the same pinned Guards
companion discovery; invalid selectors never fall through to another companion.
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
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "skills", "demand-mining", "scripts"))
    from config_paths import companion_root
    selected = companion_root(override)
    return (str(selected), "shared companion selection") if selected is not None else (None, None)


def main():
    ap = argparse.ArgumentParser(description="Validate demand-mining's companion config.")
    ap.add_argument("--skill", default=None)
    ap.add_argument("--config-dir", default=None)
    a = ap.parse_args()

    skill = a.skill or detect_skill() or DEFAULT_SKILL

    try:
        cfg, how = discover(skill, a.config_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        print("NOT READY: " + str(exc))
        return 1
    print("Config doctor for skill '%s'" % skill)
    print("Discovery env var: %s" % env_var(skill))
    if not cfg:
        print("  [%s] config located -> none found." % FAIL)
        print("       Set %s=<dir> or run: python scripts/init_config.py"
              % env_var(skill))
        return 1
    print("  resolved via %s -> %s" % (how, cfg))
    print("RESOLVED: " + cfg)
    print("-" * 60)

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))

    check("config dir exists", os.path.isdir(cfg))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "skills", "demand-mining", "scripts"))
    from data_safety import require_private, data_root
    from finalize import logical_identity
    from lib import load_config
    from redact import privacy_coverage
    from config_paths import selected_environment
    with selected_environment(cfg):
        for label, probe in (
                ("config PRIVATE destination", lambda: require_private(cfg)),
                ("DATA PRIVATE destination", data_root),
                ("configured product and IANA timezone", lambda: logical_identity(load_config())),
                ("working directory readable/writable", lambda: os.access(cfg, os.R_OK | os.W_OK))):
            try:
                value = probe()
                check(label, bool(value))
            except (OSError, ValueError, RuntimeError) as exc:
                check(label, False, str(exc))
    coverage = privacy_coverage()
    for label in ("covered", "uncovered", "unchecked"):
        print("Privacy %s: %s" % (label, ", ".join(coverage[label])))
    print("Privacy policy: %s" % coverage["policy"])

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

    # Per-product payloads. Found by config_mutation_probe: every field mutation of
    # products/<slug>/priority.json (131 of them) and taxonomy.json (11) was accepted,
    # because the content checks below only ever looked at the FLAT paths <cfg>/priority.json
    # and <cfg>/taxonomy.json. The deployed layout here is the per-product one, so those
    # checks were reading files that do not exist and passing on absence.
    prod_files = []
    if has_reg and isinstance(locals().get("data"), dict):
        for it in (data.get("products") or []):
            slug = (it or {}).get("slug")
            if not slug:
                continue
            for base in ("priority.json", "watchlist.json", "taxonomy.json"):
                rel = os.path.join("products", slug, base)
                p = os.path.join(cfg, rel)
                if os.path.isfile(p):
                    prod_files.append(rel)
                    try:
                        with open(p, "r", encoding="utf-8-sig") as f:
                            doc = json.load(f)
                        check("%s valid JSON" % rel, True)
                    except Exception as e:
                        check("%s valid JSON" % rel, False, str(e))
                        continue
                    check("%s is an object" % rel, isinstance(doc, dict),
                          "top level is %s" % type(doc).__name__)
                    if not isinstance(doc, dict):
                        continue
                    if base in ("priority.json", "watchlist.json"):
                        check("%s schema_version == 1" % rel, doc.get("schema_version") == 1,
                              "got %r" % doc.get("schema_version"))
                        check("%s scoring is an object" % rel,
                              isinstance(doc.get("scoring"), dict),
                              "got %s" % type(doc.get("scoring")).__name__)
                        check("%s privacy is an object" % rel,
                              isinstance(doc.get("privacy"), dict),
                              "got %s" % type(doc.get("privacy")).__name__)
                    else:
                        check("%s slug is a non-empty string" % rel,
                              isinstance(doc.get("slug"), str) and bool(doc.get("slug")))
                        # taxonomy is a LIST of category objects, not a mapping. The first
                        # draft of this check asserted dict and went red on real data -- the
                        # data is the fact here, so the assertion moved, not the file.
                        tx = doc.get("taxonomy")
                        check("%s taxonomy is a non-empty list" % rel,
                              isinstance(tx, list) and len(tx) > 0,
                              "got %s" % type(tx).__name__)
                        if isinstance(tx, list) and tx:
                            check("%s taxonomy entries have id+label+keywords" % rel,
                                  all(isinstance(e, dict) and isinstance(e.get("id"), str)
                                      and isinstance(e.get("label"), str)
                                      and isinstance(e.get("keywords"), list) for e in tx),
                                  "%d entr(ies)" % len(tx))
            # A registered product with no priority/watchlist at all is not "ready":
            # absence used to read exactly like a clean pass.
            has_pri = any(r.endswith(("priority.json", "watchlist.json"))
                          and ("products" + os.sep + slug + os.sep) in r
                          for r in prod_files)
            check("product '%s' has priority.json or watchlist.json" % slug, has_pri)

    # tracking.json at the config root: never named in this script before, so deleting
    # or emptying it left the doctor printing READY.
    trk = os.path.join(cfg, "tracking.json")
    if os.path.isfile(trk):
        try:
            with open(trk, "r", encoding="utf-8-sig") as f:
                tdoc = json.load(f)
            check("tracking.json valid JSON", True)
        except Exception as e:
            check("tracking.json valid JSON", False, str(e))
            tdoc = None
        if isinstance(tdoc, dict):
            check("tracking.schema_version == 1", tdoc.get("schema_version") == 1,
                  "got %r" % tdoc.get("schema_version"))
            check("tracking.sources is a non-empty list",
                  isinstance(tdoc.get("sources"), list) and len(tdoc.get("sources")) > 0,
                  "got %s" % type(tdoc.get("sources")).__name__)
            check("tracking.hard_disabled is a list",
                  isinstance(tdoc.get("hard_disabled"), list),
                  "got %s" % type(tdoc.get("hard_disabled")).__name__)
        elif tdoc is not None:
            check("tracking.json is an object", False,
                  "top level is %s" % type(tdoc).__name__)

    check("secrets/ dir present", os.path.isdir(os.path.join(cfg, "secrets")))

    from config_paths import storage_mode
    try:
        mode = storage_mode(cfg)
        check("credential storage mode", True, mode)
    except (OSError, ValueError) as exc:
        mode = None
        check("credential storage mode", False, str(exc))
    gi = os.path.join(cfg, ".gitignore")
    gi_ok = os.path.isfile(gi)
    check(".gitignore present", gi_ok)
    if mode == "B" and gi_ok:
        with open(gi, encoding="utf-8") as stream:
            txt = stream.read()
        check("Mode B .gitignore blocks secrets (secrets/* + *.env)",
              "secrets/" in txt and "*.env" in txt)

    # self-contained check (E5): no absolute-path leakage in committed config files.
    leak = []
    # The flat names stay for the flat layout; prod_files adds whatever the per-product
    # layout actually deployed. Without it this scan examined zero product files on the
    # only layout in use, and printed the same green as a real clean scan.
    scan = ["registry.json", ".gitignore", os.path.join("secrets", "README.md"),
            "priority.json", "taxonomy.json", "tracking.json"] + prod_files
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
