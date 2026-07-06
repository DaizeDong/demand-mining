#!/usr/bin/env python3
"""Deterministic EOD pipeline orchestrator — the gate that disposes what the LLM proposes.

INPUT (stdin or --in): a JSON list of *candidate demand clusters* the SKILL.md orchestration layer
already produced — Discord sessions read in context, intent + JTBD recovered, opinion-units
extracted, cross-source de-duplicated into one demand per canonical subject, each with its
(already-redacted) evidence[], its distinct authors[], and a temperature-0 per-axis score
proposal. Shape per candidate:

  {
    "title","summary","inferred_job","entities":[...],"track" (optional taxonomy track),
    "evidence":[{"channel"|"source","origin_type":"internal"|"external","redacted_snippet","ts","url"?}, ...],
    "authors":[{"user_id"? | "author_hash","urgency":should|need|blocking,"segment":free|pro|team|enterprise}, ...],
    "reach","impact_label","effort_weeks","independent_source_count","has_internal_explicit",
    "internal_mentions","importance","satisfaction","user_business_value","time_criticality",
    "risk_reduction","job_size","kano","kano_missing","velocity",
    "why","recommendation","action","competitor_status","competitor_ref",
    "new_mentions" (this run's mention delta)
  }

This module runs the DETERMINISTIC remainder: redact-on-ingest (defense-in-depth) → canonical_key
→ distinct-author intensity → three-axis score + tier → cross-day dedup (NEW/SUPPRESS/RESURFACE) →
verify gate (≥1 internal evidence + egress DLP, fail-closed) → tiered push → pool UPSERT (idempotent)
→ EOD digest (idempotent) → atomic watermark. No network except the relay/ledger subprocess seams,
both injectable + dry-runnable.
"""
from __future__ import annotations

import argparse
import json
import sys

from lib import (canonical_key, extract_entities, intensity as compute_intensity, iso,
                 load_config, now_utc, demand_id)
from redact import redact, pseudonymize, has_pii
from score import score_demand
import dedup as dd
from verify_gate import gate_batch
import push_card as pc
import digest as dg


def _redact_card(cand: dict) -> dict:
    """Defense-in-depth redact-on-ingest: even if the upstream already redacted, re-scrub every
    text field and re-pseudonymize any raw author user_id, so raw PII can never flow downstream
    regardless of upstream discipline. Mutates a shallow copy."""
    c = dict(cand)
    for f in ("title", "summary", "inferred_job", "why", "recommendation", "action"):
        if c.get(f):
            c[f] = redact(str(c[f]))["redacted"]
    ev = []
    for e in c.get("evidence", []) or []:
        e = dict(e)
        snip = e.get("redacted_snippet") or e.get("quote") or ""
        if snip:
            e["redacted_snippet"] = redact(str(snip))["redacted"]
        e.pop("quote", None)
        ev.append(e)
    c["evidence"] = ev
    authors = []
    for a in c.get("authors", []) or []:
        a = dict(a)
        if a.get("user_id"):
            a["author_hash"] = pseudonymize(str(a["user_id"]))
            a.pop("user_id", None)
        elif a.get("author") and not a.get("author_hash"):
            a["author_hash"] = pseudonymize(str(a["author"]))
            a.pop("author", None)
        authors.append(a)
    c["authors"] = authors
    return c


def _scrub_entities(ents: list) -> list:
    """Defense-in-depth: scrub raw PII out of upstream-PROPOSED entity tokens before they become the
    canonical_key (= the schedule-reminder idempotency_key persisted LONG-TERM in the need pool). A
    clean token is returned byte-identical (no over-scrub / no canonical_key churn); a token that
    still carries PII (e.g. an email or @handle the upstream slipped in) is folded to its redacted
    placeholder-derived slug so no raw email/handle/id is ever stored as the pool key."""
    out = []
    for e in ents or []:
        e = str(e)
        if has_pii(e):
            toks = extract_entities(redact(e)["redacted"])
            out.extend(toks if toks else ["redacted"])
        else:
            out.append(e)
    return out


def build_card(cand: dict, cfg: dict, run_id: str) -> dict:
    cand = _redact_card(cand)
    title = cand.get("title", "")
    job = cand.get("inferred_job") or title
    track = cand.get("track") or cand.get("taxonomy_track") or "other"
    entities = _scrub_entities(cand.get("entities") or
                               extract_entities(job + " " + title + " " + cand.get("summary", "")))
    ck = cand.get("canonical_key") or canonical_key(entities, track)

    authors = cand.get("authors", [])
    inten = compute_intensity(authors, cfg)

    sc = score_demand(cand, cfg)
    evidence = cand.get("evidence", [])
    origins = sorted(set((e.get("origin_type") or "internal") + ":" +
                         (e.get("channel") or e.get("source") or "") for e in evidence))
    isc = int(cand.get("independent_source_count", 0) or 0) or len(set(
        (e.get("channel") or e.get("source") or "") for e in evidence if (e.get("channel") or e.get("source"))))

    return {
        "demand_id": demand_id(ck),
        "canonical_key": ck,
        "cluster_id": cand.get("cluster_id", f"cl-{now_utc().date().isoformat()}-{ck[:8]}"),
        "title": title, "summary": cand.get("summary", ""), "inferred_job": job,
        "taxonomy_track": track, "track": track,
        "demand_track": cand.get("demand_track", "explicit"),
        "intents": cand.get("intents", []),
        "evidence": evidence, "independent_source_count": isc,
        "origins": origins,
        "source_set": sorted(set(e.get("source") or e.get("channel") for e in evidence
                                 if (e.get("source") or e.get("channel")))),
        "authors": authors,
        "intensity": inten["intensity"],
        "distinct_author_count": inten["distinct_author_count"],
        "new_mentions": int(cand.get("new_mentions", inten["mention_count"]) or 0),
        "rice": sc["rice"], "opportunity_score": sc["opportunity_score"],
        "urgency_wsjf": sc["urgency_wsjf"], "kano": sc["kano"],
        "final_score": sc["final_score"], "grade": sc["grade"],
        "tier": sc["tier"], "tier_reason": sc["tier_reason"],
        "velocity": sc.get("velocity"),
        "why": cand.get("why", ""), "recommendation": cand.get("recommendation", ""),
        "action": cand.get("action", ""),
        "competitor_status": cand.get("competitor_status", ""),
        "competitor_ref": cand.get("competitor_ref", ""),
        "external_corroboration": cand.get("external_corroboration", {}),
        "run_id": run_id, "schema_version": 1,
    }


def process(candidates: list[dict], cfg: dict | None = None, ledger=None,
            dry_run: bool = False, run_id: str | None = None,
            archive_dir: str | None = None) -> dict:
    cfg = cfg or load_config()
    run_id = run_id or f"demand-{now_utc().date().isoformat()}"

    cards = [build_card(c, cfg, run_id) for c in candidates]

    # ---- cross-day dedup against the base ledger (need pool) ----
    ledger_rows = []
    if ledger is not None:
        try:
            ledger_rows = ledger.list_active()
        except Exception:
            ledger_rows = []
    new_cards, resurface, suppressed, candidate_merge = [], [], [], []
    for c in cards:
        band = dd.in_candidate_band(c, ledger_rows, cfg)
        if band is not None:
            candidate_merge.append({"title": c["title"], "with": dd._row_key(band)})
        matched = dd.match_existing(c, ledger_rows, cfg)
        d = dd.decide(c, matched, cfg)
        c["_branch"] = d["branch"]
        c["_dedup_delta"] = d["delta"]
        if matched is not None:
            c["first_seen"] = dd._row_ext(matched).get(dd.EXT + "first_seen")
            c["push_count"] = int(dd._row_ext(matched).get(dd.EXT + "push_count", 0))
        if d["branch"] == dd.SUPPRESS:
            suppressed.append(c)
        elif d["branch"] == dd.RESURFACE:
            resurface.append(c)
        else:
            new_cards.append(c)

    actionable = new_cards + resurface

    # ---- verify gate (fail-closed: >=1 internal evidence + egress DLP) + bucketing ----
    g = gate_batch(actionable, cfg)
    pushable, archivable = g["pushable"], g["archivable"]

    # ---- tiered push ----
    pushed = []
    for c in pushable:
        is_update = c.get("_branch") == dd.RESURFACE
        res = pc.push_card(c, update=is_update, dry_run=dry_run)
        if res["ok"]:
            c["pushed"] = True
            c["push_count"] = int(c.get("push_count", 0)) + 1
            c["push_ts"] = iso(now_utc())
            pushed.append(c)

    # ---- pool UPSERT (NEW + RESURFACE + SUPPRESS all get a sample; idempotent UPSERT) ----
    if ledger is not None and not dry_run:
        for c in actionable + suppressed:
            prior = {}
            matched = dd.match_existing(c, ledger_rows, cfg)
            if matched:
                prior = dd._row_ext(matched)
            ext = dd.build_ext(c, prior, cfg)
            if c.get("pushed"):
                ext[dd.EXT + "push_count"] = int(c.get("push_count", 0))
            # priority: RICE-high → small iCal priority (1 highest). tier0 forces priority 1.
            prio = 1 if c.get("tier") == "tier0" else max(1, min(9, 10 - int(
                round(float(c.get("final_score", 0)) / 11.2))))
            try:
                ledger.upsert(c, ext, priority=prio)
            except Exception:
                pass

    # ---- EOD digest (idempotent item + file + deliver) ----
    coverage = {"internal": sum(1 for c in cards for e in c.get("evidence", [])
                                if (e.get("origin_type") or "internal") == "internal"),
                "external": sum(1 for c in cards for e in c.get("evidence", [])
                                if e.get("origin_type") == "external"),
                "candidates": len(candidates), "pushed": len(pushed),
                "candidate_merge": len(candidate_merge)}
    md = dg.build_markdown(archivable, coverage, cfg=cfg)
    digest_path = None
    if not dry_run:
        try:
            digest_path = str(dg.write_digest_file(md, archive_dir))
        except Exception:
            digest_path = None
        if ledger is not None:
            try:
                dg.register_digest_item(ledger, summary=f"{len(archivable)} demands, {len(pushed)} pushed")
            except Exception:
                pass
    pc.deliver(md, dry_run=dry_run)

    # ---- atomic watermark (only after the full success path) ----
    if ledger is not None and not dry_run:
        try:
            ledger.add_watermark(iso(now_utc()))
        except Exception:
            pass

    return {
        "run_id": run_id, "candidates": len(candidates), "built": len(cards),
        "new": len(new_cards), "resurface": len(resurface), "suppressed": len(suppressed),
        "candidate_merge": candidate_merge,
        "blocked": g["blocked"],
        "pushed": [c["title"] for c in pushed],
        "archivable": [c["title"] for c in archivable],
        "empty_day": len(archivable) == 0,
        "digest_path": digest_path, "digest_markdown": md,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--archive-dir", default="")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--catch-up", action="store_true",
                    help="T7: backfill missed daily-digest items since the last watermark, then exit "
                         "(idempotent; for the cron/orchestration layer after an oversleep)")
    a = ap.parse_args()

    candidates = []
    if not a.catch_up:  # catch-up backfills digests from the ledger; it reads no candidate input
        raw = open(a.infile, encoding="utf-8").read() if a.infile else sys.stdin.read()
        candidates = json.loads(raw or "[]")
        if isinstance(candidates, dict):
            candidates = candidates.get("candidates", [])

    cfg = load_config()
    ledger = None if a.no_ledger else dd.LedgerClient()
    if ledger is not None:
        try:
            ledger.init()
        except Exception:
            ledger = None
    if a.catch_up:
        if ledger is None:
            print(json.dumps({"catch_up": [], "error": "no ledger (schedule-reminder base required)"}))
            return 1
        dates = dg.catch_up_digests(ledger, ledger.get_watermark())
        print(json.dumps({"catch_up": dates}, ensure_ascii=False))
        return 0
    res = process(candidates, cfg, ledger, dry_run=a.dry_run,
                  run_id=a.run_id or None, archive_dir=a.archive_dir or None)
    res.pop("digest_markdown", None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
