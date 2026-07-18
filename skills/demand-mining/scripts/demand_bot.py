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
import subprocess
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
    return token, chans, guild, display, prod.get("slug", "product")


# --- background LLM (cost chain: cc cheap gateway -> claude full price) ---------------------------
def _llm(prompt: str, timeout=90) -> str:
    for cli in ("cc", "claude"):
        exe = os.path.expanduser(f"~/.local/bin/{cli}")
        exe = exe if os.path.isfile(exe) else cli
        try:
            p = subprocess.run([exe, "-p", prompt], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
            if p.returncode == 0 and (p.stdout or "").strip():
                return p.stdout.strip()
        except Exception:
            continue
    return ""


def _json_block(text: str):
    m = re.search(r"\[.*\]|\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


_CLASSIFY_SYS = (
    "You triage messages from the LoreStage (AI roleplay/chat product) community for PRODUCT DEMAND "
    "(bugs, feature requests, unmet needs, pain). Ignore pure social chatter, memes, greetings, "
    "moderation. For EACH numbered message return an object; reply ONLY a JSON array, same order:\n"
    '{"i":<index>,"is_demand":true|false,"confidence":0.0-1.0,"title":"<short canonical demand name '
    'or empty>","track":"<one word category>","kano":"must_be|performance|delighter|indifferent|reverse",'
    '"why":"<one clause>"}\nBe strict: confidence>=0.7 only when it is clearly a real product demand.'
)


def classify_batch(items):
    """items: [{'i','channel','text'}]. Returns list of dicts (LLM verdicts). Empty on failure."""
    if not items:
        return []
    lines = "\n".join(f'{it["i"]}. [{it["channel"]}] {it["text"][:280]}' for it in items)
    out = _llm(_CLASSIFY_SYS + "\n\nMESSAGES:\n" + lines)
    v = _json_block(out)
    return v if isinstance(v, list) else []


_REPLY_SYS = (
    "You are 'Token Radar', a friendly LoreStage community listener bot. A user just @-mentioned you "
    "or DMed you. Reply in ONE short, warm sentence: acknowledge what they said and that you have "
    "logged it for the team. No markdown, no emoji spam (one is fine), never promise a fix or a date. "
    "If it is not product feedback, reply one friendly line and note you mainly track product ideas."
)


def gen_reply(text: str) -> str:
    out = _llm(_REPLY_SYS + f'\n\nUSER MESSAGE:\n{text[:400]}\n\nYour one-line reply:')
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
    def __init__(self, cfg, chans, guild, display, dry_run, interval, display_interval, poolp):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.cfg, self.chans, self.guild_id = cfg, chans, guild
        self.display_id = int(display) if display else None
        self.dry_run, self.interval, self.display_interval = dry_run, interval, display_interval
        self.poolp = poolp
        self.rescore = _rescorer(cfg)
        self.buffer = []
        self.hi = float(cfg.get("live", {}).get("high_confidence", 0.7))
        self.lo = float(cfg.get("live", {}).get("low_confidence", 0.4))

    def log(self, m):
        sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {m}\n")
        sys.stdout.flush()

    async def on_ready(self):
        self.log(f"connected as {self.user} | monitoring {len(self.chans)} channels | "
                 f"dry_run={self.dry_run} | display={self.display_id}")
        self.loop.create_task(self._classify_loop())
        if self.display_id:
            self.loop.create_task(self._display_loop())

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

    async def _direct_reply(self, m, clean, ah):
        reply = await asyncio.to_thread(gen_reply, clean)
        self.log(f"@/DM from {ah}: {clean[:60]!r} -> reply {reply[:60]!r}")
        # a mention/DM that reads like a demand also enters the pool
        if _SIGNAL.search(clean):
            v = (await asyncio.to_thread(classify_batch,
                 [{"i": 0, "channel": "dm/mention", "text": clean}]) or [{}])
            if v and v[0].get("is_demand"):
                await asyncio.to_thread(pool.upsert, self.poolp,
                    _demand_from_verdict(v[0], clean, ah, "dm/mention"), self.rescore)
        if not self.dry_run:
            try:
                await m.reply(reply, mention_author=True)
            except Exception as e:
                self.log(f"reply failed: {e!r}")

    async def _classify_loop(self):
        while not self.is_closed():
            await asyncio.sleep(self.interval)
            batch, self.buffer = self.buffer, []
            cand = [b for b in batch if _SIGNAL.search(b["text"])]
            if not cand:
                continue
            self.log(f"classify batch: {len(cand)}/{len(batch)} passed pre-filter")
            items = [{"i": i, "channel": b["channel"], "text": b["text"]} for i, b in enumerate(cand)]
            verdicts = await asyncio.to_thread(classify_batch, items)
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

    async def _ack(self, m, conf):
        if self.dry_run:
            return
        try:
            await m.add_reaction("📝")
            if conf >= self.hi:
                await m.reply("Noted, I have logged this for the team. 📝", mention_author=True)
        except Exception as e:
            self.log(f"ack failed: {e!r}")

    async def _display_loop(self):
        while not self.is_closed():
            try:
                await self._sync_display()
            except Exception as e:
                self.log(f"display sync failed: {e!r}")
            await asyncio.sleep(self.display_interval)

    def _render(self):
        rows = pool.ranked(self.poolp)[:20]
        _TRK = {"tier0": "立即", "tier1": "本周", "tier2": "本月", "backlog": "储备"}
        lines = [f"📊 **产品需求 backlog** (live) · 更新 {iso(now_utc())[:16]}Z · 共 {len(pool.load(self.poolp))} 条", ""]
        for i, r in enumerate(rows, 1):
            hz = _TRK.get(r.get("tier"), "")
            lines.append(f"**{i}. [{hz}·{r.get('kano','')}] {r.get('title','?')[:70]}**")
            lines.append(f"    {r.get('grade','?')} {r.get('final_score','?')} · reach {r.get('reach',0)} "
                         f"· {len(r.get('evidence',[]))}证据 · {r.get('status','new')} · 末见 {(r.get('last_seen') or '')[:10]}")
        return "\n".join(lines)[:3900]

    async def _sync_display(self):
        ch = self.get_channel(self.display_id)
        if not ch:
            return
        body = self._render()
        if self.dry_run:
            self.log(f"[dry-run] would refresh display ({len(body)} chars)")
            return
        # keep ONE bot message, edit it in place (find last own message, else send)
        target = None
        async for msg in ch.history(limit=20):
            if msg.author.id == self.user.id:
                target = msg
                break
        if target:
            await target.edit(content=body)
        else:
            sent = await ch.send(body)
            try:
                await sent.pin()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="demand-mining live gateway daemon")
    ap.add_argument("--dry-run", action="store_true", help="log actions, never post/react")
    ap.add_argument("--interval", type=float, default=90.0, help="classify buffer flush seconds")
    ap.add_argument("--display-interval", type=float, default=300.0)
    ap.add_argument("--run-seconds", type=float, default=0, help="stop after N seconds (0=forever; test)")
    args = ap.parse_args()
    d = _config_dir()
    cfg = load_config()
    token, chans, guild, display, slug = _wiring(d)
    poolp = pool.pool_path(d)
    bot = DemandBot(cfg, chans, guild, display, args.dry_run, args.interval, args.display_interval, poolp)
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
