#!/usr/bin/env python3
"""Live demand-tap daemon for demand-mining (persistent Discord gateway service).

Replaces the daily batch for the interactive half: it stays connected (discord.py gateway, Message
Content Intent) and, in real time:
  * @-mention or DM  -> owned feedback analysis and a reply after any demand is saved,
  * monitored channel message -> buffered, then every `--interval` s a cheap regex pre-filter drops
    social noise and the survivors are batch-classified through the installed llmcall interface.
    A HIGH-confidence demand gets a short "logged for the team" reply + a bookmark reaction; a
    LOW-confidence one gets the reaction only; both are upserted into the demand pool (dedup +
    reach = distinct authors). Nothing user-facing fires on non-demand chatter.
  * the admin display channel receives activity notes and one summary per configured local day.

Privacy: every message body is redacted and the author id pseudonymized BEFORE it touches the pool
or an LLM prompt (redact-on-ingest, always first). Secrets (bot token) come from the companion
config secrets/ and are never printed. --dry-run logs every action WITHOUT posting/reacting, for a
safe first run against a live server.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from private_log import private_output
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import time

def _parse_args():
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
    return ap.parse_args()


def _bootstrap() -> int:
    """Start direct CLI logging before importing the optional runtime dependencies."""
    from no_console import install_no_console_window_default
    install_no_console_window_default()
    args = _parse_args()
    output = private_output(args.log_file) if args.log_file else nullcontext()
    with output:
        from importlib.util import module_from_spec, spec_from_file_location
        spec = spec_from_file_location("_demand_bot_runtime", __file__)
        if spec is None or spec.loader is None:
            raise RuntimeError("demand_bot runtime source could not be loaded")
        runtime = module_from_spec(spec)
        spec.loader.exec_module(runtime)
        return runtime._run(args)


if __name__ == "__main__":
    sys.exit(_bootstrap())


from lib import (canonical_key, find_config_dir, iso, load_config, now_utc, parse_ts,
                 merge_observations, observation_identity)
from redact import pseudonymize, safe_text, safe_data, PrivacyReviewRequired, ensure_stable_salt
from data_safety import atomic_json, file_lock, require_private
from score import score_demand
import demand_pool as pool

import discord

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
    selected = os.environ.get("DEMAND_MINING_PRODUCT") or load_config().get("product_id")
    prod = next((item for item in reg.get("products", []) if item.get("slug") == selected), None)
    if prod is None:
        raise ValueError("bot product is not registered")
    ref = prod["discord_token_ref"]
    tok_path = (d / ref) if not os.path.isabs(ref) else __import__("pathlib").Path(ref)
    token = tok_path.read_text(encoding="utf-8").strip()
    chans = {c["id"]: c.get("name", c["id"]) for c in (prod.get("discord_channels") or [])}
    guild = prod.get("discord_guild")
    display = prod.get("demand_display_channel") or os.environ.get("DEMAND_DISPLAY_CHANNEL")
    product = prod.get("display_name") or prod.get("slug", "this product")
    return token, chans, guild, display, product


# The installed llmcall interface owns routing, timeout and fallback policy.
from llmcall import call as _llmcall  # noqa: E402


# Per-item validation below also applies to adapters returning already-parsed data.
_VERDICT_SCHEMA = {"type": "array"}


def _llm(prompt: str, timeout=None, avoid=None, schema=None):
    """Return scrubbed output and provider, raising on a failed task.

    `timeout` remains a compatibility argument; installed policy controls it.
    Audit independence is the only provider-related task constraint.
    """
    constraints = {}
    if schema is not None:
        constraints["schema"] = schema
    if avoid is not None:
        constraints["avoid"] = avoid
    r = _llmcall(safe_text(prompt), **constraints)
    if not r or getattr(r, "error", None):
        raise RuntimeError("llmcall did not complete the requested task")
    value = r.data if schema is not None else r.text
    return safe_data(value), r.provider


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
    """items: [{'i','channel','text'}] -> list of verdict dicts. The shared interface
    drafts, then (only while some verdict is borderline) an independent model audits, up to
    max_rounds passes, stopping as soon as the audit stops changing anything. Clear batches cost one
    pass; ambiguous ones earn extra scrutiny. Invalid or failed output raises."""
    if not items:
        return []
    items = safe_data(items)
    sys = sys or _classify_sys(product)
    lines = "\n".join(f'{it["i"]}. [{it["channel"]}] {it["text"][:280]}' for it in items)
    draft, drafted_by = _llm(sys + "\n\nMESSAGES:\n" + lines, schema=_VERDICT_SCHEMA)  # round 1
    def checked(values):
        if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
            raise ValueError("classification must return an array of verdict objects")
        for value in values:
            if type(value.get("i")) is not int or type(value.get("is_demand")) is not bool:
                raise ValueError("classification requires integer indices and boolean is_demand")
            confidence = value.get("confidence")
            if (type(confidence) not in (int, float) or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1):
                raise ValueError("classification confidence must be a finite number in [0, 1]")
            if any(not isinstance(value.get(key), str) for key in ("title", "track", "kano", "why")):
                raise ValueError("classification title, track, kano and why must be strings")
            if value["kano"] not in {"must_be", "performance", "delighter", "indifferent", "reverse"}:
                raise ValueError("classification kano is invalid")
            if value["is_demand"] and (not value["title"].strip() or not value["track"].strip()):
                raise ValueError("classification demand requires a title and track")
        if len(values) != len(items) or {v.get("i") for v in values} != {it["i"] for it in items}:
            raise ValueError("classification omitted, duplicated or added message indices")
        return safe_data(values)

    draft = checked(draft)
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
        # Audit on a DIFFERENT model for independence: name the rung that drafted and llmcall rules
        # out its whole model family. Stating the REQUIREMENT rather than an order keeps working when
        # the ladder changes, and avoids the model that ACTUALLY answered rather than the one a
        # hardcoded order assumed would.
        revised, audited_by = _llm(audit_sys + "\n\nMESSAGES + DRAFTS:\n" + shown,
                          avoid=drafted_by, schema=_VERDICT_SCHEMA)
        revised = checked(revised)
        if _verdicts_stable(draft, revised):
            draft = revised
            break  # auditor agrees -> converged, stop early
        draft = revised
        drafted_by = audited_by
    return draft


def _reply_sys(product):
    return (
        f"You are the friendly community listener bot for {product}. A user just @-mentioned you or "
        "DMed you, and you are ALSO given the surrounding CONVERSATION CONTEXT (the thread or forum "
        "post they are in, the message they replied to, recent chat). USE the context: if they say "
        "'check this' or point at something, your reply MUST reflect the ACTUAL topic from the context, "
        "never a generic 'thanks for sharing'. Reply in ONE short, warm sentence that names the specific "
        "thing. Say it was logged only when STORAGE STATUS confirms it was saved. "
        "No markdown, no emoji spam (one is fine), never "
        "promise a fix or a date. If it is genuinely not product feedback, reply one friendly line. "
        "Reply in the SAME language the user wrote in; if unsure, use English."
    )


def gen_reply(text: str, sys=None, context: str = "") -> str:
    text, context = safe_text(text), safe_text(context)
    sys = sys or _reply_sys("this product")
    ctx = f"\n\nCONVERSATION CONTEXT:\n{context[:1200]}" if context else ""
    out, _ = _llm(sys + ctx + f'\n\nUSER MESSAGE:\n{text[:400]}\n\nYour one-line reply:')
    out = (out or "").strip().splitlines()[0] if out else ""
    out = re.sub(r"\s*[\u2013\u2014\u2015]+\s*", ", ", out)  # house rule: no en/em dash in output
    if not out:
        raise RuntimeError("reply generation returned empty output")
    return safe_text(out[:280])


def _demand_from_verdict(v, msg_text, author_hash, channel_name, product_id=None,
                         *, observation_id=None, source_id=None, timestamp=None):
    v, msg_text, channel_name = safe_data(v), safe_text(msg_text), safe_text(channel_name)
    title = (v.get("title") or "").strip()
    ents = re.findall(r"[a-z][a-z0-9\-]{2,}", title.lower())[:4] or [title.lower()[:24]]
    track = (v.get("track") or "general").strip().lower()
    ck = canonical_key(ents, track)
    evidence = {"source": channel_name, "origin_type": "internal",
                "redacted_snippet": msg_text[:200], "ts": timestamp or iso(now_utc()),
                "author_hash": author_hash, "source_id": source_id,
                "observation_id": observation_id or observation_identity(
                    channel_name, author_hash, timestamp, msg_text)}
    row = {
        "product_id": product_id,
        "canonical_key": ck, "title": title or msg_text[:48], "taxonomy_track": track,
        "kano": v.get("kano") or "performance", "why": v.get("why", ""),
        "impact_label": "medium",
        "authors": [{"author_hash": author_hash, "urgency": "need", "segment": "free"}],
        "evidence": [evidence], "source": "live-tap",
    }
    row.update(merge_observations(row))
    return row


def _message_observation(message):
    """Pseudonymize platform identity before it enters a demand or prompt."""
    if message is None:
        return {}
    channel_id = getattr(getattr(message, "channel", None), "id", None)
    message_id = getattr(message, "id", None)
    created = getattr(message, "created_at", None)
    return {
        "source_id": pseudonymize("discord-channel:" + str(channel_id)) if channel_id is not None else None,
        "observation_id": pseudonymize("discord-message:" + str(channel_id) + ":" + str(message_id))
            if channel_id is not None and message_id is not None else None,
        "timestamp": iso(created) if created is not None else None,
    }


def _rescorer(cfg):
    def rescore(row):
        row.update(score_demand(row, cfg))
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
        self._pending_batch = []
        self._direct_pending = {}
        self._direct_completed = set()
        self._classify_task = None
        self._summary_task = None
        self.classification_status = "starting"
        self.hi = float(cfg.get("live", {}).get("high_confidence", 0.7))
        self.lo = float(cfg.get("live", {}).get("low_confidence", 0.4))
        # adaptive self-refine depth for classification (1 = single pass, no audit). Replies stay
        # one-shot on purpose: a warm ack is low-stakes and latency-sensitive.
        self.classify_rounds = int(cfg.get("live", {}).get("classify_rounds", 2))
        # the admin channel gets APPENDED activity notes in real time (never an edited-in-place summary,
        # which used to clobber a reply), plus one full summary per configured local day.
        live = cfg.get("live", {})
        self.summary_hour = int(live.get("summary_hour", live.get("summary_hour_utc", 3)))
        self.summary_hour_uses_utc = "summary_hour" not in live and "summary_hour_utc" in live

    def log(self, m):
        sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {safe_text(m)}\n")
        sys.stdout.flush()

    async def on_ready(self):
        self.log(f"connected as {self.user} | monitoring {len(self.chans)} channels | "
                 f"mode={self.mode} (direct={'on' if self.post_direct else 'off'}, "
                 f"community={'on' if self.post_community else 'SILENT'}, "
                 f"dashboard={'on' if self.post_display else 'off'}) | display={self.display_id}")
        self._start_classification()
        if self.display_id and (self._summary_task is None or self._summary_task.done()):
            self._summary_task = self.loop.create_task(self._summary_loop())

    async def on_message(self, m: discord.Message):
        if m.author.bot or (self.user and m.author.id == self.user.id):
            return
        is_dm = m.guild is None
        mentioned = self.user in m.mentions if m.guild else False
        clean = safe_text(m.content or "")
        ah = pseudonymize(str(m.author.id))
        if is_dm or mentioned:
            await self._direct_reply(m, clean, ah)
            return
        if str(m.channel.id) in self.chans and (m.content or "").strip():
            self.buffer.append({"m": m, "text": clean, "ah": ah,
                                "channel": safe_text(self.chans[str(m.channel.id)])})

    async def _gather_context(self, m, history_limit=6, *, require_complete=False):
        """Assemble the surrounding context so a reply to a bare 'check this' is about the real subject:
        the thread/forum opening post + title, the replied-to message, and recent human chat. Every
        piece is redacted + author-pseudonymized BEFORE it is returned (it will reach an LLM).
        Display-only callers retain best-effort context. Direct feedback requires every
        attempted source to succeed so a partial read cannot finalize the observation."""
        parts = []
        ch = m.channel
        # 1) a forum post / thread: title + opening message is usually the real subject of "this"
        if isinstance(ch, discord.Thread):
            if (ch.name or "").strip():
                parts.append(f"[thread title] {safe_text(ch.name)[:150]}")
            try:
                opener = ch.starter_message or await ch.fetch_message(ch.id)
                if opener is not None and opener.id != m.id and (opener.content or "").strip():
                    parts.append(f"[opening post] {safe_text(opener.content)[:400]}")
            except PrivacyReviewRequired:
                raise
            except Exception as exc:
                if require_complete:
                    raise RuntimeError("context opening post unavailable") from exc
                self.log("context opening post unavailable")
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
                    parts.append(f"[replying to {who}] {safe_text(ref.content)[:300]}")
            except PrivacyReviewRequired:
                raise
            except Exception as exc:
                if require_complete:
                    raise RuntimeError("context reply reference unavailable") from exc
                self.log("context reply reference unavailable")
        # 3) recent channel history (the conversation leading up), humans only, oldest-first
        try:
            hist = []
            async for prev in ch.history(limit=history_limit, before=m):
                if prev.author.bot or not (prev.content or "").strip():
                    continue
                hist.append(f"{pseudonymize(str(prev.author.id))[:8]}: {safe_text(prev.content)[:160]}")
            if hist:
                hist.reverse()
                parts.append("[recent] " + " | ".join(hist))
        except PrivacyReviewRequired:
            raise
        except Exception as exc:
            if require_complete:
                raise RuntimeError("context recent history unavailable") from exc
            self.log("context recent history unavailable")
        return "\n".join(parts)[:1500]

    def _pool_product(self):
        value = self.cfg.get("product_id") or self.cfg.get("slug")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("live pool needs an explicit product identity")
        return value

    async def _direct_reply(self, m, clean, ah):
        # Own the observation before the first await. Repeated gateway events share
        # the same entry, and only one coroutine may advance it at a time.
        if not hasattr(self, "_direct_pending"):
            self._direct_pending = {}
        if not hasattr(self, "_direct_completed"):
            self._direct_completed = set()
        key = (str(m.channel.id), str(m.id))
        if key in self._direct_completed:
            return {"status": "complete"}
        if key in getattr(self, "_direct_quarantined", ()):
            return {"status": "quarantined"}
        entry = self._direct_pending.setdefault(key, {"m": m, "text": clean, "ah": ah})
        if entry.get("processing"):
            return {"status": "pending"}
        entry["processing"] = True
        try:
            await self._complete_direct(entry)
        except Exception as exc:
            return self._direct_failed(key, entry, exc)
        finally:
            entry["processing"] = False
        # Completed persistence and an attempted acknowledgment must not be replayed
        # when the later activity note fails.
        self._direct_completed.add(key)
        self._direct_pending.pop(key)
        note = f"Direct request from user {ah[:10]}"
        if entry.get("persisted"):
            note += f'; logged demand: "{entry["row"].get("title", "")[:80]}"'
        await self._note(note)
        return {"status": "complete", "reply_outcome": entry["reply_outcome"]}

    async def _complete_direct(self, entry):
        message, clean = entry["m"], entry["text"]
        if "context" not in entry:
            entry["context"] = await self._gather_context(message, require_complete=True)
        context = entry["context"]
        subject = f"{context}\n\n[user] {clean}".strip() if context else clean
        channel = safe_text(getattr(message.channel, "name", None) or "dm/mention")
        if "verdict" not in entry:
            if _SIGNAL.search(subject):
                verdicts = await asyncio.to_thread(
                    classify_batch, [{"i": 0, "channel": channel, "text": subject}],
                    self.classify_sys, self.product, self.classify_rounds)
                if len(verdicts) != 1:
                    raise ValueError("direct classification is incomplete")
                entry["verdict"] = verdicts[0]
            else:
                entry["verdict"] = {"is_demand": False}
        if entry["verdict"].get("is_demand") and not entry.get("persisted"):
            if "demand" not in entry:
                entry["demand"] = _demand_from_verdict(
                    entry["verdict"], clean, entry["ah"], channel, self._pool_product(),
                    **_message_observation(message))
            _, entry["row"] = await asyncio.to_thread(
                pool.upsert, self.poolp, entry["demand"], self.rescore)
            entry["persisted"] = True
        if "reply" not in entry:
            status = ("Feedback was successfully saved in the product demand pool."
                      if entry.get("persisted") else
                      "No product demand was saved. Do not say the message was logged or recorded.")
            entry["reply"] = await asyncio.to_thread(
                gen_reply, clean, self.reply_sys + "\n\nSTORAGE STATUS: " + status, context)
        if not entry.get("reply_attempted"):
            # A send exception can follow remote acceptance. Record the attempt
            # before awaiting it and never resend automatically.
            entry["reply_attempted"] = True
            entry["reply_outcome"] = "unknown" if self.post_direct else "suppressed"
            if self.post_direct:
                try:
                    await message.reply(entry["reply"], mention_author=True)
                except Exception as exc:
                    self.log(f"direct reply outcome unknown: {type(exc).__name__}; no automatic resend")
                else:
                    entry["reply_outcome"] = "confirmed"
            else:
                self.log(f"[{self.mode}] direct reply suppressed")

    def _direct_failed(self, key, entry, exc):
        """Back off a failed direct observation; quarantine a persistent privacy hold."""
        from observation_retry import RetryBackoff
        from redact import PrivacyReviewRequired
        backoff = entry.get("backoff")
        if backoff is None:
            backoff = entry["backoff"] = RetryBackoff(getattr(self, "interval", 90.0))
        held = isinstance(exc, PrivacyReviewRequired)
        if held and backoff.privacy_holds + 1 >= self._privacy_hold_limit():
            backoff.privacy_holds += 1
            try:
                paths = self._quarantine([entry], "direct", type(exc).__name__, backoff)
            except Exception as failure:
                if backoff.record_failure("quarantine:" + type(failure).__name__):
                    self.log(f"privacy quarantine refused: {type(failure).__name__}; "
                             "direct observation retained for retry")
                return {"status": "pending"}
            self._direct_pending.pop(key, None)
            if not hasattr(self, "_direct_quarantined"):
                self._direct_quarantined = set()
            self._direct_quarantined.add(key)
            self.log(f"direct feedback quarantined after {backoff.privacy_holds} privacy holds: "
                     f"{paths[0].name}")
            return {"status": "quarantined"}
        if backoff.record_failure(type(exc).__name__, privacy_hold=held):
            self.log(f"direct feedback pending: {type(exc).__name__}; observation retained for retry "
                     f"(backoff up to {int(backoff.cap_polls * backoff.interval)}s)")
        return {"status": "pending"}

    async def _retry_direct_pending(self):
        for entry in tuple(getattr(self, "_direct_pending", {}).values()):
            backoff = entry.get("backoff")
            if backoff is not None and not backoff.ready():
                continue
            await self._direct_reply(entry["m"], entry["text"], entry["ah"])

    @staticmethod
    def _privacy_hold_limit():
        from observation_retry import PRIVACY_HOLD_LIMIT
        return PRIVACY_HOLD_LIMIT

    def _batch_backoff(self):
        backoff = getattr(self, "_classify_backoff", None)
        if backoff is None:
            from observation_retry import RetryBackoff
            backoff = self._classify_backoff = RetryBackoff(getattr(self, "interval", 90.0))
        return backoff

    def _quarantine(self, entries, stage, error_name, backoff):
        """Move held observations into the PRIVATE quarantine; raises if that is refused."""
        from observation_retry import quarantine_observation
        from redact import PrivacyReviewRequired, safe_data
        paths = []
        for entry in entries:
            message = entry.get("message", entry)
            source = "classification" if stage == "classify" else "observation"
            if stage != "classify" and "demand" in entry:
                try:
                    safe_data(entry["demand"])
                except PrivacyReviewRequired:
                    source = "observation"
                else:
                    # The observation screens clean again, so the hold came from the
                    # demand pool's stored rows, which upsert re-screens on every write.
                    source = "pool"
            record = {"reason": "privacy_review_required", "stage": stage, "hold_source": source,
                      "error": error_name, "attempts": backoff.failures + 1,
                      "privacy_holds": backoff.privacy_holds, "quarantined_at": iso(now_utc()),
                      "product_id": self.cfg.get("product_id") or self.cfg.get("slug"),
                      "observation": {**_message_observation(message.get("m")),
                                      "channel": message.get("channel"),
                                      "author_hash": message.get("ah"),
                                      "redacted_text": message.get("text")}}
            for field in ("context", "verdict", "demand"):
                if field in entry:
                    record[field] = entry[field]
            paths.append(quarantine_observation(self.poolp, record))
        return paths

    async def _classify_loop(self):
        # Keep a failed batch separate from messages arriving during a retry.
        self._pending_batch = getattr(self, "_pending_batch", [])
        while not self.is_closed():
            await asyncio.sleep(self.interval)
            backoff = self._batch_backoff()
            stage = "direct"
            try:
                await self._retry_direct_pending()
                if not self._pending_batch:
                    backoff.reset()
                    batch, self.buffer = self.buffer, []
                    self._pending_batch = [{"message": message} for message in batch
                                           if _SIGNAL.search(message["text"])]
                if not self._pending_batch:
                    continue
                if not backoff.ready():
                    continue  # still backing off after a failure; nothing is logged
                self.classification_status = "processing"
                stage = "classify"
                if "verdict" not in self._pending_batch[0]:
                    items = [{"i": i, "channel": entry["message"]["channel"],
                              "text": entry["message"]["text"]}
                             for i, entry in enumerate(self._pending_batch)]
                    verdicts = await asyncio.to_thread(classify_batch, items, self.classify_sys,
                                                       self.product, self.classify_rounds)
                    vmap = {value["i"]: value for value in verdicts}
                    if set(vmap) != set(range(len(items))) or len(verdicts) != len(items):
                        raise ValueError("classification batch is incomplete")
                    for index, entry in enumerate(self._pending_batch):
                        entry["verdict"] = vmap[index]
                while self._pending_batch:
                    stage = "persist"
                    entry = self._pending_batch[0]
                    message, verdict = entry["message"], entry["verdict"]
                    confidence = float(verdict.get("confidence", 0) or 0)
                    if not verdict.get("is_demand") or confidence < self.lo:
                        self._pending_batch.pop(0)
                        continue
                    if "demand" not in entry:
                        entry["demand"] = _demand_from_verdict(
                            verdict, message["text"], message["ah"], message["channel"], self._pool_product(),
                            **_message_observation(message.get("m")))
                    action, row = await asyncio.to_thread(
                        pool.upsert, self.poolp, entry["demand"], self.rescore)
                    # This observation has completed. A later notification failure must
                    # not insert it or acknowledge it again on the next iteration.
                    self._pending_batch.pop(0)
                    self._batch_recovered(backoff)
                    self.log(f"demand({confidence:.2f}) {action}: {row.get('title', '')[:48]!r} "
                             f"reach={row.get('reach')} score={row.get('final_score')}")
                    await self._ack(message["m"], confidence)
                    if action == "new":
                        await self._note(
                            f'New demand from #{message["channel"]}: "{row.get("title", "?")[:80]}" '
                            f'({row.get("grade", "?")} {row.get("final_score", "?")}, '
                            f'reach {row.get("reach", 0)}, {row.get("taxonomy_track", "?")})')
                self.classification_status = "ready"
                self._batch_recovered(backoff)
            except Exception as exc:
                self.classification_status = "retrying"
                self._batch_failed(backoff, stage, exc)

    def _batch_recovered(self, backoff):
        failures = backoff.reset()
        if failures:
            self.log(f"classification recovered after {failures} failed attempts")

    def _batch_failed(self, backoff, stage, exc):
        """Log state changes only; quarantine observations the privacy screen keeps holding."""
        from redact import PrivacyReviewRequired
        name = type(exc).__name__
        held = isinstance(exc, PrivacyReviewRequired) and stage in ("classify", "persist")
        if held and backoff.privacy_holds + 1 >= self._privacy_hold_limit():
            backoff.privacy_holds += 1
            # A classification hold cannot name one message, so the whole unclassified
            # batch is held; a persistence hold names the head observation.
            held_entries = list(self._pending_batch if stage == "classify" else self._pending_batch[:1])
            try:
                self._quarantine(held_entries, stage, name, backoff)
            except Exception as failure:
                if backoff.record_failure("quarantine:" + type(failure).__name__):
                    self.log(f"privacy quarantine refused: {type(failure).__name__}; "
                             f"{len(self._pending_batch)} observations retained for retry")
                return
            del self._pending_batch[:len(held_entries)]
            self.log(f"privacy hold: {len(held_entries)} observations quarantined after "
                     f"{backoff.privacy_holds} attempts (stage={stage})")
            backoff.reset()
            return
        if backoff.record_failure(name, privacy_hold=held):
            self.log(f"classification pending: {name}; "
                     f"{len(self._pending_batch)} observations retained for retry "
                     f"(backoff up to {int(backoff.cap_polls * backoff.interval)}s)")

    def _start_classification(self):
        task = getattr(self, "_classify_task", None)
        if task is None or task.done():
            self._classify_task = self.loop.create_task(self._classify_loop())
            self._classify_task.add_done_callback(self._classify_done)

    def _classify_done(self, task):
        if task is not getattr(self, "_classify_task", None) or self.is_closed():
            return
        if task.cancelled():
            self.classification_status = "stopped"
            return
        error = task.exception()
        self.classification_status = "retrying"
        try:
            self.log("classification worker stopped; restarting with the retained batch"
                     + (f" ({type(error).__name__})" if error else ""))
        finally:
            self._start_classification()

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
        text = safe_text(text)
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
        """Post at the configured local hour; unknown sends require reconciliation.

        The 30-minute tick does no PRIVATE proof by itself: _maybe_daily_summary proves
        the destination only when it is about to write. A repeated failure is logged once.
        """
        last_failure = None
        while not self.is_closed():
            try:
                await self._maybe_daily_summary()
                last_failure = None
            except Exception as e:
                if repr(e) != last_failure:
                    self.log(f"summary loop failed: {e!r}")
                last_failure = repr(e)
            await asyncio.sleep(1800)  # re-check every 30 min

    def _summary_marker(self):
        from finalize import digest
        binding = {"product_id": self.cfg.get("product_id") or self.cfg.get("slug"),
                   "timezone": self.cfg["timezone"]}
        return os.path.join(os.path.dirname(self.poolp), ".daily-summary-" + digest(binding) + ".json")

    async def _maybe_daily_summary(self):
        from zoneinfo import ZoneInfo
        from finalize import logical_identity
        from pathlib import Path
        identity = logical_identity(self.cfg)
        now = now_utc().astimezone(ZoneInfo(identity["timezone"]))
        today = now.date().isoformat()
        hour = now_utc().hour if getattr(self, "summary_hour_uses_utc", False) else now.hour
        if hour < self.summary_hour or not self.post_display or not self.display_id:
            return
        if getattr(self, "_summary_settled", None) == today:
            return {"status": "confirmed", "date": today}
        # Reading the marker is not a DATA write. A day that is already confirmed, or a send
        # awaiting reconciliation, needs no write, so it must not trigger a PRIVATE proof.
        settled = self._peek_summary(today)
        if settled is not None:
            return settled
        marker = Path(require_private(self._summary_marker())["path"])
        try:
            # Do not block the event loop behind another coroutine holding the
            # lock while awaiting the adapter. Contention stays visibly pending.
            with file_lock(str(marker) + ".lock", timeout=0):
                if marker.exists():
                    state = json.loads(marker.read_text(encoding="utf-8"))
                    if hashlib.sha256(state["body"].encode("utf-8")).hexdigest() != state["content_sha256"]:
                        raise ValueError("daily summary saved content changed")
                    if state["status"] != "confirmed":
                        self._summary_note("daily summary pending reconciliation")
                        return {"status": "pending_reconciliation"}
                    if state["identity"]["date"] == today:
                        self._summary_confirmed(today)
                        return state
                body = self._render_summary(today)
                channel = self.get_channel(self.display_id)
                if channel is None:
                    raise RuntimeError("daily summary display channel unavailable before delivery")
                state = {"identity": identity, "status": "unknown", "body": body,
                         "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}
                atomic_json(marker, state)
                try:
                    receipt = await self._post_daily_summary(today, body=body, channel=channel)
                    if not receipt or receipt.get("content_sha256") != state["content_sha256"]:
                        raise ValueError("daily summary adapter receipt mismatch")
                except Exception as exc:
                    state["error_type"] = type(exc).__name__
                    atomic_json(marker, state)
                    self._summary_note("daily summary pending reconciliation")
                    return {"status": "pending_reconciliation"}
                state.update(status="confirmed", message_id=receipt["message_id"])
                atomic_json(marker, state)
                self._summary_confirmed(today)
                return state
        except TimeoutError:
            self.log("daily summary pending: another caller holds the lock")
            return {"status": "pending"}

    def _peek_summary(self, today):
        """Return the saved state when it needs no write, else None (the proven path decides)."""
        try:
            state = json.loads(open(self._summary_marker(), encoding="utf-8").read())
            body_ok = hashlib.sha256(state["body"].encode("utf-8")).hexdigest() == state["content_sha256"]
            status, date = state["status"], state["identity"]["date"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        if not body_ok:
            return None  # the locked path reports the changed content
        if status != "confirmed":
            self._summary_note("daily summary pending reconciliation")
            return {"status": "pending_reconciliation"}
        if date == today:
            self._summary_confirmed(today)
            return state
        return None

    def _summary_note(self, text):
        """Log a summary state once, not on every 30-minute tick."""
        if getattr(self, "_summary_last_note", None) != text:
            self._summary_last_note = text
            self.log(text)

    def _summary_confirmed(self, today):
        self._summary_settled = today
        self._summary_last_note = None

    def reconcile_daily_summary(self, receipt):
        """Accept identity/content-bound evidence without issuing a send."""
        from finalize import _receipt
        from pathlib import Path
        marker = Path(require_private(self._summary_marker())["path"])
        with file_lock(str(marker) + ".lock"):
            state = json.loads(marker.read_text(encoding="utf-8"))
            checksum = hashlib.sha256(state["body"].encode("utf-8")).hexdigest()
            if checksum != state["content_sha256"]:
                raise ValueError("daily summary saved content changed")
            verified = _receipt(receipt, state["identity"], checksum)
            if state["status"] == "confirmed" and state["message_id"] != verified["message_id"]:
                raise ValueError("confirmed daily summary receipt cannot be replaced")
            state.update(status="confirmed", message_id=verified["message_id"])
            atomic_json(marker, state)
            return state

    def _render_summary(self, today):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(getattr(self, "cfg", {}).get("timezone", "UTC"))
        rows = pool.load(self.poolp, product_id=self._pool_product())
        todays = [r for r in rows
                  if any(r.get(key) and parse_ts(r[key]).astimezone(zone).date().isoformat() == today
                         for key in ("last_seen", "first_seen"))]
        ranked = pool.ranked(self.poolp, product_id=self._pool_product())
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
        body = "\n".join(lines)
        if len(body) > 3900:
            body = body[:3840].rstrip() + f"\n... truncated to fit the embed ({len(body)} chars total)"
        return safe_text(body)

    async def _post_daily_summary(self, today, body=None, channel=None):
        body = safe_text(body) if body is not None else self._render_summary(today)
        if not self.post_display or not self.display_id:
            self.log(f"[{self.mode}] daily summary suppressed ({len(body)} chars)")
            return False
        ch = channel if channel is not None else self.get_channel(self.display_id)
        if not ch:
            raise RuntimeError("daily summary display channel unavailable")
        message = await ch.send(embed=discord.Embed(description=body, color=0x57F287))
        if not getattr(message, "id", None):
            raise RuntimeError("daily summary has no adapter message receipt")
        embeds = getattr(message, "embeds", [])
        if not embeds or embeds[0].description != body:
            raise RuntimeError("daily summary adapter did not confirm sent content")
        self.log(f"daily summary posted ({len(body)} chars)")
        return {"message_id": str(message.id),
                "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}


def _run(args) -> int:
    mode = "dry" if args.dry_run else args.mode
    d = _config_dir()
    cfg = load_config()
    ensure_stable_salt()
    from finalize import logical_identity
    logical_identity(cfg)
    require_private(d)
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



def main() -> int:
    from no_console import install_no_console_window_default
    install_no_console_window_default()
    args = _parse_args()
    output = private_output(args.log_file) if args.log_file else nullcontext()
    with output:
        return _run(args)



