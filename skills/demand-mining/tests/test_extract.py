"""T1, demand extraction: intent normalization, demand-vs-noise, verbatim grounding (100%),
implicit not dropped, no raw text in the built unit."""
from extract import (normalize_intents, is_demand, verbatim_grounding, build_unit,
                     jtbd_completeness)


def test_intent_normalization_clamps_enum():
    assert normalize_intents(["Feature-Request", "made_up_label"]) == ["feature_request"]
    assert normalize_intents([]) == ["chitchat"]           # empty -> safe discard bucket


def test_is_demand_vs_noise():
    assert is_demand(["feature_request"]) is True
    assert is_demand(["pain_workaround"]) is True          # strongest implicit signal
    assert is_demand(["praise"]) is False
    assert is_demand(["how_to_question"]) is False         # support, not a demand
    assert is_demand(["chitchat"]) is False


def test_verbatim_grounding_rejects_ungrounded():
    src = "honestly the export keeps timing out so i just copy paste manually now"
    assert verbatim_grounding("the export keeps timing out", src) is True
    assert verbatim_grounding("the import works perfectly", src) is False   # fabricated


def test_build_unit_grounded_demand():
    src = "the csv export keeps timing out so i built a little script to do it myself"
    p = {"intents": ["pain_workaround"], "track": "implicit",
         "aspect": "csv export reliability", "inferred_job": "reliably export my data",
         "quote": "the csv export keeps timing out",
         "jtbd": {"push": "export times out", "habit": "manual script workaround"},
         "message_id": "m1"}
    out = build_unit(p, src, "u_abc")
    assert out["ok"], out
    u = out["unit"]
    assert u["demand_track"] == "implicit"
    assert u["jtbd_completeness"]["has_implicit_force"] is True
    assert u["author_pseudo"] == "u_abc"
    assert u["canonical_key"]                              # non-empty subject key


def test_build_unit_rejects_non_demand_and_ungrounded():
    src = "love this product, great work team"
    assert build_unit({"intents": ["praise"], "quote": "love this product"}, src, "u_x")["ok"] is False
    src2 = "the search is slow"
    bad = build_unit({"intents": ["bug_complaint"], "quote": "the dashboard crashes daily"},
                     src2, "u_y")
    assert bad["ok"] is False and "grounded" in bad["reject_reason"]


def test_grounding_punctuation_insensitive_cuts_omission():
    # Omission (~2x fabrication per the architecture) is the bigger sin: a genuinely-present quote
    # that differs from the source only by PUNCTUATION (comma, em-dash) must still ground, while
    # the anti-fabrication property holds (different CONTENT WORDS are still rejected).
    src = "honestly the export keeps timing out, so i just gave up and copy-paste manually."
    assert verbatim_grounding("the export keeps timing out so i just gave up", src) is True
    assert verbatim_grounding("the import works perfectly fine", src) is False   # fabricated content
    src2 = "the dashboard is so slow it is unusable, i switched to a spreadsheet"
    assert verbatim_grounding("the dashboard is so slow it is unusable - i switched", src2) is True
    assert verbatim_grounding("the dashboard is fast and snappy", src2) is False  # fabricated content


def test_jtbd_completeness_flags_forces():
    jc = jtbd_completeness({"push": "x", "anxiety": "y"})
    assert jc["has_demand_force"] and jc["has_implicit_force"]
    assert set(jc["forces_present"]) == {"push", "anxiety"}


def test_grounding_cjk_short_quote_recall_cuts_omission():
    # Omission (~2x fabrication) is the bigger sin. A CJK demand phrase is information-dense: 4 CJK
    # chars ("批量导出" = batch-export) is a complete, meaningful ask. The char-count min_len (tuned
    # for sparse Latin tokens) wrongly rejected such genuinely-present CJK quotes => real demands
    # silently dropped. A CJK-aware meaningfulness check must accept them WITHOUT weakening either
    # the Latin fabrication guard or the contiguous-substring anti-paraphrase property.
    src = "我真的需要批量导出功能 不然每次都要手动复制 太麻烦了"
    assert verbatim_grounding("批量导出", src) is True       # real 4-char CJK quote, present
    assert verbatim_grounding("导出功能", src) is True       # real 4-char CJK quote, present
    # fabrication / paraphrase still rejected: different content chars, not a contiguous substring
    assert verbatim_grounding("一键同步到云端", src) is False  # fabricated CJK content
    assert verbatim_grounding("批量删除", src) is False        # plausible but not in source
    # Latin fabrication guard unchanged: a too-short Latin fragment is still rejected
    assert verbatim_grounding("the", "the export is slow") is False
    assert verbatim_grounding("a b", "a b c d e f") is False


def test_track_hint_ascii_keyword_word_boundary_not_substring():
    """GAP (batch 6 R1): _track_hint did naive ASCII substring matching, so 'important' hit the
    'import' integrations keyword and 'therapist' hit 'api' -> both mis-tagged 'integrations'. An
    ASCII keyword must match on a WORD BOUNDARY; CJK keywords (no spaces) must stay substring."""
    import extract
    from lib import load_config
    cfg = load_config()
    # false-positive cases: must NOT be 'integrations'
    assert extract._track_hint("this is really important to me", cfg) != "integrations"
    assert extract._track_hint("my therapist recommended it", cfg) != "integrations"
    # true-positive ASCII word still matches (whole word present)
    assert extract._track_hint("please add csv export to the api", cfg) == "integrations"
    # the actual mechanism (isolated): a stem keyword matches its word AND common inflections, but
    # NOT a longer word that merely embeds it (the trap). This is what kills the false-positive
    # without losing morphological recall.
    assert extract._kw_hit("crash", "the app keeps crashing") is True     # inflection kept
    assert extract._kw_hit("import", "the nightly imports broke") is True  # plural kept
    assert extract._kw_hit("import", "this is really important") is False  # trap rejected
    assert extract._kw_hit("api", "my therapist recommended it") is False  # embedded, rejected
    # end-to-end recall: an inflected performance keyword still routes to performance.
    assert extract._track_hint("the app keeps crashing every day", cfg) == "performance"
    # reverse guard: CJK substring matching preserved (no spaces between CJK keywords)
    assert extract._track_hint("注册流程很困难", cfg) in ("onboarding", "core-workflow")
    assert extract._track_hint("界面难用", cfg) == "ui-ux"
