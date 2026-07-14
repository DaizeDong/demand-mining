"""T6 — privacy: redact-on-ingest, unique placeholders (no collapse), stable irreversible pseudonym."""
from redact import redact, pseudonymize, has_pii


def test_email_phone_redacted():
    r = redact("ping me at jane.doe@acme.io or +1 (555) 867-5309 please")
    assert "jane.doe@acme.io" not in r["redacted"]
    assert "8675309" not in r["redacted"].replace(" ", "")
    assert r["found"].get("EMAIL") == 1 and r["found"].get("PHONE") == 1


def test_discord_id_handle_invite():
    r = redact("hey <@123456789012345678> aka @cooluser join discord.gg/abc123")
    assert "123456789012345678" not in r["redacted"]
    assert "@cooluser" not in r["redacted"]
    assert "discord.gg/abc123" not in r["redacted"]


def test_credit_card_luhn_only():
    # 4111111111111111 is a valid Luhn card -> CARD; a short ordinary number is not a card.
    r = redact("card 4111 1111 1111 1111 but item 42 stays")
    assert "4111" not in r["redacted"]
    assert r["found"].get("CARD") == 1          # only the Luhn-valid number is a CARD
    assert "42" in r["redacted"]                # short ordinary number untouched


def test_unique_placeholders_no_collapse():
    # two DISTINCT emails must get DISTINCT placeholders (anti-collapse)
    r = redact("a@example.com vs b@example.org")
    phs = [p for p, k in r["placeholders"].items() if k == "EMAIL"]
    assert len(set(phs)) == 2, r["placeholders"]
    # the SAME email twice must reuse ONE placeholder (co-reference preserved)
    r2 = redact("mail a@example.com then a@example.com again")
    phs2 = [p for p, k in r2["placeholders"].items() if k == "EMAIL"]
    assert len(phs2) == 1


def test_secret_token_entropy():
    r = redact("key sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a stays out")
    assert "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c" not in r["redacted"]
    assert r["found"].get("SECRET", 0) >= 1


def test_pseudonym_stable_and_irreversible():
    a = pseudonymize("user-123")
    b = pseudonymize("user-123")
    c = pseudonymize("user-999")
    assert a == b and a != c           # same person -> same token; different -> different
    assert a.startswith("u_") and "user-123" not in a   # not invertible / no raw id


def test_has_pii_egress_guard():
    assert has_pii("contact bob@example.com") is True
    assert has_pii("add dark mode to the settings page") is False


def test_ipv6_redacted_and_egress_blocked():
    # Architecture Tier1 lists "IPs"; IPv6 (full 8-group + ::-compressed) must redact like IPv4,
    # and a residual IPv6 must trip the egress DLP (has_pii) fail-closed — otherwise it leaks.
    r = redact("server 2001:0db8:85a3:0000:0000:8a2e:0370:7334 and gw fe80::1ff:fe23:4567:890a down")
    assert "2001:0db8:85a3" not in r["redacted"]
    assert "fe80::1ff" not in r["redacted"]
    assert r["found"].get("IP", 0) >= 2          # both addresses caught (distinct placeholders)
    assert has_pii("connect to 2001:db8::1 please") is True   # egress DLP fail-closed on IPv6
    # guard: plain decimal time / ratio (colons but no "::" and not 8 hex groups) is NOT eaten
    clean = redact("standup at 12:34:56, win rate 3:2")
    assert "12:34:56" in clean["redacted"] and "3:2" in clean["redacted"]
