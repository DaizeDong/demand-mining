"""Generated synthetic cases for the four Demand14 review findings."""
import ast
import copy
import json
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


@pytest.fixture
def transport(tmp_path, monkeypatch):
    root = tmp_path / "companion"
    root.mkdir()
    state = {"url": REVIEW["transport"][0]["url"], "config": [], "calls": []}

    def git(directory, *args, **kwargs):
        state["calls"].append(args)
        if args == ("rev-parse", "--show-toplevel"):
            output = str(root)
        elif args[0] == "config":
            output = "".join(key + ("\n" + value if value is not None else "") + "\0"
                             for key, value in state["config"])
        elif args[:2] == ("remote", "get-url"):
            output = state.get("push", state["url"]) if "--push" in args else state["url"]
        else:
            raise AssertionError("unexpected synthetic Git command")
        return subprocess.CompletedProcess(["git", *args], 0, stdout=output, stderr="")

    monkeypatch.setattr(data_safety, "git", git)
    monkeypatch.setattr(data_safety, "_visibility", lambda repo: "PRIVATE")
    monkeypatch.setattr(data_safety, "_verify_ssh_transport", lambda: None, raising=False)
    return root, state


@pytest.mark.parametrize("case", REVIEW["transport"], ids=lambda case: case["name"])
def test_data_write_binds_effective_transport(transport, monkeypatch, case):
    root, state = transport
    state.update(copy.deepcopy(case))
    for key, value in case["env"].items():
        monkeypatch.setenv(key, value)
    destination = root / "pool/result.json"
    if case["allowed"]:
        data_safety.atomic_json(destination, {"synthetic": True})
        assert destination.is_file()
    else:
        with pytest.raises(data_safety.DestinationError):
            data_safety.atomic_json(destination, {"synthetic": True})
        assert not destination.parent.exists()


def test_push_rechecks_transport_after_admission(transport, monkeypatch):
    root, state = transport
    proof = data_safety.require_private(root / "record.json")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    with pytest.raises(data_safety.DestinationError):
        data_safety.require_private_push(root, proof["repository"])


@pytest.mark.parametrize("mode", ["discovered", "explicit-config", "explicit-salt"])
def test_salt_discovery_matches_preflight_across_fresh_states(tmp_path, monkeypatch, mode):
    directory = tmp_path / "config"
    (directory / "secrets").mkdir(parents=True)
    (directory / "secrets/pseudonym_hmac_salt").write_text(REVIEW["salt"], encoding="utf-8")
    monkeypatch.delenv("DEMAND_MINING_CONFIG", raising=False)
    monkeypatch.delenv("DEMAND_MINING_PSEUDONYM_SALT", raising=False)
    monkeypatch.setattr(lib, "CONFIG_FALLBACKS", [str(directory)])
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
        or isinstance(node, ast.FunctionDef) and node.name == "_demand_from_verdict")]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    scope = {"discord": types.SimpleNamespace(Client=object), "re": re, "_SIGNAL": re.compile("export"),
             "safe_text": redact.safe_text, "safe_data": redact.safe_data,
             "canonical_key": lib.canonical_key, "iso": lib.iso, "now_utc": lib.now_utc}
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


@pytest.mark.parametrize("transport_name", ["ssh", "scoped-https"])
def test_permitted_transports_keep_nonrouting_configuration(transport, transport_name):
    root, state = transport
    state.update(copy.deepcopy(REVIEW["permitted_transport"][transport_name]))
    proof = data_safety.require_private(root / "synthetic.json")
    assert proof["repository"] == "example/demand-mining-config"


@pytest.mark.parametrize("change", [False, True], ids=["healthy", "changed-before-push"])
def test_backup_uses_current_transport_proof_at_actual_launcher(tmp_path, monkeypatch, change):
    import backup
    root = tmp_path / "companion"
    root.mkdir()
    record = root / "record.json"
    record.write_text(json.dumps(REVIEW["backup_record"]), encoding="utf-8")
    state = {"config": "", "pushes": 0}
    monkeypatch.setattr(data_safety, "_visibility", lambda repository: "PRIVATE")

    def child(argv, **kwargs):
        args = argv[3:]
        if args == ["rev-parse", "--show-toplevel"]:
            output = str(root)
        elif args[0] == "config":
            output = state["config"]
        elif args[:2] == ["remote", "get-url"]:
            output = REVIEW["transport"][0]["url"]
        elif args[0] in {"read-tree", "add", "diff", "commit", "push"}:
            output = ""
            if args[0] == "add" and change:
                key, value = next(case for case in REVIEW["transport"] if case["name"] == "blocked-config-0")["config"][0]
                state["config"] = key + "\n" + value + "\0"
            if args[0] == "push":
                state["pushes"] += 1
                assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        else:
            raise AssertionError("unexpected synthetic Git command")
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    monkeypatch.setattr(data_safety.subprocess, "run", child)
    if change:
        with pytest.raises((data_safety.DestinationError, ValueError)):
            backup._backup_locked([record])
        assert state["pushes"] == 0
    else:
        assert backup._backup_locked([record])["status"] == "confirmed"
        assert state["pushes"] == 1


def test_git_push_without_an_admitted_proof_never_launches(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(data_safety.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(data_safety.DestinationError):
        data_safety.git(tmp_path, "push", "origin", "HEAD")
    assert calls == []


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
