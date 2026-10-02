#!/usr/bin/env python3
"""Deterministic EOD pipeline orchestrator, the gate that disposes what the LLM proposes.

INPUT (stdin or --in): a JSON list of *candidate demand clusters* the SKILL.md orchestration layer
already produced, Discord sessions read in context, intent + JTBD recovered, opinion-units
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
                 load_config, now_utc, demand_id, merge_observations, observed_corroboration)
from redact import redact, pseudonymize, has_pii, safe_data
from score import score_demand
import dedup as dd
from verify_gate import gate_batch
import push_card as pc
import digest as dg


def _redact_card(cand: dict) -> dict:
    """Defense-in-depth redact-on-ingest: even if the upstream already redacted, re-scrub every
    text field and re-pseudonymize any raw author user_id, so raw PII can never flow downstream
    regardless of upstream discipline. Mutates a shallow copy."""
    c = safe_data(cand)
    for f in ("title", "summary", "inferred_job", "why", "recommendation", "action"):
        if c.get(f):
            c[f] = redact(str(c[f]))["redacted"]
    ev = []
    for e in c.get("evidence", []) or []:
        e = dict(e)
        snip = e.get("redacted_snippet") or e.get("quote") or ""
        if isinstance(snip, str):
            e["redacted_snippet"] = redact(snip)["redacted"]
        else:
            e["redacted_snippet"] = snip
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


def _canonical_fields(cand):
    title = cand.get("title", "")
    job = cand.get("inferred_job") or title
    track = cand.get("track") or cand.get("taxonomy_track") or "other"
    entities = _scrub_entities(cand.get("entities") or
                               extract_entities(job + " " + title + " " + cand.get("summary", "")))
    return cand.get("canonical_key") or canonical_key(entities, track), job, track


def merge_candidates(candidates):
    """Union exact canonical contributions before scoring; keep the first proposal.

    Count estimates may overlap, so use the largest declared count or the
    observed union size rather than adding estimates. Other scoring proposals
    and prose retain the first candidate's values.
    """
    grouped = {}
    for candidate in candidates:
        current = _redact_card(candidate)
        key, _, _ = _canonical_fields(current)
        current["canonical_key"] = key
        if "observation_index" in current:
            current.update(merge_observations(current))
        if key not in grouped:
            grouped[key] = current
            continue
        previous = grouped[key]
        if "observation_index" in previous or "observation_index" in current:
            previous.update(merge_observations(previous, current))
            continue
        authors = dd.merge_authors(previous.get("authors", []), current.get("authors", []))
        evidence = {}
        for item in previous.get("evidence", []) + current.get("evidence", []):
            evidence.setdefault(json.dumps(item, sort_keys=True, ensure_ascii=False), item)
        observed = list(evidence.values())
        sources = {item.get("channel") or item.get("source") for item in observed}
        sources.discard(None)
        sources.discard("")
        counts = {
            "reach": len(authors),
            "independent_source_count": len(sources),
            "internal_mentions": sum(item.get("origin_type", "internal") == "internal"
                                     for item in observed),
            "new_mentions": len(authors),
        }
        for field, count in counts.items():
            declared = max(float(previous.get(field, 0) or 0),
                           float(current.get(field, 0) or 0), count)
            previous[field] = declared if field == "reach" else int(declared)
        previous["authors"], previous["evidence"] = authors, observed
        previous["has_internal_explicit"] = bool(previous.get("has_internal_explicit")
                                                  or current.get("has_internal_explicit"))
    return list(grouped.values())


def build_card(cand: dict, cfg: dict, run_id: str) -> dict:
    cand = _redact_card(cand)
    if "observation_index" in cand:
        cand.update(merge_observations(cand))
    title = cand.get("title", "")
    ck, job, track = _canonical_fields(cand)

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
        "rice": sc["rice"], "rice_domain": sc["rice_domain"],
        "rice_weights": sc["rice_weights"], "opportunity_score": sc["opportunity_score"],
        "urgency_wsjf": sc["urgency_wsjf"], "kano": sc["kano"],
        "final_score": sc["final_score"], "grade": sc["grade"],
        "tier": sc["tier"], "tier_reason": sc["tier_reason"],
        "velocity": sc.get("velocity"),
        "why": cand.get("why", ""), "recommendation": cand.get("recommendation", ""),
        "action": cand.get("action", ""),
        "competitor_status": cand.get("competitor_status", ""),
        "competitor_ref": cand.get("competitor_ref", ""),
        "external_corroboration": observed_corroboration(evidence),
        "run_id": run_id, "schema_version": 1,
    }


def process(candidates: list[dict], cfg: dict | None = None, ledger=None,
            dry_run: bool = False, run_id: str | None = None,
            archive_dir: str | None = None, *, identity=None, attempt_id=None,
            collection=None) -> dict:
    """Finalize candidates locally; existing positional callers remain supported."""
    from finalize import finalize
    return finalize(candidates, cfg or load_config(), ledger, dry_run, run_id, archive_dir,
                    identity=identity, attempt_id=attempt_id, collection=collection)


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
        raw = open(a.infile, encoding="utf-8").read() if a.infile else sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        candidates = json.loads(raw or "[]")

    cfg = load_config()
    ledger = None if a.no_ledger else dd.LedgerClient(db_path=cfg.get("ledger", {}).get("db_path"),
                                        product_id=cfg.get("product_id") or cfg.get("slug"))
    if ledger is not None and not a.dry_run:
        ledger.init()
    if a.catch_up:
        if ledger is None:
            print(json.dumps({"catch_up": [], "error": "no ledger (schedule-reminder base required)"}))
            return 1
        dates = (dg.missed_digest_dates(ledger.get_watermark()) if a.dry_run else
                 dg.catch_up_digests(ledger, ledger.get_watermark()))
        print(json.dumps({"catch_up": dates}, ensure_ascii=False))
        return 0
    res = process(candidates, cfg, ledger, dry_run=a.dry_run,
                  run_id=a.run_id or None, archive_dir=a.archive_dir or None)
    res.pop("digest_markdown", None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
