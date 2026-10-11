"""The scheduled EOD entry waits out short lock holders and never dies without a record.

Generated controls only: the companion, proofs and Git are the synthetic ones from the
retention fixture; no network, provider or real DATA is touched.
"""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import data_safety
import scheduled
from test_retention import NOW, storage  # noqa: F401  (pytest fixture)


def _hold(path, seconds, ready):
    with data_safety.file_lock(path, timeout=5):
        ready.set()
        time.sleep(seconds)


def _holder(path, seconds):
    ready = threading.Event()
    thread = threading.Thread(target=_hold, args=(path, seconds, ready), daemon=True)
    thread.start()
    assert ready.wait(5), "synthetic holder never acquired the lock"
    return thread


@pytest.fixture
def entry(storage, monkeypatch, tmp_path):  # noqa: F811
    retention, root, cfg, cases, write = storage
    monkeypatch.setattr(retention.time, "time", lambda: NOW)
    monkeypatch.setattr(scheduled, "preflight",
                        lambda *args: (cfg, root, root / "pool", root / "pool/logs"))
    monkeypatch.setattr(sys, "argv", ["scheduled.py"])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return retention, root, cfg, cases, write


def test_short_business_lock_holder_is_waited_out(storage):  # noqa: F811
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 20)
    lock = write("pool/demands.jsonl.lock", 20, "")
    holder = _holder(lock, 0.6)
    report = retention.enforce(root, cfg, now=NOW, lock_wait=10)
    holder.join(5)
    assert report["status"] == "applied"
    assert not target.exists()


def test_business_lock_held_past_the_wait_is_typed_and_removes_nothing(storage):  # noqa: F811
    retention, root, cfg, cases, write = storage
    target = write("data/raw/old.json", 20)
    lock = write("pool/demands.jsonl.lock", 20, "")
    with data_safety.file_lock(lock):
        started = time.monotonic()
        with pytest.raises(retention.BusinessLockBusy, match="pool/demands.jsonl.lock"):
            retention.enforce(root, cfg, now=NOW, lock_wait=0.4)
        assert time.monotonic() - started >= 0.35
    assert isinstance(retention.BusinessLockBusy("x"), TimeoutError)
    assert target.exists()


def test_eod_defers_retention_behind_a_busy_writer_and_still_runs(entry, monkeypatch, capsys):
    retention, root, cfg, cases, write = entry
    target = write("data/raw/old.json", 20)
    lock = write("pool/demands.jsonl.lock", 20, "")
    monkeypatch.setattr(scheduled, "BUSINESS_LOCK_WAIT_SECONDS", 0.2, raising=False)
    ran = []
    monkeypatch.setattr(scheduled, "execute",
                        lambda *a: ran.append(True) or {"ok": True, "status": "complete"})
    with data_safety.file_lock(lock):
        assert scheduled.main() == 0
    assert ran == [True]
    assert json.loads(capsys.readouterr().out)["retention"] == {
        "status": "deferred", "reason": "business_lock_busy", "bytes": 0}
    assert target.exists()


def test_eod_waits_for_a_maintenance_run_holding_the_activity_lock(entry, monkeypatch, capsys):
    retention, root, cfg, cases, write = entry
    target = write("data/raw/old.json", 20)
    lock = root / "pool/retention/.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(scheduled, "ACTIVITY_WAIT_SECONDS", 10.0, raising=False)
    monkeypatch.setattr(scheduled, "execute", lambda *a: {"ok": True, "status": "complete"})
    holder = _holder(lock, 0.6)
    assert scheduled.main() == 0
    holder.join(5)
    assert json.loads(capsys.readouterr().out)["retention"]["status"] == "applied"
    assert not target.exists()


def test_failure_before_active_state_leaves_a_screened_record_in_the_log_dir(entry, monkeypatch):
    retention, root, cfg, cases, write = entry
    private = json.loads((Path(__file__).parent / "fixtures/repair_cases.json")
                         .read_text(encoding="utf-8"))["scheduled_failure"]["private_payload"]

    def fail(*args):
        raise TimeoutError("DATA lock unavailable: synthetic product lock " + private)

    monkeypatch.setattr(scheduled, "execute", fail)
    with pytest.raises(TimeoutError):
        scheduled.main()
    record = (root / "pool/logs/eod-failures.log").read_text(encoding="utf-8")
    assert "stage=execute" in record and "TimeoutError" in record
    assert "scheduled.py" in record and "synthetic product lock" in record
    assert "user1@example.com" not in record
    assert not (root.parent / "local").exists()


def test_refused_admission_is_retried_then_recorded_outside_every_repository(entry, monkeypatch, tmp_path):
    retention, root, cfg, cases, write = entry
    attempts, sleeps = [], []

    def refuse(*args):
        attempts.append(time.monotonic())
        raise data_safety.DestinationError("synthetic admission refusal")

    def refuse_write(path):
        raise data_safety.DestinationError("synthetic log refusal")

    monkeypatch.setattr(scheduled, "preflight", refuse)
    monkeypatch.setattr(data_safety, "authorize_write", refuse_write)
    monkeypatch.setattr(scheduled, "ADMISSION_RETRY_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(scheduled, "ADMISSION_BACKOFF_SECONDS", (0.1, 0.2), raising=False)
    with pytest.raises(data_safety.DestinationError):
        scheduled.main()
    assert len(attempts) >= 2
    note = (tmp_path / "local/demand-mining/eod-exit.log").read_text(encoding="utf-8")
    assert "stage=preflight" in note and "DestinationError: synthetic admission refusal" in note


def test_transient_admission_refusal_recovers_and_the_eod_runs(entry, monkeypatch, capsys):
    retention, root, cfg, cases, write = entry
    real, calls = scheduled.preflight, []

    def flaky(*args):
        calls.append(1)
        if len(calls) < 3:
            raise data_safety.DestinationError("synthetic transient refusal")
        return real(*args)

    monkeypatch.setattr(scheduled, "preflight", flaky)
    monkeypatch.setattr(scheduled, "ADMISSION_BACKOFF_SECONDS", (0.01, 0.02), raising=False)
    monkeypatch.setattr(scheduled, "execute", lambda *a: {"ok": True, "status": "complete"})
    assert scheduled.main() == 0
    assert len(calls) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "complete"


def test_starved_git_child_is_a_refusal_not_an_escaping_subprocess_error(tmp_path, monkeypatch):
    def stall(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(data_safety.subprocess, "run", stall)
    monkeypatch.setattr(data_safety, "_verify_context_environment", lambda *a: None)
    with pytest.raises(RuntimeError, match="timed out"):
        data_safety.git(tmp_path, "remote", "get-url", "--all", "origin")
