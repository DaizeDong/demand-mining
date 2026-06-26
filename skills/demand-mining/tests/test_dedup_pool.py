"""T2 dedup correctness · T7 cross-day evolution · intensity anti-stuffing · author merge."""
from lib import load_config, canonical_key, simhash, extract_entities, intensity
import dedup as dd

CFG = load_config()
EXT = dd.EXT


def _row(title, job, track, score, sources=None, competitor=""):
    ck = canonical_key(extract_entities(title + " " + job), track)
    text = title + " " + job
    ext = {
        EXT + "canonical_key": ck, EXT + "simhash": simhash(text), EXT + "text": text,
        EXT + "first_seen": "2026-06-20T12:00:00Z", EXT + "last_seen": "2026-06-24T12:00:00Z",
        EXT + "last_score": score, EXT + "source_set": sources or ["discord", "hackernews"],
        EXT + "competitor_status": competitor, EXT + "push_count": 1, EXT + "samples": [],
        EXT + "authors": [],
    }
    return {"idempotency_key": ck, "ext": ext}


def _cand(title, job, track, score, sources=None, competitor=""):
    ck = canonical_key(extract_entities(title + " " + job), track)
    ev = [{"source": s, "channel": s, "redacted_snippet": "x", "ts": "2026-06-25T11:00:00Z"}
          for s in (sources or ["discord", "hackernews"])]
    return {"canonical_key": ck, "title": title, "summary": "", "inferred_job": job,
            "track": track, "final_score": score, "evidence": ev,
            "source_set": sources or ["discord", "hackernews"], "competitor_status": competitor}


# ---------------------------------------------------------------- T2
def test_exact_match():
    row = _row("dark mode", "reduce eye strain at night", "ui-ux", 70)
    cand = _cand("dark mode", "reduce eye strain at night", "ui-ux", 72)
    assert dd.match_existing(cand, [row], CFG) is not None


def test_distinct_no_match():
    row = _row("dark mode toggle", "reduce eye strain", "ui-ux", 70)
    cand = _cand("csv export api", "bulk export records via api", "integrations", 70)
    assert dd.match_existing(cand, [row], CFG) is None
    assert dd.in_candidate_band(cand, [row], CFG) is None


def test_near_dup_rewrite_matches():
    row = _row("slack integration for notifications", "get alerts in slack channel",
               "integrations", 70, sources=["discord", "github"])
    cand = _cand("slack notification integration", "receive alerts in a slack channel",
                 "integrations", 71, sources=["discord", "github"])
    assert dd.match_existing(cand, [row], CFG) is not None


# ---------------------------------------------------------------- T7 evolution
def test_decide_new():
    cand = _cand("brand new onboarding wizard request", "guide first run setup", "onboarding", 80)
    assert dd.decide(cand, None, CFG)["branch"] == dd.NEW


def test_decide_suppress_small_delta():
    row = _row("dark mode", "reduce eye strain", "ui-ux", 70)
    cand = _cand("dark mode", "reduce eye strain", "ui-ux", 72)   # +2
    m = dd.match_existing(cand, [row], CFG)
    assert dd.decide(cand, m, CFG)["branch"] == dd.SUPPRESS


def test_decide_resurface_on_score_jump():
    row = _row("dark mode", "reduce eye strain", "ui-ux", 55)
    cand = _cand("dark mode", "reduce eye strain", "ui-ux", 80)   # +25
    m = dd.match_existing(cand, [row], CFG)
    assert dd.decide(cand, m, CFG)["branch"] == dd.RESURFACE


def test_decide_resurface_on_competitor_shipped():
    row = _row("bulk export", "export many records", "integrations", 60, competitor="")
    cand = _cand("bulk export", "export many records", "integrations", 61,
                 competitor="competitorX shipped it")
    m = dd.match_existing(cand, [row], CFG)
    assert dd.decide(cand, m, CFG)["branch"] == dd.RESURFACE


def test_decide_resurface_crossing_two_sources():
    row = _row("api rate limit", "higher api quota", "pricing-plans", 65, sources=["discord"])
    cand = _cand("api rate limit", "higher api quota", "pricing-plans", 66,
                 sources=["discord", "reddit"])
    m = dd.match_existing(cand, [row], CFG)
    assert dd.decide(cand, m, CFG)["branch"] == dd.RESURFACE


# ---------------------------------------------------------------- intensity anti-stuffing
def test_intensity_distinct_author_only():
    # one author shouting 3x must NOT triple intensity; three distinct authors do add up.
    one_loud = [{"author_hash": "u_a", "urgency": "need", "segment": "pro"}] * 3
    three = [{"author_hash": f"u_{i}", "urgency": "need", "segment": "pro"} for i in "abc"]
    i1 = intensity(one_loud, CFG)
    i3 = intensity(three, CFG)
    assert i1["distinct_author_count"] == 1 and i1["mention_count"] == 3
    assert i3["distinct_author_count"] == 3
    assert i3["intensity"] > i1["intensity"]


def test_intensity_keeps_author_max_escalation():
    # same author later escalates should->blocking: keep the strongest contribution
    auths = [{"author_hash": "u_a", "urgency": "should", "segment": "free"},
             {"author_hash": "u_a", "urgency": "blocking", "segment": "enterprise"}]
    res = intensity(auths, CFG)
    assert res["distinct_author_count"] == 1
    # blocking(3)+enterprise(4)+distinct(1) = 8
    assert res["intensity"] == 8.0


def test_merge_authors_union_and_max():
    prior = [{"author_hash": "u_a", "urgency": "should", "segment": "free"}]
    new = [{"author_hash": "u_a", "urgency": "blocking", "segment": "team"},
           {"author_hash": "u_b", "urgency": "need", "segment": "pro"}]
    merged = dd.merge_authors(prior, new)
    by = {m["author_hash"]: m for m in merged}
    assert set(by) == {"u_a", "u_b"}
    assert by["u_a"]["urgency"] == "blocking"   # escalation kept


# ---------------------------------------------------------------- batch-3 R1 (T2 evolution): decide()
# RESURFACE incomplete vs its own docstring + ARCHITECTURE. Both list an urgency/velocity JUMP as a
# RESURFACE trigger (competitor momentum / trend acceleration via trend-pulse get_trend_velocity),
# but the implementation only inspected score / new-source / competitor-shipped and IGNORED velocity
# entirely — so a demand whose velocity spiked while score/sources/competitor were unchanged stayed
# SUPPRESS and was never re-surfaced to the founder (a now-urgent need goes silent). Guarded so it
# only fires when BOTH prior and current velocity are present and the jump clears the config floor.
def _row_vel(velocity):
    r = _row("dark mode", "reduce eye strain", "ui-ux", 70)
    r["ext"][EXT + "velocity"] = velocity
    return r


def test_decide_resurface_on_velocity_jump():
    row = _row_vel(1.0)
    cand = _cand("dark mode", "reduce eye strain", "ui-ux", 70)   # same score/sources/competitor
    cand["velocity"] = 20.0                                       # urgency spike (trend acceleration)
    assert dd.decide(cand, dd.match_existing(cand, [row], CFG), CFG)["branch"] == dd.RESURFACE


def test_decide_no_overresurface_on_velocity_noise_or_absence():
    # a tiny velocity wiggle must NOT re-push (anti-spam), and absent velocity = unchanged behavior.
    row = _row_vel(1.0)
    wiggle = _cand("dark mode", "reduce eye strain", "ui-ux", 70); wiggle["velocity"] = 1.4
    assert dd.decide(wiggle, dd.match_existing(wiggle, [row], CFG), CFG)["branch"] == dd.SUPPRESS
    absent = _cand("dark mode", "reduce eye strain", "ui-ux", 70)   # no velocity key at all
    assert dd.decide(absent, dd.match_existing(absent, [row], CFG), CFG)["branch"] == dd.SUPPRESS


# ---------------------------------------------------------------- batch-4 R3 (T2 evolution): decide()
# RESURFACE missed "新外部 corroboration" — ARCHITECTURE §3 lists the FIRST external origin validating
# an internal demand as its own RESURFACE trigger, distinct from crossing the >=2 source line. With
# >=2 internal sources already present and score/competitor/velocity flat, external_origin_count going
# 0->1 (a market validation that moves confidence/RICE) was SUPPRESSed and never re-surfaced.
def _row_extcorr(internal=2, external=0):
    r = _row("dark mode", "reduce eye strain", "ui-ux", 70)
    r["ext"][EXT + "external_corroboration"] = {"internal_count": internal,
                                                "external_origin_count": external}
    return r


def test_decide_resurface_on_first_external_corroboration():
    row = _row_extcorr(internal=2, external=0)
    cand = _cand("dark mode", "reduce eye strain", "ui-ux", 70)   # same score/sources/competitor
    cand["external_corroboration"] = {"internal_count": 2, "external_origin_count": 1}
    assert dd.decide(cand, dd.match_existing(cand, [row], CFG), CFG)["branch"] == dd.RESURFACE


def test_decide_no_resurface_when_external_already_present_or_absent():
    # reverse (anti-spam): more external when one already existed (1->2) does NOT re-push; and
    # internal-only both days (external stays 0 / key absent) does NOT re-push.
    row1 = _row_extcorr(internal=2, external=1)
    more = _cand("dark mode", "reduce eye strain", "ui-ux", 70)
    more["external_corroboration"] = {"internal_count": 2, "external_origin_count": 2}
    assert dd.decide(more, dd.match_existing(more, [row1], CFG), CFG)["branch"] == dd.SUPPRESS
    row0 = _row_extcorr(internal=2, external=0)
    none_ = _cand("dark mode", "reduce eye strain", "ui-ux", 70)   # no external_corroboration key
    assert dd.decide(none_, dd.match_existing(none_, [row0], CFG), CFG)["branch"] == dd.SUPPRESS


# ---------------------------------------------------------------- T2 batch-5: escalation into Tier0
def test_decide_resurface_on_escalation_into_tier0():
    """A demand that escalates INTO tier0 (Kano must_be now missing = stop-the-bleed) must RESURFACE
    for immediate attention even with score/sources/velocity/competitor unchanged — a now-missing
    must-be is the highest-urgency transition (ARCHITECTURE: must_be missing -> immediate Tier0;
    RESURFACE on 紧迫跳变). Without this it SUPPRESSes and the critical item is never re-pushed."""
    row = _row("scheduled report export", "email reports on a schedule", "core-workflow", 60)
    row["ext"][EXT + "tier"] = "tier2"
    cand = _cand("scheduled report export", "email reports on a schedule", "core-workflow", 60)
    cand["tier"] = "tier0"          # escalated (kano must_be now missing)
    m = dd.match_existing(cand, [row], CFG)
    assert m is not None
    d = dd.decide(cand, m, CFG)
    assert d["branch"] == dd.RESURFACE
    assert d["delta"].get("escalated_to_tier0") is True


def test_decide_no_resurface_when_staying_or_leaving_tier0():
    """Guard (anti-spam / no over-resurface): a demand ALREADY tier0 that stays tier0 does NOT
    re-push (already surfaced), and a de-escalation (tier0 -> tier2) is not a resurface either.
    Only the not-tier0 -> tier0 escalation transition fires."""
    base_row = _row("scheduled report export", "email reports on a schedule", "core-workflow", 60)
    base_cand = _cand("scheduled report export", "email reports on a schedule", "core-workflow", 60)
    # staying tier0
    base_row["ext"][EXT + "tier"] = "tier0"
    base_cand["tier"] = "tier0"
    m = dd.match_existing(base_cand, [base_row], CFG)
    assert dd.decide(base_cand, m, CFG)["branch"] == dd.SUPPRESS
    # leaving tier0 (de-escalation)
    base_row["ext"][EXT + "tier"] = "tier0"
    base_cand["tier"] = "tier2"
    m = dd.match_existing(base_cand, [base_row], CFG)
    assert dd.decide(base_cand, m, CFG)["branch"] == dd.SUPPRESS


def test_decide_no_resurface_on_score_drop():
    """GAP (batch 6 R2): decide() used abs(score delta) >= jump, so a demand whose score DROPPED
    by >= jump (it became LESS important — a declining demand) was re-pushed as a RESURFACE
    evolution UPDATE card. RESURFACE means "became more important/urgent"; a decline must SUPPRESS
    (the fade path handles decline). Directional fix: only an UPWARD jump re-surfaces."""
    row = _row("dark mode", "reduce eye strain", "ui-ux", 80)
    cand = _cand("dark mode", "reduce eye strain", "ui-ux", 55)   # -25 DROP, all else flat
    m = dd.match_existing(cand, [row], CFG)
    assert dd.decide(cand, m, CFG)["branch"] == dd.SUPPRESS
    # reverse guard: an equivalent UPWARD jump still RESURFACEs (no over-suppression)
    row2 = _row("dark mode", "reduce eye strain", "ui-ux", 55)
    cand2 = _cand("dark mode", "reduce eye strain", "ui-ux", 80)  # +25 RISE
    m2 = dd.match_existing(cand2, [row2], CFG)
    assert dd.decide(cand2, m2, CFG)["branch"] == dd.RESURFACE


def test_short_tech_acronym_entities_prevent_canonical_collapse():
    """GAP (batch 6 R3): extract_entities dropped every <3-char ASCII token, so meaningful tech
    acronyms (ai/ui/ux/ml/vr/ar/qa) vanished from the subject -> two DISTINCT demands collapsed to
    the same canonical_key ('add AI mode' == 'add VR mode' == 'add|mode::other'), a false merge.
    A small frozen whitelist keeps these acronyms as entities; generic stop 2-char tokens stay out."""
    ai = extract_entities("please add AI mode")
    vr = extract_entities("please add VR mode")
    assert "ai" in ai and "vr" in vr                       # acronym kept (recall)
    assert canonical_key(ai, "other") != canonical_key(vr, "other")   # no false collapse
    # reverse guard: generic 2-char stop tokens are STILL dropped (no noise re-admitted)
    ents = extract_entities("is it on to of in")
    assert "is" not in ents and "to" not in ents and "of" not in ents and "in" not in ents
    # reverse guard: existing 3+ char behavior unchanged
    assert extract_entities("bulk export records") == ["bulk", "export", "records"]
