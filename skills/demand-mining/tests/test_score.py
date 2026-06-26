"""T3 — reproducible three-axis scoring: RICE/Opp/WSJF math, Kano tier floor, effort clamp,
monotone confidence, byte-identical reruns, weight-regression gate."""
import json
from lib import load_config, rice, opportunity, wsjf, confidence_from_evidence, impact_anchor
from score import (score_demand, assign_tier, rice_to_final, weight_regression_gate, rerank)

CFG = load_config()


def _p(**kw):
    base = {"reach": 5, "impact_label": "high", "effort_weeks": 2,
            "independent_source_count": 2, "has_internal_explicit": True, "internal_mentions": 3,
            "importance": 9, "satisfaction": 3,
            "user_business_value": 8, "time_criticality": 5, "risk_reduction": 3, "job_size": 5,
            "kano": "performance", "kano_missing": False}
    base.update(kw)
    return base


# ---------------------------------------------------------------- math
def test_rice_effort_clamp_no_explosion():
    big = rice(10, 3, 1.0, 0.01, CFG)        # tiny effort must be clamped, not /0.01
    sane = rice(10, 3, 1.0, 0.5, CFG)
    assert big["effort"] >= CFG["scoring"]["effort_min"]
    assert big["rice_raw"] == sane["rice_raw"]


def test_rice_tbd_effort_neutral():
    r = rice(5, 2, 0.8, None, CFG)
    assert r["effort"] == CFG["scoring"]["effort_tbd_default"]


def test_opportunity_double_weights_importance():
    # Importance + max(I - S, 0); high importance low satisfaction = big gap
    assert opportunity(9, 3, CFG) == 15.0       # 9 + (9-3)
    assert opportunity(5, 8, CFG) == 5.0        # satisfied: 5 + max(5-8,0)=5


def test_confidence_mechanical_monotone():
    lo = confidence_from_evidence(1, False, 1, CFG)
    mid = confidence_from_evidence(1, False, 3, CFG)
    hi = confidence_from_evidence(2, True, 3, CFG)
    assert lo < mid < hi
    assert hi == 1.0 and lo == CFG["scoring"]["confidence_map"]["single_implicit_or_external"]


def test_wsjf_job_size_floor():
    assert wsjf(8, 13, 3, 0, CFG) == round((8 + 13 + 3) / 1.0, 4)   # job_size 0 -> floor 1


def test_rice_to_final_bounded_monotone():
    a = rice_to_final(1, CFG)
    b = rice_to_final(10, CFG)
    c = rice_to_final(1e9, CFG)
    assert 0 <= a < b <= 100 and b < 100 and c <= 100 and c > b   # bounded + monotone


# ---------------------------------------------------------------- tiering
def test_kano_must_be_missing_forces_tier0():
    t = assign_tier(2, 1, "must_be", True, CFG)         # low opp/urg but must-be missing
    assert t["tier"] == "tier0"


def test_kano_indifferent_cut():
    assert assign_tier(15, 8, "indifferent", False, CFG)["tier"] == "cut"


def test_tier_matrix():
    assert assign_tier(15, 8, "performance", False, CFG)["tier"] == "tier1"   # high×high
    assert assign_tier(15, 1, "performance", False, CFG)["tier"] == "tier2"   # high×low
    assert assign_tier(2, 8, "performance", False, CFG)["tier"] == "tier2"    # low×high
    assert assign_tier(2, 1, "performance", False, CFG)["tier"] == "backlog"  # low×low


# ---------------------------------------------------------------- reproducibility (T3 core)
def test_score_byte_identical_reruns():
    a = json.dumps(score_demand(_p(), CFG), sort_keys=True)
    b = json.dumps(score_demand(_p(), CFG), sort_keys=True)
    assert a == b


def test_score_full_record_shape():
    s = score_demand(_p(), CFG)
    for k in ("rice", "opportunity_score", "urgency_wsjf", "kano", "final_score", "tier"):
        assert k in s
    assert 0 <= s["final_score"] <= 100


# ---------------------------------------------------------------- weight-regression gate
def _golden():
    items = []
    for i in range(8):
        items.append({"id": f"d{i}", "reach": 2 + i, "impact_label": "high",
                      "effort_weeks": 2, "independent_source_count": 2,
                      "has_internal_explicit": True, "internal_mentions": 3})
    return items


def test_weight_regression_autopass_identity():
    items = _golden()
    g = weight_regression_gate(items, None, None, CFG)
    assert g["decision"] == "auto_pass" and g["metrics"]["kendall_tau"] == 0.0


def test_weight_regression_detects_drift():
    items = _golden()
    # extreme re-weight: zero out reach -> reorder; gate must not silently auto-pass
    new = {"reach": 0.0, "impact": 1.0, "confidence": 1.0, "effort": 1.0}
    g = weight_regression_gate(items, None, new, CFG)
    assert g["decision"] in ("needs_review", "block")


# ---------------------------------------------------------------- batch-3 R3 (T3 effort clamp):
# `eff = max(effort_min, float(effort or effort_tbd_default))` used `or`, which silently swallows an
# explicit effort=0 (a genuine trivial / already-half-built quick-win) and an explicit negative,
# treating them as the TBD neutral default (2.0) instead of clamping to the effort floor (0.5). A
# real trivial win's RICE was understated ~4x so it sank below TBD items in ranking. Explicit-0 /
# negative must clamp to the floor; only None (=unestimated) maps to the TBD neutral default.
def test_rice_explicit_zero_effort_clamps_to_floor_not_tbd():
    floor = CFG["scoring"]["effort_min"]
    tbd = CFG["scoring"]["effort_tbd_default"]
    assert floor < tbd  # precondition: the bug is only observable when these differ
    r0 = rice(10, 2.0, 1.0, 0, CFG)
    rneg = rice(10, 2.0, 1.0, -3, CFG)
    rtbd = rice(10, 2.0, 1.0, None, CFG)
    assert r0["effort"] == floor, "explicit 0 effort must clamp to the floor, not become TBD"
    assert rneg["effort"] == floor, "negative effort must clamp to the floor"
    assert r0["rice_raw"] > rtbd["rice_raw"], "a trivial (0-effort) win must outrank a TBD item"
    assert rtbd["effort"] == tbd, "None effort stays the neutral TBD default (unchanged)"
