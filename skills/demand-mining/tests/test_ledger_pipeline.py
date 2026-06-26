"""T5 — schedule-reminder base round-trip (real subprocess) + full offline pipeline.

The ledger tests use the REAL reminder.py contract (subprocess, temp DB) — if it is not present
they skip (the offline pipeline tests still run). Asserts: canonical_key UPSERT is idempotent
(no double item across re-runs), ext.x_demand_mining_* round-trips (MUST-PRESERVE), source
isolation, and that PII in the raw input never reaches the pushed/archived card.
"""
import os
from pathlib import Path

import pytest

from lib import load_config
import dedup as dd
import run as R

CFG = load_config()
REMINDER = Path.home() / ".claude/skills/schedule-reminder/scripts/reminder.py"
have_base = REMINDER.is_file()
ledger_only = pytest.mark.skipif(not have_base, reason="schedule-reminder base not installed")


def _cand(title, job, track, sources, pii_author="user-1"):
    return {
        "title": title, "summary": "", "inferred_job": job, "track": track,
        "evidence": [{"channel": s, "origin_type": "internal" if s == "discord" else "external",
                      "redacted_snippet": "the export keeps timing out", "ts": "2026-06-25T11:00:00Z"}
                     for s in sources],
        "authors": [{"user_id": pii_author, "urgency": "need", "segment": "pro"},
                    {"user_id": "user-2", "urgency": "should", "segment": "team"}],
        "independent_source_count": len(sources),
        "reach": 4, "impact_label": "high", "effort_weeks": 2,
        "has_internal_explicit": True, "internal_mentions": 4,
        "importance": 9, "satisfaction": 2,
        "user_business_value": 8, "time_criticality": 8, "risk_reduction": 3, "job_size": 5,
        "kano": "performance", "why": "users churn on slow export",
        "recommendation": "stream the export incrementally", "new_mentions": 2,
    }


# ---------------------------------------------------------------- offline pipeline (no base)
def test_offline_pipeline_runs_and_redacts():
    cand = _cand("faster csv export", "reliably export my data", "integrations",
                 ["discord", "reddit"])
    # inject PII into a visible field; the pipeline must scrub it before push/archive
    cand["title"] = "faster csv export ping bob@acme.io"
    res = R.process([cand], CFG, ledger=None, dry_run=True)
    assert res["built"] == 1
    assert res["empty_day"] is False
    # the digest markdown must not contain the raw email (redact-on-ingest)
    assert "bob@acme.io" not in res["digest_markdown"]


def test_offline_empty_day_low_quality():
    weak = _cand("minor tweak", "small thing", "other", ["discord"])
    weak["reach"] = 1; weak["impact_label"] = "minimal"; weak["importance"] = 2
    weak["satisfaction"] = 9; weak["kano"] = "indifferent"; weak["independent_source_count"] = 1
    res = R.process([weak], CFG, ledger=None, dry_run=True)
    assert res["empty_day"] is True


# ---------------------------------------------------------------- base round-trip (real subprocess)
@ledger_only
def test_ledger_roundtrip_idempotent(tmp_path):
    db = str(tmp_path / "t.db")
    lc = dd.LedgerClient(db_path=db)
    lc.init()
    cand = _cand("dark mode", "reduce eye strain at night", "ui-ux", ["discord", "reddit"])

    R.process([cand], CFG, ledger=lc, dry_run=False, archive_dir=str(tmp_path / "pool"))
    rows1 = [r for r in lc.list_active() if r.get("source") == "demand-mining"]
    demands1 = [r for r in rows1 if dd._row_key(r).startswith("demand-mining:")
                and "watermark" not in dd._row_key(r) and "digest" not in dd._row_key(r)]

    # re-run identical input: canonical_key UPSERT must NOT create a second demand item
    R.process([cand], CFG, ledger=lc, dry_run=False, archive_dir=str(tmp_path / "pool"))
    rows2 = [r for r in lc.list_active() if r.get("source") == "demand-mining"]
    demands2 = [r for r in rows2 if dd._row_key(r).startswith("demand-mining:")
                and "watermark" not in dd._row_key(r) and "digest" not in dd._row_key(r)]
    assert len(demands2) == len(demands1)        # idempotent: no double立项


@ledger_only
def test_ledger_ext_namespace_preserved(tmp_path):
    db = str(tmp_path / "t.db")
    lc = dd.LedgerClient(db_path=db)
    lc.init()
    cand = _cand("slack alerts", "get notified in slack", "integrations", ["discord", "reddit"])
    R.process([cand], CFG, ledger=lc, dry_run=False, archive_dir=str(tmp_path / "pool"))
    rows = [r for r in lc.list_active()
            if dd._row_key(r).startswith("demand-mining:") and "watermark" not in dd._row_key(r)
            and "digest" not in dd._row_key(r)]
    assert rows, "demand item not written"
    ext = dd._row_ext(rows[0])
    # MUST-PRESERVE: our namespace survives the round-trip with the demand-only fields
    assert ext.get(dd.EXT + "intensity") is not None
    assert ext.get(dd.EXT + "distinct_author_count") == 2
    # no raw user_id ever stored (HMAC pseudonyms only)
    assert all("user-1" not in str(a) for a in ext.get(dd.EXT + "authors", []))


@ledger_only
def test_ledger_source_isolation(tmp_path):
    db = str(tmp_path / "t.db")
    lc = dd.LedgerClient(db_path=db)
    lc.init()
    # write a foreign-source item directly; our list_active(source=demand-mining) must not see it
    lc._run("add", ["--title", "foreign", "--kind", "task", "--source", "other-skill",
                    "--idempotency-key", "other-skill:x"])
    cand = _cand("export", "export data", "integrations", ["discord", "reddit"])
    R.process([cand], CFG, ledger=lc, dry_run=False, archive_dir=str(tmp_path / "pool"))
    keys = [dd._row_key(r) for r in lc.list_active()]
    assert all(not k.startswith("other-skill:") for k in keys)
