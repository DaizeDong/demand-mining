"""Deterministic regression tests at isolated model, receipt and Git boundaries."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import importlib.util
import sys
import types

import pytest

import data_safety
import lib
import push_card
import redact
import run


@pytest.fixture
def cases():
    return json.loads((Path(__file__).parent / "fixtures/repair_cases.json").read_text())


@pytest.fixture
def private_repo(tmp_path, monkeypatch):
    root = tmp_path / "companion"
    root.mkdir()
    def synthetic_git(directory, *args, **kwargs):
        if args == ("rev-parse", "--show-toplevel"):
            stdout = str(root)
        elif args == ("config", "--null", "--list"):
            stdout = ""
        elif args in {("remote", "get-url", "origin"),
                       ("remote", "get-url", "--all", "origin"),
                       ("remote", "get-url", "--all", "--push", "origin")}:
            stdout = "https://github.com/example/demand-mining-config.git"
        else:
            raise AssertionError("unapproved synthetic Git command")
        return subprocess.CompletedProcess(["git", *args], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(data_safety, "git", synthetic_git)
    monkeypatch.setattr(data_safety, "_visibility", lambda repository: "PRIVATE")
    monkeypatch.setenv("DEMAND_MINING_CONFIG", str(root))
    monkeypatch.delenv("DEMAND_MINING_DRYRUN", raising=False)
    # Default deny of the external send seam, including before a failing assertion.
    monkeypatch.setattr(push_card, "deliver", lambda message, dry_run=False: (False, "synthetic blocked"))
    return root


@pytest.fixture
def cfg():
    result = copy.deepcopy(lib.DEFAULT_CONFIG)
    result.update(product_id="example-product", timezone="America/New_York")
    return result


class Ledger:
    def __init__(self):
        self.rows = []
        self.watermarks = []

    def list_active(self):
        return self.rows

    def upsert(self, card, ext, priority=0):
        self.rows.append({"idempotency_key": "demand-mining:" + card["canonical_key"], "ext": ext})

    def _run(self, verb, args):
        return {"ok": True}

    def add_watermark(self, value):
        self.watermarks.append(value)


def test_person_and_address_are_removed_before_output(cases):
    result = redact.redact(cases["private_text"])
    assert cases["person"] not in result["redacted"]
    assert cases["address"] not in result["redacted"]
    assert redact.redact(cases["product_text"])["redacted"] == cases["product_text"]


def test_explicit_invalid_config_does_not_fall_back(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setenv("DEMAND_MINING_CONFIG", str(missing))
    with pytest.raises(ValueError, match="DEMAND_MINING_CONFIG"):
        lib.find_config_dir()


def test_nonprivate_destination_is_named(private_repo, monkeypatch):
    monkeypatch.setattr(data_safety, "_visibility", lambda repository: "PUBLIC")
    with pytest.raises(data_safety.DestinationError, match="PUBLIC") as error:
        data_safety.atomic_json(private_repo / "pool/result.json", {})
    assert str(private_repo) in str(error.value)
    assert not (private_repo / "pool").exists()


def test_ledger_failure_is_not_empty_success(private_repo, cfg, cases):
    ledger = Ledger()
    ledger.list_active = lambda: (_ for _ in ()).throw(RuntimeError("ledger unavailable"))
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert not ledger.watermarks


def test_confirmed_receipt_is_required_for_watermark(private_repo, cfg, cases, monkeypatch):
    ledger = Ledger()
    monkeypatch.setattr(push_card, "deliver", lambda message, dry_run=False: (True, "exit zero only"))
    result = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert result["status"] == "pending_reconciliation"
    assert not ledger.watermarks


def test_delivery_replay_has_same_artifacts_and_does_not_resend(private_repo, cfg, cases, monkeypatch):
    ledger, sent = Ledger(), []

    def deliver(message, dry_run=False):
        sent.append(message)
        return True, {"status": "confirmed", "message_id": "synthetic-receipt",
                      "identity": push_card._DELIVERY_IDENTITY.get(),
                      "content_sha256": hashlib.sha256(message.encode()).hexdigest()}

    monkeypatch.setattr(push_card, "deliver", deliver)
    result = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert result["status"] == "complete"
    replay = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert replay["status"] == "complete"
    assert len(sent) == 1
    assert len(ledger.watermarks) == 1
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["identity"]["timezone"] == "America/New_York"
    for artifact in manifest["artifacts"]:
        content = Path(artifact["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
        assert len(content) == artifact["bytes"]


def test_unknown_delivery_never_automatically_resends(private_repo, cfg, cases, monkeypatch):
    ledger, calls = Ledger(), []

    def deliver(message, dry_run=False):
        calls.append(message)
        raise TimeoutError("ambiguous transport")

    monkeypatch.setattr(push_card, "deliver", deliver)
    first = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    second = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert first["status"] == second["status"] == "pending_reconciliation"
    assert len(calls) == 1
    assert not ledger.watermarks


def test_empty_input_without_collection_evidence_cannot_complete(private_repo, cfg):
    with pytest.raises(ValueError, match="collection"):
        run.process([], cfg, Ledger(), archive_dir=str(private_repo / "pool"))


def _confirm(message, dry_run=False):
    return True, {"status": "confirmed", "message_id": "synthetic-receipt",
                  "identity": push_card._DELIVERY_IDENTITY.get(),
                  "content_sha256": hashlib.sha256(message.encode()).hexdigest()}


def test_changed_artifact_blocks_replay(private_repo, cfg, cases, monkeypatch):
    monkeypatch.setattr(push_card, "deliver", _confirm)
    ledger = Ledger()
    first = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    Path(first["digest_path"]).write_text("synthetic changed bytes", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact bytes differ"):
        run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))


def test_watermark_failure_retries_without_delivery(private_repo, cfg, cases, monkeypatch):
    calls = []
    monkeypatch.setattr(push_card, "deliver", lambda message, dry_run=False: (calls.append(message), _confirm(message))[1])
    ledger = Ledger()
    ledger.add_watermark = lambda value: (_ for _ in ()).throw(OSError("synthetic watermark failure"))
    with pytest.raises(OSError, match="synthetic watermark"):
        run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    ledger.add_watermark = lambda value: ledger.watermarks.append(value)
    assert run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))["ok"]
    assert len(calls) == 1


def test_receipt_without_identity_is_unknown(private_repo, cfg, cases, monkeypatch):
    def unbound(message, dry_run=False):
        ok, receipt = _confirm(message)
        receipt.pop("identity")
        return ok, receipt

    monkeypatch.setattr(push_card, "deliver", unbound)
    ledger = Ledger()
    result = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert result["status"] == "pending_reconciliation"
    assert not ledger.watermarks


def test_saved_identity_survives_midnight(cfg, monkeypatch):
    from finalize import logical_identity
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-08T06:30:00Z")
    identity = logical_identity(cfg)
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-09T06:30:00Z")
    assert logical_identity(cfg, identity) == identity
    assert identity["source_window"]["end"] == "2026-03-08T06:30:00Z"


def test_pseudonym_metadata_cannot_bypass_privacy(cases):
    result = redact.safe_data({"author_hash": "u_" + cases["person"].lower().replace(" ", "_"),
                               "user_id": 123})
    assert "example" not in result["author_hash"]
    assert "user_id" not in result


def test_overlapping_callers_finish_once(private_repo, cfg, cases, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    calls = []
    mkdir = data_safety.os.mkdir
    attempted = []

    def traced_mkdir(path, *args, **kwargs):
        attempted.append(str(path))
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(data_safety.os, "mkdir", traced_mkdir)
    monkeypatch.setattr(push_card, "deliver", lambda message, dry_run=False: (calls.append(message), _confirm(message))[1])
    ledger = Ledger()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run.process([cases["candidate"]], cfg, ledger,
                                   archive_dir=str(private_repo / "pool")), range(2)))
    except Exception:
        print("synthetic mkdir attempts: " + json.dumps(attempted))
        raise
    assert all(result["status"] == "complete" for result in results)
    assert len(calls) == 1


@pytest.fixture
def service_cases():
    path = Path(__file__).resolve().parents[3] / "tools/make_fixtures.py"
    spec = importlib.util.spec_from_file_location("synthetic_fixture_generator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fixtures()


def test_generator_reproduces_committed_fixture(service_cases, tmp_path):
    # Keep the newly generated output in synthetic OUT for an explicit source copy.
    content = json.dumps(service_cases, indent=2, sort_keys=True) + "\n"
    (tmp_path / "repair_cases.json").write_text(content, encoding="utf-8")
    path = Path(__file__).parent / "fixtures/repair_cases.json"
    assert path.read_text(encoding="utf-8") == content


def test_git_failure_is_visible_and_optional_locks_cannot_be_enabled(tmp_path, monkeypatch):
    calls = []

    def child(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 17, stdout="", stderr="synthetic private detail")

    monkeypatch.setattr(data_safety.subprocess, "run", child)
    with pytest.raises(RuntimeError, match="exit 17") as error:
        data_safety.git(tmp_path, "status", env={"GIT_OPTIONAL_LOCKS": "1"})
    assert "synthetic private detail" not in str(error.value)
    assert calls[0][1]["env"]["GIT_OPTIONAL_LOCKS"] == "0"


@pytest.mark.parametrize("verb", ["read-tree", "add", "diff", "commit", "push"])
def test_backup_failure_stops_later_steps(private_repo, monkeypatch, verb):
    import backup
    path = private_repo / "synthetic-result.json"
    path.write_text("{}", encoding="utf-8")
    calls = []

    def git(directory, *args, **kwargs):
        calls.append((args[0], kwargs))
        if args[0] == verb:
            raise RuntimeError("synthetic " + verb + " failure")
        return subprocess.CompletedProcess(["git", *args], 1 if args[0] == "diff" else 0,
                                           stdout="", stderr="")

    monkeypatch.setattr(backup, "git", git)
    monkeypatch.setattr(backup, "require_private_push", lambda *args: None)
    with pytest.raises(RuntimeError, match="synthetic " + verb):
        backup.backup([path])
    stages = ["read-tree", "add", "diff", "commit", "push"]
    assert [item[0] for item in calls] == stages[:stages.index(verb) + 1]
    for stage, kwargs in calls:
        if stage == "diff":
            assert kwargs["env"]["GIT_INDEX_FILE"]
        assert kwargs["env"].get("GIT_LITERAL_PATHSPECS") == "1"


def test_backup_rejects_directory_pathspec(private_repo):
    import backup
    with pytest.raises(ValueError, match="regular file"):
        backup.backup([private_repo])


def test_effective_push_url_cannot_point_to_another_repository(private_repo, monkeypatch):
    def wrong_push(directory, *args, **kwargs):
        if args == ("rev-parse", "--show-toplevel"):
            output = str(private_repo)
        elif args == ("config", "--null", "--list"):
            output = ""
        elif args[:2] == ("remote", "get-url"):
            output = ("https://github.com/example/other-config.git" if "--push" in args else
                      "https://github.com/example/demand-mining-config.git")
        else:
            raise AssertionError("unapproved synthetic Git command")
        return subprocess.CompletedProcess([], 0, stdout=output, stderr="")
    monkeypatch.setattr(data_safety, "git", wrong_push)
    with pytest.raises(data_safety.DestinationError, match="push destination"):
        data_safety.require_private_push(private_repo, "example/demand-mining-config")


def test_confirmed_push_bookkeeping_is_idempotent(private_repo, cfg, cases, monkeypatch):
    import dedup
    monkeypatch.setattr(push_card, "deliver", _confirm)
    ledger = Ledger()
    for _ in range(2):
        result = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
        assert result["pushed"]
    assert ledger.rows[-1]["ext"][dedup.EXT + "push_count"] == 1


def test_reconciliation_finishes_without_resend(private_repo, cfg, cases, monkeypatch):
    import finalize
    ledger = Ledger()
    first = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
    receipt = {"status": "confirmed", "identity": first["identity"],
               "content_sha256": manifest["content_sha256"], "message_id": "synthetic-reconciled"}
    finalize.reconcile(first["state_path"], receipt)
    monkeypatch.setattr(push_card, "deliver", lambda *a, **k: pytest.fail("unexpected resend"))
    second = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert second["status"] == "complete"
    assert len(ledger.watermarks) == 1


def test_explicit_empty_completion_keeps_collection_evidence(private_repo, cfg, monkeypatch):
    import finalize
    identity = finalize.logical_identity(cfg)
    handoff = {"identity": identity, "candidates": [], "collection": {
        "status": "complete", "classification": "complete", "source_window": identity["source_window"]}}
    monkeypatch.setattr(push_card, "deliver", _confirm)
    result = run.process(handoff, cfg, Ledger(), archive_dir=str(private_repo / "pool"))
    assert result["status"] == "complete" and result["empty_day"]
    assert not result["pushed"]


def test_scheduled_proposal_uses_shared_agent_interface(cfg, cases, monkeypatch):
    import scheduled
    import llmcall
    calls = []

    def call(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return types.SimpleNamespace(error=None, data={"classification": "complete",
                                                     "candidates": [cases["candidate"]]})

    monkeypatch.setattr(llmcall, "call", call)
    corpus = {"channels": {"feedback": [{"text": cases["private_text"] + " " + cases["product_text"]}]}}
    assert scheduled.propose(corpus, cfg)
    assert calls[0][1] == {"mode": "agent"}
    assert cases["person"] not in calls[0][0] and cases["address"] not in calls[0][0]
    assert cases["product_text"] in calls[0][0]


def test_scheduled_retry_reuses_handoff_and_delivery_then_continues_cutoff(private_repo, cfg, cases, monkeypatch):
    import backup
    import scheduled
    windows, sends, backups = [], [], []
    ledger = Ledger()
    ledger.init = lambda: None
    monkeypatch.setattr(scheduled, "preflight", lambda *a: (
        cfg, private_repo, private_repo / "pool", private_repo / "logs"))
    monkeypatch.setattr(scheduled.pull_discord, "_load_wiring", lambda: ([], "synthetic-token"))

    def collect(channels, token, source_window):
        windows.append(source_window)
        return {"collection": {"status": "complete", "source_window": source_window}}

    def save(paths):
        backups.append(paths)
        if len(backups) == 1:
            raise RuntimeError("synthetic backup failure")
        return {"status": "confirmed"}

    monkeypatch.setattr(scheduled.pull_discord, "pull", collect)
    monkeypatch.setattr(scheduled, "propose", lambda *a: [cases["candidate"]])
    monkeypatch.setattr(scheduled.dedup, "LedgerClient", lambda **k: ledger)
    monkeypatch.setattr(backup, "backup", save)
    monkeypatch.setattr(push_card, "deliver", lambda message, dry_run=False: (
        sends.append(message), _confirm(message))[1])
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-08T06:30:00Z")
    first = scheduled.execute()
    assert first["status"] == "delivered_backup_pending"
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-09T06:30:00Z")
    second = scheduled.execute()
    assert second["status"] == "complete" and len(sends) == 1 and len(windows) == 1
    assert first["identity"] == second["identity"]
    third = scheduled.execute()
    assert third["status"] == "complete" and len(windows) == 2
    assert windows[1]["start"] == windows[0]["end"]


@pytest.fixture
def bot_module(monkeypatch):
    # Load only the product module; no Discord client, sockets or event loop exists.
    fake = types.ModuleType("discord")
    fake.Client = type("Client", (), {})
    fake.Thread = type("Thread", (), {})
    fake.Message = type("Message", (), {})
    monkeypatch.setitem(sys.modules, "discord", fake)
    path = Path(__file__).resolve().parents[1] / "scripts/demand_bot.py"
    spec = importlib.util.spec_from_file_location("contract_bot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bot_wiring_selects_configured_product(private_repo, cfg, service_cases, monkeypatch, bot_module):
    (private_repo / "registry.json").write_text(json.dumps(service_cases["registry"]), encoding="utf-8")
    (private_repo / "synthetic-selected-token.txt").write_text("synthetic-token", encoding="utf-8")
    monkeypatch.setattr(bot_module, "load_config", lambda: cfg)
    token, channels, _, _, product = bot_module._wiring(private_repo)
    assert token == "synthetic-token" and channels == {"2": "feedback"}
    assert product == "CSV export"


@pytest.mark.parametrize("field,value", [("is_demand", "false"), ("confidence", 2.0),
                                        ("confidence", float("nan")), ("title", []),
                                        ("track", None), ("kano", "unknown")])
def test_invalid_classification_is_visible(bot_module, service_cases, monkeypatch, field, value):
    verdict = dict(service_cases["verdict"], **{field: value})
    monkeypatch.setattr(bot_module, "_llm", lambda *a, **k: ([verdict], "synthetic-provider"))
    with pytest.raises(ValueError, match="classification"):
        bot_module.classify_batch([{"i": 0, "channel": "feedback", "text": service_cases["product_text"]}])


def test_bot_classification_audit_and_reply_scrub_all_model_boundaries(bot_module, service_cases, monkeypatch):
    calls = []
    verdict = dict(service_cases["verdict"], why=service_cases["private_text"])

    def call(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return types.SimpleNamespace(error=None, data=[verdict], text=service_cases["private_text"],
                                     provider="synthetic-provider")

    monkeypatch.setattr(bot_module, "_llmcall", call)
    results = bot_module.classify_batch([{"i": 0, "channel": "feedback",
                                         "text": service_cases["private_text"]}])
    reply = bot_module.gen_reply(service_cases["private_text"], context=service_cases["private_text"])
    assert len(calls) == 3
    assert set(calls[0][1]) == {"schema"}
    assert calls[1][1]["avoid"] == "synthetic-provider"
    assert not calls[2][1]
    for text in [json.dumps(results), reply, *(item[0] for item in calls)]:
        assert service_cases["person"] not in text and service_cases["address"] not in text


@pytest.mark.parametrize("response", [{}, "FORBIDDEN"])
def test_collection_errors_do_not_become_empty_success(service_cases, monkeypatch, response):
    import pull_discord
    monkeypatch.setattr(pull_discord, "_get", lambda *a: response)
    with pytest.raises((ValueError, RuntimeError)):
        pull_discord.pull(service_cases["registry"]["products"][0]["discord_channels"], "synthetic-token")


def test_collection_requires_a_declared_source():
    import pull_discord
    with pytest.raises(ValueError, match="source"):
        pull_discord.pull([], "synthetic-token")


def test_collection_preserves_distinct_redacted_channels(service_cases, monkeypatch):
    import pull_discord
    channels = [{"id": "1", "name": service_cases["person"]},
                {"id": "2", "name": service_cases["person"]}]
    message = dict(service_cases["message"], content=service_cases["private_text"])
    monkeypatch.setattr(pull_discord, "_get", lambda *a: [message])
    result = pull_discord.pull(channels, "synthetic-token", full=True)
    assert len(result["channels"]) == 2
    text = json.dumps(result)
    assert service_cases["person"] not in text and service_cases["address"] not in text


@pytest.mark.parametrize("response", ["", "[]", '{"ok": false}', '{"error": "synthetic failure"}'])
def test_ledger_rejects_failed_or_missing_response(private_repo, monkeypatch, response):
    import dedup
    ledger = dedup.LedgerClient(cmd=["synthetic-ledger"], db_path=str(private_repo / "synthetic.db"))
    monkeypatch.setattr(dedup.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        [], 0, stdout=response, stderr=""))
    with pytest.raises(ValueError, match="reminder.py"):
        ledger.init()


def test_ledger_cannot_hide_missing_rows_or_repeat_cursor(monkeypatch):
    import dedup
    ledger = dedup.LedgerClient(cmd=["synthetic-ledger"])
    monkeypatch.setattr(ledger, "_run", lambda *a: {})
    with pytest.raises(ValueError, match="items array"):
        ledger.list_active()
    monkeypatch.setattr(ledger, "_run", lambda *a: {"items": [], "next_cursor": "synthetic-cursor"})
    with pytest.raises(ValueError, match="cursor"):
        ledger.list_active()


def _complete_pure_coroutine(coroutine):
    # These doubles complete synchronously at await points, requiring no socket-
    # backed asyncio event loop and no external work.
    try:
        coroutine.send(None)
    except StopIteration as stopped:
        return stopped.value
    finally:
        coroutine.close()
    raise AssertionError("test coroutine unexpectedly requested external scheduling")


def test_bot_context_privacy_is_checked_before_return(bot_module, service_cases):
    bot = bot_module.DemandBot.__new__(bot_module.DemandBot)
    bot.user = None
    bot.log = lambda message: None
    channel = bot_module.discord.Thread()
    channel.name = service_cases["private_text"]
    channel.starter_message = types.SimpleNamespace(id=1, content=service_cases["private_text"])

    async def history(**kwargs):
        yield types.SimpleNamespace(author=types.SimpleNamespace(id=42, bot=False),
                                    content=service_cases["private_text"])

    channel.history = history
    message = types.SimpleNamespace(id=2, channel=channel, reference=None)
    context = _complete_pure_coroutine(bot._gather_context(message))
    assert service_cases["person"] not in context and service_cases["address"] not in context
    assert "CSV export" in context


def test_bot_daily_summary_unknown_send_is_reconcilable(private_repo, cfg, bot_module):
    bot = bot_module.DemandBot.__new__(bot_module.DemandBot)
    bot.cfg, bot.poolp = cfg, str(private_repo / "demands.jsonl")
    bot.post_display, bot.display_id, bot.summary_hour = True, 2, 0
    bot.get_channel = lambda channel_id: object()
    bot.log = lambda message: None
    bot._render_summary = lambda today: "CSV export needs retry support."
    sends = []

    async def send(today, body=None, channel=None):
        sends.append(body)
        raise TimeoutError("synthetic ambiguous delivery")

    bot._post_daily_summary = send
    assert _complete_pure_coroutine(bot._maybe_daily_summary())["status"] == "pending_reconciliation"
    assert _complete_pure_coroutine(bot._maybe_daily_summary())["status"] == "pending_reconciliation"
    assert len(sends) == 1
    state = json.loads(Path(bot._summary_marker()).read_text(encoding="utf-8"))
    bot.reconcile_daily_summary({"identity": state["identity"], "status": "confirmed",
                                 "message_id": "synthetic-message", "content_sha256": state["content_sha256"]})
    assert _complete_pure_coroutine(bot._maybe_daily_summary())["status"] == "confirmed"
    assert len(sends) == 1


def test_bot_summary_groups_timestamps_by_configured_local_day(private_repo, cfg, bot_module, monkeypatch):
    bot = bot_module.DemandBot.__new__(bot_module.DemandBot)
    bot.cfg, bot.poolp = cfg, str(private_repo / "demands.jsonl")
    row = {"title": "CSV export", "last_seen": "2026-03-09T01:30:00Z"}
    monkeypatch.setattr(bot_module.pool, "load", lambda path, *, product_id: [row])
    monkeypatch.setattr(bot_module.pool, "ranked", lambda path, *, product_id: [row])
    assert "Touched today: 1" in bot._render_summary("2026-03-08")
    assert "Touched today: 0" in bot._render_summary("2026-03-09")


def test_direct_implicit_retry_freezes_cutoff_and_continues_next_day(private_repo, cfg, cases, monkeypatch):
    calls = []
    monkeypatch.setattr(push_card, "deliver", lambda message, dry_run=False: (
        calls.append(message), _confirm(message))[1])
    ledger = Ledger()
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-08T06:30:00Z")
    first = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-08T07:30:00Z")
    second = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert first["identity"] == second["identity"] and len(calls) == 1
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-09T06:30:00Z")
    third = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert third["identity"]["source_window"]["start"] == first["identity"]["source_window"]["end"]


def test_direct_implicit_failed_run_resumes_after_midnight(private_repo, cfg, cases, monkeypatch):
    calls = []
    monkeypatch.setattr(push_card, "deliver", lambda message, dry_run=False: (
        calls.append(message), _confirm(message))[1])
    ledger = Ledger()
    original = ledger.add_watermark
    ledger.add_watermark = lambda value: (_ for _ in ()).throw(OSError("synthetic interruption"))
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-08T06:30:00Z")
    with pytest.raises(OSError, match="synthetic interruption"):
        run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    ledger.add_watermark = original
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-09T06:30:00Z")
    resumed = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert resumed["status"] == "complete" and len(calls) == 1
    assert resumed["identity"]["date"] == "2026-03-08"


def test_rejected_implicit_input_can_be_corrected(private_repo, cfg, cases, monkeypatch):
    monkeypatch.setattr(push_card, "deliver", _confirm)
    ledger = Ledger()
    invalid = dict(cases["candidate"], evidence=[])
    with pytest.raises(ValueError, match="candidate validation"):
        run.process([invalid], cfg, ledger, archive_dir=str(private_repo / "pool"))
    result = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert result["status"] == "complete"


def test_intervening_implicit_input_cannot_erase_an_earlier_receipt(private_repo, cfg, cases, monkeypatch):
    calls = []
    monkeypatch.setattr(push_card, "deliver", lambda message, dry_run=False: (
        calls.append(message), _confirm(message))[1])
    ledger = Ledger()
    second_input = dict(cases["candidate"], recommendation="add bounded retries")
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-08T06:30:00.100000Z")
    first = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-08T06:30:00.200000Z")
    second = run.process([second_input], cfg, ledger, archive_dir=str(private_repo / "pool"))
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-08T06:30:00.300000Z")
    replay = run.process([cases["candidate"]], cfg, ledger, archive_dir=str(private_repo / "pool"))
    assert replay["identity"] == first["identity"] and len(calls) == 2
    pointer = json.loads(Path(replay["implicit_identity_path"]).read_text(encoding="utf-8"))
    assert pointer["identity"] == second["identity"]


def test_summary_channel_absence_does_not_poison_delivery_state(private_repo, cfg, bot_module):
    bot = bot_module.DemandBot.__new__(bot_module.DemandBot)
    bot.cfg, bot.poolp = cfg, str(private_repo / "demands.jsonl")
    bot.post_display, bot.display_id, bot.summary_hour = True, 2, 0
    bot.log = lambda text: None
    bot._render_summary = lambda day: "CSV export needs retries."
    bot.get_channel = lambda channel_id: None
    with pytest.raises(RuntimeError, match="before delivery"):
        _complete_pure_coroutine(bot._maybe_daily_summary())
    assert not Path(bot._summary_marker()).exists()
    bot.get_channel = lambda channel_id: object()

    async def send(today, body=None, channel=None):
        return {"message_id": "synthetic-summary", "content_sha256": hashlib.sha256(body.encode()).hexdigest()}

    bot._post_daily_summary = send
    assert _complete_pure_coroutine(bot._maybe_daily_summary())["status"] == "confirmed"


def test_summary_accepts_slug_and_keeps_legacy_utc_hour(private_repo, cfg, bot_module, monkeypatch):
    bot = bot_module.DemandBot.__new__(bot_module.DemandBot)
    bot.cfg = {**cfg, "slug": cfg["product_id"]}
    bot.cfg.pop("product_id")
    bot.poolp = str(private_repo / "demands.jsonl")
    bot.post_display, bot.display_id, bot.summary_hour = True, 2, 3
    bot.summary_hour_uses_utc = True
    bot.log = lambda text: None
    bot._render_summary = lambda day: "CSV export needs retries."
    bot.get_channel = lambda channel_id: object()

    async def send(today, body=None, channel=None):
        return {"message_id": "synthetic-summary", "content_sha256": hashlib.sha256(body.encode()).hexdigest()}

    bot._post_daily_summary = send
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-09T04:30:00Z")  # 00:30 in configured zone
    assert _complete_pure_coroutine(bot._maybe_daily_summary())["status"] == "confirmed"


@pytest.mark.parametrize("checkpoint_fault", [None, "failure", "interrupt"])
def test_scheduled_completion_snapshot_has_final_bytes(private_repo, cfg, cases, monkeypatch, checkpoint_fault):
    import backup
    import scheduled
    ledger, calls, snapshots = Ledger(), [], []
    ledger.init = lambda: None
    monkeypatch.setattr(scheduled, "preflight", lambda *a: (
        cfg, private_repo, private_repo / "pool", private_repo / "logs"))
    monkeypatch.setattr(scheduled.pull_discord, "_load_wiring", lambda: ([], "synthetic-token"))
    monkeypatch.setattr(scheduled.pull_discord, "pull", lambda channels, token, source_window: {
        "collection": {"status": "complete", "source_window": source_window}})
    monkeypatch.setattr(scheduled, "propose", lambda *a: [cases["candidate"]])
    monkeypatch.setattr(scheduled.dedup, "LedgerClient", lambda **k: ledger)
    monkeypatch.setattr(push_card, "deliver", lambda message, dry_run=False: (
        calls.append(message), _confirm(message))[1])

    def save(paths):
        snapshots.append({str(path): Path(path).read_bytes() for path in paths})
        if checkpoint_fault == "interrupt" and len(snapshots) == 2:
            raise KeyboardInterrupt("synthetic abrupt caller interruption")
        if checkpoint_fault == "failure" and len(snapshots) == 2:
            raise RuntimeError("synthetic completion snapshot failure")
        return {"status": "confirmed"}

    monkeypatch.setattr(backup, "backup", save)
    monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-08T06:30:00Z")
    if checkpoint_fault == "interrupt":
        with pytest.raises(KeyboardInterrupt, match="synthetic abrupt"):
            scheduled.execute()
        monkeypatch.setenv("DEMAND_MINING_NOW", "2026-03-09T06:30:00Z")
        result = scheduled.execute()
        assert result["identity"]["date"] == "2026-03-08"
    else:
        result = scheduled.execute()
    if checkpoint_fault == "failure":
        assert result["status"] == "delivered_backup_pending"
        assert result["backup"]["delivery_snapshot"]["status"] == "confirmed"
        result = scheduled.execute()
    assert result["status"] == "complete" and len(calls) == 1
    assert snapshots[-1] == {path: Path(path).read_bytes() for path in snapshots[-1]}
    assert not list((private_repo / "pool/scheduled").glob("*/checkpoint-pending.json"))
