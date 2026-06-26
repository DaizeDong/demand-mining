#!/usr/bin/env python3
"""demand-mining shared library — deterministic primitives, stdlib only.

Everything here is a PURE function (no clock, no network) unless explicitly noted, so the
acceptance-gate pytest suite can byte-compare outputs. Network/MCP/LLM work (Discord ingest,
intent reading, JTBD interpretation, competitor deep-dive) lives in the SKILL.md orchestration
layer, NOT here. This file holds: config discovery + defaults, entity normalization,
canonical_key, SimHash/Hamming/Jaccard, the three orthogonal prioritization frameworks
(RICE / Opportunity-ODI / WSJF), Kano gating, distinct-author intensity, and time helpers.

The privacy layer (redact-on-ingest, HMAC pseudonyms, unique placeholders) lives in redact.py —
it must run BEFORE any text reaches an LLM/embedding, so it is kept separate and called first.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:  # BOM-safe stdout on Windows GBK consoles
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --------------------------------------------------------------------------- config

# The single tunable surface lives in the companion config repo. Discovery probe order mirrors
# market-intel's companion convention so demand-mining-config can reuse the same machinery.
CONFIG_ENV = "DEMAND_MINING_CONFIG"
CONFIG_FALLBACKS = ["~/.demand-mining-config", "~/.config/demand-mining-config"]

# Eight mutually-exclusive Stage-A intent labels (a message may carry >1). `chitchat` is the
# discard bucket (logged-then-dropped). Frozen enum — a new label is a schema_version bump, never
# a free-form LLM invention (anti-pattern: category drift breaks cross-day comparability).
INTENT_LABELS = ["feature_request", "bug_complaint", "pain_workaround", "competitor_compare",
                 "pricing_objection", "how_to_question", "praise", "chitchat"]
# Labels that constitute a real demand signal (how_to_question = support, praise/chitchat = not a
# demand). pain_workaround is the strongest implicit signal (user already built a half-fix).
DEMAND_LABELS = {"feature_request", "bug_complaint", "pain_workaround",
                 "competitor_compare", "pricing_objection"}

DEFAULT_CONFIG = {
    "schema_version": 1,
    # Demand taxonomy / tracks: cluster + weight cosmetic-ranking aid (NOT a score multiplier here;
    # the three axes below carry the ranking). Each track is keyword-seeded for the deterministic
    # cluster hint; the real subject anchor is the canonical_key entity set.
    "taxonomy": [
        {"id": "onboarding", "label": "Onboarding / activation", "weight": 1.0,
         "keywords": ["onboard", "setup", "getting started", "first time", "signup",
                      "tutorial", "新手", "上手", "注册"], "enabled": True},
        {"id": "core-workflow", "label": "Core workflow", "weight": 1.1,
         "keywords": ["workflow", "flow", "step", "process", "main", "core", "流程"],
         "enabled": True},
        {"id": "integrations", "label": "Integrations / API", "weight": 1.0,
         "keywords": ["integration", "api", "webhook", "connect", "import", "export",
                      "sync", "zapier", "集成", "对接"], "enabled": True},
        {"id": "performance", "label": "Performance / reliability", "weight": 1.1,
         "keywords": ["slow", "lag", "crash", "error", "timeout", "down", "fail",
                      "bug", "卡", "崩", "报错"], "enabled": True},
        {"id": "pricing-plans", "label": "Pricing / plans", "weight": 1.0,
         "keywords": ["price", "pricing", "plan", "cost", "expensive", "free tier",
                      "quota", "limit", "贵", "收费", "套餐"], "enabled": True},
        {"id": "ui-ux", "label": "UI / UX", "weight": 0.9,
         "keywords": ["ui", "ux", "design", "confusing", "hard to find", "layout",
                      "dark mode", "界面", "难用"], "enabled": True},
        {"id": "mobile", "label": "Mobile / cross-platform", "weight": 0.9,
         "keywords": ["mobile", "ios", "android", "app", "tablet", "手机"],
         "enabled": True},
        {"id": "other", "label": "Other / uncategorized", "weight": 0.8,
         "keywords": [], "enabled": True},
    ],
    "focus_topics": ["activation friction", "competitor switch", "power-user workflow"],
    # Hard mutes — clusters whose text matches these are dropped (spam/noise).
    "exclude": ["airdrop giveaway", "promo code spam", "nsfw"],
    "scoring": {
        # ---- A) RICE (ordering / bang-for-buck) — Intercom anchored scale, no free numerals.
        "rice_weights": {"reach": 1.0, "impact": 1.0, "confidence": 1.0, "effort": 1.0},
        "impact_anchors": {"massive": 3.0, "high": 2.0, "medium": 1.0,
                           "low": 0.5, "minimal": 0.25},
        # Confidence = mechanical function of (source tier × cross-validation count). NOT a guess.
        # Keys: "<min_origins>+<has_internal_explicit?>". The gate maps independent_source_count>=2
        # straight to the high band, encoding "≥2 independent sources = high confidence".
        "confidence_map": {
            "internal_explicit_multi": 1.0,   # internal explicit + >=2 independent origins
            "internal_cluster_3plus": 0.8,    # single internal cluster, >=3 mentions
            "cross_validated_multi": 0.8,     # >=2 independent origins (no internal-explicit): cross-validated
            "single_implicit_or_external": 0.5,  # 1-2 complaints, or single external trend
            "unverified_frontier": 0.3,       # pure unvalidated frontier
        },
        "min_independent_sources": 2,
        # Effort is a clamped denominator (anti-pattern: small-divisor score explosions). TBD =>
        # neutral placeholder until an engineering owner fills it; never silently a tiny number.
        "effort_min": 0.5,            # person-weeks clamp floor (prevents /0.1 blowups)
        "effort_tbd_default": 2.0,    # neutral when owner has not estimated yet
        # ---- B) Opportunity / Gap (ODI, Ulwick): Importance + max(Importance - Satisfaction, 0).
        # Importance & Satisfaction are 0-10 anchored; importance is double-weighted by the formula.
        "opportunity_importance_max": 10.0,
        "opportunity_satisfaction_max": 10.0,
        # ---- C) Urgency (WSJF / velocity): (UBV + TimeCriticality + RiskReduction) / JobSize.
        "urgency_fibonacci": [1, 2, 3, 5, 8, 13],
        "time_criticality_anchors": {"competitor_shipped": 13, "competitor_building": 8,
                                     "time_preference": 3, "no_deadline": 1},
        # ---- D) Kano gate (orthogonal): must_be missing => Tier0 (immediate, score-decoupled).
        "kano_levels": ["must_be", "performance", "delighter", "indifferent", "reverse"],
        "kano_must_be_to_tier0": True,
        # ---- intensity (need-weight, anti-vote-stuffing): summed per DISTINCT author only.
        "urgency_score": {"should": 1, "need": 2, "blocking": 3},
        "segment_score": {"free": 1, "pro": 2, "team": 3, "enterprise": 4},
        # ---- dedup (two-gate, prevents false merges). Reuses daily-hotspots' validated constants.
        "dedup_cosine_threshold": 0.83,
        "dedup_simhash_hamming": 3,
        "candidate_merge_band": [0.78, 0.83],   # boundary => human review, never auto-merge
        # ---- tiering / push thresholds (RICE final, normalized 0-100).
        "min_score_to_archive": 40,
        "min_score_to_push": 70,
        "flagship_score": 80,
        "tier_bands": {"tier1": 80, "tier2": 60, "backlog": 40},  # >= band => that tier
        # ---- evolution / cross-day.
        "resurface_score_jump": 15,
        "resurface_velocity_jump": 5.0,   # abs velocity jump (trend accel) that re-surfaces a demand
        "lookback_days": 30,
        "samples_cap": 30,
        "fading_quiet_days": 5,
        # ---- governance (self-evolve gates).
        "override_budget": 0.2,
        "golden_set_drift_band": 1,
        "weight_regression": {"max_tau": 0.25, "max_push_churn_frac": 0.20,
                              "catastrophic_tau": 0.6, "catastrophic_churn_frac": 0.5},
    },
    "push": {"channel": "discord-relay", "max_per_day": 5},
    "delegation": {"market-intel": {"enabled": True, "scale": "standard", "daily_cap": 4},
                   "daily-hotspots": {"enabled": True, "consume_archive": True}},
    # privacy retention (days). Tier0 raw is short-TTL; the pool (redacted) is long-lived.
    "privacy": {"raw_retention_days": 14, "pseudo_map_retention_days": 7},
}


def find_config_dir() -> Path | None:
    p = os.environ.get(CONFIG_ENV)
    if p and Path(p).expanduser().is_dir():
        return Path(p).expanduser()
    for cand in CONFIG_FALLBACKS:
        d = Path(cand).expanduser()
        if d.is_dir():
            return d
    return None


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _product_dir(d: Path) -> Path | None:
    """Resolve the active product dir under a companion-repo root via registry.json (first product).
    Returns None if there is no registry / products layout (flat config)."""
    reg = d / "registry.json"
    if not reg.is_file():
        return None
    try:
        products = json.loads(reg.read_text(encoding="utf-8-sig")).get("products", [])
    except Exception:
        return None
    for p in products:
        slug_ = p.get("slug")
        if slug_ and (d / "products" / slug_).is_dir():
            return d / "products" / slug_
    return None


def load_config(explicit_path: str | None = None) -> dict:
    """Probe for the tunable surface; deep-merge over DEFAULT_CONFIG. Never raises on absence — a
    missing companion repo degrades to the built-in default set (documented). Supports two layouts:
      * flat:        <dir>/priority.json (or watchlist.json)
      * per-product: <dir>/registry.json -> products/<slug>/{priority,taxonomy}.json
    So DEMAND_MINING_CONFIG may point at the repo ROOT (per-product) or directly at a product dir."""
    out = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy baseline

    def merge_file(p: Path):
        nonlocal out
        if p.is_file():
            try:
                out = _deep_merge(out, json.loads(p.read_text(encoding="utf-8-sig")))
            except Exception:
                pass

    if explicit_path:
        merge_file(Path(explicit_path).expanduser())
        return out

    d = find_config_dir()
    if not d:
        return out
    # flat layout at the probed dir
    flat = False
    for cand in (d / "priority.json", d / "watchlist.json"):
        if cand.is_file():
            merge_file(cand)
            flat = True
    # also merge a taxonomy.json at the same level if present
    merge_file(d / "taxonomy.json")
    # per-product layout (repo root with registry.json)
    if not flat:
        pd = _product_dir(d)
        if pd:
            merge_file(pd / "priority.json")
            merge_file(pd / "taxonomy.json")
    return out


# --------------------------------------------------------------------------- entities

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]*|[一-鿿぀-ヿ가-힯]+")
_ALIAS = {
    "gpt4": "gpt-4", "gpt-4o": "gpt-4", "gpt4o": "gpt-4",
    "claude-3": "claude", "claude3": "claude",
    "llms": "llm", "agents": "agent", "integrations": "integration",
    "workflows": "workflow", "imports": "import", "exports": "export",
}
_ENTITY_STOP = set(
    "the a an of to for and or in on with is are be it its your you this that we i my our "
    "using use used can will would could should just now today vs via from into out up down get "
    "got make made build built want need would like please could able really very much more "
    "when how why what where who do does did not no yes also even still only".split()
)
# Meaningful <3-char tech acronyms that the generic "drop short ASCII tokens" filter would otherwise
# eat — losing them collapses DISTINCT demands to one canonical_key ("add AI mode" == "add VR mode").
# Frozen whitelist (not free-form): kept as entities; generic 2-char stop tokens (is/to/of/in/...)
# remain filtered via _ENTITY_STOP, so no noise is re-admitted.
_SHORT_KEEP = {"ai", "ui", "ux", "ml", "vr", "ar", "qa"}


def slug(s: str) -> str:
    s = (s or "").strip().lower()
    return _ALIAS.get(s, s)


def extract_entities(text: str, max_n: int = 8) -> list[str]:
    """Deterministic, dependency-free NER stand-in: lowercase content tokens, alias-folded,
    stop-word filtered, order-preserving dedup, capped. Good enough for a canonical_key — the
    heavy lifting is the multi-signal dedup (entities + semantic + author)."""
    toks = _TOKEN_RE.findall((text or "").lower())
    out, seen = [], set()
    for t in toks:
        if t in _ENTITY_STOP:
            continue
        if t.isascii() and len(t) < 3 and t not in _SHORT_KEEP:
            continue
        t = slug(t)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_n:
            break
    return out


def canonical_key(entities: list[str], track: str) -> str:
    """Content-pure dedupe key = sorted unique entity slugs ⊕ track. NEVER includes a timestamp,
    author, or message id (replay-safe). Used directly as the schedule-reminder idempotency_key
    (prefixed with the skill name by the ledger client)."""
    ents = sorted(set(slug(e) for e in entities if e))
    return "|".join(ents) + "::" + slug(track or "")


def demand_id(canonical: str) -> str:
    return "dm-" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- similarity

def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _hash64(token: str) -> int:
    return int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:8], "big")


def simhash(text: str) -> int:
    """64-bit SimHash over content tokens. Deterministic (md5-seeded), no external deps."""
    toks = [t for t in _TOKEN_RE.findall((text or "").lower())
            if not (t.isascii() and len(t) < 3) and t not in _ENTITY_STOP]
    if not toks:
        return 0
    v = [0] * 64
    for t in toks:
        hv = _hash64(slug(t))
        for i in range(64):
            v[i] += 1 if (hv >> i) & 1 else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= (1 << i)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# --------------------------------------------------------------------------- intensity

def intensity(authors: list[dict], cfg: dict | None = None) -> dict:
    """Need-weighted demand intensity, accumulated per DISTINCT author only (anti-vote-stuffing:
    one loud user must not inflate intensity; repeats only bump mention_count). NO time decay —
    a long-standing strong need keeps its weight (time-sensitivity is the separate `velocity`).

    `authors`: list of {author_hash, urgency, segment} (urgency in should/need/blocking,
    segment in free/pro/team/enterprise). Pure.
    Returns {intensity, distinct_author_count, mention_count}.
    """
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    us, ss = sc["urgency_score"], sc["segment_score"]
    seen: dict[str, float] = {}
    mention = 0
    for a in authors or []:
        mention += 1
        h = a.get("author_hash") or a.get("author") or ""
        if not h:
            continue
        contrib = float(us.get((a.get("urgency") or "should").lower(), 1)) + \
            float(ss.get((a.get("segment") or "free").lower(), 1))
        # distinct author: keep the MAX contribution that author ever expressed (a user who later
        # escalates to "blocking" should count at their strongest, not their first weak mention).
        seen[h] = max(seen.get(h, 0.0), contrib)
    distinct = len(seen)
    return {
        "intensity": round(sum(seen.values()) + distinct, 4),
        "distinct_author_count": distinct,
        "mention_count": mention,
    }


# --------------------------------------------------------------------------- RICE / Opportunity / WSJF

def confidence_from_evidence(independent_source_count: int, has_internal_explicit: bool,
                             internal_mentions: int, cfg: dict | None = None) -> float:
    """Mechanical Confidence (NOT a guess): source tier × cross-validation count → fraction.
    Encodes "≥2 independent sources = high confidence". Monotone non-decreasing in evidence.
    Returns one of the four anchored bands from config (1.0 / 0.8 / 0.5 / 0.3)."""
    cfg = cfg or load_config()
    cm = cfg["scoring"]["confidence_map"]
    n = int(independent_source_count or 0)
    if has_internal_explicit and n >= 2:
        return float(cm["internal_explicit_multi"])
    if int(internal_mentions or 0) >= 3:
        return float(cm["internal_cluster_3plus"])
    # >=2 INDEPENDENT origins cross-validate the demand even without an internal-explicit mention:
    # the architecture wants "≥2 independent sources" encoded into Confidence as a high band, and
    # the score must be MONOTONE non-decreasing in independent_source_count. Previously n=1 and n>=2
    # were both 0.5 (a 1->2 cross-validation gave NO lift) — the >=2 line now clears the single band.
    if n >= 2:
        return float(cm.get("cross_validated_multi", cm["internal_cluster_3plus"]))
    if n >= 1:
        return float(cm["single_implicit_or_external"])
    return float(cm["unverified_frontier"])


def rice(reach: float, impact: float, confidence: float, effort: float,
         cfg: dict | None = None) -> dict:
    """RICE = (Reach × Impact × Confidence) / Effort, with Effort clamped to a floor so a tiny
    estimate cannot explode the score (anti-pattern). Pure. `impact` is an anchored value
    (use impact_anchor()); `confidence` is the mechanical fraction (use confidence_from_evidence);
    `reach` is the real distinct-author × source-breadth count (no estimation)."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    # Distinguish None (=unestimated TBD => neutral default) from an explicit 0 / negative (a genuine
    # trivial / already-half-built quick-win) — `effort or default` wrongly swallowed an explicit 0
    # into the TBD default, understating a trivial win's RICE ~4x. Explicit 0/neg clamps to the floor.
    e = float(sc.get("effort_tbd_default", 2.0)) if effort is None else float(effort)
    eff = max(float(sc.get("effort_min", 0.5)), e)
    raw = (max(0.0, float(reach)) * max(0.0, float(impact)) * max(0.0, float(confidence))) / eff
    return {"rice_raw": round(raw, 6), "reach": float(reach), "impact": float(impact),
            "confidence": float(confidence), "effort": eff}


def impact_anchor(label: str, cfg: dict | None = None) -> float:
    cfg = cfg or load_config()
    return float(cfg["scoring"]["impact_anchors"].get((label or "medium").lower(), 1.0))


def opportunity(importance: float, satisfaction: float, cfg: dict | None = None) -> float:
    """Ulwick ODI gap score: Importance + max(Importance − Satisfaction, 0). Importance is thus
    double-weighted. High importance + low satisfaction = the #1 gap. Pure; range 0..(2*max)."""
    cfg = cfg or load_config()
    imax = float(cfg["scoring"].get("opportunity_importance_max", 10.0))
    i = max(0.0, min(imax, float(importance)))
    s = max(0.0, min(float(cfg["scoring"].get("opportunity_satisfaction_max", 10.0)),
                     float(satisfaction)))
    return round(i + max(i - s, 0.0), 4)


def wsjf(user_business_value: float, time_criticality: float, risk_reduction: float,
         job_size: float, cfg: dict | None = None) -> float:
    """WSJF urgency = (UBV + TimeCriticality + RiskReduction) / JobSize. Inputs are Fibonacci
    anchored (1/2/3/5/8/13). JobSize clamped to a floor (same anti-small-divisor rule). Pure.
    Competitor-just-shipped maps TimeCriticality=13 (highest) — the cross-skill differentiator."""
    cfg = cfg or load_config()
    js = max(1.0, float(job_size or 1.0))
    return round((float(user_business_value) + float(time_criticality) +
                  float(risk_reduction)) / js, 4)


# --------------------------------------------------------------------------- time

def now_utc() -> datetime:
    """Clock seam: DEMAND_MINING_NOW / SCHEDULE_NOW override for deterministic tests/replay."""
    for var in ("DEMAND_MINING_NOW", "SCHEDULE_NOW"):
        v = os.environ.get(var)
        if v:
            return parse_ts(v)
    return datetime.now(timezone.utc)


def parse_ts(s: str) -> datetime:
    s = (s or "").strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def age_hours(ts: str, ref: datetime | None = None) -> float:
    ref = ref or now_utc()
    try:
        return max(0.0, (ref - parse_ts(ts)).total_seconds() / 3600.0)
    except Exception:
        return 0.0
