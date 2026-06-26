"""T6 — privacy: redact-on-ingest, unique placeholders (no collapse), stable irreversible pseudonym."""
from redact import redact, pseudonymize, has_pii


def test_email_phone_redacted():
    r = redact("ping me at jane.doe@acme.io or +1 (201) 306-8634 please")
    assert "jane.doe@acme.io" not in r["redacted"]
    assert "3068634" not in r["redacted"].replace(" ", "")
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
    r = redact("a@x.com vs b@y.com")
    phs = [p for p, k in r["placeholders"].items() if k == "EMAIL"]
    assert len(set(phs)) == 2, r["placeholders"]
    # the SAME email twice must reuse ONE placeholder (co-reference preserved)
    r2 = redact("mail a@x.com then a@x.com again")
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
    assert has_pii("contact bob@x.com") is True
    assert has_pii("add dark mode to the settings page") is False
