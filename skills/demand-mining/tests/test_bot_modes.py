"""The daemon's output gates decide what reaches a LIVE community, so they get a regression test.

Three independent gates, because they carry different risk:
  post_direct    = replying to an @-mention/DM (user-initiated; fires in shadow too).
  post_community = UNPROMPTED channel auto-reply + reaction on a detected demand (live only).
  post_display   = the internal admin dashboard (any mode except dry).

Regression guard: a @/DM once went unanswered in shadow because the direct reply shared the
community gate. shadow MUST answer a direct @/DM while staying silent on unprompted chatter.
"""
import pytest

pytest.importorskip("discord")  # the daemon needs discord.py; skip cleanly if absent
import demand_bot  # noqa: E402


def _bot(mode):
    return demand_bot.DemandBot(cfg={}, chans={}, guild=None, display=None, mode=mode,
                                interval=90, display_interval=300, poolp="pool.jsonl")


@pytest.mark.parametrize("mode,direct,community,display", [
    ("dry", False, False, False),
    ("shadow", True, False, True),
    ("live", True, True, True),
])
def test_gates_per_mode(mode, direct, community, display):
    b = _bot(mode)
    assert b.post_direct is direct
    assert b.post_community is community
    assert b.post_display is display


def test_shadow_answers_direct_but_stays_community_silent():
    """The exact bug: shadow must answer a direct @/DM yet never post unprompted."""
    b = _bot("shadow")
    assert b.post_direct is True        # @/DM reply fires
    assert b.post_community is False    # unprompted channel auto-reply does not


def test_daily_summary_is_english_and_dash_free(tmp_path):
    """The admin channel content must be English (community language) and carry no en/em dashes."""
    import re
    import json as _json
    import demand_bot as _b
    pool_file = tmp_path / "demands.jsonl"
    pool_file.write_text(_json.dumps({
        "canonical_key": "k", "title": "Fix reload button", "grade": "A", "final_score": 90,
        "reach": 5, "status": "new", "first_seen": "2026-07-19T00:00:00Z",
        "last_seen": "2026-07-19T00:00:00Z"}) + "\n", encoding="utf-8")
    bot = _b.DemandBot.__new__(_b.DemandBot)
    bot.poolp = str(pool_file)
    out = bot._render_summary("2026-07-19")
    assert "Daily Demand Summary" in out and "All-time top" in out and "Fix reload button" in out
    assert not re.search(r"[一-鿿]", out)   # no Chinese
    assert not re.search(r"[–—―]", out)  # no en/em/bar dash
