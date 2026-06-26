"""T1 — demand extraction: intent normalization, demand-vs-noise, verbatim grounding (100%),
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


def test_jtbd_completeness_flags_forces():
    jc = jtbd_completeness({"push": "x", "anxiety": "y"})
    assert jc["has_demand_force"] and jc["has_implicit_force"]
    assert set(jc["forces_present"]) == {"push", "anxiety"}
