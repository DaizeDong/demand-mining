"""Generated synthetic cases for the four Demand14 review findings."""
import ast
import copy
import json
import importlib.util
import datetime
import os
from pathlib import Path
import re
import subprocess
import types

import pytest
import data_safety
import dedup
import finalize
import lib
import redact


CASES = json.loads((Path(__file__).parent / "fixtures/repair_cases.json").read_text(encoding="utf-8"))
REVIEW = CASES["review15"]


@pytest.mark.parametrize("mode", ["discovered", "explicit-config", "explicit-salt"])
def test_salt_discovery_matches_preflight_across_fresh_states(tmp_path, monkeypatch, mode):
    import config_paths
    profile = tmp_path / "profile"
    profile.mkdir()
    # The proven sibling of the (patched) source root, the only place discovery looks.
    directory = tmp_path / "demand-mining-config"
    (directory / "secrets").mkdir(parents=True)
    (directory / ".companion").write_text("demand-mining\n", encoding="utf-8")
    (directory / "secrets/pseudonym_hmac_salt").write_text(REVIEW["salt"], encoding="utf-8")
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("USERPROFILE", str(profile))
    for name in ("DEMAND_MINING_CONFIG", "DEMAND_MINING_CONFIG_DIR",
                 "DEMAND_MINING_DATA_DIR", "DEMAND_MINING_PSEUDONYM_SALT"):
        monkeypatch.delenv(name, raising=False)
    config_paths.resolver()
    monkeypatch.setattr(config_paths, "ROOT", tmp_path / "source")
    if mode == "explicit-config":
        monkeypatch.setenv("DEMAND_MINING_CONFIG", str(directory))
    elif mode == "explicit-salt":
        monkeypatch.setenv("DEMAND_MINING_PSEUDONYM_SALT", REVIEW["salt"])
    outputs = []
    for byte in (b"a", b"b"):
        monkeypatch.setattr(redact, "_EPHEMERAL_SALT", None)
        monkeypatch.setattr(redact.os, "urandom", lambda size, value=byte: value * size)
        redact.ensure_stable_salt()
        outputs.append([redact.pseudonymize(user) for user in REVIEW["users"]])
    assert outputs[0] == outputs[1]
    assert len(set(outputs[0])) == len(REVIEW["users"])


@pytest.mark.parametrize("value", ["", " ", "\n\t"])
def test_real_salt_preflight_refuses_blank_configured_bytes(tmp_path, monkeypatch, value):
    directory = tmp_path / "config"
    (directory / "secrets").mkdir(parents=True)
    (directory / "secrets/pseudonym_hmac_salt").write_text(value, encoding="utf-8")
    monkeypatch.setenv("DEMAND_MINING_CONFIG", str(directory))
    monkeypatch.delenv("DEMAND_MINING_PSEUDONYM_SALT", raising=False)
    monkeypatch.setattr(redact, "_EPHEMERAL_SALT", None)
    with pytest.raises(ValueError):
        redact.ensure_stable_salt()


def _ledger(store, product):
    client = dedup.LedgerClient(cmd=["synthetic-reminder"])
    client.run_identity = {"product_id": product, "date": "2026-01-17", "timezone": "UTC",
                           "source_window": {"start": "2026-01-16T00:00:00Z", "end": "2026-01-17T00:00:00Z"}}

    def call(verb, args):
        if verb == "list":
            return {"ok": True, "items": list(copy.deepcopy(store).values())}
        assert verb == "add"
        key = args[args.index("--idempotency-key") + 1]
        row = {"idempotency_key": key, "source": dedup.SOURCE,
               "ext": json.loads(args[args.index("--ext") + 1])}
        store[key] = row
        return {"ok": True, "item": row}
    client._run = call
    return client


def test_shared_ledger_keeps_products_and_watermarks_separate():
    store = {}
    clients = [_ledger(store, product) for product in REVIEW["products"]]
    cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
    for index, client in enumerate(clients):
        identity = client.run_identity
        current = {**cfg, "product_id": identity["product_id"], "timezone": "UTC"}
        plan = finalize._prepare([CASES["candidate"]], current, client, identity, "synthetic-run")
        assert plan["result"]["new"] == 1
        for mutation in plan["mutations"]:
            ext = dict(mutation["ext"], **{dedup.EXT + "push_count": 1})
            client.upsert(mutation["card"], ext)
            client.upsert(mutation["card"], ext)
        client.add_watermark("2026-01-" + str(17 + index) + "T00:00:00Z")
    assert len(store) == 4
    for index, client in enumerate(clients):
        assert client.get_watermark() == "2026-01-" + str(17 + index) + "T00:00:00Z"
        identity = client.run_identity
        plan = finalize._prepare([CASES["candidate"]], {**cfg, "product_id": identity["product_id"], "timezone": "UTC"},
                                 client, identity, "synthetic-replay")
        assert plan["result"]["suppressed"] == 1
        assert all(row["ext"][dedup.EXT + "identity"]["product_id"] == identity["product_id"]
                   for row in client.list_active())


def test_unknown_legacy_rows_are_not_assigned_to_a_product():
    cfg = {**copy.deepcopy(lib.DEFAULT_CONFIG), "product_id": REVIEW["products"][0], "timezone": "UTC"}
    client = _ledger({}, cfg["product_id"])
    first = finalize._prepare([CASES["candidate"]], cfg, client, client.run_identity, "synthetic-first")
    mutation = first["mutations"][0]
    legacy = {"idempotency_key": dedup.KEY_PREFIX + mutation["card"]["canonical_key"],
              "ext": {key: value for key, value in mutation["ext"].items() if key != dedup.EXT + "identity"}}
    legacy["ext"][dedup.EXT + "push_count"] = 1
    client._run = lambda verb, args: {"ok": True, "items": [legacy]}
    plan = finalize._prepare([CASES["candidate"]], cfg, client, client.run_identity, "synthetic-second")
    assert plan["result"]["new"] == 1


def _bot_namespace():
    # Execute the shipped class and demand conversion definitions without loading a gateway,
    # provider SDK, event loop, thread or external agent.
    path = Path(__file__).parents[1] / "scripts/demand_bot.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if (
        isinstance(node, ast.ClassDef) and node.name == "DemandBot"
        or isinstance(node, ast.FunctionDef) and node.name in {
            "_demand_from_verdict", "_message_observation"})]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    scope = {"discord": types.SimpleNamespace(Client=object), "re": re, "_SIGNAL": re.compile("export"),
             "safe_text": redact.safe_text, "safe_data": redact.safe_data,
             "canonical_key": lib.canonical_key, "iso": lib.iso, "now_utc": lib.now_utc,
             "merge_observations": lib.merge_observations,
             "observation_identity": lib.observation_identity, "pseudonymize": redact.pseudonymize}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[])),
                 str(path), "exec"), scope)
    return scope


def _run_coroutine(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("synthetic await unexpectedly yielded")


@pytest.mark.parametrize("fault", ["none", "classifier", "second-upsert"])
def test_worker_retains_failed_batch_and_does_not_replay_completed_items(fault):
    scope = _bot_namespace()
    bot = object.__new__(scope["DemandBot"])
    bot.cfg = {"product_id": REVIEW["products"][0], "timezone": "UTC"}
    bot.buffer = [{**row, "m": row["id"]} for row in REVIEW["worker"]]
    bot.interval, bot.classify_sys, bot.product, bot.classify_rounds = 1, "", "synthetic", 1
    bot.poolp, bot.rescore, bot.lo = "synthetic-pool", None, 0.4
    bot.is_closed = lambda: False
    logs, writes, acknowledgments, classification_calls = [], [], [], []
    bot.log = logs.append
    attempts = {"sleep": 0, "upsert": 0}

    class StopControl(BaseException):
        pass

    async def sleep(seconds):
        attempts["sleep"] += 1
        if attempts["sleep"] > 3:
            raise StopControl()

    async def to_thread(function, *args):
        return function(*args)

    def classify(items, *args):
        classification_calls.append(copy.deepcopy(items))
        if fault == "classifier" and len(classification_calls) == 1:
            raise ValueError("synthetic classifier failure")
        return [{**CASES["verdict"], "i": row["i"], "confidence": 0.9,
                 "title": "export option " + str(row["i"])} for row in items]

    def upsert(path, demand, rescore):
        assert demand["product_id"] == bot.cfg["product_id"]
        attempts["upsert"] += 1
        if fault == "second-upsert" and attempts["upsert"] == 2:
            raise OSError("synthetic pool failure")
        writes.append(demand["evidence"][0]["redacted_snippet"])
        return "merged", {"title": demand["title"], "reach": 1, "final_score": 1}

    async def acknowledge(message, confidence):
        acknowledgments.append(message)

    async def note(message):
        pass

    scope.update(asyncio=types.SimpleNamespace(sleep=sleep, to_thread=to_thread),
                 classify_batch=classify, pool=types.SimpleNamespace(upsert=upsert))
    bot._ack, bot._note = acknowledge, note
    try:
        _run_coroutine(bot._classify_loop())
    except StopControl:
        pass
    assert sorted(writes) == sorted(row["text"] for row in REVIEW["worker"])
    assert sorted(acknowledgments) == sorted(row["id"] for row in REVIEW["worker"])
    assert len(writes) == len(set(writes)) == 3
    if fault != "none":
        assert any("pending" in message or "retry" in message for message in logs)


def test_repeated_ready_events_own_one_classification_task():
    scope = _bot_namespace()
    bot = object.__new__(scope["DemandBot"])
    bot.user, bot.chans, bot.mode, bot.display_id = "synthetic", {}, "dry", None
    bot.post_direct = bot.post_community = bot.post_display = False
    bot.log = lambda message: None
    tasks = []

    class Task:
        def __init__(self, coroutine):
            self.coroutine = coroutine
        def done(self):
            return False
        def add_done_callback(self, callback):
            self.callback = callback

    def create_task(coroutine):
        task = Task(coroutine)
        tasks.append(task)
        return task

    bot.loop = types.SimpleNamespace(create_task=create_task)
    try:
        _run_coroutine(bot.on_ready())
        _run_coroutine(bot.on_ready())
        assert len(tasks) == 1
        assert getattr(bot, "_classify_task", None) is tasks[0]
    finally:
        for task in tasks:
            task.coroutine.close()


def test_backup_commits_and_pushes_declared_files_without_a_destination_query(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("_review15_backup", Path(data_safety.__file__).with_name("backup.py"))
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    root = tmp_path / "companion"
    root.mkdir()
    (root / ".git").mkdir()
    record = root / "pool/runs/synthetic/state.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(REVIEW["backup_record"]), encoding="utf-8")
    calls = []

    def child(argv, **kwargs):
        args = argv[3:]
        if args[0] not in {"read-tree", "add", "diff", "commit", "push"}:
            raise AssertionError("unexpected synthetic Git command")
        calls.append(args[0])
        if args[0] == "push":
            assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(data_safety.subprocess, "run", child)
    result = backup._backup_locked([record])
    assert result == {"status": "confirmed", "files": ["pool/runs/synthetic/state.json"], "committed": False}
    assert calls == ["read-tree", "add", "diff", "push"]
    assert [p.name for p in root.iterdir() if p.name.startswith(".demand-backup-index-")] == []


def test_unexpected_worker_exit_restarts_with_the_same_pending_batch():
    scope = _bot_namespace()
    bot = object.__new__(scope["DemandBot"])
    pending = [{"message": REVIEW["worker"][0]}]
    bot._pending_batch = pending
    bot.is_closed = lambda: False
    logs, tasks = [], []
    bot.log = logs.append
    old = types.SimpleNamespace(done=lambda: True, cancelled=lambda: False,
                                exception=lambda: RuntimeError("synthetic worker termination"))
    bot._classify_task = old

    class NewTask:
        def __init__(self, coroutine):
            self.coroutine = coroutine
        def add_done_callback(self, callback):
            self.callback = callback
        def done(self):
            return False

    def create_task(coroutine):
        task = NewTask(coroutine)
        tasks.append(task)
        return task

    bot.loop = types.SimpleNamespace(create_task=create_task)
    try:
        bot._classify_done(old)
        assert len(tasks) == 1 and bot._classify_task is tasks[0]
        assert bot._pending_batch is pending
        assert bot.classification_status == "retrying"
        assert logs
    finally:
        for task in tasks:
            task.coroutine.close()
