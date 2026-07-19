"""Context-aware @/DM handling: a bare "check this" must be answered against the surrounding subject
(thread/forum opening post, replied-to message, recent chat), and everything fed to the LLM must be
redacted + author-pseudonymized first.
"""
import asyncio
import types
from unittest import mock

import pytest

pytest.importorskip("discord")
import discord  # noqa: E402
import demand_bot as b  # noqa: E402


class FakeMsg:
    def __init__(self, mid, content, author_id, is_bot=False):
        self.id = mid
        self.content = content
        self.author = types.SimpleNamespace(id=author_id, bot=is_bot)


def _aiter(items):
    async def gen(*a, **k):
        for it in items:
            yield it
    return gen


@pytest.fixture
def bot():
    # discord.Client.user is a read-only property; patch it on the class for the test's lifetime.
    with mock.patch.object(b.DemandBot, "user", types.SimpleNamespace(id=999)):
        yield b.DemandBot.__new__(b.DemandBot)


def test_gen_reply_threads_context_into_the_prompt(monkeypatch):
    cap = {}

    def fake(prompt, timeout=90, chain=b._DEFAULT_CHAIN):
        cap["p"] = prompt
        return "Logged the lorebook retrieval bug for the team."
    monkeypatch.setattr(b, "_llm", fake)
    out = b.gen_reply("check this!", b._reply_sys("P"),
                      context="[opening post] lorebook retrieval fails after 20 messages")
    assert "CONVERSATION CONTEXT" in cap["p"] and "lorebook retrieval fails" in cap["p"]
    assert "lorebook" in out


def test_gen_reply_without_context_is_unchanged(monkeypatch):
    cap = {}
    monkeypatch.setattr(b, "_llm", lambda p, timeout=90, chain=b._DEFAULT_CHAIN: cap.setdefault("p", p) or "ok")
    b.gen_reply("hi", b._reply_sys("P"))
    # the block header "CONVERSATION CONTEXT:" only appears when context is supplied (the system prompt
    # mentions the phrase without a colon, as an instruction, so match on the colon form)
    assert "CONVERSATION CONTEXT:" not in cap["p"]


def test_gather_context_reads_thread_reply_and_history(bot):
    thread = mock.MagicMock()
    thread.__class__ = discord.Thread  # so isinstance(ch, discord.Thread) is True
    thread.name = "lorebook retrieval broken"
    thread.starter_message = FakeMsg(100, "My lorebook entries stop being retrieved after ~20 messages.", 42)
    thread.fetch_message = mock.AsyncMock(return_value=FakeMsg(55, "here is the reply target", 43))
    thread.history = _aiter([FakeMsg(90, "yeah same here", 44),
                             FakeMsg(91, "any workaround?", 45),
                             FakeMsg(92, "i am a bot, skip me", 999, is_bot=True)])
    m = mock.MagicMock(id=101, channel=thread)
    m.reference = types.SimpleNamespace(message_id=55, resolved=None)

    ctx = asyncio.run(bot._gather_context(m))
    assert "[thread title]" in ctx and "lorebook retrieval broken" in ctx
    assert "[opening post]" in ctx and "stop being retrieved" in ctx
    assert "[replying to" in ctx and "here is the reply target" in ctx
    assert "[recent]" in ctx and "yeah same here" in ctx and "any workaround?" in ctx
    assert "i am a bot, skip me" not in ctx          # bot messages excluded
    assert len(ctx) <= 1500


def test_gather_context_redacts_pii_before_returning(bot):
    thread = mock.MagicMock()
    thread.__class__ = discord.Thread
    thread.name = "billing issue"
    thread.starter_message = FakeMsg(100, "email me at alice@example.com about my charge", 42)
    thread.fetch_message = mock.AsyncMock(return_value=None)
    thread.history = _aiter([])
    m = mock.MagicMock(id=101, channel=thread)
    m.reference = None
    ctx = asyncio.run(bot._gather_context(m))
    assert "alice@example.com" not in ctx  # a real email must be redacted before it reaches the LLM


def test_gather_context_survives_api_errors(bot):
    thread = mock.MagicMock()
    thread.__class__ = discord.Thread
    thread.name = "topic"
    thread.starter_message = None
    thread.fetch_message = mock.AsyncMock(side_effect=RuntimeError("boom"))

    async def boom(*a, **k):
        raise RuntimeError("history down")
        yield  # pragma: no cover
    thread.history = boom
    m = mock.MagicMock(id=101, channel=thread)
    m.reference = types.SimpleNamespace(message_id=7, resolved=None)
    ctx = asyncio.run(bot._gather_context(m))  # must not raise
    assert "[thread title]" in ctx  # the one source that worked still contributes


def test_direct_reply_redacts_thread_title_into_pool(bot, monkeypatch):
    """Regression: a forum/thread title is user-authored and can carry PII; the chan_label derived
    from it must be redacted before it reaches the classifier prompt OR the pool evidence source."""
    thread = mock.MagicMock()
    thread.__class__ = discord.Thread
    thread.name = "billing broken for john.doe@example.com"
    thread.starter_message = None
    thread.fetch_message = mock.AsyncMock(return_value=None)
    thread.history = _aiter([])
    m = mock.MagicMock(id=1, channel=thread)
    m.reference = None
    bot.post_direct = False  # skip the actual send
    bot.mode = "shadow"
    bot.classify_sys = bot.reply_sys = "sys"
    bot.product = "P"
    bot.classify_rounds = 1
    bot.rescore = lambda r: r
    bot.poolp = "x"
    bot._note = mock.AsyncMock()
    monkeypatch.setattr(b, "gen_reply", lambda *a, **k: "reply")
    monkeypatch.setattr(b, "classify_batch", lambda *a, **k: [
        {"i": 0, "is_demand": True, "confidence": 0.9, "title": "Billing issue",
         "track": "billing", "kano": "must_be"}])
    cap = {}
    monkeypatch.setattr(b.pool, "upsert",
                        lambda path, demand, rescore=None: (cap.setdefault("d", demand), ("new", demand))[1])
    import asyncio as _a
    _a.run(bot._direct_reply(m, "check this billing", "u_hash"))
    source = cap["d"]["evidence"][0]["source"]
    assert "john.doe@example.com" not in source  # raw email from the title must be scrubbed
