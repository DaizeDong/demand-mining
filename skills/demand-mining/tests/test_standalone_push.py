"""Generated regressions for standalone card delivery admission and ownership."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

import lib
import push_card


@pytest.fixture
def card(monkeypatch):
    # Privacy classifier availability is independent of delivery ownership.
    monkeypatch.setattr(push_card, "has_pii", lambda text: False)
    return {"title": "Synthetic export recovery", "canonical_key": "synthetic-export-recovery",
            "product_id": "example-product", "tier": "tier2", "grade": "A", "final_score": 80}


@pytest.mark.parametrize("identifier", [None, "", "   ", False, True, 0, [], {}])
def test_real_send_rejects_missing_stable_identity_before_transport(card, monkeypatch, identifier):
    for field in ("demand_id", "canonical_key", "id"):
        card[field] = identifier
    monkeypatch.delenv("DEMAND_MINING_DRYRUN", raising=False)
    calls = []
    monkeypatch.setattr(push_card, "deliver", lambda *args, **kwargs: calls.append(args) or (True, {}))
    result = push_card.push_card(card)
    assert result["ok"] is False
    assert "identity" in result["detail"]
    assert calls == []


def test_preview_without_identity_needs_no_relay_or_private_state(card, monkeypatch, tmp_path):
    card.pop("canonical_key")
    monkeypatch.setenv("DEMAND_MINING_CONFIG", str(tmp_path / "uninitialized"))
    def forbidden(*args, **kwargs):
        raise AssertionError("preview must not invoke an external relay")
    monkeypatch.setattr(push_card.subprocess, "run", forbidden)
    result = push_card.push_card(card, dry_run=True)
    assert result["ok"] is True
    assert "dry-run" in result["detail"]
    assert not list(tmp_path.iterdir())


from test_run_contract import private_repo, cfg


@pytest.fixture
def event_repo(private_repo, cfg, monkeypatch):
    for name in ("DEMAND_MINING_CONFIG_DIR", "DEMAND_MINING_DATA_DIR", "DEMAND_MINING_PRODUCT"):
        monkeypatch.delenv(name, raising=False)
    (private_repo / "priority.json").write_text(json.dumps(cfg), encoding="utf-8")
    return private_repo


@pytest.fixture
def confirmed(monkeypatch):
    sent = []
    def deliver(message, dry_run=False):
        assert not dry_run
        identity = copy.deepcopy(push_card._DELIVERY_IDENTITY.get())
        sent.append((message, identity))
        return True, {"status": "confirmed", "message_id": "synthetic-message",
                      "identity": identity,
                      "content_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest()}
    monkeypatch.setattr(push_card, "deliver", deliver)
    return sent


def test_existing_delivery_context_keeps_the_callers_owner(card, confirmed, monkeypatch, tmp_path):
    import finalize
    monkeypatch.delenv("DEMAND_MINING_DRYRUN", raising=False)
    monkeypatch.setenv("DEMAND_MINING_CONFIG", str(tmp_path / "uninitialized"))
    card.pop("canonical_key")
    identity = {"product_id": "example-caller", "date": "2032-02-02", "run": "synthetic-run"}
    def forbidden(*args, **kwargs):
        raise AssertionError("an existing delivery owner must not enter standalone admission")
    monkeypatch.setattr(lib, "load_config", forbidden)
    monkeypatch.setattr(finalize, "deliver_event", forbidden)
    with push_card.delivery_context(identity):
        assert push_card.push_card(card)["ok"] is True
    assert confirmed[0][1] == identity
    assert not list(tmp_path.iterdir())


def test_standalone_revision_cannot_replace_an_existing_owner(card, confirmed, monkeypatch):
    monkeypatch.delenv("DEMAND_MINING_DRYRUN", raising=False)
    def forbidden():
        raise AssertionError("revision conflict must precede config lookup")
    monkeypatch.setattr(lib, "load_config", forbidden)
    with push_card.delivery_context({"run": "synthetic-run"}):
        with pytest.raises(ValueError, match="revision"):
            push_card.push_card(card, update=True, revision="release-1")
    assert confirmed == []


@pytest.mark.parametrize("alias", ["demand_id", "canonical_key", "id"])
@pytest.mark.parametrize("identifier,expected", [(" Synthetic Exact Card ", " Synthetic Exact Card "), (42, "42")])
def test_stable_alias_replay_uses_one_durable_owner(event_repo, card, confirmed, monkeypatch, alias, identifier, expected):
    card.pop("canonical_key")
    card[alias] = identifier
    first = push_card.push_card(card)
    monkeypatch.setenv("DEMAND_MINING_NOW", "2032-02-02T12:00:00Z")
    second = push_card.push_card(card)
    assert first["ok"] and second["ok"]
    assert len(confirmed) == 1
    assert confirmed[0][1] == {"product_id": "example-product", "card_id": expected, "event": "new"}
    assert second["state_path"] == first["state_path"]
    state = json.loads(Path(first["state_path"]).read_text(encoding="utf-8"))
    assert state["phase"] == "complete"
    assert state["delivery"]["message_id"] == "synthetic-message"


def test_new_update_and_product_are_distinct_events(event_repo, cfg, card, confirmed):
    results = [push_card.push_card(card), push_card.push_card(card, update=True)]
    cfg["product_id"] = card["product_id"] = "second-example-product"
    (event_repo / "priority.json").write_text(json.dumps(cfg), encoding="utf-8")
    results.append(push_card.push_card(card))
    assert all(row["ok"] for row in results)
    assert len(confirmed) == 3
    assert len({row["state_path"] for row in results}) == 3


@pytest.mark.parametrize("revision", [None, "release-1"])
def test_same_identity_changed_content_cannot_create_another_send(event_repo, card, confirmed, revision):
    first = push_card.push_card(card, update=revision is not None, revision=revision)
    card["title"] = "Synthetic changed export recovery"
    with pytest.raises(ValueError, match="content"):
        push_card.push_card(card, update=revision is not None, revision=revision)
    assert len(confirmed) == 1
    assert Path(first["state_path"]).is_file()


def test_distinct_stable_update_revisions_send_once_each(event_repo, card, confirmed):
    paths = []
    for revision in ("release-1", "release-2"):
        card["title"] = "Synthetic export recovery " + revision
        first = push_card.push_card(card, update=True, revision=revision)
        second = push_card.push_card(card, update=True, revision=revision)
        assert first["ok"] and second["ok"]
        assert first["state_path"] == second["state_path"]
        assert first["identity"] == {"product_id": "example-product",
                                     "card_id": "synthetic-export-recovery",
                                     "event": "update", "revision": revision}
        paths.append(first["state_path"])
    assert len(set(paths)) == len(confirmed) == 2


@pytest.mark.parametrize("update,revision", [(True, ""), (True, "  "), (True, 42),
                                             (True, False), (False, "release-1")])
def test_revision_requires_an_explicit_nonempty_update_token(event_repo, card, confirmed, update, revision):
    with pytest.raises(ValueError, match="revision"):
        push_card.push_card(card, update=update, revision=revision)
    assert confirmed == []
    assert not (event_repo / "pool").exists()


@pytest.mark.parametrize("kind", ["timeout", "nonzero", "plain", "missing_id", "wrong_identity", "wrong_hash"])
@pytest.mark.parametrize("revision", [None, "release-1"])
def test_unknown_adapter_result_is_never_automatically_retried(event_repo, card, monkeypatch, kind, revision):
    sent = []
    def deliver(message, dry_run=False):
        sent.append(message)
        if kind == "timeout":
            raise TimeoutError("synthetic uncertain transport")
        receipt = {"status": "confirmed", "message_id": "synthetic-message",
                   "identity": copy.deepcopy(push_card._DELIVERY_IDENTITY.get()),
                   "content_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest()}
        if kind == "plain":
            return True, "exit zero only"
        if kind == "missing_id":
            receipt.pop("message_id")
        if kind == "wrong_identity":
            receipt["identity"] = {"product_id": "unrelated-example"}
        if kind == "wrong_hash":
            receipt["content_sha256"] = "0" * 64
        return kind != "nonzero", receipt
    monkeypatch.setattr(push_card, "deliver", deliver)
    first = push_card.push_card(card, update=revision is not None, revision=revision)
    second = push_card.push_card(card, update=revision is not None, revision=revision)
    assert first["ok"] is second["ok"] is False
    assert first["status"] == second["status"] == "pending_reconciliation"
    assert len(sent) == 1
    assert json.loads(Path(first["state_path"]).read_text())["phase"] == "delivery_unknown"


@pytest.mark.parametrize("revision", [None, "release-1"])
def test_matching_reconciliation_completes_without_resend(event_repo, card, monkeypatch, revision):
    import finalize
    sent = []
    monkeypatch.setattr(push_card, "deliver", lambda message, **kwargs: sent.append(message) or (False, {}))
    pending = push_card.push_card(card, update=revision is not None, revision=revision)
    state = json.loads(Path(pending["state_path"]).read_text())
    receipt = {"status": "confirmed", "message_id": "synthetic-reconciled",
               "identity": state["identity"], "content_sha256": state["content_sha256"]}
    assert finalize.reconcile_event(pending["state_path"], receipt) == receipt
    assert push_card.push_card(card, update=revision is not None, revision=revision)["ok"] is True
    assert len(sent) == 1


@pytest.mark.parametrize("contradict", [False, True])
def test_receipt_survives_interruption_before_complete_state(event_repo, card, confirmed, monkeypatch, contradict):
    import finalize
    write = finalize.atomic_json
    def interrupted(path, value):
        if Path(path).name == "state.json" and value.get("phase") == "complete":
            raise OSError("synthetic interruption after confirmed receipt")
        return write(path, value)
    monkeypatch.setattr(finalize, "atomic_json", interrupted)
    with pytest.raises(OSError, match="interruption"):
        push_card.push_card(card)
    state_path, = (event_repo / "pool/card-deliveries").glob("*/state.json")
    receipt_path = state_path.with_name("receipt.json")
    original = receipt_path.read_bytes()
    monkeypatch.setattr(finalize, "atomic_json", write)
    if contradict:
        replacement = json.loads(original)
        replacement["message_id"] = "different-synthetic-message"
        with pytest.raises(ValueError, match="replace|differ|contradict"):
            finalize.reconcile_event(state_path, replacement)
        assert receipt_path.read_bytes() == original
    assert push_card.push_card(card)["ok"] is True
    assert len(confirmed) == 1


@pytest.mark.parametrize("damage", ["missing_state", "missing_receipt", "receipt_identity", "malformed_state"])
def test_damaged_durable_evidence_never_opens_a_new_send(event_repo, card, confirmed, damage):
    first = push_card.push_card(card)
    state_path = Path(first["state_path"])
    receipt_path = state_path.with_name("receipt.json")
    if damage == "missing_state":
        state_path.unlink()
    elif damage == "missing_receipt":
        receipt_path.unlink()
    elif damage == "receipt_identity":
        receipt = json.loads(receipt_path.read_text())
        receipt["identity"] = {"product_id": "unrelated-example"}
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    else:
        state_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        push_card.push_card(card)
    assert len(confirmed) == 1


def test_concurrent_callers_share_the_real_event_lock(event_repo, card, confirmed, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import time
    original = push_card.deliver
    def slow(message, dry_run=False):
        time.sleep(0.1)
        return original(message, dry_run=dry_run)
    monkeypatch.setattr(push_card, "deliver", slow)
    start = Barrier(2)
    def invoke():
        start.wait(timeout=5)
        return push_card.push_card(copy.deepcopy(card))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert all(row["ok"] for row in results)
    assert len(confirmed) == 1


@pytest.mark.parametrize("setting", ["missing_product", "wrong_product"])
def test_card_cannot_choose_or_override_configured_product(event_repo, cfg, card, confirmed, setting):
    if setting == "missing_product":
        cfg.pop("product_id")
    else:
        card["product_id"] = "unrelated-example-product"
    (event_repo / "priority.json").write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="product"):
        push_card.push_card(card)
    assert confirmed == []
    assert not (event_repo / "pool").exists()


@pytest.mark.parametrize("revision", [None, "release-1"])
def test_direct_cli_uses_the_same_durable_owner(event_repo, card, confirmed, monkeypatch, capsys, revision):
    import io
    import types
    card["_update"] = revision is not None
    arguments = [] if revision is None else ["--revision", revision]
    for _ in range(2):
        monkeypatch.setattr(push_card.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(json.dumps(card).encode())))
        assert push_card.main(arguments) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["ok"] is True
        assert result["identity"].get("revision") == revision
    assert len(confirmed) == 1


def test_environment_preview_does_not_reserve_the_later_real_event(event_repo, card, confirmed, monkeypatch):
    actual = push_card.deliver
    def deliver(message, dry_run=False):
        return (True, "[dry-run] synthetic preview") if dry_run else actual(message)
    monkeypatch.setattr(push_card, "deliver", deliver)
    monkeypatch.setenv("DEMAND_MINING_DRYRUN", "1")
    assert push_card.push_card(card)["ok"]
    assert not (event_repo / "pool").exists()
    assert confirmed == []
    monkeypatch.delenv("DEMAND_MINING_DRYRUN")
    assert push_card.push_card(card)["ok"]
    assert len(confirmed) == 1


@pytest.mark.parametrize("field", ["identity", "message", "revision"])
def test_egress_refusal_precedes_event_reservation(event_repo, card, confirmed, monkeypatch, field):
    revision = None
    if field == "identity":
        card["canonical_key"] = "synthetic-private-identity"
        needle = card["canonical_key"]
    elif field == "revision":
        revision = needle = "synthetic-private-revision"
    else:
        needle = "Weighted RICE:"
    monkeypatch.setattr(push_card, "has_pii", lambda text: needle in text)
    with pytest.raises(ValueError, match="DLP"):
        push_card.push_card(card, update=revision is not None, revision=revision)
    assert confirmed == []
    assert not (event_repo / "pool").exists()


def test_standalone_event_never_invokes_scheduled_finalization(event_repo, card, confirmed, monkeypatch):
    import finalize
    def forbidden(*args, **kwargs):
        raise AssertionError("standalone card delivery must not own a scheduled ledger or watermark")
    monkeypatch.setattr(finalize, "finalize", forbidden)
    monkeypatch.setattr(finalize.dd, "LedgerClient", forbidden)
    assert push_card.push_card(card)["ok"]
    assert len(confirmed) == 1
    assert not (event_repo / "pool/runs").exists()


def test_held_event_lock_never_falls_through_to_an_unlocked_call(event_repo, card, confirmed, monkeypatch):
    import finalize
    from data_safety import file_lock
    first = push_card.push_card(card)
    lock = Path(first["state_path"]).with_name(".lock")
    monkeypatch.setattr(finalize, "file_lock", lambda path: file_lock(path, timeout=0))
    with file_lock(lock):
        with pytest.raises(TimeoutError, match="lock unavailable"):
            push_card.push_card(card)
    assert len(confirmed) == 1


def test_ignored_durable_event_is_refused_before_transport(event_repo, card, confirmed, monkeypatch):
    import data_safety
    import types
    boundary = data_safety._storage_contract().load_boundary()
    original = boundary.read_private_companion_git
    def ignored(snapshot, *arguments):
        if arguments[:4] == ("check-ignore", "--no-index", "-q", "--") and arguments[-1].endswith("/state.json"):
            return types.SimpleNamespace(returncode=0, stdout="")
        return original(snapshot, *arguments)
    monkeypatch.setattr(boundary, "read_private_companion_git", ignored)
    with pytest.raises(data_safety.DestinationError, match="ignored"):
        push_card.push_card(card)
    assert confirmed == []
    assert not (event_repo / "pool").exists()


def test_event_contract_does_not_admit_arbitrary_siblings(event_repo, card, confirmed):
    import data_safety
    first = push_card.push_card(card)
    target = Path(first["state_path"]).with_name("undeclared.json")
    with pytest.raises(data_safety.DestinationError, match="exactly one owner"):
        data_safety.atomic_json(target, {"synthetic": True})
    assert not target.exists()
