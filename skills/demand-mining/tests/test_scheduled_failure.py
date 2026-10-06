"""Generated controls for bounded scheduled failure evidence; all effects are synthetic."""
import json
from pathlib import Path
import sys
import types
import urllib.error

import pytest

import scheduled
from test_run_contract import cases, cfg, private_repo
from test_run_contract import Ledger, _confirm

REAL_PROPOSE = scheduled.propose
REAL_PROCESS = scheduled.run.process

@pytest.fixture
def scheduled_caller(private_repo, cfg, cases, monkeypatch):
    monkeypatch.setattr(scheduled, "preflight", lambda *a: (
        cfg, private_repo, private_repo / "pool", private_repo / "logs"))
    monkeypatch.setattr(scheduled.pull_discord, "_load_wiring", lambda: ([], "FAKE_CANARY_TOKEN"))
    monkeypatch.setattr(scheduled.pull_discord, "pull", lambda channels, token, source_window: {
        "collection": {"status": "complete", "source_window": source_window}})
    monkeypatch.setattr(scheduled, "propose", lambda *a: [])
    monkeypatch.setattr(scheduled.dedup, "LedgerClient", lambda **k: pytest.fail("unexpected ledger access"))
    monkeypatch.setattr(scheduled.run, "process", lambda *a, **k: pytest.fail("unexpected finalizer access"))
    return private_repo


def persisted_caller(root):
    paths = list((root / "pool/scheduled").glob("*/*/caller.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


@pytest.mark.parametrize("stage,kind,category", [
    ("collection_config", "missing", "config_missing"),
    ("collection_config", "invalid", "config_invalid"),
    ("collection", "timeout", "timeout"),
    ("collection", "http403", "http_access_denied"),
    ("collection", "http404", "http_source_unavailable"),
    ("collection", "http429", "http_rate_limited"),
    ("collection", "http503", "http_server_error"),
    ("collection", "url", "transport_error"),
    ("collection", "unknown", "unknown"),
    ("classification", "unknown", "unknown"),
])
def test_original_exception_propagates_with_private_enum_evidence(
        scheduled_caller, cases, monkeypatch, stage, kind, category):
    payload = cases["scheduled_failure"]["private_payload"]
    if kind == "missing":
        failure = FileNotFoundError(payload)
    elif kind == "invalid":
        failure = ValueError(payload)
    elif kind == "timeout":
        failure = RuntimeError(payload)
        failure.__cause__ = urllib.error.URLError(TimeoutError(payload))
    elif kind.startswith("http"):
        failure = RuntimeError(payload)
        failure.__cause__ = urllib.error.HTTPError(
            "https://example.com/synthetic", int(kind[4:]), payload, {}, None)
    elif kind == "url":
        failure = urllib.error.URLError(payload)
    else:
        failure = RuntimeError(payload)

    def fail(*a, **k):
        raise failure

    target, name = ((scheduled.pull_discord, "_load_wiring") if stage == "collection_config"
                    else (scheduled.pull_discord, "pull") if stage == "collection"
                    else (scheduled, "propose"))
    monkeypatch.setattr(target, name, fail)
    before = Path.cwd()
    with pytest.raises(type(failure)) as caught:
        scheduled.execute(backup_enabled=False)
    assert caught.value is failure and Path.cwd() == before
    caller = persisted_caller(scheduled_caller)
    assert (caller["status"], caller["error_type"], caller["error_stage"], caller["error_category"]) == (
        "failed", type(failure).__name__, stage, category)
    for path in (scheduled_caller / "pool/scheduled").rglob("*.json"):
        content = path.read_text(encoding="utf-8")
        assert "FAKE_CANARY_TOKEN" not in content and "user1@example.com" not in content
        assert "synthetic feedback body" not in content and "Traceback" not in content
    assert not list((scheduled_caller / "pool/scheduled").rglob("handoff.json"))


@pytest.mark.parametrize("reasons,category", [
    (["budget_exhausted"], "llm_budget_exhausted"),
    (["timeout", "budget_exhausted"], "llm_timeout"),
    (["not_installed"], "llm_not_installed"),
    (["process_cleanup_failed"], "llm_process_cleanup_failed"),
    (["policy_refusal", "group_already_refused"], "llm_policy_refusal"),
    (["error"], "unknown"),
    ([], "unknown"),
    ([None], "unknown"),
])
def test_llm_failure_uses_emitted_codes_without_provider_text(
        scheduled_caller, cases, monkeypatch, reasons, category):
    payload = cases["scheduled_failure"]["private_payload"]
    response = types.SimpleNamespace(error=payload, text=payload, attempts=[
        types.SimpleNamespace(ok=False, reason=reason, error=payload, provider=payload)
        for reason in reasons])
    fake = types.ModuleType("llmcall")
    fake.call = lambda prompt, **kwargs: response
    monkeypatch.setitem(sys.modules, "llmcall", fake)
    monkeypatch.setattr(scheduled, "propose", REAL_PROPOSE)
    with pytest.raises(RuntimeError, match="did not complete"):
        scheduled.execute(backup_enabled=False)
    caller = persisted_caller(scheduled_caller)
    assert caller["error_stage"] == "classification" and caller["error_category"] == category
    content = json.dumps(caller)
    assert "FAKE_CANARY_TOKEN" not in content and "user1@example.com" not in content


def test_unknown_attempt_code_cannot_become_persisted_text(scheduled_caller, cases, monkeypatch):
    payload = cases["scheduled_failure"]["unknown_reason"]
    response = types.SimpleNamespace(error=payload, attempts=[
        types.SimpleNamespace(ok=False, reason=payload), types.SimpleNamespace(ok=False, reason={})])
    fake = types.ModuleType("llmcall")
    fake.call = lambda prompt, **kwargs: response
    monkeypatch.setitem(sys.modules, "llmcall", fake)
    monkeypatch.setattr(scheduled, "propose", REAL_PROPOSE)
    with pytest.raises(RuntimeError):
        scheduled.execute(backup_enabled=False)
    caller = persisted_caller(scheduled_caller)
    assert caller["error_category"] == "unknown"
    assert "FAKE_CANARY_REASON" not in json.dumps(caller)


@pytest.mark.parametrize("stage", ["ledger", "finalization"])
def test_later_operation_failure_keeps_original_exception(scheduled_caller, monkeypatch, stage):
    failure = RuntimeError("FAKE_CANARY_TOKEN")
    def fail(*a, **k):
        raise failure
    ledger = types.SimpleNamespace(init=fail if stage == "ledger" else lambda: None)
    monkeypatch.setattr(scheduled.dedup, "LedgerClient", lambda **k: ledger)
    monkeypatch.setattr(scheduled.run, "process", fail)
    with pytest.raises(RuntimeError) as caught:
        scheduled.execute(backup_enabled=False)
    assert caught.value is failure
    caller = persisted_caller(scheduled_caller)
    assert caller["error_stage"] == stage and caller["error_category"] == "unknown"


@pytest.mark.parametrize("phase", ["backup_delivery", "backup_completion"])
def test_backup_failure_keeps_pending_result_and_enum_evidence(
        scheduled_caller, cases, monkeypatch, phase):
    import backup
    ledger, backups, sends = Ledger(), [], []
    ledger.init = lambda: None
    monkeypatch.setattr(scheduled, "propose", lambda *a: [cases["candidate"]])
    monkeypatch.setattr(scheduled.dedup, "LedgerClient", lambda **k: ledger)
    monkeypatch.setattr(scheduled.run, "process", REAL_PROCESS)
    monkeypatch.setattr(scheduled.push_card, "deliver", lambda message, dry_run=False: (
        sends.append(message), _confirm(message))[1])
    def save(paths):
        backups.append(paths)
        if len(backups) == (1 if phase == "backup_delivery" else 2):
            raise TimeoutError(cases["scheduled_failure"]["private_payload"])
        return {"status": "confirmed"}
    monkeypatch.setattr(backup, "backup", save)
    result = scheduled.execute()
    assert result["status"] == "delivered_backup_pending" and result["ok"] is False
    assert len(sends) == 1
    failed = persisted_caller(scheduled_caller)["backup"]
    assert (failed["error_type"], failed["error_stage"], failed["error_category"]) == (
        "TimeoutError", phase, "timeout")
    assert "FAKE_CANARY_TOKEN" not in json.dumps(failed)


def test_failed_collection_retry_clears_failure_fields_without_new_identity(
        scheduled_caller, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("FAKE_CANARY_TOKEN")
    monkeypatch.setattr(scheduled.pull_discord, "pull", fail)
    with pytest.raises(RuntimeError):
        scheduled.execute(backup_enabled=False)
    failed = persisted_caller(scheduled_caller)
    monkeypatch.setattr(scheduled.pull_discord, "pull", lambda channels, token, source_window: {
        "collection": {"status": "complete", "source_window": source_window}})
    monkeypatch.setattr(scheduled.dedup, "LedgerClient", lambda **k: types.SimpleNamespace(init=lambda: None))
    monkeypatch.setattr(scheduled.run, "process", lambda *a, **k: {"status": "pending_reconciliation"})
    assert scheduled.execute(backup_enabled=False)["status"] == "pending_reconciliation"
    retried = persisted_caller(scheduled_caller)
    assert retried["identity"] == failed["identity"] and retried["attempt_id"] != failed["attempt_id"]
    assert all(field not in retried for field in ("error_type", "error_stage", "error_category"))
