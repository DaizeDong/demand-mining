#!/usr/bin/env python3
"""EOD digest + brainstorm + idempotent registration (Acceptance Gate T4/T7). Stdlib, PURE build.

The EOD five-stage pipeline's deterministic tail: ④ 2D tiering done in score.py, this builds ⑤ —
the structured brainstorm + iteration-direction queue + the human-readable digest. Structure (not
free-form): demands are split into two pools and ordered deterministically:

  * Quick-win  — high demand / low effort (Kano performance|must_be): ship-now bang-for-buck.
  * Big-bet    — high impact / lower confidence (Kano delighter): strategic differentiators.

Each iteration direction shows all THREE orthogonal axes (RICE detail · Opportunity+intensity ·
WSJF urgency), Kano band, velocity trend, linked competitor/hotspot signal, evidence count, and a
suggested horizon (this week / this month / quarter / backlog) + an order number. On an empty day
it writes an honest "今日无合格新需求" — never filler.

The digest is a schedule-reminder idempotent item (key=demand-mining:digest:<date>) so a re-run /
catch-up never double-sends. Catch-up backfill is bounded (an overslept laptop never floods).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import timedelta
from pathlib import Path

from lib import find_config_dir, iso, load_config, now_utc, parse_ts

CATCHUP_CAP = 30


def resolve_archive_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    d = find_config_dir()
    if d:
        return d / "pool"
    return Path.home() / ".demand-mining-config" / "pool"


# --------------------------------------------------------------------------- brainstorm structure

def _is_cut(c: dict) -> bool:
    """A Kano indifferent/reverse demand (or one already tiered 'cut') is noise — never an iteration
    direction. Mirrors iteration_queue's filter so cut noise leaks into NO surface (queue OR pools)."""
    return c.get("tier") == "cut" or (c.get("kano") or "").lower() in ("indifferent", "reverse")


def split_pools(cards: list[dict], cfg: dict | None = None) -> dict:
    """Quick-win vs Big-bet split (deterministic). Quick-win = high opportunity & modest effort &
    Kano in {must_be, performance}. Big-bet = Kano delighter OR (high impact & lower confidence).
    Anything else falls into 'other' (still listed, lower in the queue). Kano cut/indifferent/reverse
    noise is excluded from every pool (it must never be recommended as a brainstorm direction)."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    opp_hi = float(sc.get("opportunity_high", 10.0))
    eff_modest = float(sc.get("quickwin_effort_max", 3.0))
    quick, big, other = [], [], []
    for c in cards:
        if _is_cut(c):
            continue
        kano = (c.get("kano") or "").lower()
        opp = float(c.get("opportunity_score", 0) or 0)
        eff = float((c.get("rice") or {}).get("effort", 99) or 99)
        conf = float((c.get("rice") or {}).get("confidence", 1.0) or 1.0)
        impact = float((c.get("rice") or {}).get("impact", 1.0) or 1.0)
        if opp >= opp_hi and eff <= eff_modest and kano in ("must_be", "performance", ""):
            quick.append(c)
        elif kano == "delighter" or (impact >= 2.0 and conf <= 0.5):
            big.append(c)
        else:
            other.append(c)
    keyf = lambda c: -float(c.get("final_score", 0))
    return {"quick_win": sorted(quick, key=keyf), "big_bet": sorted(big, key=keyf),
            "other": sorted(other, key=keyf)}


def iteration_queue(cards: list[dict], cfg: dict | None = None) -> list[dict]:
    """Ordered iteration-direction queue (deterministic). Order = tier rank (tier0<tier1<tier2<
    backlog) then final_score desc then canonical_key asc (replay-safe tie-break). Each entry
    exposes all three axes so the decision is auditable, never a single opaque number."""
    cfg = cfg or load_config()
    tier_rank = {"tier0": 0, "tier1": 1, "tier2": 2, "backlog": 3, "cut": 9}
    # Kano indifferent/reverse => tier "cut" (砍, do not build): drop it from the actionable queue
    # so noise is never recommended as an iteration direction (an all-noise day => empty queue).
    cards = [c for c in cards if not _is_cut(c)]
    ordered = sorted(cards, key=lambda c: (tier_rank.get(c.get("tier", "backlog"), 5),
                                           -float(c.get("final_score", 0)),
                                           str(c.get("canonical_key", ""))))
    horizon = {"tier0": "immediate", "tier1": "this-week", "tier2": "this-month",
               "backlog": "backlog"}
    out = []
    for i, c in enumerate(ordered, 1):
        rc = c.get("rice") or {}
        out.append({
            "order": i,
            "demand": c.get("title") or c.get("inferred_job", ""),
            "canonical_key": c.get("canonical_key"),
            "tier": c.get("tier"),
            "horizon": horizon.get(c.get("tier", "backlog"), "backlog"),
            "rice": {"reach": rc.get("reach"), "impact": rc.get("impact"),
                     "confidence": rc.get("confidence"), "effort": rc.get("effort"),
                     "rice_raw": rc.get("rice_raw"), "final_score": c.get("final_score")},
            "opportunity_score": c.get("opportunity_score"),
            "intensity": c.get("intensity"),
            "distinct_authors": c.get("distinct_author_count"),
            "urgency_wsjf": c.get("urgency_wsjf"),
            "kano": c.get("kano"),
            "velocity": c.get("velocity"),
            "competitor_ref": c.get("competitor_ref", ""),
            "evidence_count": len(c.get("evidence", [])),
        })
    return out


# --------------------------------------------------------------------------- markdown

def build_markdown(cards: list[dict], coverage: dict | None = None,
                   date: str | None = None, cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    date = date or now_utc().date().isoformat()
    coverage = coverage or {}
    # Count conservation: 合格 (qualified/actionable) must match the rendered body. Kano cut/noise is
    # dropped from the queue AND every pool, so it must NOT be counted as 合格 — surface it as 剔噪
    # instead, so 合格 + 剔噪 == total reconciles (no over-reporting of the actionable count).
    actionable = [c for c in cards if not _is_cut(c)]
    n_cut = len(cards) - len(actionable)
    cov = (f"> 覆盖: 内部需求 {coverage.get('internal',0)} · 外部 {coverage.get('external',0)}"
           f" · 候选 {coverage.get('candidates',0)} · 合格 {len(actionable)} · 剔噪 {n_cut}"
           f" · 推送 {coverage.get('pushed',0)} · 候审合并 {coverage.get('candidate_merge',0)}"
           f" · gen {iso(now_utc())}")
    lines = [f"# Demand Mining EOD — {date}", "", cov, ""]
    # An empty card list OR a day whose every card is Kano cut/noise has ZERO actionable demands:
    # emit the honest empty-day message, never a dangling empty iteration-queue header (filler).
    queue = iteration_queue(cards, cfg)
    if not queue:
        lines += ["**今日无合格新需求** (no demand cleared the evidence + score floor).",
                  "诚实空日，非灌水。", ""]
        return "\n".join(lines)

    pools = split_pools(cards, cfg)

    lines.append("## 迭代方向队列 (顺序 · 需求程度 · 紧迫性)")
    for q in queue:
        rc = q["rice"]
        lines.append(
            f"{q['order']}. **[{q['tier']}/{q['horizon']}] {q['demand']}** — "
            f"final {rc['final_score']} · RICE(R={rc['reach']},I={rc['impact']},"
            f"C={rc['confidence']},E={rc['effort']})={rc['rice_raw']} · "
            f"Opp={q['opportunity_score']}(intensity {q['intensity']},"
            f"{q['distinct_authors']} 人) · WSJF={q['urgency_wsjf']} · "
            f"Kano={q['kano']}" + (f" · 竞品 {q['competitor_ref']}" if q['competitor_ref'] else "")
            + f" · 证据×{q['evidence_count']}")
    lines.append("")

    for name, label in (("quick_win", "⚡ Quick-win (高需求/低工作量)"),
                        ("big_bet", "🎲 Big-bet (高影响/低确信)")):
        pool = pools[name]
        if not pool:
            continue
        lines.append(f"## {label}")
        for c in pool:
            lines.append(f"- {c.get('grade','?')} {c.get('final_score')} — "
                         f"{c.get('title') or c.get('inferred_job','?')} "
                         f"(`{c.get('taxonomy_track', c.get('track','?'))}`, Kano={c.get('kano')})")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- headlines (pushed msg)

_MD_INLINE_NEUTRALIZE = {ord("`"): "'", ord("|"): "/"}


def _inline(s) -> str:
    """Flatten an (already-redacted) string to one injection-safe inline markdown span: collapse all
    whitespace and neutralize the two chars that could break out of an inline context (`` ` `` / |)."""
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip().translate(_MD_INLINE_NEUTRALIZE)


def _truncate_prose(s: str, cap: int) -> str:
    """Trim to <=cap chars at a SENTENCE boundary so a prose summary never ends mid-sentence."""
    s = (s or "").strip()
    if len(s) <= cap:
        return s
    cut = s[:cap]
    ends = [cut.rfind(p) + len(p) for p in ("。", "！", "？", "；", ". ", "! ", "? ") if p in cut]
    ends = [j for j in ends if j >= cap * 0.5]
    return cut[:max(ends)].strip() if ends else cut.rstrip() + "…"


# A demand's defining axis is "how soon + what kind of need" — that is the 【】 domain tag here
# (e.g. 【立即·刚需】), the thing that makes a headline actionable. Cut / indifferent / reverse noise
# never reaches build_headlines (filtered by _is_cut), so only these buildable bands appear.
_HORIZON_CN = {"tier0": "立即", "tier1": "本周", "tier2": "本月", "backlog": "储备"}
_KANO_CN = {"must_be": "刚需", "performance": "期望", "delighter": "惊喜"}


def _demand_tag(card: dict) -> str:
    hz = _HORIZON_CN.get(card.get("tier") or "backlog", "储备")
    kano = _KANO_CN.get((card.get("kano") or "").lower())
    return f"{hz}·{kano}" if kano else hz


def _num(v, default: float = 0.0) -> float:
    """Coerce a score to float for ranking; a missing/None/non-numeric value sorts as ``default``
    rather than raising. score.py always emits a numeric final_score, but a hand-built or malformed
    card must never crash the headlines build."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_headlines(cards: list[dict], coverage: dict | None = None, date: str | None = None,
                    cap: int = 5, digest_hint: str | None = None, cfg: dict | None = None) -> str:
    """The PUSHED daily message: ONE ranked 'headlines' digest, not a message per demand.

    Layout per item (bold headline line so the parts are easy to tell apart):
        **N.【立即·刚需】需求标题**
        <一段人话摘要 — why it is a real demand + what to do, sentence-boundary trimmed>
        grade final_score · RICE=rice_raw · N证据
    The 【】 tag is the demand's urgency·need-type (立即/本周/本月 · 刚需/期望/惊喜) — the axis that
    makes a demand actionable, mirroring daily-hotspots' 领域 tag.

    DELIBERATE DIVERGENCE from daily-hotspots: there is NO per-headline link and NO url anywhere.
    demand-mining mines PRIVATE conversations, redacts at ingest, and its egress gate
    (push_card.deliver's has_pii) aborts on ANY url/handle — so evidence stays private and the pushed
    message carries none. `digest_hint` is a PLAIN-TEXT pointer (never a url) to the full digest in
    the private companion archive. Every copied field is already redacted and is _inline-flattened
    (injection-safe). Kano cut/indifferent noise is excluded; an all-noise day yields the honest
    empty-day line (never filler)."""
    cfg = cfg or load_config()
    date = date or now_utc().date().isoformat()
    coverage = coverage or {}
    all_cards = cards or []
    actionable = [c for c in all_cards if not _is_cut(c)]
    n_cut = len(all_cards) - len(actionable)
    tier_rank = {"tier0": 0, "tier1": 1, "tier2": 2, "backlog": 3, "cut": 9}
    ranked = sorted(actionable, key=lambda c: (tier_rank.get(c.get("tier", "backlog"), 5),
                                               -_num(c.get("final_score")),
                                               str(c.get("canonical_key", ""))))
    top = ranked[:max(1, int(cap))]
    header = (f"📊 **需求头条** · {date}\n"
              f"合格 {len(actionable)} · 精选 {len(top)} · 剔噪 {n_cut}"
              f" · 候选 {coverage.get('candidates', '?')}")
    if not actionable:
        return header + "\n\n今日无合格新需求（诚实空日，非灌水；完整记录见私有归档）。"
    lines = [header, ""]
    for i, c in enumerate(top, 1):
        title = _inline(c.get("title")) or _inline(c.get("inferred_job")) or "?"
        tag = _demand_tag(c)
        why, rec = _inline(c.get("why")), _inline(c.get("recommendation"))
        if why and rec:
            sep = "" if why[-1] in "。！？.!?；;，, " else "。"
            prose = f"{why}{sep}建议：{rec}"
        elif why:
            prose = why
        elif rec:
            prose = f"建议：{rec}"
        else:
            prose = _inline(c.get("inferred_job"))
        summ = _truncate_prose(prose, 280)
        rc = c.get("rice") or {}
        fs = c.get("final_score")
        rr = rc.get("rice_raw")
        meta = (f"{c.get('grade') or '?'} {fs if isinstance(fs, (int, float)) else '?'}"
                f" · RICE={rr if rr is not None else '?'}"
                f" · {len(c.get('evidence') or [])}证据")
        lines.append(f"**{i}.【{tag}】{title}**")
        if summ:
            lines.append(summ)
        lines.append(meta)
        lines.append("")
    extra = len(actionable) - len(top)
    hint = _inline(digest_hint) if digest_hint else ""
    if hint:
        note = f"；另有 {extra} 条见完整版" if extra > 0 else ""
        lines.append(f"📄 完整版（全部字段 + RICE + 证据）: {hint}{note}")
    else:
        lines.append(f"另有 {extra} 条合格需求；完整卡片见当日私有归档。" if extra > 0
                     else "完整卡片见当日私有归档。")
    return "\n".join(lines)


def write_digest_file(markdown: str, archive_dir: str | None = None,
                      date: str | None = None) -> Path:
    date = date or now_utc().date().isoformat()
    base = resolve_archive_dir(archive_dir) / "digests" / date[:4]
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{date}.md"
    path.write_text(markdown, encoding="utf-8", newline="\n")
    return path


def register_digest_item(ledger, date: str | None = None, summary: str = "") -> dict:
    date = date or now_utc().date().isoformat()
    key = f"demand-mining:digest:{date}"
    ext = {"x_demand_mining_digest_date": date, "x_demand_mining_digest_summary": summary[:200]}
    args = ["--title", f"demand-mining digest {date}", "--kind", "task",
            "--source", "demand-mining", "--idempotency-key", key,
            "--ext", json.dumps(ext, ensure_ascii=False)]
    return ledger._run("add", args)


# --------------------------------------------------------------------------- catch-up (pure)

def missed_digest_dates(last_run, now=None, cap: int = CATCHUP_CAP,
                        tz_offset_h: float = 0.0) -> list[str]:
    """Local calendar dates whose digest was missed since the watermark (bounded). Pure.
      normal run → [today]; overslept N → [today-N+1..today]; same-day re-run → []; cold start →
      [today]; long outage → most-recent `cap` dates, today always present."""
    off = timedelta(hours=float(tz_offset_h))
    now_dt = (parse_ts(now) if now else now_utc()) + off
    today = now_dt.date()
    if not last_run:
        return [today.isoformat()]
    try:
        last_date = (parse_ts(last_run) + off).date()
    except Exception:
        return [today.isoformat()]
    if last_date >= today:
        return []
    start = max(last_date + timedelta(days=1), today - timedelta(days=max(0, int(cap)) - 1))
    out, d = [], start
    while d <= today:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def catch_up_digests(ledger, last_run, now=None, cap: int = CATCHUP_CAP,
                     tz_offset_h: float = 0.0) -> list[str]:
    dates = missed_digest_dates(last_run, now=now, cap=cap, tz_offset_h=tz_offset_h)
    for d in dates:
        try:
            register_digest_item(ledger, date=d, summary="catch-up")
        except Exception:
            pass
    return dates


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    cards = data.get("cards", data if isinstance(data, list) else [])
    print(build_markdown(cards, data.get("coverage"), data.get("date")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
