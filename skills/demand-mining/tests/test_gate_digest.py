"""T4, verify gate (schema + >=1 internal evidence + egress DLP, fail-closed) and EOD digest
(quick-win/big-bet split, iteration queue order, catch-up dates, empty-day honesty)."""
from lib import load_config
from verify_gate import validate_card, gate_batch
from digest import (split_pools, iteration_queue, missed_digest_dates, build_markdown,
                    build_headlines)
from redact import redact as _core_redact
import push_card as _pc

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
            "title": "contact bob@example.com" if pii else "faster csv export",
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


def test_iteration_queue_excludes_cut_noise():
    # Kano indifferent/reverse => tier "cut" (砍, do not build). A cut demand is noise and must
    # NOT surface as an iteration direction; only real (non-cut) demands are recommended.
    cut = _card(score=5, tier="cut", kano="indifferent"); cut["canonical_key"] = "noise|x::other"
    real = _card(score=80, tier="tier1", kano="performance"); real["canonical_key"] = "real|y::ui-ux"
    q = iteration_queue([cut, real], CFG)
    assert all(x["tier"] != "cut" for x in q), [x["tier"] for x in q]
    assert [x["canonical_key"] for x in q] == ["real|y::ui-ux"]
    assert q[0]["order"] == 1                      # order numbers stay contiguous after the drop
    # all-noise day yields an empty actionable queue (no filler iteration directions)
    assert iteration_queue([cut], CFG) == []


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


# ---------------------------------------------------------------- batch-2 R1 (T4): cut noise must
# not leak into the Big-bet pool. Batch 1 dropped Kano cut/indifferent from the iteration_queue,
# but split_pools still routed a high-impact/low-confidence indifferent card into big_bet, so it
# was recommended as a "Big-bet" iteration direction in the EOD markdown (recommending noise).
def test_split_pools_excludes_cut_noise():
    noise = {"kano": "indifferent", "tier": "cut", "opportunity_score": 2,
             "final_score": 30, "title": "noise idea", "canonical_key": "n::other",
             "rice": {"impact": 3.0, "confidence": 0.5, "effort": 2.0, "rice_raw": 2.25}}
    real = _card()
    pools = split_pools([noise, real], CFG)
    flat = pools["quick_win"] + pools["big_bet"] + pools["other"]
    assert noise not in flat, "Kano cut/indifferent noise must not enter any brainstorm pool"
    assert real in flat, "a genuine demand must still be pooled (no over-filter)"
    md = build_markdown([noise, real], date="2026-06-25")
    assert "noise idea" not in md, "cut noise must never be recommended in the EOD digest"


# ---------------------------------------------------------------- batch-2 R2 (T4): a day whose only
# cards are Kano cut/noise has zero actionable demands => the digest must print the honest empty-day
# message, NOT a dangling "迭代方向队列" header with no items (filler-by-omission).
def test_all_cut_day_is_honest_empty():
    cards = [{"kano": "indifferent", "tier": "cut", "opportunity_score": 1, "final_score": 10,
              "title": "noise1", "canonical_key": "a::other",
              "rice": {"impact": 1, "confidence": 1, "effort": 2, "rice_raw": 0.5}},
             {"kano": "reverse", "tier": "cut", "opportunity_score": 1, "final_score": 5,
              "title": "noise2", "canonical_key": "b::other",
              "rice": {"impact": 1, "confidence": 1, "effort": 2, "rice_raw": 0.2}}]
    md = build_markdown(cards, date="2026-06-25")
    assert "今日无合格新需求" in md, "all-cut day must emit the honest empty-day message"
    assert "迭代方向队列" not in md, "must not emit a dangling empty iteration-queue header"


# ---------------------------------------------------------------- batch-3 R2 (T4 count conservation):
# batches 1-2 correctly dropped Kano cut/noise from the iteration queue AND every brainstorm pool,
# but the EOD coverage header still printed "合格 {len(cards)}" counting the cut noise, so the
# header OVER-reported the qualified/actionable count vs the rendered body (a reader sees 合格 2 but
# only 1 queue item). Conservation: the 合格 header must equal the actionable queue, and the cut
# count must be surfaced so actionable + cut == total reconciles.
def test_eod_coverage_count_excludes_cut_noise():
    import re
    real = _card()  # tier1 performance = actionable
    noise = {"kano": "indifferent", "tier": "cut", "opportunity_score": 1, "final_score": 70,
             "title": "noise idea", "canonical_key": "n::other",
             "rice": {"impact": 1, "confidence": 1, "effort": 2, "rice_raw": 0.5}}
    md = build_markdown([real, noise], coverage={"internal": 2}, date="2026-06-25")
    q = iteration_queue([real, noise], CFG)
    m = re.search(r"合格 (\d+)", md)
    assert m and int(m.group(1)) == len(q) == 1, "合格 header must match the actionable queue (cut excluded)"
    assert re.search(r"剔噪 (\d+)", md).group(1) == "1", "the excluded cut-noise count must be surfaced"
    # a pure-real day is unchanged (no over-correction): 合格 == total, 剔噪 0
    md2 = build_markdown([real], coverage={"internal": 1}, date="2026-06-25")
    assert re.search(r"合格 (\d+)", md2).group(1) == "1"
    assert re.search(r"剔噪 (\d+)", md2).group(1) == "0"


# ---------------------------------------------------------------- batch-4 R1 (T6 egress DLP): the
# fail-closed PII scan only covered top-level user-visible fields (title/summary/...), but
# evidence[].redacted_snippet is ALSO rendered into the pushed card and archived to the pool, a
# residual email/phone hiding in a snippet rode straight through validate_card (ok=True) and leaked.
# Architecture §4 / T6: nothing with leftover PII may ever be pushed/archived (落池后无邮箱/电话).
def test_blocks_residual_pii_in_evidence_snippet():
    c = _card()                                   # clean top-level fields
    c["evidence"][0]["redacted_snippet"] = "pls email me at alice@example.com about this"
    ok, errs = validate_card(c, CFG)
    assert not ok and any("PII" in e and "evidence" in e for e in errs)


def test_clean_evidence_snippet_still_passes():   # reverse: no false-positive on clean snippets
    ok, errs = validate_card(_card(), CFG)
    assert ok, errs


# ---------------------------------------------------------------- headlines (pushed daily message)
def test_headlines_rank_tag_and_meta():
    t0 = _card(score=90, tier="tier0", kano="must_be")
    t0["title"] = "批量导出对账单"
    t1 = _card(score=60, tier="tier1", kano="performance")
    t1["title"] = "移动端离线草稿"
    h = build_headlines([t1, t0], {"candidates": 7}, date="2026-07-15", cap=5)
    # tier0 ranks above tier1 regardless of input order; bold numbered headline + 领域=urgency·need
    assert h.index("批量导出对账单") < h.index("移动端离线草稿")
    assert "**1.【立即·刚需】批量导出对账单**" in h
    assert "**2.【本周·期望】移动端离线草稿**" in h
    assert "📊 **需求头条** · 2026-07-15" in h
    assert "证据" in h and "RICE=" in h


def test_headlines_cap_and_overflow_note():
    cards = [_card(score=90 - i, tier="tier1") for i in range(8)]
    for i, c in enumerate(cards):
        c["title"] = f"需求{i}"
        c["canonical_key"] = f"k|{i}"
    h = build_headlines(cards, cap=5, date="2026-07-15")
    assert "精选 5" in h and "合格 8" in h
    assert "另有 3 条" in h                      # overflow acknowledged, not silently dropped


def test_headlines_excludes_cut_noise():
    real = _card(score=80, tier="tier1", kano="performance"); real["title"] = "真需求"
    noise = _card(score=50, tier="cut", kano="indifferent"); noise["title"] = "噪声"
    h = build_headlines([real, noise], date="2026-07-15")
    assert "真需求" in h and "噪声" not in h
    assert "剔噪 1" in h


def test_headlines_empty_day_is_honest():
    assert "今日无合格新需求" in build_headlines([], {"candidates": 0}, date="2026-07-15")
    # an all-cut day is also an honest empty day (no filler headline)
    cut = _card(tier="cut", kano="reverse")
    assert "今日无合格新需求" in build_headlines([cut], date="2026-07-15")


def test_headlines_prose_from_why_and_recommendation():
    c = _card(tier="tier0", kano="must_be")
    c["why"] = "用户反复手动导出，耗时且易错"
    c["recommendation"] = "一键区间导出"
    h = build_headlines([c], date="2026-07-15")
    assert "用户反复手动导出" in h and "建议：一键区间导出" in h
    assert "。。" not in h                        # sentence-join must not double a full stop


def test_headlines_carries_no_url_and_passes_egress_gate():
    # demand-mining's egress deliver() aborts on ANY url/handle; the headlines must therefore contain
    # neither, and the date header / year ranges must NOT trip the core phone matcher.
    c = _card(tier="tier1"); c["why"] = "2020-2026 讨论持续升温，是高频痛点"
    h = build_headlines([c], date="2026-07-15", digest_hint="私有归档 2026/2026-07-15.md")
    assert "http" not in h and "@" not in h
    assert not _core_redact(h)["found"], "headlines must be clean for the fail-closed has_pii gate"
    ok, detail = _pc.deliver(h, dry_run=True)
    assert ok is True and "dry-run" in detail    # gate does not abort a clean headlines message


def test_headlines_robust_to_malformed_score():
    # score.py always emits a numeric final_score, but a hand-built/malformed card must not crash the
    # build: None sorts as 0 and shows '?'; a non-numeric string must not raise in the sort key.
    bad = [{"tier": "tier1", "title": "空分数", "final_score": None, "canonical_key": "k|a"},
           {"tier": "tier1", "title": "坏分数", "final_score": "NaNlike", "canonical_key": "k|b"},
           {"tier": "tier0", "title": "正常", "final_score": 90, "grade": "A", "canonical_key": "k|c"}]
    h = build_headlines(bad, date="2026-07-15")           # must not raise
    assert "正常" in h and "空分数" in h and "坏分数" in h
    assert "None" not in h                                 # explicit-None score renders '?', never 'None'


def test_headlines_injection_neutralized():
    c = _card(tier="tier0", kano="must_be")
    c["title"] = "evil`code`\n|table"            # backticks / pipe / newline in an (untrusted) field
    h = build_headlines([c], date="2026-07-15")
    line = [ln for ln in h.splitlines() if "evil" in ln][0]
    assert "`" not in line and "|" not in line and "\n" not in line.rstrip()


def test_inline_normalizes_en_em_dashes_at_runtime():
    # runtime injection: an LLM-supplied demand field must never carry an en/em dash into the push.
    import digest as _dg
    em, en, bar = chr(0x2014), chr(0x2013), chr(0x2015)
    assert _dg._inline("batch" + em + "export") == "batch, export"
    for d in (em, en, bar):
        assert d not in _dg._inline("p " + d + " q")
