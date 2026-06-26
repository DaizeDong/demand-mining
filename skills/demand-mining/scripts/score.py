#!/usr/bin/env python3
"""Three-axis prioritization (Acceptance Gate T3). PURE aggregation — byte-identical across runs.

The per-axis INPUTS are PROPOSED upstream by the pinned, temperature-0 LLM judge with anchored
rubric samples (that step lives in SKILL.md, outside this deterministic boundary). THIS file is
the pure aggregator that disposes them into a reproducible ordering. The three axes are kept
ORTHOGONAL (never collapsed into one number — that hides the trade-off):

  A) demand strength  = Opportunity/ODI  (Importance + max(Importance−Satisfaction,0))   "how strong"
  B) ordering         = RICE             ((Reach×Impact×Confidence)/Effort)               "do first?"
  C) urgency          = WSJF             ((UBV+TimeCriticality+RiskReduction)/JobSize)     "how soon"
  + Kano gate (orthogonal): a missing/broken Must-be is forced to Tier0, decoupled from the score.

`final_score` (0-100) is a BOUNDED, monotone, saturating transform of RICE used only as the
ordering metric and push threshold; it never replaces the orthogonal display of all three axes.
Effort/JobSize are clamped (anti-pattern: small-divisor explosions). Tier is a deterministic 2D
matrix (demand × urgency) with the Kano floor on top — only tier BANDS are argued, never single
points (anti false-precision).
"""
from __future__ import annotations

import json
import sys

from lib import (load_config, rice as rice_calc, opportunity as opp_calc, wsjf as wsjf_calc,
                 confidence_from_evidence, impact_anchor)

TIER0, TIER1, TIER2, BACKLOG, CUT = "tier0", "tier1", "tier2", "backlog", "cut"


def grade(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 75:
        return "B+"
    if score >= 65:
        return "B"
    if score >= 55:
        return "C+"
    if score >= 45:
        return "C"
    return "D"


def rice_to_final(rice_raw: float, cfg: dict | None = None) -> float:
    """Bounded saturating map RICE→[0,100): 100·r/(r+k). Monotone increasing in rice_raw, needs
    no arbitrary ceiling. k = the RICE value that scores 50 (config `rice_half_score`)."""
    cfg = cfg or load_config()
    k = float(cfg["scoring"].get("rice_half_score", 6.0))
    r = max(0.0, float(rice_raw))
    return round(100.0 * r / (r + k), 4) if (r + k) > 0 else 0.0


def assign_tier(opportunity_score: float, urgency_wsjf: float, kano: str | None,
                kano_missing: bool, cfg: dict | None = None) -> dict:
    """Deterministic tiering (replay-safe). Step0 Kano floor → Step1 2D matrix.
      * Kano indifferent/reverse        → CUT (noise; do not build).
      * Kano must_be AND currently missing/broken → TIER0 (immediate stop-the-bleed), score-decoupled.
      * high demand × high urgency       → TIER1 (this week)
      * high demand × low urgency        → TIER2 (this month, strategic)
      * low demand × high urgency        → TIER2 (quick-fix priority)
      * low × low                        → BACKLOG
    Thresholds are config-tunable; the function only argues bands."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    k = (kano or "").lower()
    if k in ("indifferent", "reverse"):
        return {"tier": CUT, "reason": f"kano={k} (not a real demand)"}
    if sc.get("kano_must_be_to_tier0", True) and k == "must_be" and kano_missing:
        return {"tier": TIER0, "reason": "kano=must_be and missing/broken (immediate)"}

    opp_hi = float(sc.get("opportunity_high", 10.0))
    urg_hi = float(sc.get("wsjf_high", 3.0))
    demand_high = float(opportunity_score) >= opp_hi
    urgent = float(urgency_wsjf) >= urg_hi
    if demand_high and urgent:
        return {"tier": TIER1, "reason": "high demand × high urgency"}
    if demand_high and not urgent:
        return {"tier": TIER2, "reason": "high demand, strategic schedule"}
    if (not demand_high) and urgent:
        return {"tier": TIER2, "reason": "low demand but urgent quick-fix"}
    return {"tier": BACKLOG, "reason": "low demand × low urgency"}


def score_demand(proposal: dict, cfg: dict | None = None) -> dict:
    """Aggregate one demand's three axes + Kano into a reproducible record. PURE.

    `proposal` carries the upstream temperature-0 estimates:
      reach (real distinct-author × source-breadth count), impact_label, effort_weeks (or None=TBD),
      independent_source_count, has_internal_explicit, internal_mentions,
      importance(0-10), satisfaction(0-10),
      user_business_value, time_criticality, risk_reduction, job_size (WSJF fibonacci),
      kano, kano_missing, velocity (optional).
    Returns rice{...}, opportunity_score, urgency_wsjf, final_score, grade, tier, plus the axes
    echoed so the card/digest can display all three orthogonally."""
    cfg = cfg or load_config()

    conf = confidence_from_evidence(
        int(proposal.get("independent_source_count", 0)),
        bool(proposal.get("has_internal_explicit", False)),
        int(proposal.get("internal_mentions", 0)),
        cfg,
    )
    imp = impact_anchor(proposal.get("impact_label", "medium"), cfg)
    rc = rice_calc(float(proposal.get("reach", 0)), imp, conf,
                   proposal.get("effort_weeks"), cfg)
    final = rice_to_final(rc["rice_raw"], cfg)

    opp = opp_calc(float(proposal.get("importance", 0)),
                   float(proposal.get("satisfaction", 0)), cfg)
    urg = wsjf_calc(float(proposal.get("user_business_value", 0)),
                    float(proposal.get("time_criticality", 0)),
                    float(proposal.get("risk_reduction", 0)),
                    float(proposal.get("job_size", 1)), cfg)

    tier = assign_tier(opp, urg, proposal.get("kano"),
                       bool(proposal.get("kano_missing", False)), cfg)

    return {
        "rice": rc,
        "confidence": conf,
        "impact": imp,
        "opportunity_score": opp,
        "urgency_wsjf": urg,
        "kano": (proposal.get("kano") or "").lower() or None,
        "final_score": final,
        "grade": grade(final),
        "tier": tier["tier"],
        "tier_reason": tier["reason"],
        "velocity": proposal.get("velocity"),
    }


# --------------------------------------------------------------------------- weight-regression gate
# rice_weights is a live tuning surface. Because score_demand is a pure function of the persisted
# proposal, a whole golden set can be re-ranked under any weight vector WITHOUT re-evaluating — the
# gate is fully deterministic (LLM proposes weights, code disposes: auto_pass/needs_review/block).
# (Re-weighting reach/impact/confidence/effort scales rice_raw; final_score order may shift.)

def _final_map(items: list, weights: dict | None, cfg: dict) -> dict:
    use = cfg
    if weights is not None:
        use = json.loads(json.dumps(cfg))
        use["scoring"]["rice_weights"] = weights
    out = {}
    for it in items:
        rw = use["scoring"]["rice_weights"]
        conf = confidence_from_evidence(int(it.get("independent_source_count", 2)),
                                        bool(it.get("has_internal_explicit", True)),
                                        int(it.get("internal_mentions", 3)), use)
        imp = impact_anchor(it.get("impact_label", "medium"), use)
        # apply per-factor weights as multiplicative emphasis (weight 1.0 = neutral), then RICE.
        rc = rice_calc(float(it.get("reach", 0)) * float(rw.get("reach", 1.0)),
                       imp * float(rw.get("impact", 1.0)),
                       conf * float(rw.get("confidence", 1.0)),
                       (float(it.get("effort_weeks") or use["scoring"]["effort_tbd_default"]) *
                        float(rw.get("effort", 1.0))), use)
        out[it["id"]] = rice_to_final(rc["rice_raw"], use)
    return out


def _kendall_tau_distance(order_a: list, order_b: list) -> float:
    pos = {x: i for i, x in enumerate(order_b)}
    seq = [pos[x] for x in order_a if x in pos]
    n = len(seq)
    if n < 2:
        return 0.0
    disc = sum(1 for i in range(n) for j in range(i + 1, n) if seq[i] > seq[j])
    return round(disc / (n * (n - 1) / 2.0), 6)


def rerank(items: list, weights: dict | None = None, cfg: dict | None = None) -> list:
    cfg = cfg or load_config()
    fm = _final_map(items, weights, cfg)
    return [i for i, _ in sorted(fm.items(), key=lambda kv: (-kv[1], str(kv[0])))]


def rank_drift(items: list, weights_a: dict | None, weights_b: dict | None,
               cfg: dict | None = None, top_n: int | None = None) -> dict:
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    fa, fb = _final_map(items, weights_a, cfg), _final_map(items, weights_b, cfg)
    oa = [i for i, _ in sorted(fa.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    ob = [i for i, _ in sorted(fb.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    tau = _kendall_tau_distance(oa, ob)
    pos_b = {x: i for i, x in enumerate(ob)}
    max_shift = max((abs(i - pos_b[x]) for i, x in enumerate(oa)), default=0)
    floor = sc.get("min_score_to_push", 70)
    push_a = {i for i, v in fa.items() if v >= floor}
    push_b = {i for i, v in fb.items() if v >= floor}
    churned = sorted(push_a ^ push_b)
    denom = len(items) or 1
    n = top_n or max(1, min(len(items), len(items) // 2))
    top_left = set(oa[:n]) - set(ob[:n])
    return {"kendall_tau": tau, "max_rank_shift": max_shift,
            "push_floor_churn_frac": round(len(churned) / denom, 6),
            "push_floor_churned": churned, "top_n": n,
            "top_n_churn_frac": round(len(top_left) / float(n), 6)}


def weight_regression_gate(items: list, old_weights: dict | None, new_weights: dict | None,
                           cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    b = cfg["scoring"].get("weight_regression", {}) or {}
    max_tau, max_churn = float(b.get("max_tau", 0.25)), float(b.get("max_push_churn_frac", 0.20))
    cat_tau, cat_churn = float(b.get("catastrophic_tau", 0.6)), float(b.get("catastrophic_churn_frac", 0.5))
    d = rank_drift(items, old_weights, new_weights, cfg)
    tau, churn = d["kendall_tau"], d["push_floor_churn_frac"]
    reasons = []
    if tau >= cat_tau:
        reasons.append(f"catastrophic rank reversal: kendall_tau {tau} >= {cat_tau}")
    if churn >= cat_churn:
        reasons.append(f"catastrophic push-floor churn: {churn} >= {cat_churn}")
    if reasons:
        decision = "block"
    else:
        over = []
        if tau > max_tau:
            over.append(f"rank drift over budget: kendall_tau {tau} > {max_tau}")
        if churn > max_churn:
            over.append(f"push-floor churn over budget: {churn} > {max_churn}")
        decision, reasons = ("needs_review", over) if over else ("auto_pass", [])
    return {"decision": decision, "reasons": reasons, "metrics": d,
            "budget": {"max_tau": max_tau, "max_push_churn_frac": max_churn,
                       "catastrophic_tau": cat_tau, "catastrophic_churn_frac": cat_churn}}


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(score_demand(data), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
