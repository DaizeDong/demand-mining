#!/usr/bin/env python3
"""Need pool — cross-day dedup, intensity accumulation, evolution (Acceptance Gate T2/T5/T7).

Backed by the schedule-reminder base (frozen contract api_version 1.0.0): subprocess only, NEVER
read the .db / build SQL / put it on OneDrive. Each demand is a base item with kind=task (a demand
is an executable iteration candidate — never an event), source=demand-mining, and the demand-only
data namespaced under ext.x_demand_mining_* (MUST-PRESERVE round-trip). idempotency_key =
'demand-mining:' + canonical_key, so re-capturing the same demand UPSERTs (same id, ext merged) —
that is the built-in cross-day idempotency.

Two clean layers:
  * PURE (no DB): match_existing (double-gate dedup), decide (NEW/SUPPRESS/RESURFACE),
    merge_authors + accumulate_intensity (distinct-author, anti-stuffing), build_ext.
  * LedgerClient: thin reminder.py wrapper.

Dedup is double-gated to forbid single-signal merges (anti-pattern #9): pure-semantic merges
"same words different need"; pure string-match misses rewrites. We require entity overlap AND a
similarity signal (cosine OR SimHash) AND subject agreement. The 0.78-0.83 boundary band is
flagged candidate-merge for human review, never auto-merged.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from lib import (canonical_key, hamming, iso, jaccard, load_config, now_utc, simhash,
                 extract_entities, intensity as compute_intensity)

SOURCE = "demand-mining"
EXT = "x_demand_mining_"
KEY_PREFIX = "demand-mining:"

NEW, SUPPRESS, RESURFACE = "NEW", "SUPPRESS", "RESURFACE"


# --------------------------------------------------------------------------- pure: matching

def _token_set(text: str) -> set:
    return set(extract_entities(text, max_n=64))


def _subject_agree(cand_text: str, row_text: str) -> bool:
    """Subject-agreement guard: the weak soft-match rungs only fire when the two share a subject
    (one entity set is a subset of the other = same demand evolving, OR same leading entity). A
    distinct demand that merely shares generic words ("slow", "add", "please") is vetoed."""
    ce = extract_entities(cand_text, max_n=64)
    re_ = extract_entities(row_text, max_n=64)
    if not ce or not re_:
        return True
    cset, rset = set(ce), set(re_)
    if cset <= rset or rset <= cset:
        return True
    return ce[0] == re_[0]


def match_existing(candidate: dict, ledger_rows: list[dict], cfg: dict | None = None):
    """Return the best matching existing row (NOT in the candidate-merge boundary band) or None.
    Double-gate: exact canonical key > (entity-overlap AND SimHash near-dup AND subject) >
    (entity-overlap AND moderate cosine AND subject) > pure high-cosine near-dup. Pure."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    ham_thr = int(sc.get("dedup_simhash_hamming", 3))
    cos_thr = float(sc.get("dedup_cosine_threshold", 0.83))
    band = sc.get("candidate_merge_band", [0.78, 0.83])
    lo = float(band[0])

    ckey = candidate["canonical_key"]
    ctext = candidate.get("title", "") + " " + candidate.get("summary", "") + " " + \
        candidate.get("inferred_job", "")
    csh = simhash(ctext)
    ctoks = _token_set(ctext)

    for row in ledger_rows:
        if _row_key(row) == ckey:
            return row

    best, best_sim = None, 0.0
    for row in ledger_rows:
        rtext = _row_ext(row).get(EXT + "text", "")
        if not rtext:
            continue
        rsh = int(_row_ext(row).get(EXT + "simhash", 0) or 0)
        ham_ok = bool(rsh) and hamming(csh, rsh) <= ham_thr
        cos = jaccard(ctoks, _token_set(rtext))
        ent_overlap = len(set(_row_key(row).split("::")[0].split("|")) &
                          set(ckey.split("::")[0].split("|")))
        rkey_track = _row_key(row).split("::")[-1]
        ckey_track = ckey.split("::")[-1]
        strong = (ent_overlap >= 2) or (ent_overlap >= 1 and rkey_track == ckey_track)
        subj = _subject_agree(ctext, rtext)
        # boundary band (lo..cos_thr) with only a weak signal => candidate-merge, do NOT auto-merge.
        in_band = lo <= cos < cos_thr
        match_ok = (cos >= cos_thr) or (strong and ham_ok and subj and not in_band) or \
                   (strong and 0.45 <= cos < lo and subj)
        if match_ok and cos >= best_sim:
            best, best_sim = row, cos
    return best


def in_candidate_band(candidate: dict, ledger_rows: list[dict], cfg: dict | None = None):
    """Return a row in the 0.78-0.83 boundary band (human-review candidate) or None. Surfaced as
    an explicit gap, never auto-merged."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    lo, hi = sc.get("candidate_merge_band", [0.78, 0.83])
    ctext = candidate.get("title", "") + " " + candidate.get("summary", "")
    ctoks = _token_set(ctext)
    for row in ledger_rows:
        rtext = _row_ext(row).get(EXT + "text", "")
        if not rtext:
            continue
        cos = jaccard(ctoks, _token_set(rtext))
        if float(lo) <= cos < float(hi):
            return row
    return None


# --------------------------------------------------------------------------- pure: evolution

def decide(candidate: dict, matched: dict | None, cfg: dict | None = None) -> dict:
    """Three-branch matrix (pure). RESURFACE on a material change — new external corroboration,
    competitor shipped, urgency/velocity jump, a new origin crossing the ≥2 line, or a score jump.
    Otherwise SUPPRESS (already-pushed demand recurs: count it, do not re-push)."""
    cfg = cfg or load_config()
    jump = float(cfg["scoring"].get("resurface_score_jump", 15))
    vel_jump = float(cfg["scoring"].get("resurface_velocity_jump", 5.0))
    if matched is None:
        return {"branch": NEW, "delta": {}}

    ext = _row_ext(matched)
    prev_score = float(ext.get(EXT + "last_score", 0) or 0)
    prev_sources = set(ext.get(EXT + "source_set", []) or [])
    prev_comp = ext.get(EXT + "competitor_status", "")

    cur_score = float(candidate.get("final_score", 0))
    cur_sources = set(candidate.get("source_set", []) or
                      [e.get("source") for e in candidate.get("evidence", [])])
    cur_comp = candidate.get("competitor_status", "")

    new_sources = cur_sources - prev_sources
    crossed_two = (len(prev_sources) < 2 <= len(cur_sources))
    competitor_shipped = bool(cur_comp) and cur_comp != prev_comp and "shipped" in cur_comp.lower()
    # urgency/velocity JUMP (docstring + ARCHITECTURE RESURFACE trigger): a demand whose velocity
    # spikes (trend acceleration / competitor momentum via trend-pulse) is now-urgent even with the
    # score/sources/competitor unchanged — re-surface it. Guarded: only when BOTH a prior and a
    # current velocity are present and the absolute jump clears the floor, so a missing velocity or
    # a tiny wiggle never re-pushes (anti-spam, no over-resurface).
    prev_vel, cur_vel = ext.get(EXT + "velocity"), candidate.get("velocity")
    velocity_jumped = (prev_vel is not None and cur_vel is not None and
                       abs(float(cur_vel) - float(prev_vel)) >= vel_jump)

    # FIRST external corroboration (ARCHITECTURE §3 RESURFACE trigger "新外部 corroboration"): an
    # internal-only demand that now gets its first external ORIGIN validating it is a confidence/RICE-
    # moving market event, distinct from crossing the >=2 source line. Guarded to the 0 -> >=1
    # external_origin_count transition only, so more external when one already existed never re-pushes
    # (anti-spam) and an internal-only demand (count stays 0 / absent) never fires.
    prev_ext_origins = int((ext.get(EXT + "external_corroboration") or {})
                           .get("external_origin_count", 0) or 0)
    cur_ext_origins = int((candidate.get("external_corroboration") or {})
                          .get("external_origin_count", 0) or 0)
    external_corroboration_new = (prev_ext_origins == 0 and cur_ext_origins >= 1)

    # ESCALATION INTO Tier0 (ARCHITECTURE RESURFACE trigger "紧迫跳变" + Kano stop-the-bleed): a demand
    # that crosses INTO tier0 (its Kano must_be is now missing/broken) is the highest-urgency
    # transition there is — it must re-surface for immediate attention even with score/sources/
    # velocity/competitor all flat, otherwise a now-critical stop-the-bleed item silently SUPPRESSes
    # and is never re-pushed. Guarded to the not-tier0 -> tier0 transition only: a demand that STAYS
    # tier0 (already surfaced) or DE-escalates out of tier0 does not re-push (anti-spam).
    prev_tier = str(ext.get(EXT + "tier", "") or "")
    cur_tier = str(candidate.get("tier", "") or "")
    escalated_to_tier0 = (cur_tier == "tier0" and prev_tier != "tier0")

    # DIRECTIONAL score jump: RESURFACE means the demand became MORE important/urgent (an upward
    # jump), so re-push an evolution UPDATE card. abs() wrongly re-surfaced a DECLINING demand (a big
    # downward drop) as if it were resurging — but a decline is handled by the fade path and must
    # SUPPRESS here (anti-spam). Only an upward jump >= floor is material.
    material = (
        (cur_score - prev_score) >= jump or
        (len(new_sources) >= 1 and crossed_two) or
        competitor_shipped or
        velocity_jumped or
        external_corroboration_new or
        escalated_to_tier0
    )
    branch = RESURFACE if material else SUPPRESS
    return {"branch": branch, "delta": {
        "score_delta": round(cur_score - prev_score, 4),
        "new_sources": sorted(new_sources),
        "crossed_two_sources": crossed_two,
        "competitor_shipped": competitor_shipped,
        "velocity_jumped": velocity_jumped,
        "external_corroboration_new": external_corroboration_new,
        "escalated_to_tier0": escalated_to_tier0,
    }}


# --------------------------------------------------------------------------- pure: intensity merge

def merge_authors(prior_authors: list[dict], new_authors: list[dict]) -> list[dict]:
    """Union author contributions across days, keyed by author_hash, keeping each author's MAX
    (urgency,segment) contribution. So a returning author who escalates raises intensity; a
    returning author repeating the same ask does NOT (anti-stuffing). Pure, order-stable."""
    by: dict[str, dict] = {}
    rank_u = {"should": 1, "need": 2, "blocking": 3}
    rank_s = {"free": 1, "pro": 2, "team": 3, "enterprise": 4}
    for a in (prior_authors or []) + (new_authors or []):
        h = a.get("author_hash") or a.get("author") or a.get("author_pseudo") or ""
        if not h:
            continue
        cur = by.get(h)
        score = rank_u.get((a.get("urgency") or "should").lower(), 1) + \
            rank_s.get((a.get("segment") or "free").lower(), 1)
        if cur is None or score > cur["_rank"]:
            by[h] = {"author_hash": h, "urgency": a.get("urgency", "should"),
                     "segment": a.get("segment", "free"), "_rank": score}
    out = [{k: v for k, v in d.items() if k != "_rank"} for d in by.values()]
    return sorted(out, key=lambda d: d["author_hash"])


# --------------------------------------------------------------------------- ledger glue

def _row_key(row: dict) -> str:
    return row.get("idempotency_key") or _row_ext(row).get(EXT + "canonical_key", "")


def _row_ext(row: dict) -> dict:
    return row.get("ext") or {}


def build_ext(card: dict, prior_ext: dict | None = None, cfg: dict | None = None) -> dict:
    """Construct/merge the x_demand_mining_* ext namespace (MUST-PRESERVE). Merges authors across
    days, recomputes distinct-author intensity, appends a redacted sample, caps the ring buffer,
    tracks first/last seen + source_set + push_count. Stores ONLY redacted/distilled data —
    never raw conversation text (privacy hard rule)."""
    cfg = cfg or load_config()
    cap = int(cfg["scoring"].get("samples_cap", 30))
    prior_ext = prior_ext or {}
    now = iso(now_utc())
    text = (card.get("title", "") + " " + card.get("inferred_job", "") + " " +
            card.get("summary", ""))[:400]

    authors = merge_authors(prior_ext.get(EXT + "authors", []), card.get("authors", []))
    inten = compute_intensity(authors, cfg)

    samples = list(prior_ext.get(EXT + "samples", []))
    samples.append({"ts": now, "final_score": card.get("final_score"),
                    "intensity": inten["intensity"],
                    "distinct_authors": inten["distinct_author_count"],
                    "rice": (card.get("rice") or {}).get("rice_raw"),
                    "velocity": card.get("velocity")})
    samples = samples[-cap:]

    sources = sorted(set(prior_ext.get(EXT + "source_set", []) or []) |
                     set(card.get("source_set", []) or
                         [e.get("source") for e in card.get("evidence", [])]))
    mention = int(prior_ext.get(EXT + "mention_count", 0)) + int(card.get("new_mentions", 0) or 0)

    return {
        EXT + "canonical_key": card["canonical_key"],
        EXT + "cluster_id": card.get("cluster_id", ""),
        EXT + "simhash": simhash(text),
        EXT + "text": text,                              # redacted, distilled job — not raw chat
        EXT + "first_seen": prior_ext.get(EXT + "first_seen", now),
        EXT + "last_seen": now,
        EXT + "last_score": card.get("final_score", 0),
        EXT + "demand_track": card.get("demand_track", "explicit"),
        EXT + "taxonomy_track": card.get("taxonomy_track", card.get("track", "other")),
        EXT + "kano": card.get("kano"),
        EXT + "intensity": inten["intensity"],
        EXT + "distinct_author_count": inten["distinct_author_count"],
        EXT + "mention_count": max(mention, inten["mention_count"]),
        EXT + "authors": authors,                        # [{author_hash,urgency,segment}] HMAC only
        EXT + "source_set": sources,
        EXT + "rice": card.get("rice"),
        EXT + "opportunity_score": card.get("opportunity_score"),
        EXT + "urgency_wsjf": card.get("urgency_wsjf"),
        EXT + "tier": card.get("tier"),
        EXT + "velocity": card.get("velocity"),
        EXT + "competitor_status": card.get("competitor_status", ""),
        EXT + "competitor_ref": card.get("competitor_ref", ""),
        EXT + "external_corroboration": card.get("external_corroboration", {}),
        EXT + "status_reason": card.get("status_reason", ""),
        EXT + "push_count": int(prior_ext.get(EXT + "push_count", 0)),
        EXT + "samples": samples,
        EXT + "evidence": card.get("evidence", [])[:8],  # redacted snippets only
    }


class LedgerClient:
    """Subprocess wrapper around reminder.py. Honors --db / SCHEDULE_DB_PATH; --now via env.
    reminder.py located via DEMAND_MINING_REMINDER_CMD (JSON list / shell string) or by probing
    the reminder ledger (no machine path baked in)."""

    def __init__(self, cmd=None, db_path=None, actor=SOURCE):
        self.cmd = self._resolve_cmd(cmd)
        self.db_path = db_path or os.environ.get("SCHEDULE_DB_PATH")
        self.actor = actor

    @staticmethod
    def _resolve_cmd(cmd):
        if cmd:
            return cmd if isinstance(cmd, list) else shlex.split(cmd)
        env = os.environ.get("DEMAND_MINING_REMINDER_CMD")
        if env:
            try:
                v = json.loads(env)
                if isinstance(v, list):
                    return v
            except Exception:
                return shlex.split(env)
        probe = Path.home() / ".claude/skills/schedule-reminder/scripts/reminder.py"
        return [sys.executable, str(probe)]

    def _run(self, verb, args):
        base = list(self.cmd)
        if self.db_path:
            base += ["--db", self.db_path]
        base += ["--actor", self.actor, verb] + args
        proc = subprocess.run(base, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        out = (proc.stdout or "").strip()
        if proc.returncode != 0:
            err = (proc.stderr or out).strip()
            raise RuntimeError(f"reminder.py {verb} failed rc={proc.returncode}: {err[:300]}")
        return json.loads(out) if out else {}

    def init(self):
        return self._run("init", [])

    def list_active(self, limit=500):
        rows, cursor = [], None
        while True:
            args = ["--source", SOURCE, "--active", "--limit", str(limit)]
            if cursor:
                args += ["--cursor", cursor]
            res = self._run("list", args)
            rows += res.get("items", [])
            cursor = res.get("next_cursor")
            if not cursor:
                break
        return rows

    def upsert(self, card, ext, title=None, priority=0):
        key = KEY_PREFIX + card["canonical_key"]
        args = ["--title", (title or card.get("title") or card["canonical_key"])[:120],
                "--kind", "task", "--source", SOURCE,
                "--priority", str(int(priority or 0)),
                "--idempotency-key", key, "--ext", json.dumps(ext, ensure_ascii=False)]
        return self._run("add", args)

    def add_watermark(self, last_run_at):
        ext = {EXT + "last_run_at": last_run_at}
        args = ["--title", "demand-mining watermark", "--kind", "task", "--source", SOURCE,
                "--idempotency-key", KEY_PREFIX + "watermark",
                "--ext", json.dumps(ext, ensure_ascii=False)]
        return self._run("add", args)

    def get_watermark(self):
        try:
            rows = self.list_active()
        except Exception:
            return None
        for r in rows:
            if _row_key(r) == KEY_PREFIX + "watermark":
                return _row_ext(r).get(EXT + "last_run_at")
        return None


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    cand = data["candidate"]
    ledger = data.get("ledger", [])
    cfg = load_config()
    matched = match_existing(cand, ledger, cfg)
    res = decide(cand, matched, cfg)
    res["matched_key"] = _row_key(matched) if matched else None
    band = in_candidate_band(cand, ledger, cfg)
    res["candidate_merge_with"] = _row_key(band) if band else None
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
