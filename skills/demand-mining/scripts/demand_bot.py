#!/usr/bin/env python3
"""Live demand-tap daemon for demand-mining (persistent Discord gateway service).

Replaces the daily batch for the interactive half: it stays connected (discord.py gateway, Message
Content Intent) and, in real time:
  * @-mention or DM  -> an immediate, on-brand reply (the user engaged us, so we always answer),
  * monitored channel message -> buffered, then every `--interval` s a cheap regex pre-filter drops
    social noise and the survivors are batch-classified by a background LLM (cc -> claude cost chain).
    A HIGH-confidence demand gets a short "logged for the team" reply + a bookmark reaction; a
    LOW-confidence one gets the reaction only; both are upserted into the demand pool (dedup +
    reach = distinct authors). Nothing user-facing fires on non-demand chatter.
  * every `--display-interval` s the admin display channel's pinned backlog is refreshed.

Privacy: every message body is redacted and the author id pseudonymized BEFORE it touches the pool
or an LLM prompt (redact-on-ingest, always first). Secrets (bot token) come from the companion
config secrets/ and are never printed. --dry-run logs every action WITHOUT posting/reacting, for a
safe first run against a live server.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time

from lib import canonical_key, find_config_dir, iso, load_config, now_utc
from redact import pseudonymize, redact
from score import score_demand
import demand_pool as pool

try:
    import discord
except ImportError:
    sys.stderr.write("demand_bot: discord.py not installed (pip install discord.py)\n")
    raise

# --- cheap product-signal pre-filter (drops obvious social chatter before any LLM spend) ----------
_SIGNAL = re.compile(
    r"(error|bug|broken|crash|blank|reload|fail|can'?t|cannot|won'?t|doesn'?t|isn'?t|not work|stopped|"
    r"stuck|glitch|freeze|lag|slow|down|outage|rate.?limit|wish|would (be|love)|want to|need to|"
    r"please add|feature|suggest|should (add|have|be)|how (do|to|can) i|is there (a|any) way|"
    r"why (does|is|isn|won|can'?t|doesn)|import|export|can'?t (find|save|load|login|pay|connect)|"
    r"model|token|route|provider|character|lorebook|preset|reply|generat|cost|credit|balance|plan|"
    r"subscri|plus|billing|refund|报错|无法|不能|卡住|崩|闪退|求|建议|希望|怎么|为什么(不|没|会)|需求|功能)",
    re.I)


def _config_dir():
    d = find_config_dir()
    if not d:
        raise SystemExit("demand_bot: no config dir (DEMAND_MINING_CONFIG); tap not wired.")
    return d


def _wiring(d):
    reg = json.loads((d / "registry.json").read_text(encoding="utf-8-sig"))
    prod = (reg.get("products") or [{}])[0]
    ref = prod["discord_token_ref"]
    tok_path = (d / ref) if not os.path.isabs(ref) else __import__("pathlib").Path(ref)
    token = tok_path.read_text(encoding="utf-8").strip()
    chans = {c["id"]: c.get("name", c["id"]) for c in (prod.get("discord_channels") or [])}
    guild = prod.get("discord_guild")
    display = prod.get("demand_display_channel") or os.environ.get("DEMAND_DISPLAY_CHANNEL")
    product = prod.get("display_name") or prod.get("slug", "this product")
    return token, chans, guild, display, product


# --- background LLM: delegate to the shared llmcall primitive (codex -> cc -> claude) -------------
# The chain, every headless footgun (read-only codex, --ephemeral, absolute-path fallback, MCP off,
# json-envelope unwrap) and the single model/effort source now live in ONE package. This wrapper only
# keeps the local `_llm(prompt, chain=...)` signature so the callers (classify_batch, gen_reply) and
# the per-round reorder (the audit round runs cc,claude,codex for cross-model independence) are
# unchanged.
from llmcall import DEFAULT_CHAIN as _DEFAULT_CHAIN  # noqa: E402
from llmcall import call as _llmcall  # noqa: E402


def _llm(prompt: str, timeout=120, chain=_DEFAULT_CHAIN) -> str:
    """First backend in `chain` that returns non-empty wins (str, "" on total failure). A reordered
    chain runs a step on a DIFFERENT model than generated it (cross-model audit)."""
    return _llmcall(prompt, chain=list(chain), timeout=timeout).text


def _json_block(text: str):
    m = re.search(r"\[.*\]|\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _classify_sys(product):
    return (
        f"You triage messages from the {product} community for PRODUCT DEMAND "
        "(bugs, feature requests, unmet needs, pain). Ignore pure social chatter, memes, greetings, "
        "moderation. For EACH numbered message return an object; reply ONLY a JSON array, same order:\n"
        '{"i":<index>,"is_demand":true|false,"confidence":0.0-1.0,"title":"<short canonical demand name '
        'or empty>","track":"<one word category>","kano":"must_be|performance|delighter|indifferent|reverse",'
        '"why":"<one clause>"}\nBe strict: confidence>=0.7 only when it is clearly a real product demand.'
    )


def _audit_sys(product):
    return (
        f"You independently AUDIT another model's product-demand classifications for the {product} "
        "community. Each numbered message is shown WITH a draft verdict. Judge each on your own and "
        "return ONLY a corrected JSON array (same schema and order): flip is_demand if the draft is "
        "wrong, recalibrate confidence, fix title/track/kano. Keep a verdict as-is if already correct. "
        'Schema per item: {"i","is_demand","confidence","title","track","kano","why"}.'
    )


def _uncertain(v):
    """A verdict is worth a second look only if its confidence sits in the ambiguous band. Confidently
    clear verdicts (very high or very low) do not, so a clean batch converges in one pass."""
    try:
        c = float(v.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        return False
    return 0.3 <= c <= 0.85


def _verdicts_stable(a, b):
    """Converged when the auditor changed nothing that matters: same is_demand + same confidence band
    per index. Ignores prose (title/why) churn so we do not loop forever on cosmetic rewording."""
    ka = {x.get("i"): x for x in a if isinstance(x, dict)}
    kb = {x.get("i"): x for x in b if isinstance(x, dict)}
    if ka.keys() != kb.keys():
        return False
    for i, x in ka.items():
        y = kb[i]
        if bool(x.get("is_demand")) != bool(y.get("is_demand")):
            return False
        if round(float(x.get("confidence", 0) or 0), 1) != round(float(y.get("confidence", 0) or 0), 1):
            return False
    return True


def classify_batch(items, sys=None, product="this product", max_rounds=2):
    """items: [{'i','channel','text'}] -> list of verdict dicts. Adaptive self-refine chain: codex
    drafts, then (only while some verdict is borderline) a DIFFERENT model audits and revises, up to
    max_rounds passes, stopping as soon as the audit stops changing anything. Clear batches cost one
    pass; genuinely ambiguous ones earn extra scrutiny. Empty on failure."""
    if not items:
        return []
    sys = sys or _classify_sys(product)
    lines = "\n".join(f'{it["i"]}. [{it["channel"]}] {it["text"][:280]}' for it in items)
    draft = _json_block(_llm(sys + "\n\nMESSAGES:\n" + lines))          # round 1: codex generates
    draft = [v for v in draft if isinstance(v, dict)] if isinstance(draft, list) else []
    if not draft:
        return []
    audit_sys = _audit_sys(product)
    by_i = {it["i"]: it for it in items}
    for _ in range(max(0, max_rounds - 1)):
        if not any(_uncertain(v) for v in draft):
            break  # every verdict is confidently clear -> converged
        shown = "\n".join(
            f'{v.get("i")}. [{by_i.get(v.get("i"), {}).get("channel", "?")}] '
            f'{(by_i.get(v.get("i"), {}).get("text", "") or "")[:280]}\n   draft: '
            f'{json.dumps({k: v.get(k) for k in ("is_demand", "confidence", "title", "track", "kano")}, ensure_ascii=False)}'
            for v in draft)
        # audit on a DIFFERENT model (cc first) for independence; codex is the last resort here
        revised = _json_block(_llm(audit_sys + "\n\nMESSAGES + DRAFTS:\n" + shown,
                                   chain=("cc", "claude", "codex")))
        revised = [v for v in revised if isinstance(v, dict)] if isinstance(revised, list) else []
        if not revised or _verdicts_stable(draft, revised):
            break  # auditor agrees -> converged, stop early
        draft = revised
    return draft


def _reply_sys(product):
    return (
        f"You are the friendly community listener bot for {product}. A user just @-mentioned you or "
        "DMed you, and you are ALSO given the surrounding CONVERSATION CONTEXT (the thread or forum "
        "post they are in, the message they replied to, recent chat). USE the context: if they say "
        "'check this' or point at something, your reply MUST reflect the ACTUAL topic from the context, "
        "never a generic 'thanks for sharing'. Reply in ONE short, warm sentence that names the specific "
        "thing and says you have logged it for the team. No markdown, no emoji spam (one is fine), never "
        "promise a fix or a date. If it is genuinely not product feedback, reply one friendly line. "
        "Reply in the SAME language the user wrote in; if unsure, use English."
    )


def gen_reply(text: str, sys=None, context: str = "") -> str:
    sys = sys or _reply_sys("this product")
    ctx = f"\n\nCONVERSATION CONTEXT:\n{context[:1200]}" if context else ""
    out = _llm(sys + ctx + f'\n\nUSER MESSAGE:\n{text[:400]}\n\nYour one-line reply:')
    out = (out or "").strip().splitlines()[0] if out else ""
    out = re.sub(r"\s*[\u2013\u2014\u2015]+\s*", ", ", out)  # house rule: no en/em dash in output
    return out[:280] or "Thanks, I have logged this for the team. 📝"


def _demand_from_verdict(v, msg_text, author_hash, channel_name):
    title = (v.get("title") or "").strip()
    ents = re.findall(r"[a-z][a-z0-9\-]{2,}", title.lower())[:4] or [title.lower()[:24]]
    track = (v.get("track") or "general").strip().lower()
    ck = canonical_key(ents, track)
    return {
        "canonical_key": ck, "title": title or msg_text[:48], "taxonomy_track": track,
        "kano": v.get("kano") or "performance", "why": v.get("why", ""),
        "reach": 1, "impact_label": "medium",
        "independent_source_count": 1, "has_internal_explicit": True,
        "authors": [{"author_hash": author_hash, "urgency": "need", "segment": "free"}],
        "evidence": [{"source": channel_name, "origin_type": "internal",
                      "redacted_snippet": msg_text[:200], "ts": iso(now_utc())}],
        "source": "live-tap",
    }


def _rescorer(cfg):
    def rescore(row):
        try:
            sc = score_demand(row, cfg)
            row["rice"] = sc["rice"]
            row["final_score"] = sc["final_score"]
            row["grade"] = sc["grade"]
            row["tier"] = sc["tier"]
        except Exception:
            pass
        return row
    return rescore


class DemandBot(discord.Client):
    def __init__(self, cfg, chans, guild, display, mode, interval, display_interval, poolp,
                 product="this product"):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.cfg, self.chans, self.guild_id = cfg, chans, guild
        self.product = product
        self.classify_sys = _classify_sys(product)
        self.reply_sys = _reply_sys(product)
        self.display_id = int(display) if display else None
        # the modes gate THREE independent kinds of output, because they carry different risk:
        #   post_direct    = replying when a user @-mentions or DMs the bot. The user initiated
        #                    contact; a DM is private and an @-reply is a direct answer. Low risk, so
        #                    it fires in shadow too (silencing it just looks broken to the user).
        #   post_community = UNPROMPTED output into monitored channels: the passive "logged for the
        #                    team" auto-reply + reaction when the bot detects a demand in the chat.
        #                    This is what shadow exists to hold back until you have reviewed it.
        #   post_display   = the admin dashboard channel (internal, always fine outside dry).
        #   dry = nothing external (log only); shadow = direct + dashboard, community-silent; live = all.
        self.mode = mode
        self.post_direct = mode in ("shadow", "live")   # @/DM replies (user-initiated)
        self.post_community = mode == "live"            # unprompted channel auto-replies + reactions
        self.post_display = mode in ("live", "shadow")  # the admin dashboard channel
        self.interval, self.display_interval = interval, display_interval
        self.poolp = poolp
        self.rescore = _rescorer(cfg)
        self.buffer = []
        self.hi = float(cfg.get("live", {}).get("high_confidence", 0.7))
        self.lo = float(cfg.get("live", {}).get("low_confidence", 0.4))
        # adaptive self-refine depth for classification (1 = single pass, no audit). Replies stay
        # one-shot on purpose: a warm ack is low-stakes and latency-sensitive.
        self.classify_rounds = int(cfg.get("live", {}).get("classify_rounds", 2))
        # the admin channel gets APPENDED activity notes in real time (never an edited-in-place summary,
        # which used to clobber a reply), plus ONE full summary per day at summary_hour_utc.
        self.summary_hour = int(cfg.get("live", {}).get("summary_hour_utc", 3))

    def log(self, m):
        sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {m}\n")
        sys.stdout.flush()

    async def on_ready(self):
        self.log(f"connected as {self.user} | monitoring {len(self.chans)} channels | "
                 f"mode={self.mode} (direct={'on' if self.post_direct else 'off'}, "
                 f"community={'on' if self.post_community else 'SILENT'}, "
                 f"dashboard={'on' if self.post_display else 'off'}) | display={self.display_id}")
        self.loop.create_task(self._classify_loop())
        if self.display_id:
            self.loop.create_task(self._summary_loop())

    async def on_message(self, m: discord.Message):
        if m.author.bot or (self.user and m.author.id == self.user.id):
            return
        is_dm = m.guild is None
        mentioned = self.user in m.mentions if m.guild else False
        clean = redact(m.content or "")["redacted"]
        ah = pseudonymize(str(m.author.id))
        if is_dm or mentioned:
            await self._direct_reply(m, clean, ah)
            return
        if str(m.channel.id) in self.chans and (m.content or "").strip():
            self.buffer.append({"m": m, "text": clean, "ah": ah,
                                "channel": self.chans[str(m.channel.id)]})

    async def _gather_context(self, m, history_limit=6):
        """Assemble the surrounding context so a reply to a bare 'check this' is about the real subject:
        the thread/forum opening post + title, the replied-to message, and recent human chat. Every
        piece is redacted + author-pseudonymized BEFORE it is returned (it will reach an LLM). Best
        effort: any source that errors is simply skipped, so gathering never breaks the reply."""
        parts = []
        ch = m.channel
        # 1) a forum post / thread: title + opening message is usually the real subject of "this"
        if isinstance(ch, discord.Thread):
            if (ch.name or "").strip():
                parts.append(f"[thread title] {redact(ch.name)['redacted'][:150]}")
            try:
                opener = ch.starter_message or await ch.fetch_message(ch.id)
                if opener is not None and opener.id != m.id and (opener.content or "").strip():
                    parts.append(f"[opening post] {redact(opener.content)['redacted'][:400]}")
            except Exception:
                pass
        # 2) the specific message this one is a reply to
        ref_id = m.reference.message_id if (m.reference and m.reference.message_id) else None
        if ref_id:
            try:
                ref = m.reference.resolved
                if not isinstance(ref, discord.Message):
                    ref = await ch.fetch_message(ref_id)
                if ref is not None and (ref.content or "").strip():
                    who = "the bot" if (self.user and ref.author.id == self.user.id) \
                        else pseudonymize(str(ref.author.id))[:8]
                    parts.append(f"[replying to {who}] {redact(ref.content)['redacted'][:300]}")
            except Exception:
                pass
        # 3) recent channel history (the conversation leading up), humans only, oldest-first
        try:
            hist = []
            async for prev in ch.history(limit=history_limit, before=m):
                if prev.author.bot or not (prev.content or "").strip():
                    continue
                hist.append(f"{pseudonymize(str(prev.author.id))[:8]}: {redact(prev.content)['redacted'][:160]}")
            if hist:
                hist.reverse()
                parts.append("[recent] " + " | ".join(hist))
        except Exception:
            pass
        return "\n".join(parts)[:1500]

    async def _direct_reply(self, m, clean, ah):
        context = await self._gather_context(m)
        reply = await asyncio.to_thread(gen_reply, clean, self.reply_sys, context)
        self.log(f"@/DM from {ah}: {clean[:50]!r} ctx={len(context)}c -> reply {reply[:50]!r}")
        # send the reply FIRST: it is one-shot and fast, and must not wait on the (slower, possibly
        # multi-round) demand analysis below. The user gets answered promptly either way.
        if self.post_direct:
            try:
                await m.reply(reply, mention_author=True)
            except Exception as e:
                self.log(f"reply failed: {e!r}")
        else:
            self.log(f"[{self.mode}] direct reply suppressed")
        # THEN mine a demand from the FULL subject (context + message), so a bare "check this" still
        # yields the real underlying need instead of an empty verdict.
        mined = None
        subject = f"{context}\n\n[user] {clean}".strip() if context else clean
        # redact the channel label too: a forum/thread .name is a USER-authored title (can hold an
        # email or handle), and it flows into both the classifier prompt and the pool evidence source.
        # An admin-set text-channel name ("general") and "dm/mention" pass through redact unchanged.
        chan_label = redact(getattr(m.channel, "name", None) or "dm/mention")["redacted"]
        if _SIGNAL.search(subject):
            v = (await asyncio.to_thread(classify_batch,
                 [{"i": 0, "channel": chan_label, "text": subject}], self.classify_sys,
                 self.product, self.classify_rounds) or [{}])
            if v and v[0].get("is_demand"):
                _act, row = await asyncio.to_thread(pool.upsert, self.poolp,
                    _demand_from_verdict(v[0], subject, ah, chan_label), self.rescore)
                mined = row.get("title")
        # brief activity note to the admin channel (English): who we answered, and any demand mined
        note = f"\U0001f4dd Replied to user `{ah[:10]}`"
        if mined:
            note += f' · logged demand: "{mined[:80]}"'
        await self._note(note)

    async def _classify_loop(self):
        while not self.is_closed():
            await asyncio.sleep(self.interval)
            batch, self.buffer = self.buffer, []
            cand = [b for b in batch if _SIGNAL.search(b["text"])]
            if not cand:
                continue
            self.log(f"classify batch: {len(cand)}/{len(batch)} passed pre-filter")
            items = [{"i": i, "channel": b["channel"], "text": b["text"]} for i, b in enumerate(cand)]
            verdicts = await asyncio.to_thread(classify_batch, items, self.classify_sys,
                                               self.product, self.classify_rounds)
            vmap = {v.get("i"): v for v in verdicts if isinstance(v, dict)}
            for i, b in enumerate(cand):
                v = vmap.get(i)
                if not v or not v.get("is_demand"):
                    continue
                conf = float(v.get("confidence", 0) or 0)
                if conf < self.lo:
                    continue
                action, row = await asyncio.to_thread(pool.upsert, self.poolp,
                    _demand_from_verdict(v, b["text"], b["ah"], b["channel"]), self.rescore)
                self.log(f"demand({conf:.2f}) {action}: {row.get('title','')[:48]!r} "
                         f"reach={row.get('reach')} score={row.get('final_score')}")
                await self._ack(b["m"], conf)
                if action == "new":  # note only genuinely new demands, not every recurrence
                    await self._note(
                        f'\U0001f50d New demand from #{b["channel"]}: "{row.get("title", "?")[:80]}" '
                        f'({row.get("grade", "?")} {row.get("final_score", "?")}, '
                        f'reach {row.get("reach", 0)}, {row.get("taxonomy_track", "?")})')

    async def _ack(self, m, conf):
        if not self.post_community:
            return
        try:
            await m.add_reaction("📝")
            if conf >= self.hi:
                await m.reply("Noted, I have logged this for the team. 📝", mention_author=True)
        except Exception as e:
            self.log(f"ack failed: {e!r}")

    async def _note(self, text):
        """APPEND one short line to the admin channel (never edit-in-place -- editing a prior message
        is what used to overwrite a user reply with the backlog). All channel text is English."""
        if not self.post_display or not self.display_id:
            self.log(f"[note] {text}")
            return
        ch = self.get_channel(self.display_id)
        if not ch:
            return
        try:
            await ch.send(text[:1900])
        except Exception as e:
            self.log(f"note failed: {e!r}")

    async def _summary_loop(self):
        """Post ONE full summary per day at summary_hour_utc: today's demands + the all-time backlog.
        Date-gated by a marker file so daemon restarts never double-post or skip."""
        while not self.is_closed():
            try:
                await self._maybe_daily_summary()
            except Exception as e:
                self.log(f"summary loop failed: {e!r}")
            await asyncio.sleep(1800)  # re-check every 30 min

    def _summary_marker(self):
        return os.path.join(os.path.dirname(self.poolp), ".last_summary")

    async def _maybe_daily_summary(self):
        now = now_utc()
        today = iso(now)[:10]
        if now.hour < self.summary_hour:
            return
        try:
            if open(self._summary_marker(), encoding="utf-8").read().strip() == today:
                return
        except OSError:
            pass
        await self._post_daily_summary(today)
        try:
            with open(self._summary_marker(), "w", encoding="utf-8") as f:
                f.write(today)
        except OSError:
            pass

    def _render_summary(self, today):
        rows = pool.load(self.poolp)
        todays = [r for r in rows
                  if (r.get("last_seen") or "")[:10] == today or (r.get("first_seen") or "")[:10] == today]
        ranked = pool.ranked(self.poolp)
        lines = [f"\U0001f4ca **Daily Demand Summary, {today}**",
                 f"Touched today: {len(todays)} | Total backlog: {len(rows)}", ""]
        if todays:
            lines.append("__Today__")
            for r in sorted(todays, key=lambda r: -float(r.get("final_score", 0) or 0))[:15]:
                lines.append(f'- "{r.get("title", "?")[:70]}" ({r.get("grade", "?")} '
                             f'{r.get("final_score", "?")}, reach {r.get("reach", 0)})')
            lines.append("")
        lines.append(f"__All-time top {min(15, len(ranked))}__")
        for i, r in enumerate(ranked[:15], 1):
            lines.append(f'{i}. "{r.get("title", "?")[:70]}" ({r.get("grade", "?")} '
                         f'{r.get("final_score", "?")}, reach {r.get("reach", 0)}, {r.get("status", "new")})')
        return "\n".join(lines)[:3900]

    async def _post_daily_summary(self, today):
        body = self._render_summary(today)
        if not self.post_display or not self.display_id:
            self.log(f"[{self.mode}] daily summary suppressed ({len(body)} chars)")
            return
        ch = self.get_channel(self.display_id)
        if not ch:
            return
        try:
            await ch.send(embed=discord.Embed(description=body, color=0x57F287))
            self.log(f"daily summary posted ({len(body)} chars)")
        except Exception as e:
            self.log(f"summary post failed: {e!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="demand-mining live gateway daemon")
    ap.add_argument("--mode", choices=("dry", "shadow", "live"), default="shadow",
                    help="dry=log only; shadow=capture+dashboard but community-silent (default, 24/7 review); "
                         "live=reply/react in the community too")
    ap.add_argument("--dry-run", action="store_true", help="alias for --mode dry")
    ap.add_argument("--interval", type=float, default=90.0, help="classify buffer flush seconds")
    ap.add_argument("--display-interval", type=float, default=300.0)
    ap.add_argument("--run-seconds", type=float, default=0, help="stop after N seconds (0=forever; test)")
    ap.add_argument("--log-file", default=None,
                    help="redirect stdout+stderr here (required under pythonw, where stdout is None)")
    args = ap.parse_args()
    if args.log_file:
        # one redirect fixes three things at once: no console window under pythonw, a durable log,
        # and (critically) sys.stdout is a real file so log() and discord tracebacks never hit None.
        f = open(args.log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
    mode = "dry" if args.dry_run else args.mode
    d = _config_dir()
    cfg = load_config()
    token, chans, guild, display, product = _wiring(d)
    poolp = pool.pool_path(d)
    bot = DemandBot(cfg, chans, guild, display, mode, args.interval, args.display_interval, poolp,
                    product=product)
    if args.run_seconds:
        async def _timed():
            await asyncio.sleep(args.run_seconds)
            await bot.close()
        bot.loop.create_task(_timed()) if False else None  # scheduled inside on_ready path
        orig_ready = bot.on_ready
        async def ready2():
            await orig_ready()
            bot.loop.create_task(_timed())
        bot.on_ready = ready2
    bot.run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
