"""The bot token file is a credvault projection: missing or empty must exit with the regen command."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

import lib
import pull_discord

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def bot_module(monkeypatch):
    # Load only the product module; no Discord client, sockets or event loop exists.
    fake = types.ModuleType("discord")
    for name in ("Client", "Thread", "Message"):
        setattr(fake, name, type(name, (), {}))
    monkeypatch.setitem(sys.modules, "discord", fake)
    spec = importlib.util.spec_from_file_location("token_projection_bot", SCRIPTS / "demand_bot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGEN = "python -m credvault materialize --target demand-mining/acmecorp-bot-token"


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_read_bot_token_missing_or_empty_names_regen_command(tmp_path, content):
    path = tmp_path / "secrets" / "acmecorp-bot-token"
    if content is not None:
        path.parent.mkdir()
        path.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        lib.read_bot_token(path, "synthetic_reader")
    msg = str(exc.value)
    assert msg.startswith("synthetic_reader: bot token file ")
    assert REGEN in msg and "do not hand-write" in msg
    assert "write the bot token there" not in msg


def test_read_bot_token_returns_stripped_value(tmp_path):
    path = tmp_path / "acmecorp-bot-token"
    path.write_text("synthetic-token\n", encoding="utf-8")
    assert lib.read_bot_token(path, "synthetic_reader") == "synthetic-token"


def _registry(root):
    (root / "registry.json").write_text(json.dumps({"products": [{
        "slug": "acmecorp", "display_name": "AcmeCorp",
        "discord_channels": [{"id": "1", "name": "feedback"}],
        "discord_token_ref": "secrets/acmecorp-bot-token"}]}), encoding="utf-8")


@pytest.mark.parametrize("content", [None, ""])
def test_pull_discord_wiring_fails_clearly(tmp_path, monkeypatch, content):
    _registry(tmp_path)
    if content is not None:
        (tmp_path / "secrets").mkdir()
        (tmp_path / "secrets" / "acmecorp-bot-token").write_text(content, encoding="utf-8")
    monkeypatch.setattr(pull_discord, "find_config_dir", lambda: tmp_path)
    monkeypatch.setenv("DEMAND_MINING_PRODUCT", "acmecorp")
    with pytest.raises(SystemExit, match="pull_discord: bot token file") as exc:
        pull_discord._load_wiring()
    assert REGEN in str(exc.value)


@pytest.mark.parametrize("content", [None, ""])
def test_demand_bot_wiring_fails_clearly(tmp_path, monkeypatch, bot_module, content):
    _registry(tmp_path)
    if content is not None:
        (tmp_path / "secrets").mkdir()
        (tmp_path / "secrets" / "acmecorp-bot-token").write_text(content, encoding="utf-8")
    monkeypatch.setenv("DEMAND_MINING_PRODUCT", "acmecorp")
    with pytest.raises(SystemExit, match="demand_bot: bot token file") as exc:
        bot_module._wiring(tmp_path)
    assert REGEN in str(exc.value)
