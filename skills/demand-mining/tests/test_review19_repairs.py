"""Full-module Demand19 regressions with generated inputs and synthetic effects."""
import builtins
import copy
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import sys
import types
import unittest

ROOT = Path(__file__).parents[3]
SCRIPTS = ROOT / "skills/demand-mining/scripts"


def source_bytes(path):
    for node in reversed((path, *path.parents)):
        info = os.lstat(node)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise AssertionError("unsafe source topology")
        if node != path and not stat.S_ISDIR(info.st_mode):
            raise AssertionError("unsafe source ancestor")
        if node == path and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise AssertionError("unsafe source leaf")
    return path.read_bytes()


generator = types.ModuleType("demand19_generated")
generator.__file__ = str(ROOT / "tools/make_fixtures.py")
exec(compile(source_bytes(Path(generator.__file__)), generator.__file__, "exec"), generator.__dict__)
CASES = generator.review19_cases()


class ControlledStop(BaseException):
    pass


class Harness:
    def __init__(self, behavior="normal"):
        self.behavior = behavior
        self.visibility = "PRIVATE"
        self.files, self.opens, self.writes, self.children, self.proofs = {}, [], [], [], []
        self.modules = {}
        self.root = CASES["private_root"]
        self.dirs = {self.root, self.root + "/logs", self.root + "/.git"}
        self.sys = types.SimpleNamespace(**vars(sys))
        self.sys.stdout, self.sys.stderr = io.StringIO(), io.StringIO()
        self.original_streams = self.sys.stdout, self.sys.stderr
        self.args = types.SimpleNamespace(
            config_dir=self.root, python="synthetic-python", mode="dry", interval=90,
            display_interval=300, log_dir=self.root + "/logs", min_backoff=1.0,
            max_backoff=4.0, dry_run=True, run_seconds=0,
            log_file=self.root + "/logs/direct.log")
        self.files[self.root + "/priority.json"] = json.dumps(CASES["config"]).encode()
        self.files[self.root + "/registry.json"] = json.dumps(CASES["registry"]).encode()
        self.files[self.root + "/synthetic-token.txt"] = CASES["synthetic_token"].encode()
        h = self

        class MemoryPath:
            def __init__(self, value):
                self.value = str(value).replace("\\", "/").rstrip("/") or "/"
            def __str__(self):
                return self.value
            def __fspath__(self):
                return self.value
            def __eq__(self, other):
                return self.value == str(other)
            def __hash__(self):
                return hash(self.value)
            def __truediv__(self, other):
                return MemoryPath(self.value.rstrip("/") + "/" + str(other))
            @property
            def parent(self):
                return MemoryPath(str(PurePosixPath(self.value).parent))
            @property
            def parents(self):
                return [MemoryPath(str(p)) for p in PurePosixPath(self.value).parents]
            @property
            def name(self):
                return PurePosixPath(self.value).name
            @property
            def parts(self):
                return PurePosixPath(self.value).parts
            def lstat(self):
                if not self.exists():
                    raise FileNotFoundError(self.value)
                return types.SimpleNamespace(st_mode=stat.S_IFDIR if self.is_dir() else stat.S_IFREG,
                                             st_nlink=1, st_file_attributes=0)
            def expanduser(self):
                return self
            def resolve(self):
                return self
            def absolute(self):
                return self
            def is_relative_to(self, other):
                return self.value == str(other) or self.value.startswith(str(other).rstrip("/") + "/")
            def is_dir(self):
                return self.value in h.dirs
            def is_file(self):
                return self.value in h.files
            def exists(self):
                return self.is_dir() or self.is_file()
            def mkdir(self, **kwargs):
                h.dirs.add(self.value)
            def read_text(self, encoding="utf-8"):
                return h.files[self.value].decode(encoding)
            def read_bytes(self):
                return h.files[self.value]

        class MemoryFile:
            def __init__(self, path, mode):
                self.name, self.mode, self.closed = str(path), mode, False
                h.opens.append((self.name, h.visibility))
                h.files.setdefault(self.name, b"")
            def write(self, value):
                if h.behavior == "write-error":
                    raise OSError("synthetic append failed")
                self.raw_write(value)
                return len(value)
            def raw_write(self, value):
                if self.closed:
                    raise ValueError("closed synthetic stream")
                data = value.encode("utf-8") if isinstance(value, str) else value
                h.files[self.name] = h.files.get(self.name, b"") + data
                h.writes.append((self.name, data, h.visibility))
            def flush(self):
                pass
            def close(self):
                self.closed = True
            def fileno(self):
                return 77
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()

        def memory_open(path, mode="r", **kwargs):
            if mode not in ("a", "ab"):
                raise AssertionError("unexpected product open mode")
            if h.behavior == "open-error":
                raise OSError("synthetic open failed")
            return MemoryFile(path, mode)

        class Parser:
            def __init__(self, **kwargs):
                pass
            def add_argument(self, *args, **kwargs):
                pass
            def parse_args(self):
                return h.args

        def sleep(seconds):
            if h.behavior == "revoke-before-restart" and len(h.children) == 1:
                h.visibility = "PUBLIC"
                return
            raise ControlledStop()

        class Pipe:
            def __init__(self):
                self.index = 0
                self.closed = False
            def read(self, size):
                assert 0 < size <= 65536
                if self.index == 1 and h.behavior == "revoke-during-child":
                    h.visibility = "PUBLIC"
                chunks = [x.encode() for x in CASES["child_chunks"]] + [bytes.fromhex(CASES["binary_tail_hex"])]
                if self.index >= len(chunks):
                    if h.behavior == "revoke-after-child":
                        h.visibility = "PUBLIC"
                    return b""
                chunk = chunks[self.index]
                self.index += 1
                return chunk
            def close(self):
                self.closed = True

        class Child:
            def __init__(self):
                self.stdout, self.returncode = Pipe(), None
                self.terminated, self.killed = False, False
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                self.returncode = 0 if self.returncode is None else self.returncode
                return self.returncode
            def terminate(self):
                self.terminated, self.returncode = True, -15
            def kill(self):
                self.killed, self.returncode = True, -9

        def popen(argv, **kwargs):
            assert kwargs["stdout"] == "SYNTHETIC_PIPE"
            assert kwargs["stderr"] == "SYNTHETIC_STDOUT"
            if h.behavior == "launch-error":
                raise OSError(CASES["startup_error"])
            child = Child()
            h.children.append(child)
            if len(h.children) > 1:
                raise ControlledStop()
            return child

        def call(argv, **kwargs):
            if h.behavior == "launch-error":
                raise OSError(CASES["startup_error"])
            child = Child()
            h.children.append(child)
            if len(h.children) > 1:
                raise ControlledStop()
            while True:
                chunk = child.stdout.read(65536)
                if not chunk:
                    break
                # Models inherited OS handles, which bypass Python stream.write.
                kwargs["stdout"].raw_write(chunk)
            return child.wait()

        class Client:
            def __init__(self, **kwargs):
                self.loop = types.SimpleNamespace()
            def run(self, token, **kwargs):
                if h.behavior == "direct-startup-error":
                    raise RuntimeError(CASES["startup_error"])
                self.log(CASES["log_messages"][0])
                if h.behavior == "direct-revoke":
                    h.visibility = "PUBLIC"
                self.log(CASES["log_messages"][1])

        self.Path = MemoryPath
        self.open = memory_open
        self.os = types.SimpleNamespace(
            environ={"DEMAND_MINING_CONFIG": self.root}, name="synthetic",
            path=types.SimpleNamespace(join=posixpath.join, dirname=posixpath.dirname,
                abspath=lambda value: value, isfile=lambda value: False, isabs=posixpath.isabs,
                lexists=lambda value: MemoryPath(value).exists()),
            makedirs=lambda path, **kwargs: self.dirs.add(str(path)))
        self.time = types.SimpleNamespace(time=lambda: 1, strftime=lambda fmt: "2026-01-15", sleep=sleep)
        self.subprocess = types.SimpleNamespace(Popen=popen, call=call, PIPE="SYNTHETIC_PIPE",
            STDOUT="SYNTHETIC_STDOUT", TimeoutExpired=TimeoutError)
        self.effects = {
            "os": self.os, "sys": self.sys, "time": self.time,
            "pathlib": types.SimpleNamespace(Path=MemoryPath),
            "argparse": types.SimpleNamespace(ArgumentParser=Parser),
            "subprocess": self.subprocess,
            "zoneinfo": types.SimpleNamespace(ZoneInfo=lambda zone: __import__("datetime").timezone.utc),
            "discord": types.SimpleNamespace(Client=Client,
                Intents=types.SimpleNamespace(default=lambda: types.SimpleNamespace())),
            "llmcall": types.SimpleNamespace(call=self.forbidden),
            "redact": types.SimpleNamespace(redact=lambda value: {"redacted": value},
                safe_data=copy.deepcopy, safe_text=lambda value: value, has_pii=lambda value: False,
                pseudonymize=lambda value: "synthetic-author", PrivacyReviewRequired=RuntimeError,
                ensure_stable_salt=lambda: None),
            "demand_pool": types.SimpleNamespace(pool_path=lambda config: self.root + "/pool.jsonl"),
            "dedup": types.SimpleNamespace(), "verify_gate": types.SimpleNamespace(gate_batch=self.forbidden),
            "push_card": types.SimpleNamespace(), "digest": types.SimpleNamespace(),
            "finalize": types.SimpleNamespace(logical_identity=lambda cfg: {"synthetic": True}),
            "no_console": types.SimpleNamespace(
                install_no_console_window_default=lambda: False, no_window_kwargs=lambda: {}),
        }

    @staticmethod
    def forbidden(*args, **kwargs):
        raise AssertionError("external effect forbidden")

    def importer(self, name, globals=None, locals=None, fromlist=(), level=0):
        if name in self.modules:
            return self.modules[name]
        if name in self.effects:
            return self.effects[name]
        allowed = {"__future__", "contextlib", "json", "hashlib", "importlib.util", "stat",
                   "urllib.parse", "re", "tempfile", "datetime", "asyncio", "math", "traceback"}
        if name in allowed:
            return builtins.__import__(name, globals, locals, fromlist, level)
        raise AssertionError("unadmitted product import: " + name)

    def load(self, name, path=None):
        path = path or SCRIPTS / (name + ".py")
        module = types.ModuleType(name)
        module.__file__ = str(path)
        module.__dict__["__builtins__"] = {
            **vars(builtins), "__import__": self.importer, "open": self.open}
        self.modules[name] = module
        exec(compile(source_bytes(path), str(path), "exec"), module.__dict__)
        if name == "data_safety":
            module.git = self.git
            module._companion_proof = self.companion
            # Every append re-proves under this harness; proof reuse has its own tests.
            module.PROOF_CACHE_TTL = 0
        return module

    def git(self, root, *args, **kwargs):
        if args == ("rev-parse", "--show-toplevel"):
            text = self.root
        elif args == ("config", "--null", "--list"):
            text = "remote.origin.url\n" + CASES["private_remote"] + "\0"
        elif args[:2] == ("remote", "get-url"):
            text = CASES["private_remote"]
        else:
            raise AssertionError("unadmitted synthetic Git query")
        return types.SimpleNamespace(stdout=text)

    def companion(self, root):
        repository = CASES["private_remote"].removeprefix("https://github.com/").removesuffix(".git")
        if self.visible(repository) != "PRIVATE":
            raise self.modules["data_safety"].DestinationError("synthetic repository visibility is not PRIVATE")
        return types.SimpleNamespace(root=self.Path(self.root), repositories=(repository,),
                                     signature="synthetic-current-transport")

    def visible(self, repository):
        self.proofs.append((repository, self.visibility))
        return self.visibility

    def scoring(self):
        lib = self.load("lib")
        score = self.load("score")
        run = self.load("run")
        return lib, score, run

    def logging(self):
        self.load("lib")
        self.load("data_safety")
        try:
            os.lstat(SCRIPTS / "private_log.py")
        except FileNotFoundError:
            pass
        else:
            self.load("private_log")
        self.load("score")
        return self.load("daemon_supervisor"), self.load("demand_bot")

    def invoke(self, function):
        try:
            function()
        except ControlledStop:
            return "controlled-stop"
        except (SystemExit, self.modules["data_safety"].DestinationError):
            return "stopped"
        return "returned"


class ScoringPathTests(unittest.TestCase):
    def test_generated_weights_and_effort_use_one_production_path(self):
        h = Harness()
        lib, score, run = h.scoring()
        for case in CASES["scores"]:
            with self.subTest(case=case["name"]):
                cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
                cfg["scoring"]["rice_weights"].update(case["weights"])
                proposal = copy.deepcopy(CASES["proposal"])
                proposal["effort_weeks"] = case["effort"]
                for key in ("independent_source_count", "has_internal_explicit", "internal_mentions"):
                    if case.get("evidence_mode") == "missing":
                        proposal.pop(key, None)
                    elif case.get("evidence_mode") == "null":
                        proposal[key] = None
                    elif case.get("evidence_mode") == "zero":
                        proposal[key] = 0
                before = copy.deepcopy((proposal, cfg))
                actual = score.score_demand(proposal, cfg)["final_score"]
                self.assertAlmostEqual(actual, case["expected"], places=4)
                self.assertEqual(run.build_card(proposal, cfg, "synthetic-run19")["final_score"], actual)
                self.assertEqual(score._final_map([proposal], None, cfg)[proposal["id"]], actual)
                self.assertEqual(score._final_map([proposal], cfg["scoring"]["rice_weights"], cfg)[proposal["id"]], actual)
                self.assertEqual((proposal, cfg), before)

    def test_original_score_regression_functions_on_full_modules(self):
        h = Harness()
        h.scoring()
        old = h.load("old_score_tests", ROOT / "skills/demand-mining/tests/test_score.py")
        selected = [value for name, value in vars(old).items() if name.startswith("test_") and callable(value)]
        self.assertEqual(len(selected), 17)
        for method in selected:
            with self.subTest(original=method.__name__):
                method()


class CurrentLogAdmissionTests(unittest.TestCase):
    def test_supervisor_preserves_all_private_child_bytes_and_uses_pipe(self):
        h = Harness()
        supervisor, _ = h.logging()
        self.assertEqual(h.invoke(supervisor.main), "controlled-stop")
        expected = b"".join(x.encode() for x in CASES["child_chunks"]) + bytes.fromhex(CASES["binary_tail_hex"])
        self.assertEqual(h.files[h.root + "/logs/daemon-2026-01-15.log"], expected)
        self.assertGreaterEqual(len(h.proofs), len(h.writes))
        self.assertTrue(all(state == "PRIVATE" for _, _, state in h.writes))
        self.assertIs(h.children[0].stdout.closed, True)

    def test_supervisor_revocation_during_output_stops_child_without_unsafe_append(self):
        h = Harness("revoke-during-child")
        supervisor, _ = h.logging()
        self.assertEqual(h.invoke(supervisor.main), "stopped")
        self.assertTrue(h.children[0].terminated or h.children[0].killed)
        self.assertEqual(len(h.children), 1)
        self.assertTrue(all(state == "PRIVATE" for _, _, state in h.writes))
        self.assertEqual(h.files[h.root + "/logs/daemon-2026-01-15.log"], CASES["child_chunks"][0].encode())

    def test_supervisor_revocation_after_exit_and_before_restart_is_current(self):
        for behavior in ("revoke-after-child", "revoke-before-restart"):
            with self.subTest(behavior=behavior):
                h = Harness(behavior)
                supervisor, _ = h.logging()
                self.assertEqual(h.invoke(supervisor.main), "stopped")
                self.assertEqual(len(h.children), 1)
                self.assertTrue(all(state == "PRIVATE" for _, _, state in h.writes))
                self.assertTrue(all(state == "PRIVATE" for _, state in h.opens))

    def test_supervisor_launch_exception_is_retained_in_private_log(self):
        h = Harness("launch-error")
        supervisor, _ = h.logging()
        self.assertEqual(h.invoke(supervisor.main), "controlled-stop")
        text = h.files[h.root + "/logs/supervisor.log"].decode()
        self.assertIn(CASES["startup_error"], text)
        self.assertIn("launch failed", text)
        self.assertIn("restarting", text)

    def test_direct_daemon_revalidates_each_write_and_restores_streams(self):
        h = Harness("direct-revoke")
        _, bot = h.logging()
        self.assertEqual(h.invoke(bot.main), "stopped")
        text = h.files[h.args.log_file].decode()
        self.assertIn(CASES["log_messages"][0], text)
        self.assertNotIn(CASES["log_messages"][1], text)
        self.assertTrue(all(state == "PRIVATE" for _, _, state in h.writes))
        self.assertEqual((h.sys.stdout, h.sys.stderr), h.original_streams)

    def test_direct_private_success_and_startup_failure_keep_log_records(self):
        for behavior in ("normal", "direct-startup-error"):
            with self.subTest(behavior=behavior):
                h = Harness(behavior)
                _, bot = h.logging()
                if behavior == "normal":
                    self.assertEqual(bot.main(), 0)
                    text = h.files[h.args.log_file].decode()
                    self.assertTrue(all(message in text for message in CASES["log_messages"]))
                else:
                    with self.assertRaisesRegex(RuntimeError, CASES["startup_error"]):
                        bot.main()
                    text = h.files[h.args.log_file].decode()
                    self.assertIn(CASES["startup_error"], text)
                    self.assertIn("Traceback", text)
                self.assertEqual((h.sys.stdout, h.sys.stderr), h.original_streams)

    def test_private_log_io_failure_stops_without_silent_success(self):
        for behavior in ("open-error", "write-error"):
            with self.subTest(behavior=behavior):
                h = Harness(behavior)
                _, bot = h.logging()
                self.assertEqual(h.invoke(bot.main), "stopped")
                self.assertEqual((h.sys.stdout, h.sys.stderr), h.original_streams)
                self.assertFalse(h.writes)


if __name__ == "__main__":
    unittest.main()
