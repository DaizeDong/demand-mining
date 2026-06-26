#!/usr/bin/env python3
"""Deterministic verify gate (Acceptance Gate T4 schema + anti-filler + T6 egress DLP). Stdlib.

LLM proposes, this gate disposes — fail-closed, final veto. A demand card is BLOCKED (never pushed,
never archived to the pool) unless EVERY rule holds:

  * canonical_key + taxonomy track present       * final_score in [0,100]
  * three axes present (rice, opportunity, wsjf)  * tier in the enum
  * >=1 INTERNAL evidence unit {channel, redacted_snippet, ts}  (an iteration suggestion with no
    internal grounding is forbidden filler — anti-pattern: no-evidence filler)
  * independent_source_count >= min_independent_sources for any card that crosses the push floor
  * egress DLP: the card carries NO residual PII (redact.has_pii over the user-visible fields) —
    fail-closed, nothing with leftover PII is ever pushed/archived.

Missing/short/PII = explicit gap returned, not a silent pass. Used per-card (validate_card) and as
the batch quality filter (gate_batch). An honest empty day returns empty_day=True, never filler.
"""
from __future__ import annotations

import json
import sys

from lib import load_config
from redact import has_pii

_AXES = ("rice", "opportunity_score", "urgency_wsjf")
_TIERS = ("tier0", "tier1", "tier2", "backlog", "cut")
_PII_FIELDS = ("title", "summary", "inferred_job", "why", "action", "recommendation")


def validate_card(card: dict, cfg: dict | None = None) -> tuple[bool, list[str]]:
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    errs = []

    if not card.get("canonical_key"):
        errs.append("missing canonical_key")
    if not (card.get("taxonomy_track") or card.get("track")):
        errs.append("missing taxonomy track")

    for ax in _AXES:
        if card.get(ax) is None:
            errs.append(f"missing axis {ax}")

    try:
        fs = float(card.get("final_score"))
        if not (0 <= fs <= 100):
            errs.append(f"final_score out of [0,100]: {fs}")
    except (TypeError, ValueError):
        errs.append("final_score missing/non-numeric")

    if card.get("tier") not in _TIERS:
        errs.append(f"tier not in {_TIERS}: {card.get('tier')}")

    # >=1 internal evidence (the no-filler rule): an iteration candidate MUST be grounded in at
    # least one internal (Discord) demand snippet. External corroboration is a bonus, not a sub.
    ev = card.get("evidence") or []
    internal = [e for e in ev if (e.get("channel") or e.get("source"))
                and e.get("redacted_snippet") is not None and e.get("ts")
                and (e.get("origin_type") or "internal") == "internal"]
    # tolerate evidence that doesn't tag origin_type: treat discord/internal channels as internal
    if not internal:
        internal = [e for e in ev if (e.get("channel") in ("discord", "internal")
                    or (e.get("source") == "discord")) and e.get("ts")]
    if len(internal) < 1:
        errs.append("need >=1 internal evidence {channel,redacted_snippet,ts}")

    # >=2 independent source red line only enforced for push-floor crossers (a backlog implicit
    # single-complaint demand is allowed in the pool but cannot be pushed as decision-grade).
    isc = int(card.get("independent_source_count", 0) or 0)
    min_src = int(sc.get("min_independent_sources", 2))
    if float(card.get("final_score", 0) or 0) >= float(sc.get("min_score_to_push", 70)) \
            and isc < min_src:
        errs.append(f"push-grade card needs independent_source_count >= {min_src}, have {isc}")

    # egress DLP (fail-closed): no residual PII may ride along in user-visible text.
    leaked = [f for f in _PII_FIELDS if card.get(f) and has_pii(str(card.get(f)))]
    if leaked:
        errs.append(f"residual PII in fields {leaked} (egress blocked)")
    # evidence[].redacted_snippet is ALSO user-visible (rendered into the pushed card + archived to
    # the pool), so it must clear the SAME egress DLP — a residual email/phone hiding in a snippet is
    # an exfil path identical to one in the title. Fail-closed per evidence unit.
    snippet_leaks = [i for i, e in enumerate(ev)
                     if (e.get("redacted_snippet") or e.get("quote"))
                     and has_pii(str(e.get("redacted_snippet") or e.get("quote")))]
    if snippet_leaks:
        errs.append(f"residual PII in evidence snippet idx {snippet_leaks} (egress blocked)")

    return (len(errs) == 0, errs)


def gate_batch(cards: list[dict], cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    passed, blocked = [], []
    for c in cards:
        ok, errs = validate_card(c, cfg)
        (passed if ok else blocked).append(c if ok else {"title": c.get("title", "?"),
                                                          "errors": errs})

    min_push = float(sc.get("min_score_to_push", 70))
    min_arch = float(sc.get("min_score_to_archive", 40))
    max_push = int(cfg.get("push", {}).get("max_per_day", 5))

    # Tier0 (must-be missing) is always push-eligible regardless of score (stop-the-bleed), then
    # the score floor for the rest. Never filler: only floor-clearing cards are pushable/archivable.
    tier0 = [c for c in passed if c.get("tier") == "tier0"]
    rest = sorted([c for c in passed if c.get("tier") != "tier0"
                   and float(c.get("final_score", 0)) >= min_push],
                  key=lambda c: -float(c.get("final_score", 0)))
    pushable = (tier0 + rest)[:max_push]
    archivable = [c for c in passed if c.get("tier") == "tier0"
                  or float(c.get("final_score", 0)) >= min_arch]
    digest_only = [c for c in archivable if c not in pushable]

    return {"passed": passed, "blocked": blocked, "pushable": pushable,
            "archivable": archivable, "digest_only": digest_only,
            "empty_day": len(archivable) == 0}


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    if isinstance(data, list):
        print(json.dumps(gate_batch(data), ensure_ascii=False))
        return 0
    ok, errs = validate_card(data)
    print(json.dumps({"ok": ok, "errors": errs}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
