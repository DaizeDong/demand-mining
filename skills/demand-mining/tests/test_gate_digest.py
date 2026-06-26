"""T4 — verify gate (schema + >=1 internal evidence + egress DLP, fail-closed) and EOD digest
(quick-win/big-bet split, iteration queue order, catch-up dates, empty-day honesty)."""
from lib import load_config
from verify_gate import validate_card, gate_batch
from digest import split_pools, iteration_queue, missed_digest_dates, build_markdown

CFG = load_config()


def _card(score=80, tier="tier1", n=2, internal=True, pii=False, kano="performance"):
    ev = [{"channel": "discord" if internal else "reddit", "origin_type":
           "internal" if internal else "external", "redacted_snippet": "the export is slow",
           "ts": "2026-06-25T11:00:00Z"},
          {"channel": "reddit", "origin_type": "external", "redacted_snippet": "same issue",
           "ts": "2026-06-25T10:00:00Z"}]
    return {"canonical_key": "k|export::integrations", "taxonomy_track": "integrations",
            "rice": {"reach": 5, "impact": 2, "confidence": 1.0, "effort": 2, "rice_raw": 5},
            "opportunity_score": 12, "urgency_wsjf": 4, "kano": kano,
            "final_score": score, "grade": "B", "tier": tier,
            "independent_source_count": n, "evidence": ev,
            "title": "contact bob@x.com" if pii else "faster csv export",
            "why": "users churn", "recommendation": "stream the export"}


# ---------------------------------------------------------------- T4 gate
def test_full_card_passes():
    ok, errs = validate_card(_card(), CFG)
    assert ok, errs


def test_blocks_no_internal_evidence():
    c = _card(internal=False)
    c["evidence"] = [{"channel": "reddit", "origin_type": "external",
                      "redacted_snippet": "x", "ts": "2026-06-25T10:00:00Z"}]
    ok, errs = validate_card(c, CFG)
    assert not ok and any("internal evidence" in e for e in errs)


def test_blocks_residual_pii_egress():
    ok, errs = validate_card(_card(pii=True), CFG)
    assert not ok and any("PII" in e for e in errs)


def test_push_grade_needs_two_sources():
    ok, errs = validate_card(_card(score=90, n=1), CFG)
    assert not ok and any("independent_source_count" in e for e in errs)


def test_tier0_pushable_regardless_of_score():
    low_t0 = _card(score=10, tier="tier0", kano="must_be")
    g = gate_batch([low_t0], CFG)
    assert low_t0 in g["pushable"]        # stop-the-bleed bypasses the score floor


def test_empty_day_honest():
    g = gate_batch([_card(score=10, tier="backlog")], CFG)
    assert g["empty_day"] is True and g["pushable"] == []


def test_push_cap():
    cards = []
    for i in range(8):
        c = _card(score=90, tier="tier1")
        c["canonical_key"] = f"k{i}"
        c["title"] = f"demand-{i}"
        cards.append(c)
    g = gate_batch(cards, CFG)
    assert len(g["pushable"]) <= CFG["push"]["max_per_day"]


# ---------------------------------------------------------------- digest / brainstorm
def test_split_pools_quickwin_bigbet():
    qw = _card(score=80)
    qw["opportunity_score"] = 15
    qw["rice"] = {"reach": 5, "impact": 2, "confidence": 1.0, "effort": 2, "rice_raw": 5}
    qw["kano"] = "performance"
    bb = _card(score=70)
    bb["kano"] = "delighter"
    pools = split_pools([qw, bb], CFG)
    assert qw in pools["quick_win"]
    assert bb in pools["big_bet"]


def test_iteration_queue_tier_order():
    t0 = _card(score=30, tier="tier0"); t0["canonical_key"] = "a"
    t1 = _card(score=90, tier="tier1"); t1["canonical_key"] = "b"
    bk = _card(score=50, tier="backlog"); bk["canonical_key"] = "c"
    q = iteration_queue([bk, t1, t0], CFG)
    assert [x["tier"] for x in q] == ["tier0", "tier1", "backlog"]
    assert q[0]["order"] == 1


def test_empty_day_markdown_honest():
    md = build_markdown([], {"candidates": 0}, cfg=CFG)
    assert "今日无合格新需求" in md


# ---------------------------------------------------------------- T7 catch-up
def test_catchup_normal_one_day():
    dates = missed_digest_dates("2026-06-24T12:00:00Z", "2026-06-25T12:00:00Z")
    assert dates == ["2026-06-25"]


def test_catchup_same_day_rerun_empty():
    assert missed_digest_dates("2026-06-25T08:00:00Z", "2026-06-25T12:00:00Z") == []


def test_catchup_overslept_backfill_bounded():
    dates = missed_digest_dates("2026-01-01T00:00:00Z", "2026-06-25T12:00:00Z", cap=30)
    assert len(dates) == 30 and dates[-1] == "2026-06-25"


def test_catchup_cold_start_no_storm():
    assert missed_digest_dates("", "2026-06-25T12:00:00Z") == ["2026-06-25"]
