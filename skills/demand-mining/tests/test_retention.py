"""Offline expiry tests use generated fixtures and the standalone TTL runtime."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import subprocess

import pytest

import data_safety
import lib
import scheduled

NOW = 1_800_000_000
DAY = 86_400
SOURCE = Path(__file__).resolve().parents[3]


@pytest.fixture
def storage(tmp_path, monkeypatch):
    try:
        retention = importlib.import_module("retention")
    except ModuleNotFoundError:
        pytest.fail("The retention entrypoint is not implemented")
    root = tmp_path / "companion"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setenv("DEMAND_MINING_CONFIG", str(root))
    monkeypatch.delenv("DEMAND_MINING_DATA_DIR", raising=False)

    def private(path):
        path = data_safety._unaliased_path(path)
        if not path.is_relative_to(root):
            raise data_safety.DestinationError("synthetic outside boundary")
        return {"path": str(path), "root": str(root), "transport": {"synthetic": True}}

    monkeypatch.setattr(data_safety, "require_private", private)
    cases = json.loads((Path(__file__).parent / "fixtures/repair_cases.json").read_text(encoding="utf-8"))["retention"]
    cfg = copy.deepcopy(lib.DEFAULT_CONFIG)

    def write(relative, age, content=None):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cases["body"] if content is None else content, encoding="utf-8", newline="\n")
        os.utime(path, (NOW - age * DAY, NOW - age * DAY))
        return path

    return retention, root, cfg, cases, write


def paths(report):
    return [item["path"] for item in (report.get("plan") or {}).get("items", [])]


def test_raw_expiry_covers_chunks_but_preserves_recent_manual_and_ledger(storage):
    retention, root, cfg, cases, write = storage
    for name in ["data/corpus_old.json", "data/chunks/part.json", "data/eod2_chunks/part.json", "data/eod3_chunks/part.json"]:
        write(name, 14)
    write("data/raw/current.json", 13)
    write("data/manual-corpus/input.json", 30)
    write("data/authors_legacy.json", 30)
    write("pool/demands.jsonl", 30, cases["ledger"])
    report = retention.plan_retention(root, cfg, now=NOW)
    assert paths(report) == ["data/chunks/part.json", "data/corpus_old.json", "data/eod2_chunks/part.json", "data/eod3_chunks/part.json"]
    assert report["manual_review"] == ["data/authors_legacy.json", "data/manual-corpus/input.json"]
    assert (root / "pool/demands.jsonl").read_text() == cases["ledger"]
    assert not (root / "pool/retention").exists()


def test_mapping_uses_seven_days_and_requires_explicit_registration(storage):
    retention, root, cfg, cases, write = storage
    old = write("data/pseudo-maps/old.json", 8, json.dumps(cases["mapping"]))
    recent = write("data/pseudo-maps/recent.json", 6, json.dumps(cases["mapping"]))
    unknown = write("data/pseudo-maps/unregistered.json", 30)
    retention.register_artifact(root, old, "pseudo_map", captured_at=NOW - 8 * DAY)
    retention.register_artifact(root, recent, "pseudo_map", captured_at=NOW - 6 * DAY)
    report = retention.plan_retention(root, cfg, now=NOW)
    assert paths(report) == ["data/pseudo-maps/old.json"]
    assert "data/pseudo-maps/unregistered.json" in report["manual_review"]
    assert unknown.exists()


def test_exact_plan_rejects_changed_bytes_even_with_same_size_and_mtime(storage):
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 20)
    report = retention.plan_retention(root, cfg, now=NOW)
    plan_path = retention.write_plan(root, report["plan"])
    approved = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    before = target.stat()
    assert len(cases["body"]) == len(cases["replacement"])
    target.write_text(cases["replacement"], encoding="utf-8", newline="\n")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="snapshot|changed"):
        retention.apply_retention(root, cfg, plan_path, approved, now=NOW)
    assert target.read_text() == cases["replacement"]


def test_pending_recovery_prevents_expiry_and_completed_run_does_not(storage):
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 20)
    marker = write("pool/scheduled/product/active.json", 20, json.dumps(cases["pending"]))
    report = retention.enforce(root, cfg, now=NOW)
    assert report["status"] == "deferred"
    assert target.exists()
    marker.write_text(json.dumps(cases["complete"]), encoding="utf-8")
    report = retention.enforce(root, cfg, now=NOW)
    assert report["status"] == "applied"
    assert report["bytes"] == len(cases["body"].encode())
    assert not target.exists()
    assert marker.exists()


def test_held_business_lock_refuses_cleanup_without_touching_ledger(storage):
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 20)
    ledger = write("pool/demands.jsonl", 20, cases["ledger"])
    lock = write("pool/demands.jsonl.lock", 20, "")
    with data_safety.file_lock(lock):
        with pytest.raises(TimeoutError):
            retention.enforce(root, cfg, now=NOW)
    assert target.exists()
    assert ledger.read_text() == cases["ledger"]


def test_held_raw_collection_lock_refuses_cleanup(storage):
    retention, root, cfg, cases, write = storage
    target = write("data/chunks/old.json", 20)
    lock = write("data/chunks/.lock", 20, "")
    with data_safety.file_lock(lock):
        with pytest.raises(TimeoutError):
            retention.enforce(root, cfg, now=NOW)
    assert target.exists()


def test_core_files_cannot_be_registered_as_expiring_raw(storage):
    retention, root, cfg, cases, write = storage
    ledger = write("pool/demands.jsonl", 30, cases["ledger"])
    with pytest.raises(ValueError, match="eligible|core"):
        retention.register_artifact(root, ledger, "raw", captured_at=NOW - 30 * DAY)
    assert ledger.exists()


@pytest.mark.parametrize("value", [0, -1, True, "14"])
def test_invalid_retention_never_deletes(storage, value):
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 30)
    cfg["privacy"]["raw_retention_days"] = value
    with pytest.raises(ValueError, match="positive integer"):
        retention.enforce(root, cfg, now=NOW)
    assert target.exists()


def test_scheduled_entry_expires_before_collecting_and_holds_activity(storage, monkeypatch, capsys):
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 20)
    monkeypatch.setattr(retention.time, "time", lambda: NOW)
    monkeypatch.setattr(scheduled, "preflight", lambda *args: (cfg, root, root / "pool", root / "pool/logs"))
    monkeypatch.setattr(sys, "argv", ["scheduled.py"])

    def collect(*args):
        assert not target.exists()
        with pytest.raises(TimeoutError):
            with retention.activity(root):
                pass
        return {"ok": True, "status": "complete"}

    monkeypatch.setattr(scheduled, "execute", collect)
    assert scheduled.main() == 0
    assert json.loads(capsys.readouterr().out)["retention"]["status"] == "applied"


def test_corpus_writer_registers_exact_bytes_and_respects_activity_lock(storage):
    retention, root, cfg, cases, write = storage
    target = root / "data/manual-corpus/new.json"
    body = cases["body"].encode()
    with retention.activity(root):
        with pytest.raises(TimeoutError):
            retention.write_corpus(target, body, captured_at=NOW - 20 * DAY)
    retention.write_corpus(target, body, captured_at=NOW - 20 * DAY)
    assert target.read_bytes() == body
    assert paths(retention.plan_retention(root, cfg, now=NOW)) == ["data/manual-corpus/new.json"]


@pytest.mark.parametrize("marker,field", [("pool/runs/run/state.json", "phase"),
                                          ("pool/implicit/product/active.json", "status"),
                                          ("pool/scheduled/product/checkpoint-pending.json", None)])
def test_other_recovery_markers_defer(storage, marker, field):
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 20)
    write(marker, 30, json.dumps({field: "prepared"} if field else {}))
    assert retention.enforce(root, cfg, now=NOW)["status"] == "deferred"
    assert target.exists()


def test_hardlinks_and_nested_repositories_are_refused(storage):
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 20)
    other = root / "data/raw/alias.json"
    os.link(target, other)
    with pytest.raises(data_safety.DestinationError, match="hardlink"):
        retention.plan_retention(root, cfg, now=NOW)
    other.unlink()
    (target.parent / ".git").mkdir()
    with pytest.raises(ValueError, match="nested"):
        retention.plan_retention(root, cfg, now=NOW)
    assert target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows native deletion boundary")
@pytest.mark.parametrize("change", ["mtime", "hardlink", "nested_repo"])
def test_native_delete_rechecks_after_python_snapshot(storage, monkeypatch, change):
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 20)
    invoke = subprocess.run

    def change_then_invoke(*args, **kwargs):
        if change == "mtime":
            os.utime(target, (NOW, NOW))
        elif change == "hardlink":
            os.link(target, target.with_name("alias.json"))
        else:
            (target.parent / ".git").mkdir()
        return invoke(*args, **kwargs)

    monkeypatch.setattr(retention.subprocess, "run", change_then_invoke)
    with pytest.raises(subprocess.CalledProcessError) as error:
        retention.enforce(root, cfg, now=NOW)
    expected = {"mtime": "snapshot changed", "hardlink": "verified link metadata", "nested_repo": "nested repositories"}
    assert expected[change] in error.value.stderr.decode("utf-8", errors="replace")
    assert target.exists()
    receipt = json.loads((root / "pool/retention/latest-receipt.json").read_text())
    assert receipt["deleted"] == []


@pytest.mark.skipif(os.name != "nt", reason="Windows native deletion boundary")
def test_native_delete_does_not_need_optional_powershell_modules(storage, monkeypatch):
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 20)
    monkeypatch.setenv("PSModulePath", str(root / "missing-powershell-modules"))
    assert retention.enforce(root, cfg, now=NOW)["status"] == "applied"
    assert not target.exists()
