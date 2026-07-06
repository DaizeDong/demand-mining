#!/usr/bin/env python3
"""Regression guard for v0.1.2 fixes.

D7/D2: the redactor missed full-width / ideographic-dot obfuscated emails (a realistic input for a
CJK-targeted product). NFKC normalization + confusable-dot folding must now catch them.
D3 (T7): the catch-up backfill must be reachable via `run.py --catch-up` and must not block on stdin.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import redact  # noqa: E402
import run  # noqa: E402


def test_fullwidth_email_is_caught():
    r = redact.redact("联系 ｊｏｈｎ＠ｅｖｉｌ．ｃｏｍ 谢谢")
    assert r["found"].get("EMAIL") and redact.has_pii("ｊｏｈｎ＠ｅｖｉｌ．ｃｏｍ")


def test_ideographic_dot_email_is_caught():
    r = redact.redact("mail me at bob@host。com")
    assert r["found"].get("EMAIL") and redact.has_pii("bob@host。com")


def test_normal_email_still_caught():
    assert redact.has_pii("a@b.com") and not redact.has_pii("just plain words no pii")


def test_plain_cjk_not_over_redacted():
    r = redact.redact("用户希望增加暗色模式和更快的导出功能")
    assert r["found"] == {} and not redact.has_pii("用户希望增加暗色模式")


def test_catch_up_flag_reachable_non_blocking(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run.py", "--catch-up", "--no-ledger"])
    rc = run.main()
    out = capsys.readouterr().out
    assert rc == 1 and "catch_up" in out  # no ledger -> reported cleanly, no stdin block/crash
