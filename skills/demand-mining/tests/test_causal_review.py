"""Reproduce privacy, archive-completeness and interrupted-receipt defects."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

import digest
import finalize
import push_card
import redact
import run
from test_run_contract import Ledger, cases, cfg, private_repo


@pytest.mark.parametrize("index", range(12))
def test_explicit_personal_context_stays_inside_privacy_boundary(cases, index):
    case = cases["causal_review"]["personal_contexts"][index]
    result = redact.redact(case["text"])
    assert case["marker"] not in result["redacted"]
    if result["review_required"]:
        with pytest.raises(redact.PrivacyReviewRequired):
            redact.safe_text(case["text"])
        with pytest.raises(redact.PrivacyReviewRequired):
            redact.safe_data({"evidence": [{"note": case["text"]}]})
    else:
        assert case["marker"] not in redact.safe_text(case["text"])
        assert case["marker"] not in json.dumps(redact.safe_data({"evidence": [case["text"]]}))


@pytest.mark.parametrize("index", range(4))
def test_product_field_requests_remain_usable(cases, index):
    text = cases["causal_review"]["product_texts"][index]
    assert redact.safe_text(text) == text


def test_archive_contains_demand_details_and_each_evidence_source(cases, cfg):
    card = cases["causal_review"]["archive_card"]
    markdown = digest.build_markdown([card], date="2026-01-17", cfg=cfg)
    for name in ("why", "recommendation", "inferred_job"):
        assert card[name] in markdown
    for evidence in card["evidence"]:
        for name in ("redacted_snippet", "source", "channel", "ts"):
            assert evidence[name] in markdown
    for item in card["extension"]["proposed_checks"]:
        assert item in markdown


def test_archive_preserves_full_nested_record_and_escapes_markup(cases, cfg):
    card = copy.deepcopy(cases["causal_review"]["archive_card"])
    card["extension"]["markup"] = cases["causal_review"]["markup_text"]
    markdown = digest.build_markdown([card], date="2026-01-17", cfg=cfg)
    # The full private record must remain reconstructable, including fields the
    # rank summary does not know. Payload lines must not become Markdown headings.
    blocks = []
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.endswith("json") and line[:-4] and set(line[:-4]) == {"`"}:
            end = lines.index(line[:-4], index + 1)
            blocks.append(json.loads("\n".join(lines[index + 1:end])))
    assert redact.safe_data(card) in blocks
    assert "# synthetic injected heading" not in lines
    assert "<script>synthetic</script>" not in lines


def test_archive_is_stable_and_does_not_expand_delivered_headlines(cases, cfg):
    card = copy.deepcopy(cases["causal_review"]["archive_card"])
    other = copy.deepcopy(card)
    other["canonical_key"] = "later-export-check"
    other["title"] = "verify export completeness"
    other["final_score"] = 75
    first = digest.build_markdown([card, other], date="2026-01-17", cfg=cfg)
    second = digest.build_markdown([other, card], date="2026-01-17", cfg=cfg)
    assert first == second
    headline = digest.build_headlines([card], date="2026-01-17", cfg=cfg)
    assert all(e["redacted_snippet"] not in headline for e in card["evidence"])


def _interrupted_receipt_run(private_repo, cfg, cases, monkeypatch):
    ledger, calls = Ledger(), []
    identity = finalize.logical_identity(cfg)
    original = finalize.atomic_json

    class InterruptedAfterReceipt(BaseException):
        pass

    def confirm(message, dry_run=False):
        calls.append(message)
        return True, {"status": "confirmed", "message_id": cases["causal_review"]["receipt_id"],
                      "identity": push_card._DELIVERY_IDENTITY.get(),
                      "content_sha256": hashlib.sha256(message.encode()).hexdigest()}

    def interrupted(path, value):
        result = original(path, value)
        if Path(path).name == "receipt.json":
            raise InterruptedAfterReceipt()
        return result

    monkeypatch.setattr(push_card, "deliver", confirm)
    monkeypatch.setattr(finalize, "atomic_json", interrupted)
    archive = private_repo / "pool"
    with pytest.raises(InterruptedAfterReceipt):
        run.process([cases["candidate"]], cfg, ledger, archive_dir=str(archive), identity=identity)
    monkeypatch.setattr(finalize, "atomic_json", original)
    run_dir = archive / "runs" / finalize.digest(identity)
    assert json.loads((run_dir / "state.json").read_text())["phase"] == "delivery_unknown"
    assert not ledger.watermarks and len(calls) == 1
    return ledger, calls, identity, archive, run_dir


def test_saved_confirmed_receipt_completes_restart_without_another_send(private_repo, cfg, cases, monkeypatch):
    ledger, calls, identity, archive, run_dir = _interrupted_receipt_run(private_repo, cfg, cases, monkeypatch)
    result = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(archive), identity=identity)
    assert result["status"] == "complete"
    assert result["delivery"]["message_id"] == cases["causal_review"]["receipt_id"]
    again = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(archive), identity=identity)
    assert again["status"] == "complete"
    assert len(calls) == len(ledger.watermarks) == 1


@pytest.mark.parametrize("fault", ["missing", "invalid_json", "unconfirmed", "identity", "hash", "missing_message"])
def test_unusable_saved_receipt_never_resends_or_advances_cursor(private_repo, cfg, cases, monkeypatch, fault):
    ledger, calls, identity, archive, run_dir = _interrupted_receipt_run(private_repo, cfg, cases, monkeypatch)
    path = run_dir / "receipt.json"
    receipt = json.loads(path.read_text())
    if fault == "missing":
        path.unlink()
    elif fault == "invalid_json":
        path.write_text("{", encoding="utf-8")
    else:
        if fault == "unconfirmed":
            receipt["status"] = "unknown"
        elif fault == "identity":
            receipt["identity"]["product_id"] = "different-synthetic-product"
        elif fault == "hash":
            receipt["content_sha256"] = "0" * 64
        else:
            receipt.pop("message_id")
        path.write_text(json.dumps(receipt), encoding="utf-8")
    try:
        result = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(archive), identity=identity)
    except ValueError:
        pass
    else:
        assert result["status"] == "pending_reconciliation"
    assert len(calls) == 1 and not ledger.watermarks
