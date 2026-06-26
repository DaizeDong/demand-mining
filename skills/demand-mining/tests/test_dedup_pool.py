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
