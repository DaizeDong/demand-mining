"""Standalone, hash-bound TTL for Demand's explicitly temporary PRIVATE files.

General storage inventory belongs to skill-smith. This runtime only knows Demand's
raw collection areas; it never retires arbitrary contract entries or Git history.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

import data_safety

RAW_DIRS = ("raw", "chunks", "eod2_chunks", "eod3_chunks")
MANAGED_DIRS = (*RAW_DIRS, "manual-corpus", "pseudo-maps")
STATE = Path("pool/retention")


def companion_root(path):
    return Path(data_safety.require_private(path)["root"])


def _admit(companion):
    proof = data_safety.require_private(companion)
    root = Path(proof["path"])
    if root != Path(proof["root"]):
        raise ValueError("retention requires the exact PRIVATE companion root")
    return root, proof["transport"]


def _policy(cfg):
    privacy = cfg.get("privacy", {})
    result = {}
    for kind, key, default in (("raw", "raw_retention_days", 14),
                               ("pseudo_map", "pseudo_map_retention_days", 7)):
        value = privacy.get(key, default)
        if type(value) is not int or value <= 0:
            raise ValueError(key + " must be a positive integer")
        result[kind] = value
    return result


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(p in {"", ".", ".."} or p.endswith((" ", "."))
                   for p in value.split("/"))):
        raise ValueError("retention path must be an ordinary relative POSIX path")
    return value


def artifact_kind(relative):
    """Return a permitted classification, never infer a map from its contents."""
    parts = _relative(relative).split("/")
    if any(part.startswith(".") or part.endswith(".lock") for part in parts):
        return None
    if parts[0] != "data":
        return None
    if len(parts) == 2 and parts[1].startswith("corpus") and parts[1].endswith(".json"):
        return "raw"
    if len(parts) >= 3 and parts[1] in (*RAW_DIRS, "manual-corpus"):
        return "raw"
    if len(parts) >= 3 and parts[1] == "pseudo-maps":
        return "pseudo_map"
    return None


def _snapshot(root, relative):
    path = root / _relative(relative)
    data_safety._unaliased_path(path)
    if data_safety._repository_root(path) != root:
        raise ValueError("retention refuses nested repositories")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("retention requires an ordinary unlinked file")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if (info.st_size, info.st_mtime_ns, info.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("retention snapshot changed while hashing")
    return {"path": relative, "bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns, "sha256": digest}


def _files(directory):
    if not os.path.lexists(directory):
        return
    data_safety._unaliased_path(directory)
    if not directory.is_dir():
        raise ValueError("expected retention directory")
    if (directory / ".git").exists():
        raise ValueError("retention refuses nested repositories")
    for entry in sorted(directory.iterdir()):
        data_safety._unaliased_path(entry)
        if entry.is_dir():
            yield from _files(entry)
        elif entry.is_file():
            yield entry
        else:
            raise ValueError("unsupported retention file type")


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _registry(root):
    path = root / STATE / "registry.json"
    if not os.path.lexists(path):
        return {}
    data_safety._unaliased_path(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("artifacts"), dict):
        raise ValueError("invalid retention registry")
    for relative, row in value["artifacts"].items():
        if (not isinstance(row, dict) or artifact_kind(relative) != row.get("kind")
                or row.get("kind") is None or not isinstance(row.get("sha256"), str)
                or not _timestamp(row.get("captured_at"))):
            raise ValueError("invalid retention registration")
    return value["artifacts"]


@contextmanager
def activity(companion):
    """Serialize maintenance with scheduled runs and managed corpus I/O."""
    root, _ = _admit(companion)
    with data_safety.file_lock(root / STATE / ".lock", timeout=0):
        yield root


def _pool_roots(root):
    pools = [root / "pool"]
    override = os.environ.get("DEMAND_MINING_DATA_DIR")
    if override is not None:
        extra = Path(data_safety.require_private(override)["path"])
        if extra not in pools:
            pools.append(extra)
    return pools


@contextmanager
def _business_locks(root):
    with ExitStack() as stack:
        locks = {path for area in [root / "data", *_pool_roots(root)]
                 for path in _files(area) if path.name.endswith(".lock")}
        for path in sorted(locks - {root / STATE / ".lock"}):
            stack.enter_context(data_safety.file_lock(path, timeout=0, create=False))
        yield


def _pending(root):
    for pool in _pool_roots(root):
        for path in _files(pool):
            relative = path.relative_to(pool).parts
            if not relative or relative[0] not in {"scheduled", "implicit", "runs"}:
                continue
            if path.name == "checkpoint-pending.json":
                return True
            if ((relative[0] in {"scheduled", "implicit"} and path.name == "active.json")
                    or (relative[0] == "runs" and path.name == "state.json")):
                state = json.loads(path.read_text(encoding="utf-8"))
                field = "phase" if relative[0] == "runs" else "status"
                if not isinstance(state, dict) or state.get(field) != "complete":
                    return True
    return False


def plan_retention(companion, cfg, *, now=None):
    """Read-only preview: inspect names/metadata and hash eligible expired files."""
    policy = _policy(cfg)
    root, transport = _admit(companion)
    now = time.time() if now is None else now
    if not _timestamp(now):
        raise ValueError("invalid retention clock")
    registry = _registry(root)
    data = root / "data"
    candidates = []
    if os.path.lexists(data):
        data_safety._unaliased_path(data)
        candidates.extend(path for path in data.iterdir()
                          if path.name.startswith(("corpus", "authors")) and path.suffix == ".json")
        for name in MANAGED_DIRS:
            candidates.extend(_files(data / name))
    items, manual = [], []
    for path in sorted(candidates):
        relative = path.relative_to(root).as_posix()
        data_safety._unaliased_path(path)
        if not path.is_file():
            raise ValueError("retention candidate is not a regular file")
        kind = artifact_kind(relative)
        row = registry.get(relative)
        needs_registration = relative.startswith(("data/manual-corpus/", "data/pseudo-maps/"))
        if kind is None or (needs_registration and row is None):
            manual.append(relative)
            continue
        captured = row["captured_at"] if row else path.stat().st_mtime
        if now - captured < policy[kind] * 86400:
            continue
        snapshot = _snapshot(root, relative)
        if row and snapshot["sha256"] != row["sha256"]:
            manual.append(relative)
            continue
        items.append({**snapshot, "kind": kind, "captured_at": captured})
    plan = {"schema_version": 1, "root": str(root), "transport": transport,
            "policy": policy, "created_at": now, "registry": registry, "items": items}
    return {"plan": plan, "manual_review": manual}


def register_artifact(companion, path, kind, *, captured_at=None):
    captured_at = time.time() if captured_at is None else captured_at
    if not _timestamp(captured_at):
        raise ValueError("captured_at must be a nonnegative timestamp")
    with activity(companion) as root:
        _register(root, path, kind, captured_at)


def _register(root, path, kind, captured_at):
    path = data_safety._unaliased_path(path)
    relative = path.relative_to(root).as_posix()
    if kind not in {"raw", "pseudo_map"} or artifact_kind(relative) != kind:
        raise ValueError("artifact is not eligible for retention; core state is protected")
    registry = _registry(root)
    registry[relative] = {"kind": kind, "captured_at": captured_at,
                          "sha256": _snapshot(root, relative)["sha256"]}
    data_safety.atomic_json(root / STATE / "registry.json",
                            {"schema_version": 1, "artifacts": registry})


def write_corpus(path, content, *, captured_at=None):
    """Publish collected bytes under the retention mutex; register known raw areas."""
    captured_at = time.time() if captured_at is None else captured_at
    if not _timestamp(captured_at):
        raise ValueError("captured_at must be a nonnegative timestamp")
    proof = data_safety.require_private(path)
    path = Path(proof["path"])
    with activity(proof["root"]) as root:
        managed = artifact_kind(path.relative_to(root).as_posix()) == "raw"
        data_safety.atomic_bytes(path, content)
        if managed:
            _register(root, path, "raw", captured_at)
    return path


def write_plan(companion, plan):
    root, _ = _admit(companion)
    return data_safety.atomic_json(root / STATE / "current-plan.json", plan)


def _remove(root, item):
    expected = {key: item[key] for key in ("path", "bytes", "mtime_ns", "sha256")}
    if _snapshot(root, item["path"]) != expected:
        raise ValueError("retention snapshot changed before deletion")
    if os.name == "nt":
        shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not shell:
            raise RuntimeError("PowerShell is required for literal-path retention deletion")
        subprocess.run([shell, "-NoProfile", "-NonInteractive",
                        "-File", str(Path(__file__).with_name("retention_remove.ps1")),
                        "-Root", str(root), "-Relative", item["path"],
                        "-ExpectedBytes", str(item["bytes"]),
                        "-ExpectedMtimeNs", str(item["mtime_ns"]), "-ExpectedSha256", item["sha256"]],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    else:
        (root / item["path"]).unlink()


def _apply(root, cfg, plan_path, approved_sha256, now):
    data_safety._unaliased_path(plan_path)
    if Path(plan_path) != root / STATE / "current-plan.json":
        raise ValueError("only the companion current retention plan can be applied")
    content = Path(plan_path).read_bytes()
    if hashlib.sha256(content).hexdigest() != approved_sha256:
        raise ValueError("reviewed retention plan changed")
    plan = json.loads(content)
    current = plan_retention(root, cfg, now=now)
    for key in ("schema_version", "root", "transport", "policy", "registry"):
        if plan.get(key) != json.loads(json.dumps(current["plan"][key])):
            raise ValueError("retention plan admission or policy changed")
    known = {item["path"]: item for item in current["plan"]["items"]}
    selected = plan.get("items")
    if not isinstance(selected, list) or len({item["path"] for item in selected}) != len(selected):
        raise ValueError("invalid retention plan items")
    if any(known.get(item["path"]) != item for item in selected):
        raise ValueError("retention snapshot or eligibility changed")
    if _pending(root):
        return {"status": "deferred", "reason": "pending_recovery", "bytes": 0,
                "manual_review": current["manual_review"]}
    receipt = {"status": "applying", "plan_sha256": approved_sha256,
               "deleted": [], "bytes": 0, "manual_review": current["manual_review"]}
    receipt_path = root / STATE / "latest-receipt.json"
    data_safety.atomic_json(receipt_path, receipt)
    for item in selected:
        if _admit(root)[1] != current["plan"]["transport"] or _pending(root):
            raise ValueError("retention admission or recovery state changed")
        _remove(root, item)
        receipt["deleted"].append(item["path"])
        receipt["bytes"] += item["bytes"]
        data_safety.atomic_json(receipt_path, receipt)
    registry = _registry(root)
    remaining = {key: row for key, row in registry.items() if key not in receipt["deleted"]}
    if remaining != registry:
        data_safety.atomic_json(root / STATE / "registry.json", {"schema_version": 1, "artifacts": remaining})
    receipt["status"] = "applied" if selected else "clean"
    data_safety.atomic_json(receipt_path, receipt)
    return receipt


def apply_retention(companion, cfg, plan_path, approved_sha256, *, now=None):
    _policy(cfg)
    with activity(companion) as root, _business_locks(root):
        return _apply(root, cfg, plan_path, approved_sha256, now)


def enforce(companion, cfg, *, now=None, locked=False):
    _policy(cfg)
    if not locked:
        with activity(companion) as root:
            return enforce(root, cfg, now=now, locked=True)
    root, _ = _admit(companion)
    with _business_locks(root):
        if _pending(root):
            return {"status": "deferred", "reason": "pending_recovery", "bytes": 0}
        report = plan_retention(root, cfg, now=now)
        path = write_plan(root, report["plan"])
        return _apply(root, cfg, path, hashlib.sha256(path.read_bytes()).hexdigest(), now)


def main():
    from lib import find_config_dir, load_config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "enforce", "apply", "register"))
    parser.add_argument("--config-dir")
    parser.add_argument("--approved-sha256")
    parser.add_argument("--path")
    parser.add_argument("--kind", choices=("raw", "pseudo_map"))
    parser.add_argument("--captured-at", type=float)
    args = parser.parse_args()
    if args.config_dir:
        os.environ["DEMAND_MINING_CONFIG"] = args.config_dir
    companion = find_config_dir()
    if companion is None:
        raise ValueError("retention requires an initialized PRIVATE companion")
    root, _ = _admit(companion)
    cfg = load_config()
    if args.action == "plan":
        report = plan_retention(root, cfg)
    elif args.action == "enforce":
        report = enforce(root, cfg)
    elif args.action == "apply":
        report = apply_retention(root, cfg, root / STATE / "current-plan.json", args.approved_sha256)
    else:
        if not args.path or not args.kind:
            parser.error("register requires --path and --kind")
        register_artifact(root, args.path, args.kind, captured_at=args.captured_at)
        report = {"status": "registered"}
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
