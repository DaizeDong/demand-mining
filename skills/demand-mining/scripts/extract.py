#!/usr/bin/env python3
"""Demand extraction — deterministic structuring + grounding (Acceptance Gate T1). Stdlib, PURE.

The INTERPRETIVE work (reading a Discord session, judging intent in context, recovering the
JTBD four forces, doing the three-layer language translation literal→job→emotion) is the LLM's
job in SKILL.md — it cannot be a keyword regex (anti-pattern #10: keyword chitchat filtering
mis-handles "April/Penny"-style ambiguity and sarcasm). THIS file is the deterministic frame the
LLM proposal must satisfy:

  * normalize_intents  — clamp proposed labels to the frozen 8-label enum; classify demand vs noise.
  * is_demand          — does this opinion-unit carry a real demand signal (not praise/chitchat)?
  * verbatim_grounding — every extracted demand MUST quote a span that is locatable in the
                         REDACTED source text; if not locatable → REJECT (research shows omission
                         ≈ 2× fabrication and ~7.7% of quotes are unfindable — so we fail-closed on
                         ungrounded extractions rather than trust them).
  * build_unit         — assemble one validated, grounded, dual-track (explicit|implicit) opinion
                         unit into the canonical shape the pool/score layers consume.

Nothing here ever sees raw PII: callers pass the already-redacted text (run.py redacts first).
"""
from __future__ import annotations

import json
import re
import sys

from lib import (INTENT_LABELS, DEMAND_LABELS, canonical_key, extract_entities,
                 load_config, slug)

# JTBD four forces — Push (struggle with status quo) + Pull (attraction to new) decide "is there a
# real demand"; Anxiety (switching fear) + Habit (old habit to drop) are the IMPLICIT goldmine the
# user did not say out loud. We carry all four so the implicit pool is never dropped.
JTBD_FORCES = ("push", "pull", "anxiety", "habit")
TRACKS_EXPLICIT, TRACK_IMPLICIT = "explicit", "implicit"


def normalize_intents(proposed: list[str]) -> list[str]:
    """Clamp an LLM-proposed label list to the frozen enum, dedup, preserve order. Unknown labels
    are dropped (a new label needs a schema bump, never silent acceptance). Empty → ['chitchat']
    (the safe discard bucket) so a unit with no recognized intent is treated as noise, not demand."""
    out, seen = [], set()
    for lab in (proposed or []):
        s = slug(lab).replace("-", "_").replace(" ", "_")
        if s in INTENT_LABELS and s not in seen:
            seen.add(s)
            out.append(s)
    return out or ["chitchat"]


def is_demand(intents: list[str]) -> bool:
    """A unit carries a real demand iff at least one normalized label is in DEMAND_LABELS.
    praise / how_to_question / chitchat alone => NOT a demand (how_to is support, not a need)."""
    return any(lab in DEMAND_LABELS for lab in normalize_intents(intents))


def _norm_for_match(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def verbatim_grounding(quote: str, redacted_source: str, min_len: int = 6) -> bool:
    """A demand's evidence quote MUST be locatable (substring, whitespace/case-insensitive) in the
    REDACTED source text. Returns False (=> caller REJECTS the extraction) when:
      * quote is too short to be meaningful, or
      * quote is not found in the source (LLM fabricated/paraphrased it).
    This is the fail-closed anti-hallucination gate: an ungrounded demand is dropped, not trusted.
    A placeholder like [PERSON_1] in the quote is fine — it is part of the redacted source too."""
    q = _norm_for_match(quote)
    if len(q) < int(min_len):
        return False
    return q in _norm_for_match(redacted_source)


def jtbd_completeness(jtbd: dict) -> dict:
    """Score how complete a proposed JTBD breakdown is. Not a hard gate (an implicit unit may only
    have anxiety/habit), but we record which forces were recovered so the implicit goldmine is
    auditable. Push|Pull present => a real demand is asserted; Anxiety|Habit present => implicit
    signal captured."""
    present = [f for f in JTBD_FORCES if (jtbd or {}).get(f)]
    return {
        "forces_present": present,
        "has_demand_force": any(f in present for f in ("push", "pull")),
        "has_implicit_force": any(f in present for f in ("anxiety", "habit")),
    }


def build_unit(proposal: dict, redacted_source: str, author_pseudo: str,
               cfg: dict | None = None) -> dict:
    """Validate + structure ONE opinion-unit proposed by the LLM into the canonical demand shape.

    `proposal` (from the temperature-0 extractor in SKILL.md) carries:
       {intents:[...], track: explicit|implicit, aspect, polarity, quote, jtbd:{push,pull,...},
        inferred_job, kano(optional), urgency(optional), segment(optional), message_id}
    Returns {ok, unit?, reject_reason?}. A unit is REJECTED (fail-closed) when it is not a demand,
    or its quote is not verbatim-grounded in the redacted source. The returned unit NEVER contains
    raw text — only the (already redacted) grounded quote + distilled job/aspect."""
    cfg = cfg or load_config()
    intents = normalize_intents(proposal.get("intents"))
    if not is_demand(intents):
        return {"ok": False, "reject_reason": f"not a demand (intents={intents})"}

    quote = proposal.get("quote", "")
    if not verbatim_grounding(quote, redacted_source):
        return {"ok": False, "reject_reason": "quote not verbatim-grounded in redacted source"}

    track = proposal.get("track", TRACKS_EXPLICIT)
    track = track if track in (TRACKS_EXPLICIT, TRACK_IMPLICIT) else TRACKS_EXPLICIT
    aspect = proposal.get("aspect") or ""
    # canonical subject = the INFERRED job (never the literal feature ask) + aspect entities, so
    # two users asking for different literal features of the SAME job collapse correctly.
    job = proposal.get("inferred_job") or aspect or quote
    entities = extract_entities((job + " " + aspect))
    track_id = proposal.get("track_id") or _track_hint(job + " " + aspect, cfg)
    ck = canonical_key(entities, track_id)
    jc = jtbd_completeness(proposal.get("jtbd", {}))

    unit = {
        "canonical_key": ck,
        "intents": intents,
        "demand_track": track,                      # explicit | implicit (dual-track record)
        "taxonomy_track": track_id,                 # onboarding / core-workflow / ...
        "aspect": aspect,
        "inferred_job": job,
        "polarity": proposal.get("polarity", "negative"),
        "quote": quote,                             # already-redacted, grounded span
        "jtbd": {f: (proposal.get("jtbd", {}) or {}).get(f, "") for f in JTBD_FORCES},
        "jtbd_completeness": jc,
        "kano": (proposal.get("kano") or "").lower() or None,
        "urgency": (proposal.get("urgency") or "should").lower(),
        "segment": (proposal.get("segment") or "free").lower(),
        "author_pseudo": author_pseudo,             # HMAC, never raw id
        "message_ref": proposal.get("message_id") or proposal.get("message_ref") or "",
        "entities": entities,
    }
    return {"ok": True, "unit": unit}


def _track_hint(text: str, cfg: dict) -> str:
    """Deterministic taxonomy track by keyword hit count (ties → config order). Same input → same
    label (byte-identical). 'other' is the safe default when nothing matches."""
    hay = (text or "").lower()
    tracks = [t for t in cfg.get("taxonomy", []) if t.get("enabled", True)]
    best, best_key = None, None
    for order, t in enumerate(tracks):
        hits = sum(1 for kw in t.get("keywords", []) if kw and kw.lower() in hay)
        key = (hits, float(t.get("weight", 1.0)), -order)
        if hits > 0 and (best_key is None or key > best_key):
            best_key, best = key, t
    return best["id"] if best else "other"


def main() -> int:
    """CLI: stdin {proposal, redacted_source, author_pseudo} → build_unit result."""
    data = json.loads(sys.stdin.read() or "{}")
    out = build_unit(data.get("proposal", {}), data.get("redacted_source", ""),
                     data.get("author_pseudo", "u_unknown"))
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
