"""Inert regressions for context recovery, batch conservation and capability checks."""
import argparse
import ast
import builtins
import contextlib
import copy
from datetime import datetime, time, timedelta, timezone
import hashlib
import html
import inspect
import io
import json
from pathlib import Path
import re
import types
import unittest

ROOT = Path(__file__).parents[3]
SCRIPTS = ROOT / "skills/demand-mining/scripts"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/repair_cases.json").read_text(encoding="utf-8"))
CASES = FIXTURE["review17"]


def finish(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("inert coroutine unexpectedly yielded")


def definitions(name, scope, *, constants=True):
    path = SCRIPTS / name
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    body = [node for node in tree.body if isinstance(node, kinds)
            or constants and isinstance(node, (ast.Assign, ast.AnnAssign))]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *body], type_ignores=[])),
                 str(path), "exec"), scope)
    return types.SimpleNamespace(**scope)


def product():
    modules, files, sends = {}, {}, []
    cfg = {}
    fixed = datetime.fromisoformat(CASES["source_window"]["end"].replace("Z", "+00:00"))

    def importer(name, globals=None, locals=None, fromlist=(), level=0):
        if name in modules:
            value = modules[name]
            if isinstance(value, Exception):
                raise value
            return value
        if name == "inspect":
            return inspect
        if name == "__future__":
            return builtins.__import__(name, globals, locals, fromlist, level)
        raise AssertionError("unexpected product import: " + name)

    class MemoryPath:
        def __init__(self, value):
            self.value = str(value).replace("\\", "/").rstrip("/")
        def __str__(self):
            return self.value
        def __truediv__(self, name):
            return MemoryPath(self.value + "/" + str(name))
        @property
        def parent(self):
            return MemoryPath(self.value.rsplit("/", 1)[0])
        def expanduser(self):
            return self
        def exists(self):
            return self.value in files
        def is_dir(self):
            return True
        def is_file(self):
            return True
        def read_bytes(self):
            if not self.exists():
                raise FileNotFoundError(self.value)
            return files[self.value]
        def read_text(self, encoding="utf-8"):
            return self.read_bytes().decode(encoding)

    def atomic(path, value):
        files[str(path)] = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
        return MemoryPath(path)

    common = {
        "__builtins__": {**vars(builtins), "__import__": importer},
        "json": json, "re": re, "hashlib": hashlib, "html": html,
        "datetime": datetime, "date": datetime.date, "time": time,
        "timedelta": timedelta, "timezone": timezone,
        "Path": MemoryPath, "ZoneInfo": lambda zone: timezone.utc,
        "ZoneInfoNotFoundError": ValueError,
        "os": types.SimpleNamespace(environ={}, access=lambda *args: True, R_OK=4, W_OK=2),
        "safe_data": copy.deepcopy, "safe_text": lambda value: value,
        "has_pii": lambda value: False,
        "redact": lambda value: {"redacted": value},
        "pseudonymize": lambda value: "u_" + hashlib.sha256(value.encode()).hexdigest()[:16],
        "require_private": lambda path: {"path": str(path)},
        "file_lock": lambda *args, **kwargs: contextlib.nullcontext(),
        "data_root": lambda: MemoryPath("synthetic/pool"),
        "atomic_json": atomic, "find_config_dir": lambda: MemoryPath("synthetic/config"),
        "ensure_stable_salt": lambda: None, "privacy_coverage": lambda: {"synthetic": True},
        "shutil": types.SimpleNamespace(which=lambda value: value),
        "argparse": argparse,
    }
    lib = definitions("lib.py", dict(common))
    cfg.update(copy.deepcopy(lib.DEFAULT_CONFIG))
    cfg.update(product_id=CASES["product_id"], timezone="UTC", source_window=CASES["source_window"])
    cfg["scoring"].update(min_score_to_push=40, min_score_to_archive=20)
    common.update({key: value for key, value in vars(lib).items() if not key.startswith("__")})
    common.update(compute_intensity=lib.intensity, load_config=lambda: copy.deepcopy(cfg), now_utc=lambda: fixed,
                  date=type(fixed.date()), find_config_dir=lambda: MemoryPath("synthetic/config"))
    score = definitions("score.py", {**common, "rice_calc": lib.rice,
                                    "opp_calc": lib.opportunity, "wsjf_calc": lib.wsjf})
    common.update(domain_factors=score.domain_factors, domain_factor_text=score.domain_factor_text)
    dd = definitions("dedup.py", dict(common))
    run_scope = {**common, "dd": dd, "score_demand": score.score_demand,
                 "compute_intensity": lib.intensity}
    run = definitions("run.py", run_scope)
    modules["run"] = run
    modules["redact"] = types.SimpleNamespace(safe_data=copy.deepcopy)
    gate = definitions("verify_gate.py", dict(common))
    dg = definitions("digest.py", dict(common))
    dg.register_digest_item = lambda *args, **kwargs: None
    def write_digest(markdown, directory, day):
        path = MemoryPath(directory) / (day + ".md")
        files[str(path)] = markdown.encode("utf-8")
        return path
    dg.write_digest_file = write_digest
    current = {}
    def delivery_context(identity):
        current["identity"] = identity
        return contextlib.nullcontext()
    def deliver(text, dry_run=False):
        sends.append(text)
        return {"status": "confirmed", "message_id": CASES["receipt"],
                "identity": current["identity"],
                "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    pc = types.SimpleNamespace(delivery_context=delivery_context, deliver=deliver,
                               _relay_cmd=lambda: ["synthetic-relay"])
    final_scope = {**common, "dd": dd, "dg": dg, "pc": pc, "gate_batch": gate.gate_batch}
    final = definitions("finalize.py", final_scope)
    scheduled = definitions("scheduled.py", {**common, "logical_identity": final.logical_identity,
                            "push_card": pc, "inspect": inspect})
    return types.SimpleNamespace(cfg=cfg, modules=modules, lib=lib, run=run, dd=dd,
                                 final=final, scheduled=scheduled, files=files, sends=sends,
                                 common=common)


class ContextRecoveryTests(unittest.TestCase):
    def make_bot(self, failed_source=None, send_failure=False):
        p = product()
        events, attempts = [], {}
        class Message:
            def __init__(self, mid, text):
                self.id, self.content = mid, text
                self.author = types.SimpleNamespace(id=42, bot=False)
        class Thread:
            id, name, starter_message = 10, "Synthetic discussion", None
        def step(name):
            attempts[name] = attempts.get(name, 0) + 1
            events.append(name)
            if name == failed_source and attempts[name] == 1:
                raise RuntimeError("synthetic context source unavailable")
        async def fetch(mid):
            step("opener" if mid == 10 else "reference")
            return Message(mid, CASES["context_text"])
        async def history(**kwargs):
            step("history")
            yield Message(8, CASES["context_text"])
        channel = Thread()
        channel.fetch_message, channel.history = fetch, history
        if failed_source != "opener":
            channel.starter_message = Message(10, CASES["context_text"])
        async def send(text, **kwargs):
            events.append("send")
            if send_failure:
                raise RuntimeError("synthetic ambiguous send")
        message = Message(11, CASES["direct_text"])
        message.channel, message.reply = channel, send
        message.reference = types.SimpleNamespace(message_id=9, resolved=None)
        async def to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)
        def classify(*args):
            events.append("classify")
            return [copy.deepcopy(FIXTURE["review16"]["verdict"])]
        def persist(path, demand, rescore):
            events.append("persist")
            return "new", demand
        def generate(*args):
            events.append("generate")
            return FIXTURE["review16"]["reply"]
        scope = {**p.common, "discord": types.SimpleNamespace(Client=object, Thread=Thread, Message=Message),
                 "PrivacyReviewRequired": type("PrivacyReviewRequired", (Exception,), {}),
                 "asyncio": types.SimpleNamespace(to_thread=to_thread),
                 "classify_batch": classify, "gen_reply": generate,
                 "pool": types.SimpleNamespace(upsert=persist)}
        module = definitions("demand_bot.py", scope)
        scope.update(classify_batch=classify, gen_reply=generate)
        bot = object.__new__(module.DemandBot)
        bot.cfg, bot.user = p.cfg, None
        bot.product, bot.classify_sys, bot.reply_sys = "AcmeCorp", "", ""
        bot.classify_rounds, bot.rescore, bot.poolp = 1, None, "synthetic/pool"
        bot.post_direct, bot.mode = True, "shadow"
        bot.log = lambda value: None
        async def note(value):
            events.append("note")
        bot._note = note
        return bot, message, events, attempts

    def direct(self, bot, message):
        return finish(bot._direct_reply(message, message.content, "u_1111111111111111"))

    def test_actual_context_sources_keep_retry_ownership(self):
        for source in CASES["context_sources"]:
            with self.subTest(source=source):
                bot, message, events, attempts = self.make_bot(source)
                self.assertEqual(self.direct(bot, message)["status"], "pending")
                self.assertTrue(bot._direct_pending)
                entry = next(iter(bot._direct_pending.values()))
                self.assertNotIn("context", entry)
                self.assertNotIn("verdict", entry)
                self.assertNotIn("classify", events)
                self.assertNotIn("persist", events)
                self.assertNotIn("send", events)
                finish(bot._retry_direct_pending())
                self.assertFalse(bot._direct_pending)
                self.assertEqual(attempts[source], 2)
                self.assertEqual(events.count("persist"), 1)
                self.assertEqual(events.count("send"), 1)
                self.assertLess(events.index("persist"), events.index("send"))
                self.direct(bot, message)
                self.assertEqual(events.count("persist"), 1)
                self.assertEqual(events.count("send"), 1)

    def test_recovered_context_does_not_repeat_ambiguous_send(self):
        bot, message, events, attempts = self.make_bot("history", send_failure=True)
        self.assertEqual(self.direct(bot, message)["status"], "pending")
        finish(bot._retry_direct_pending())
        self.direct(bot, message)
        self.assertEqual(events.count("persist"), 1)
        self.assertEqual(events.count("send"), 1)
        self.assertFalse(bot._direct_pending)

    def test_default_context_display_retains_available_parts(self):
        bot, message, events, attempts = self.make_bot("opener")
        context = finish(bot._gather_context(message))
        self.assertIn("[thread title]", context)
        self.assertIn("[recent]", context)


class BatchConservationTests(unittest.TestCase):
    def setUp(self):
        self.p = product()
        self.identity = {"product_id": CASES["product_id"], "timezone": "UTC",
                         "date": CASES["date"], "source_window": CASES["source_window"]}
        class Ledger:
            def __init__(self):
                self.rows, self.writes, self.watermarks = {}, [], []
            def list_active(self):
                return list(copy.deepcopy(self.rows).values())
            def upsert(self, card, ext, priority=1):
                key = card["canonical_key"]
                self.rows[key] = {"ext": copy.deepcopy(ext), "idempotency_key": key}
                self.writes.append(copy.deepcopy(ext))
            def add_watermark(self, value):
                self.watermarks.append(value)
        self.ledger = Ledger()

    def prepare(self, candidates):
        return self.p.final._prepare(candidates, self.p.cfg, self.ledger,
                                     self.identity, "synthetic-review17")

    def test_batch_merges_authors_and_evidence_before_score_and_headlines(self):
        candidates = copy.deepcopy(CASES["candidates"])
        original = copy.deepcopy(candidates)
        plan = self.prepare(candidates)
        self.assertEqual(len(plan["cards"]), 1)
        self.assertEqual(len(plan["mutations"]), 1)
        card = plan["cards"][0]
        self.assertEqual({a["author_hash"] for a in card["authors"]},
                         {a["author_hash"] for c in candidates for a in c["authors"]})
        self.assertEqual(card["evidence"], [e for c in candidates for e in c["evidence"]])
        self.assertEqual(card["distinct_author_count"], 2)
        self.assertEqual(card["rice"]["reach"], 2)
        self.assertEqual(card["rice"]["confidence"], 1)
        self.assertGreater(card["final_score"], self.p.run.build_card(
            candidates[0], self.p.cfg, "synthetic-review17")["final_score"])
        self.assertEqual(plan["headlines"].count(card["title"]), 1)
        self.assertEqual(plan["result"]["proposed_push"], [card["title"]])
        self.assertEqual(candidates, original)

    def test_identical_duplicate_does_not_inflate_contributions(self):
        candidate = copy.deepcopy(CASES["candidates"][0])
        once = self.prepare([candidate])["cards"][0]
        repeated = self.prepare([candidate, copy.deepcopy(candidate)])["cards"][0]
        for key in ("authors", "evidence", "distinct_author_count", "intensity",
                    "rice", "final_score", "new_mentions"):
            self.assertEqual(repeated[key], once[key], key)

    def test_distinct_canonical_subjects_stay_separate(self):
        candidates = copy.deepcopy(CASES["candidates"])
        candidates[1]["canonical_key"] = "theme|preview::ui-ux"
        self.assertEqual(len(self.prepare(candidates)["cards"]), 2)

    def test_full_finalizer_replay_preserves_union_and_single_send(self):
        candidates = copy.deepcopy(CASES["candidates"])
        def invoke():
            return self.p.final.finalize(
                candidates, self.p.cfg, self.ledger, False, "synthetic-review17",
                "synthetic/archive", identity=self.identity, attempt_id="synthetic-attempt")
        first = invoke()
        self.assertEqual(first["status"], "complete")
        self.assertEqual(len(self.ledger.rows), 1)
        ext = next(iter(self.ledger.rows.values()))["ext"]
        prefix = self.p.dd.EXT
        self.assertEqual(len(ext[prefix + "authors"]), 2)
        self.assertEqual(len(ext[prefix + "evidence"]), 2)
        self.assertEqual(ext[prefix + "push_count"], 1)
        self.assertEqual(len(self.p.sends), 1)
        self.assertEqual(self.p.sends[0].count(candidates[0]["title"]), 1)
        before = (copy.deepcopy(self.ledger.rows), len(self.ledger.writes),
                  list(self.ledger.watermarks), list(self.p.sends))
        self.assertEqual(invoke()["status"], "complete")
        self.assertEqual((self.ledger.rows, len(self.ledger.writes),
                          self.ledger.watermarks, self.p.sends), before)


class PreflightTests(unittest.TestCase):
    def main(self, module):
        p = product()
        p.modules["llmcall"] = module
        output = io.StringIO()
        parser = types.SimpleNamespace(
            add_argument=lambda *a, **k: None,
            parse_args=lambda: types.SimpleNamespace(config_dir=None, log_dir=None, preflight=True))
        p.scheduled.main.__globals__["argparse"] = types.SimpleNamespace(ArgumentParser=lambda: parser)
        with contextlib.redirect_stdout(output):
            result = p.scheduled.main()
        return result, output.getvalue()

    def test_missing_installed_interface_never_reports_ready(self):
        with self.assertRaisesRegex(ValueError, "llmcall"):
            self.main(ImportError("synthetic llmcall missing"))

    def test_incompatible_installed_interfaces_are_named_failures(self):
        for call in (None, lambda prompt: None):
            with self.subTest(callable=callable(call)):
                with self.assertRaisesRegex(ValueError, "llmcall"):
                    self.main(types.SimpleNamespace(call=call))

    def test_compatible_installed_interface_is_checked_without_calling(self):
        calls = []
        def call(prompt, *, mode):
            calls.append((prompt, mode))
            raise AssertionError("preflight must not call a model")
        result, output = self.main(types.SimpleNamespace(call=call))
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output)["status"], "ready")
        self.assertEqual(calls, [])


class ConformanceTests(unittest.TestCase):
    def test_current_version_matches_latest_recorded_release(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        release = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M).group(1)
        self.assertEqual(json.loads((ROOT / ".claude-plugin/plugin.json").read_text(
            encoding="utf-8"))["version"], release)
        for name in ("README.md", "README_CN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("status-v" + release + "%20", text)
            self.assertIn("Roadmap-v" + release + "-", text)
        self.assertIn("Current: **v" + release + "**",
                      (ROOT / "ROADMAP.md").read_text(encoding="utf-8"))

    def boundary(self, present):
        path = ROOT / "skills/demand-mining/tests/test_no_real_pii_in_repo.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "test_data_boundary_holds")
        paths, calls = [], []
        class GuardPath:
            def __init__(self, value="synthetic"):
                self.value = value
            def __truediv__(self, name):
                return GuardPath(self.value + "/" + name)
            def __str__(self):
                return self.value
            def is_file(self):
                paths.append(self.value)
                return present
        def run(args, **kwargs):
            calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        scope = {"REPO_ROOT": GuardPath(), "sys": types.SimpleNamespace(executable="synthetic-python"),
                 "subprocess": types.SimpleNamespace(run=run)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), scope)
        scope["test_data_boundary_holds"]()
        return paths, calls

    def test_missing_shared_boundary_scanner_is_not_a_pass(self):
        with self.assertRaisesRegex(AssertionError, "guards"):
            self.boundary(False)

    def test_present_shared_boundary_scanner_is_invoked(self):
        paths, calls = self.boundary(True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "synthetic/guards/tools/data_boundary.py")


if __name__ == "__main__":
    unittest.main()
