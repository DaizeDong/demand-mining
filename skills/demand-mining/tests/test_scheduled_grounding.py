"""Scheduled grounding fails closed per quote, not per day; rejected replies carry an enum reason.

All text here is self-evidently synthetic (Acme / FAKE_CANARY); the candidate shape comes from
the generated repair_cases fixture."""
import copy
import json
import sys
import types

import pytest

import redact
import scheduled
from test_run_contract import cases, cfg, private_repo
from test_scheduled_failure import REAL_PROPOSE, persisted_caller, scheduled_caller

CORPUS_TS = "2026-01-15T12:00:00.123000+00:00"
OTHER_TS = "2026-01-15T13:30:00.000000+00:00"
GROUNDED = "Acme export keeps failing on large reports, please add retry support."
OTHER = "FAKE_CANARY_ROW the Acme dashboard loads slowly every morning."


def _corpus():
    return {"channels": {"feedback": [
        {"text": GROUNDED, "ts": CORPUS_TS, "author_hash": redact.pseudonymize("acme-user-1")},
        {"text": OTHER, "ts": OTHER_TS, "author_hash": redact.pseudonymize("acme-user-2")},
    ]}, "collection": {"status": "complete"}}


def _install_agent(monkeypatch, candidates, classification="complete"):
    llmcall = types.ModuleType("llmcall")
    llmcall.call = lambda prompt, **kwargs: types.SimpleNamespace(
        error=None, data={"classification": classification, "candidates": candidates})
    monkeypatch.setitem(sys.modules, "llmcall", llmcall)


def _candidate(cases, evidence, title="reliable acme export"):
    candidate = copy.deepcopy(cases["candidate"])
    candidate["title"] = title
    candidate["evidence"] = evidence
    return candidate


def test_one_ungrounded_quote_does_not_discard_the_grounded_candidates(cfg, cases, monkeypatch):
    grounded = _candidate(cases, [
        # The model rewrote the corpus time (no fractional seconds, Z suffix): same instant.
        {"channel": "feedback", "redacted_snippet": "please add retry support",
         "ts": "2026-01-15T12:00:00.123Z"},
        # A paraphrase that is not in the corpus: this quote alone is dropped.
        {"channel": "feedback", "redacted_snippet": "exports should retry automatically",
         "ts": CORPUS_TS},
    ])
    invented = _candidate(cases, [
        {"channel": "feedback", "redacted_snippet": "Acme needs a dark mode toggle"}], "dark mode")
    _install_agent(monkeypatch, [grounded, invented])
    report = {}
    proposed = scheduled.propose(_corpus(), cfg, report)
    assert [item["title"] for item in proposed] == ["reliable acme export"]
    assert [item["redacted_snippet"] for item in proposed[0]["evidence"]] == [GROUNDED]
    assert report == {"proposed": 2, "kept": 1, "candidates_dropped": 1,
                      "evidence_proposed": 3, "evidence_dropped": 2}


def test_malformed_discarded_fields_do_not_trip_the_privacy_screen(cfg, cases, monkeypatch):
    candidate = _candidate(cases, [
        {"channel": "feedback", "redacted_snippet": "please add retry support",
         "ts": "around noon", "author_hash": None, "origin_type": "internal"}])
    candidate["authors"] = [{"author_hash": None}]
    _install_agent(monkeypatch, [candidate])
    proposed = scheduled.propose(_corpus(), cfg)
    assert len(proposed) == 1
    assert proposed[0]["authors"] == [{"author_hash": redact.pseudonymize("acme-user-1"),
                                       "urgency": "need", "segment": "free"}]
    assert proposed[0]["evidence"][0]["ts"] == CORPUS_TS


def test_wrong_time_still_does_not_ground(cfg, cases, monkeypatch):
    _install_agent(monkeypatch, [_candidate(cases, [
        {"channel": "feedback", "redacted_snippet": "please add retry support", "ts": OTHER_TS}])])
    with pytest.raises(ValueError) as caught:
        scheduled.propose(_corpus(), cfg)
    assert caught.value._scheduled_error_category == "agent_ungrounded"


@pytest.mark.parametrize("classification,candidates,category", [
    ("complete", "FAKE_CANARY_NOT_A_LIST", "agent_malformed"),
    ("partial", [], "agent_incomplete"),
    ("complete", None, "agent_ungrounded"),
])
def test_rejected_reply_is_recorded_with_an_enum_reason(
        scheduled_caller, cfg, cases, monkeypatch, classification, candidates, category):
    if candidates is None:
        candidates = [_candidate(cases, [
            {"channel": "feedback", "redacted_snippet": "FAKE_CANARY_QUOTE not in the corpus"}])]
    # The shared fixture stubs propose out; this test needs the real one.
    monkeypatch.setattr(scheduled, "propose", REAL_PROPOSE)
    monkeypatch.setattr(scheduled.pull_discord, "pull", lambda channels, token, source_window: {
        **_corpus(), "collection": {"status": "complete", "source_window": source_window}})
    _install_agent(monkeypatch, candidates, classification)
    with pytest.raises(ValueError):
        scheduled.execute(backup_enabled=False)
    caller = persisted_caller(scheduled_caller)
    assert (caller["error_type"], caller["error_stage"], caller["error_category"]) == (
        "ValueError", "classification", category)
    for path in (scheduled_caller / "pool/scheduled").rglob("*.json"):
        assert "FAKE_CANARY" not in path.read_text(encoding="utf-8")
    if category == "agent_ungrounded":
        assert caller["grounding"] == {"proposed": 1, "kept": 0, "candidates_dropped": 1,
                                       "evidence_proposed": 1, "evidence_dropped": 1}
