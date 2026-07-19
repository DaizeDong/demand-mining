"""Adaptive self-refine classification chain: codex drafts, a DIFFERENT model audits, and the loop
iterates ONLY while a verdict is borderline, stopping the moment the audit stops changing anything.
Depth is data-driven: a clear batch costs one pass, an ambiguous one earns extra scrutiny.
"""
import json
from unittest import mock

import pytest

pytest.importorskip("discord")
import demand_bot as b  # noqa: E402


def test_uncertain_band():
    assert b._uncertain({"confidence": 0.5})
    assert not b._uncertain({"confidence": 0.95})   # confidently a demand
    assert not b._uncertain({"confidence": 0.1})    # confidently not
    assert not b._uncertain({"confidence": "nan"})  # unparseable -> not uncertain


def test_verdicts_stable_ignores_prose_but_catches_substance():
    a = [{"i": 0, "is_demand": True, "confidence": 0.8}]
    assert b._verdicts_stable(a, [{"i": 0, "is_demand": True, "confidence": 0.82, "title": "x"}])
    assert not b._verdicts_stable(a, [{"i": 0, "is_demand": False, "confidence": 0.8}])   # flip
    assert not b._verdicts_stable(a, [{"i": 0, "is_demand": True, "confidence": 0.5}])    # band move
    assert not b._verdicts_stable(a, a + [{"i": 1, "is_demand": True, "confidence": 0.8}])  # key set


def _patch_llm(outputs):
    seq = iter(outputs)
    calls = []

    def fake(prompt, timeout=90, chain=b._DEFAULT_CHAIN):
        calls.append(chain)
        return next(seq)
    return fake, calls


def test_clear_batch_is_single_pass():
    fake, calls = _patch_llm([
        json.dumps([{"i": 0, "is_demand": True, "confidence": 0.99, "title": "t",
                     "track": "x", "kano": "performance", "why": "w"}]),
    ])
    with mock.patch.object(b, "_llm", fake):
        v = b.classify_batch([{"i": 0, "channel": "c", "text": "broken"}], b._classify_sys("P"), "P", 2)
    assert v[0]["is_demand"] is True
    assert len(calls) == 1  # 0.99 is not borderline -> no audit round


def test_borderline_triggers_cross_model_audit_and_takes_revision():
    fake, calls = _patch_llm([
        json.dumps([{"i": 0, "is_demand": True, "confidence": 0.6, "title": "maybe",
                     "track": "x", "kano": "performance", "why": "w"}]),   # codex draft (borderline)
        json.dumps([{"i": 0, "is_demand": False, "confidence": 0.55, "title": "",
                     "track": "x", "kano": "indifferent", "why": "not really"}]),  # audit flips it
    ])
    with mock.patch.object(b, "_llm", fake):
        v = b.classify_batch([{"i": 0, "channel": "c", "text": "kinda wish"}], b._classify_sys("P"), "P", 2)
    assert len(calls) == 2                 # generate + exactly one audit (max_rounds=2)
    assert calls[1][0] == "cc"             # audit ran on a DIFFERENT model first (independence)
    assert v[0]["is_demand"] is False      # the audit's revision won


def test_audit_agreement_stops_early():
    draft = [{"i": 0, "is_demand": True, "confidence": 0.6, "title": "x",
              "track": "x", "kano": "performance", "why": "w"}]
    # three rounds allowed, but the auditor agrees on the first audit -> stop at 2 calls, not 3
    fake, calls = _patch_llm([json.dumps(draft), json.dumps(draft), json.dumps(draft)])
    with mock.patch.object(b, "_llm", fake):
        b.classify_batch([{"i": 0, "channel": "c", "text": "kinda wish"}], b._classify_sys("P"), "P", 3)
    assert len(calls) == 2


def test_rounds_one_disables_audit():
    fake, calls = _patch_llm([
        json.dumps([{"i": 0, "is_demand": True, "confidence": 0.6, "title": "x",
                     "track": "x", "kano": "performance", "why": "w"}]),
    ])
    with mock.patch.object(b, "_llm", fake):
        b.classify_batch([{"i": 0, "channel": "c", "text": "kinda wish"}], b._classify_sys("P"), "P", 1)
    assert len(calls) == 1  # max_rounds=1 -> never audits even when borderline
