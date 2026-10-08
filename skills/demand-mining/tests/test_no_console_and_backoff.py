"""Windowless child processes, PRIVATE proof reuse and bounded live retries.

Regression for a terminal-window storm: the daemon runs under pythonw.exe (no console), every
log line and pool write ran the PRIVATE companion proof (about twenty git.exe launches), none of
those launches asked for CREATE_NO_WINDOW, and one held observation was retried every poll
forever. All data below is synthetic.
"""
import ast
import contextlib
import importlib.util
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

import daemon_supervisor
import data_safety
import no_console
import observation_retry
from private_log import PrivateLog, PrivateLogError
from redact import PrivacyReviewRequired

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}


class StopControl(BaseException):
    pass


# --------------------------------------------------------------------------- layer 1

def test_no_window_kwargs_only_on_windows():
    assert no_console.no_window_kwargs("win32") == {"creationflags": no_console.CREATE_NO_WINDOW}
    assert no_console.no_window_kwargs("linux") == {}
    assert no_console.CREATE_NO_WINDOW == 0x08000000


def unprotected_spawns(source, filename="<synthetic>"):
    """Every subprocess launch must pass **no_window_kwargs() or an explicit creationflags."""
    found, missing = 0, []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                and node.func.attr in SPAWNERS):
            continue
        found += 1
        protected = any(
            keyword.arg == "creationflags"
            or keyword.arg is None and isinstance(keyword.value, ast.Call)
            and getattr(keyword.value.func, "id", None) == "no_window_kwargs"
            for keyword in node.keywords)
        if not protected:
            missing.append(f"{filename}:{node.lineno}")
    return found, missing


def test_every_skill_subprocess_launch_requests_no_window():
    total, missing = 0, []
    for path in sorted(SCRIPTS.glob("*.py")):
        found, bad = unprotected_spawns(path.read_text(encoding="utf-8"), path.name)
        total += found
        missing += bad
    assert missing == []
    # data_safety git, dedup ledger CLI, push_card relay, retention removal, supervisor child.
    assert total >= 5, "the scan found too few launches to mean anything"


def test_structural_scan_negative_control():
    bare = "import subprocess\nsubprocess.run(['git', 'status'], capture_output=True)\n"
    assert unprotected_spawns(bare) == (1, ["<synthetic>:2"])
    covered = ("import subprocess\nsubprocess.run(['git', 'status'], **no_window_kwargs())\n"
               "subprocess.Popen(['git'], creationflags=0)\n")
    assert unprotected_spawns(covered) == (2, [])


@pytest.mark.parametrize("platform,expected", [("win32", no_console.CREATE_NO_WINDOW), ("linux", None)])
def test_supervisor_launches_daemon_without_a_window(monkeypatch, platform, expected):
    launches = []

    class Child:
        stdout = types.SimpleNamespace(read=lambda size: b"", close=lambda: None)
        def wait(self):
            return 0
        def poll(self):
            return 0

    def popen(argv, **kwargs):
        launches.append(kwargs)
        return Child()

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(daemon_supervisor, "require_private", lambda path: {"path": str(path)})
    monkeypatch.setattr(daemon_supervisor, "subprocess", types.SimpleNamespace(
        Popen=popen, PIPE=-1, STDOUT=-2, TimeoutExpired=subprocess.TimeoutExpired))
    log = types.SimpleNamespace(path="synthetic.log", write=lambda chunk: len(chunk))
    assert daemon_supervisor._run_child(["pythonw.exe", "demand_bot.py"], {}, log) == 0
    assert launches[0].get("creationflags") == expected


# --------------------------------------------------------------------------- layer 2

class FakePopen:
    def __init__(self, args, bufsize=-1, executable=None, stdin=None, stdout=None, stderr=None,
                 preexec_fn=None, close_fds=True, shell=False, cwd=None, env=None,
                 universal_newlines=None, startupinfo=None, creationflags=0, **kwargs):
        self.creationflags = creationflags


def fresh_popen():
    return type("FreshPopen", (FakePopen,), {"__init__": FakePopen.__init__})


def test_installer_defaults_no_window_without_a_console():
    popen = fresh_popen()
    assert no_console.install_no_console_window_default(
        platform="win32", console_window=lambda: 0, popen=popen) is True
    assert popen(["git", "status"]).creationflags == no_console.CREATE_NO_WINDOW
    # Existing flags are kept and the default is added.
    assert popen(["git"], creationflags=0x200).creationflags == 0x200 | no_console.CREATE_NO_WINDOW
    # A positional creationflags (index 13 after args) is handled the same way.
    positional = popen(["git"], -1, None, None, None, None, None, True, False, None, None, None, None, 0)
    assert positional.creationflags == no_console.CREATE_NO_WINDOW
    # An explicit request for a console or a detached process is respected.
    for explicit in (no_console.CREATE_NEW_CONSOLE, no_console.DETACHED_PROCESS):
        assert popen(["cmd"], creationflags=explicit).creationflags == explicit


def test_installer_is_idempotent():
    popen = fresh_popen()
    for _ in range(3):
        assert no_console.install_no_console_window_default(
            platform="win32", console_window=lambda: 0, popen=popen) is True
    wrapper = popen.__init__
    assert getattr(wrapper, "__wrapped__") is FakePopen.__init__  # wrapped exactly once
    assert popen(["git"]).creationflags == no_console.CREATE_NO_WINDOW


@pytest.mark.parametrize("platform,window", [("win32", 4242), ("linux", 0)])
def test_installer_negative_control_with_console_or_off_windows(platform, window):
    popen = fresh_popen()
    original = popen.__init__
    assert no_console.install_no_console_window_default(
        platform=platform, console_window=lambda: window, popen=popen) is False
    assert popen.__init__ is original
    assert popen(["git"]).creationflags == 0


def test_console_detection_falls_back_to_the_interpreter_name():
    assert no_console.console_missing("win32", r"C:\Python\pythonw.exe", lambda: None) is True
    assert no_console.console_missing("win32", r"C:\Python\python.exe", lambda: None) is False
    assert no_console.console_missing("linux", "pythonw.exe", lambda: 0) is False


@pytest.mark.skipif(sys.platform != "win32", reason="CreateProcess flags exist only on Windows")
@pytest.mark.parametrize("installed", [True, False])
def test_real_popen_passes_create_no_window_to_createprocess(monkeypatch, installed):
    import _winapi
    seen = []
    original = _winapi.CreateProcess

    def record(*args):
        seen.append(args[5])
        return original(*args)

    monkeypatch.setattr(subprocess._winapi, "CreateProcess", record)
    probe = type("ProbePopen", (subprocess.Popen,), {})
    if installed:
        assert no_console.install_no_console_window_default(
            platform="win32", console_window=lambda: 0, popen=probe)
    child = probe([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child.wait(timeout=60)
    assert bool(seen[-1] & no_console.CREATE_NO_WINDOW) is installed


# --------------------------------------------------------------------------- proof reuse

@pytest.fixture
def companion(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    (profile / ".pii-guard").mkdir(parents=True)
    receipt = profile / ".pii-guard/visibility.json"
    receipt.write_text(json.dumps({"example-owner/private-vault": "PRIVATE"}), encoding="utf-8")
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(profile))
    root = tmp_path / "private"
    (root / ".git").mkdir(parents=True)
    (root / ".git/config").write_text("[core]\n", encoding="utf-8")
    state = {"proofs": 0, "visibility": "PRIVATE"}

    def transport(directory):
        state["proofs"] += 1
        if state["visibility"] != "PRIVATE":
            raise data_safety.DestinationError("synthetic companion is not PRIVATE")
        return {"root": str(directory), "repository": "example-owner/private-vault",
                "repositories": ("example-owner/private-vault",), "sha256": "synthetic"}

    clock = [1000.0]
    monkeypatch.setattr(data_safety, "_transport_proof", transport)
    monkeypatch.setattr(data_safety, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(data_safety, "PROOF_CACHE_TTL", 600.0)
    data_safety.clear_proof_cache()

    def revoke():
        state["visibility"] = "PUBLIC"
        receipt.write_text(json.dumps({"example-owner/private-vault": "PUBLIC", "_note": "revoked"}),
                           encoding="utf-8")

    return types.SimpleNamespace(root=root, state=state, clock=clock, revoke=revoke, receipt=receipt)


def test_successful_proof_is_reused_within_ttl(companion):
    for name in ("a.log", "b.log", "nested/c.json"):
        assert data_safety.require_private(companion.root / name)["visibility"] == "PRIVATE"
    assert companion.state["proofs"] == 1
    companion.clock[0] += 599
    data_safety.require_private(companion.root / "d.log")
    assert companion.state["proofs"] == 1
    companion.clock[0] += 2  # past the TTL
    data_safety.require_private(companion.root / "e.log")
    assert companion.state["proofs"] == 2


def test_failed_proof_is_never_cached(companion):
    companion.state["visibility"] = "PUBLIC"
    for _ in range(3):
        with pytest.raises(data_safety.DestinationError):
            data_safety.require_private(companion.root / "a.log")
    assert companion.state["proofs"] == 3
    companion.state["visibility"] = "PRIVATE"
    data_safety.require_private(companion.root / "a.log")
    assert companion.state["proofs"] == 4


@pytest.mark.parametrize("change", ["receipt", "repo_config", "environment", "proof_seam"])
def test_signature_change_forces_a_fresh_proof(companion, monkeypatch, change):
    data_safety.require_private(companion.root / "a.log")
    if change == "receipt":
        companion.receipt.write_text(json.dumps({"example-owner/private-vault": "PRIVATE",
                                                 "_refreshed": "later"}), encoding="utf-8")
    elif change == "repo_config":
        (companion.root / ".git/config").write_text(
            "[remote \"origin\"]\n\turl = https://github.com/example-owner/other.git\n", encoding="utf-8")
    elif change == "environment":
        monkeypatch.setenv("GIT_SSH_VARIANT", "ssh")
    else:
        monkeypatch.setattr(data_safety, "_companion_proof", lambda directory: None)
    data_safety.require_private(companion.root / "b.log")
    assert companion.state["proofs"] == 2


def test_revocation_is_caught_at_the_next_write_inside_the_ttl(companion, monkeypatch):
    def prove(directory, *args):
        proof = data_safety._transport_proof(directory)
        return types.SimpleNamespace(root=proof["root"], repositories=proof["repositories"],
                                     signature=proof["sha256"])
    def read(snapshot, *args):
        if args == ("rev-parse", "--verify", "HEAD"):
            return types.SimpleNamespace(returncode=0, stdout="synthetic-head")
        if args[:4] == ("check-ignore", "--no-index", "-q", "--"):
            return types.SimpleNamespace(returncode=1, stdout="")
        raise AssertionError("unapproved synthetic artifact Git command")
    boundary = types.SimpleNamespace(prove_private_companion=prove,
                                     read_private_companion_git=read,
                                     GitError=data_safety.DestinationError)
    monkeypatch.setattr(data_safety._storage_contract(), "load_boundary", lambda: boundary)
    path = companion.root / "pool/logs/daemon.log"
    with PrivateLog(path) as log:
        log.write("synthetic first line\n")
        companion.revoke()
        with pytest.raises(PrivateLogError):
            log.write("synthetic second line\n")
    assert path.read_text(encoding="utf-8") == "synthetic first line\n"
    with pytest.raises(data_safety.DestinationError):
        data_safety.atomic_json(companion.root / "pool/runs/synthetic/state.json", {"synthetic": True})
    assert not (companion.root / "pool/runs/synthetic/state.json").exists()


def test_negative_control_without_signature_change_the_cache_hides_an_in_memory_flip(companion):
    # Proves the reuse is real: a revocation that changes no proof input is only seen at expiry.
    data_safety.require_private(companion.root / "a.log")
    companion.state["visibility"] = "PUBLIC"
    data_safety.require_private(companion.root / "b.log")
    companion.clock[0] += 601
    with pytest.raises(data_safety.DestinationError):
        data_safety.require_private(companion.root / "c.log")


def test_backup_push_admission_always_proves_fresh(companion):
    data_safety.require_private(companion.root / "a.log")
    data_safety.require_private_push(companion.root, "example-owner/private-vault")
    data_safety.require_private_push(companion.root, "example-owner/private-vault")
    assert companion.state["proofs"] == 3


def test_ttl_zero_disables_reuse(companion, monkeypatch):
    monkeypatch.setattr(data_safety, "PROOF_CACHE_TTL", 0)
    for _ in range(3):
        data_safety.require_private(companion.root / "a.log")
    assert companion.state["proofs"] == 3


# --------------------------------------------------------------------------- backoff

def test_backoff_doubles_from_one_poll_to_a_one_hour_cap():
    backoff = observation_retry.RetryBackoff(90)
    delays, changes = [], []
    for _ in range(9):
        changes.append(backoff.record_failure("PrivacyReviewRequired", privacy_hold=True))
        delays.append(backoff.delay_seconds())
    assert delays == [90, 180, 360, 720, 1440, 2880, 3600, 3600, 3600]
    assert changes == [True] + [False] * 8
    assert backoff.record_failure("OSError") is True  # a different error is a state change
    assert backoff.exhausted()


def test_backoff_waits_the_scheduled_number_of_polls():
    backoff = observation_retry.RetryBackoff(90)
    backoff.record_failure("OSError")
    assert backoff.ready()  # the first retry is the next poll
    backoff.record_failure("OSError")
    assert [backoff.ready() for _ in range(2)] == [False, True]
    assert backoff.reset() == 2 and backoff.ready()


@pytest.fixture
def bot_module(monkeypatch):
    fake = types.ModuleType("discord")
    fake.Client = type("Client", (), {})
    fake.Thread = type("Thread", (), {})
    fake.Message = type("Message", (), {})
    monkeypatch.setitem(sys.modules, "discord", fake)
    monkeypatch.setitem(sys.modules, "llmcall", types.SimpleNamespace(call=None))
    path = SCRIPTS / "demand_bot.py"
    spec = importlib.util.spec_from_file_location("backoff_bot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERDICT = {"i": 0, "is_demand": True, "confidence": 0.9, "title": "CSV export broken",
           "track": "export", "kano": "must_be", "why": "synthetic"}


def live_bot(module, tmp_path, upsert, writes):
    bot = module.DemandBot.__new__(module.DemandBot)
    bot.cfg = {"product_id": "acme-widget", "timezone": "UTC"}
    message = types.SimpleNamespace(id=7, channel=types.SimpleNamespace(id=3), created_at=None)
    bot.buffer = [{"m": message, "text": "CSV export is broken again", "ah": "u_0123456789abcdef",
                   "channel": "feedback"}]
    bot.interval, bot.classify_sys, bot.product, bot.classify_rounds = 90, "", "Acme", 1
    bot.poolp, bot.rescore, bot.lo, bot.hi = str(tmp_path / "pool/demands.jsonl"), None, 0.4, 0.7
    bot.post_community = False
    bot.is_closed = lambda: False
    bot.logs = []
    bot.log = bot.logs.append
    bot.ticks = 0

    async def note(text):
        pass

    bot._note = note
    return bot


def run_loop(module, bot, monkeypatch, upsert, ticks):
    calls = []

    async def sleep(seconds):
        bot.ticks += 1
        if bot.ticks > ticks:
            raise StopControl()

    async def to_thread(function, *args):
        return function(*args)

    def recording_upsert(path, demand, rescore):
        calls.append(bot.ticks)
        return upsert(path, demand, rescore)

    monkeypatch.setattr(module, "asyncio", types.SimpleNamespace(sleep=sleep, to_thread=to_thread))
    monkeypatch.setattr(module, "classify_batch", lambda items, *args: [dict(VERDICT, i=row["i"]) for row in items])
    monkeypatch.setattr(module, "pool", types.SimpleNamespace(upsert=recording_upsert))
    with pytest.raises(StopControl):
        coroutine = bot._classify_loop()
        try:
            while True:
                coroutine.send(None)
        finally:
            coroutine.close()
    return calls


@pytest.fixture
def quarantine_writes(monkeypatch):
    writes = []
    monkeypatch.setattr(data_safety, "atomic_json", lambda path, value: writes.append((Path(path), value)))
    return writes


def test_privacy_hold_backs_off_then_quarantines_once(bot_module, tmp_path, monkeypatch, quarantine_writes):
    def held(path, demand, rescore):
        raise PrivacyReviewRequired("synthetic hold")

    bot = live_bot(bot_module, tmp_path, held, quarantine_writes)
    calls = run_loop(bot_module, bot, monkeypatch, held, ticks=200)
    # 90 s polls: attempts at 90 s, 180 s, 360 s ... capped at one hour, then quarantine.
    assert calls == [1, 2, 4, 8, 16, 32, 64, 104]
    assert len(calls) == observation_retry.PRIVACY_HOLD_LIMIT
    assert bot._pending_batch == []
    assert len(quarantine_writes) == 1
    path, record = quarantine_writes[0]
    assert path.parent == tmp_path / "pool/quarantine" and path.name.startswith("privacy-hold-")
    assert record["schema"] == observation_retry.QUARANTINE_SCHEMA
    assert record["stage"] == "persist" and record["hold_source"] == "pool"
    assert record["observation"]["redacted_text"] == "CSV export is broken again"
    assert re.fullmatch(r"u_[0-9a-f]{16}", record["observation"]["observation_id"])
    assert record["demand"]["product_id"] == "acme-widget"
    # Only state changes are logged: first pending and quarantined, not 200 retry lines.
    assert len(bot.logs) == 2
    assert bot.logs[0].startswith("classification pending: PrivacyReviewRequired")
    assert bot.logs[1].startswith("privacy hold: 1 observations quarantined after 8 attempts")


def test_refused_quarantine_keeps_the_observation_pending(bot_module, tmp_path, monkeypatch):
    def held(path, demand, rescore):
        raise PrivacyReviewRequired("synthetic hold")

    def refuse(path, value):
        raise data_safety.DestinationError("synthetic companion is not PRIVATE")

    monkeypatch.setattr(data_safety, "atomic_json", refuse)
    bot = live_bot(bot_module, tmp_path, held, [])
    run_loop(bot_module, bot, monkeypatch, held, ticks=400)
    assert len(bot._pending_batch) == 1  # never dropped, never written to a fallback
    assert bot._pending_batch[0]["message"]["text"] == "CSV export is broken again"
    assert sum("privacy quarantine refused" in line for line in bot.logs) == 1
    assert len(bot.logs) == 2


def test_transient_failure_backs_off_without_quarantine_and_recovers(
        bot_module, tmp_path, monkeypatch, quarantine_writes):
    attempts = []

    def flaky(path, demand, rescore):
        attempts.append(1)
        if len(attempts) < 4:
            raise OSError("synthetic pool failure")
        return "new", {"title": demand["title"], "reach": 1, "final_score": 1}

    bot = live_bot(bot_module, tmp_path, flaky, quarantine_writes)
    calls = run_loop(bot_module, bot, monkeypatch, flaky, ticks=20)
    assert calls == [1, 2, 4, 8]
    assert quarantine_writes == [] and bot._pending_batch == []
    assert bot.logs[0].startswith("classification pending: OSError")
    assert any(line == "classification recovered after 3 failed attempts" for line in bot.logs)


def test_old_behavior_negative_control_would_retry_every_poll(bot_module, tmp_path, monkeypatch,
                                                              quarantine_writes):
    # Replacing the backoff with an always-ready gate restores the incident behavior: a retry and
    # a log line on every poll. This shows the schedule, not the harness, limits the attempts.
    def held(path, demand, rescore):
        raise PrivacyReviewRequired("synthetic hold")

    bot = live_bot(bot_module, tmp_path, held, quarantine_writes)
    bot._classify_backoff = types.SimpleNamespace(
        ready=lambda: True, reset=lambda: 0, record_failure=lambda *a, **k: True,
        privacy_holds=0, failures=0, cap_polls=1, interval=90)
    calls = run_loop(bot_module, bot, monkeypatch, held, ticks=10)
    assert calls == list(range(1, 11))


def test_direct_privacy_hold_is_quarantined_after_bounded_attempts(
        bot_module, tmp_path, monkeypatch, quarantine_writes):
    bot = live_bot(bot_module, tmp_path, None, quarantine_writes)
    bot.buffer = []
    message = types.SimpleNamespace(id=9, channel=types.SimpleNamespace(id=4), created_at=None)
    tries = []

    async def held(entry):
        tries.append(bot.ticks)
        raise PrivacyReviewRequired("synthetic hold")

    bot._complete_direct = held
    first = bot._direct_reply(message, "how do I export to CSV", "u_fedcba9876543210")
    with contextlib.suppress(StopIteration):
        first.send(None)
    run_loop(bot_module, bot, monkeypatch, None, ticks=200)
    assert tries == [0, 1, 3, 7, 15, 31, 63, 103]
    assert bot._direct_pending == {}
    assert ("4", "9") in bot._direct_quarantined
    assert quarantine_writes[0][1]["stage"] == "direct"
    assert len(bot.logs) == 2


# --------------------------------------------------------------------------- summary loop

def summary_bot(module, tmp_path, monkeypatch, *, send_ok=True):
    bot = module.DemandBot.__new__(module.DemandBot)
    bot.cfg = {"product_id": "acme-widget", "timezone": "UTC"}
    bot.poolp = str(tmp_path / "demands.jsonl")
    bot.post_display, bot.display_id, bot.summary_hour = True, 2, 0
    bot.get_channel = lambda channel_id: object()
    bot.logs = []
    bot.log = bot.logs.append
    bot._render_summary = lambda today: "CSV export needs retries."
    proofs = []

    def require_private(path):
        proofs.append(str(path))
        return {"path": str(path)}

    def atomic_json(path, value):
        Path(path).write_text(json.dumps(value), encoding="utf-8")

    async def send(today, body=None, channel=None):
        if not send_ok:
            raise TimeoutError("synthetic ambiguous delivery")
        import hashlib
        return {"message_id": "synthetic-summary",
                "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}

    bot._post_daily_summary = send
    monkeypatch.setattr(module, "require_private", require_private)
    monkeypatch.setattr(module, "atomic_json", atomic_json)
    monkeypatch.setattr(module, "file_lock", lambda *a, **k: contextlib.nullcontext())
    return bot, proofs


def complete(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("summary coroutine unexpectedly yielded")


def test_summary_loop_proves_only_when_it_writes(bot_module, tmp_path, monkeypatch):
    bot, proofs = summary_bot(bot_module, tmp_path, monkeypatch)
    ticks = []

    async def sleep(seconds):
        ticks.append(seconds)
        if len(ticks) >= 12:
            raise StopControl()

    monkeypatch.setattr(bot_module, "asyncio", types.SimpleNamespace(sleep=sleep))
    bot.is_closed = lambda: False
    with pytest.raises(StopControl):
        complete(bot._summary_loop())
    assert len(ticks) == 12
    assert len(proofs) == 1  # the one write that posted today's summary

    restarted, again = summary_bot(bot_module, tmp_path, monkeypatch)
    for _ in range(5):
        assert complete(restarted._maybe_daily_summary())["status"] == "confirmed"
    assert again == []  # an already-confirmed day needs no write and no proof


def test_summary_negative_control_a_new_day_still_proves(bot_module, tmp_path, monkeypatch):
    bot, proofs = summary_bot(bot_module, tmp_path, monkeypatch)
    complete(bot._maybe_daily_summary())
    marker = Path(bot._summary_marker())
    state = json.loads(marker.read_text(encoding="utf-8"))
    state["identity"]["date"] = "2000-01-01"
    marker.write_text(json.dumps(state), encoding="utf-8")
    fresh, proofs = summary_bot(bot_module, tmp_path, monkeypatch)
    assert complete(fresh._maybe_daily_summary())["status"] == "confirmed"
    assert len(proofs) == 1


def test_pending_reconciliation_is_logged_once_and_not_reproved(bot_module, tmp_path, monkeypatch):
    bot, proofs = summary_bot(bot_module, tmp_path, monkeypatch, send_ok=False)
    for _ in range(6):
        assert complete(bot._maybe_daily_summary())["status"] == "pending_reconciliation"
    assert len(proofs) == 1
    assert bot.logs.count("daily summary pending reconciliation") == 1
