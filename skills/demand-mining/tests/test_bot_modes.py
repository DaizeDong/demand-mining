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
