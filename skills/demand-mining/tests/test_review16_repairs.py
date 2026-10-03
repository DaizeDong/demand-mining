"""Product-bound live pools and recoverable direct feedback, using generated inputs."""
import ast
import contextlib
import copy
import json
import os
from pathlib import Path
import re
import tempfile
import types
import unittest
from unittest.mock import patch

import demand_pool as pool
import lib
import redact

CASES = json.loads((Path(__file__).parent / "fixtures/repair_cases.json").read_text(encoding="utf-8"))["review16"]


def finish(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("inert coroutine unexpectedly yielded")


def bot_namespace():
    path = Path(__file__).parents[1] / "scripts/demand_bot.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = [node for node in tree.body if (
        isinstance(node, ast.ClassDef) and node.name == "DemandBot"
        or isinstance(node, ast.FunctionDef) and node.name in {
            "_demand_from_verdict", "_message_observation", "_reply_sys"})]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    scope = {"discord": types.SimpleNamespace(Client=object), "re": re,
             "_SIGNAL": re.compile("export"), "safe_text": redact.safe_text,
             "safe_data": redact.safe_data, "canonical_key": lib.canonical_key,
             "iso": lib.iso, "now_utc": lib.now_utc, "parse_ts": lib.parse_ts,
             "merge_observations": lib.merge_observations,
             "observation_identity": lib.observation_identity, "pseudonymize": redact.pseudonymize}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[])),
                 str(path), "exec"), scope)
    return scope


class ProductPoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "demands.jsonl")
        self.addCleanup(patch.stopall)
        patch.object(pool, "_file_lock", lambda path: contextlib.nullcontext()).start()

        def write(path, rows):
            Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        patch.object(pool, "_atomic_write", write).start()

    def demand(self, product, **fields):
        return {**copy.deepcopy(CASES["demand"]), "product_id": product, **fields}

    def test_two_products_keep_independent_same_subjects(self):
        first, second = CASES["products"]
        a = pool.upsert(self.path, self.demand(first))
        b = pool.upsert(self.path, self.demand(second, authors=[{"author_hash": CASES["second_author"]}]))
        self.assertEqual((a[0], b[0]), ("new", "new"))
        self.assertEqual([row["product_id"] for row in pool.load(self.path, product_id=first)], [first])
        self.assertEqual([row["product_id"] for row in pool.load(self.path, product_id=second)], [second])
        self.assertEqual(pool.load(self.path, product_id=first)[0]["reach"], 1)

    def test_same_product_recurrence_deduplicates_authors_and_evidence(self):
        product = CASES["products"][0]
        value = self.demand(product)
        self.assertEqual(pool.upsert(self.path, value)[0], "new")
        action, replay = pool.upsert(self.path, value)
        self.assertEqual(action, "merged")
        self.assertEqual(len(replay["evidence"]), 1)
        row = pool.upsert(self.path, self.demand(product, authors=[{"author_hash": CASES["second_author"]}]))[1]
        self.assertEqual(row["reach"], 2)
        self.assertEqual(len(row["evidence"]), 2)
        self.assertEqual({item["author_hash"] for item in row["evidence"]},
                         {item["author_hash"] for item in row["authors"]})

    def test_status_and_summary_never_include_another_product(self):
        first, second = CASES["products"]
        pool.upsert(self.path, self.demand(first))
        pool.upsert(self.path, self.demand(second))
        pool.upsert(self.path, self.demand(second, **CASES["exclusive"]))
        self.assertTrue(pool.set_status(self.path, CASES["demand"]["canonical_key"], "shipped", product_id=first))
        self.assertEqual(pool.ranked(self.path, product_id=first), [])
        self.assertEqual(len(pool.ranked(self.path, product_id=second)), 2)
        self.assertEqual(pool.load(self.path, product_id=second)[0]["status"], "new")
        scope = bot_namespace()
        scope["pool"] = pool
        for product, contains in ((first, False), (second, True)):
            bot = object.__new__(scope["DemandBot"])
            bot.cfg, bot.poolp = {"product_id": product, "timezone": "UTC"}, self.path
            self.assertEqual(CASES["exclusive"]["title"] in bot._render_summary("2026-01-15"), contains)

    def test_legacy_requires_explicit_per_key_attribution(self):
        first, second = CASES["products"]
        legacy = copy.deepcopy(CASES["demand"])
        Path(self.path).write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        self.assertEqual(pool.load(self.path, product_id=first), [])
        self.assertEqual(pool.upsert(self.path, self.demand(first))[0], "new")
        stored = [json.loads(line) for line in Path(self.path).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(stored[0], legacy)
        before = Path(self.path).read_bytes()
        with self.assertRaises(ValueError):
            pool.attribute_legacy(self.path, legacy["canonical_key"], product_id=first)
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.assertTrue(pool.attribute_legacy(self.path, legacy["canonical_key"], product_id=second))
        self.assertFalse(pool.attribute_legacy(self.path, legacy["canonical_key"], product_id=second))
        self.assertEqual(len(pool.load(self.path, product_id=second)), 1)

    def test_missing_product_identity_cannot_read_or_write(self):
        for value in (None, "", " "):
            with self.subTest(product=value):
                with self.assertRaises(ValueError):
                    pool.upsert(self.path, self.demand(value))
                with self.assertRaises(ValueError):
                    pool.load(self.path, product_id=value)
        self.assertFalse(Path(self.path).exists())


    def test_rescorer_cannot_change_the_product_or_key(self):
        first, second = CASES["products"]
        for changes in ({"product_id": second}, {"canonical_key": CASES["exclusive"]["canonical_key"]}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    pool.upsert(self.path, self.demand(first), rescore=lambda row: {**row, **changes})
                self.assertFalse(Path(self.path).exists())

    def test_privacy_screening_cannot_rebind_a_legacy_product(self):
        legacy = copy.deepcopy(CASES["demand"])
        Path(self.path).write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        before = Path(self.path).read_bytes()
        product = CASES["products"][0]
        real_safe_data = redact.safe_data
        with patch.object(redact, "safe_data", lambda value: "changed" if value == product else real_safe_data(value)):
            with self.assertRaises(ValueError):
                pool.attribute_legacy(self.path, legacy["canonical_key"], product_id=product)
        self.assertEqual(Path(self.path).read_bytes(), before)


class DirectFeedbackTests(unittest.TestCase):
    def setup_bot(self, fault=None):
        scope = bot_namespace()
        bot = object.__new__(scope["DemandBot"])
        bot.cfg = {"product_id": CASES["products"][0], "timezone": "UTC"}
        bot.product, bot.classify_sys = "Acme", ""
        bot.reply_sys = scope["_reply_sys"]("Acme")
        bot.classify_rounds, bot.rescore, bot.poolp = 1, None, "synthetic-pool"
        bot.post_direct, bot.mode = True, "shadow"
        bot.log = lambda message: None
        events, attempts = [], {}
        raw = CASES["message"]

        def step(name):
            events.append(name)
            attempts[name] = attempts.get(name, 0) + 1
            if name == fault and attempts[name] == 1:
                raise RuntimeError("synthetic " + name + " failure")

        async def context(message, **kwargs):
            step("context")
            return ""

        async def send(text, **kwargs):
            step("reply")
            return types.SimpleNamespace(id="synthetic-receipt")

        async def note(text):
            step("note")

        async def to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        def classify(*args):
            step("classify")
            return [copy.deepcopy(CASES["verdict"])]

        def persist(path, demand, rescore):
            step("persist")
            self.assertEqual(demand["product_id"], bot.cfg["product_id"])
            return "new", demand

        def generate(*args):
            step("generate")
            return CASES["reply"]

        bot._gather_context, bot._note = context, note
        message = types.SimpleNamespace(id=raw["id"], channel=types.SimpleNamespace(
            id=raw["channel_id"], name="feedback"), reply=send)
        scope.update(asyncio=types.SimpleNamespace(to_thread=to_thread),
                     classify_batch=classify, gen_reply=generate,
                     pool=types.SimpleNamespace(upsert=persist))
        return bot, message, events, attempts

    def direct(self, bot, message):
        return finish(bot._direct_reply(message, CASES["message"]["text"], CASES["message"]["ah"]))

    def check_recovery(self, fault):
        bot, message, events, attempts = self.setup_bot(fault)
        try:
            self.direct(bot, message)
        except RuntimeError:
            pass
        self.assertTrue(getattr(bot, "_direct_pending", {}), "direct observation has no retry owner")
        self.assertNotIn("reply", events)
        finish(bot._retry_direct_pending())
        self.assertFalse(bot._direct_pending)
        self.assertEqual(attempts["reply"], 1)
        self.assertEqual(attempts["persist"], 2 if fault == "persist" else 1)
        self.assertLess(events.index("persist"), events.index("reply"))
        self.direct(bot, message)
        self.assertEqual(attempts["reply"], 1)
        self.assertEqual(attempts["persist"], 2 if fault == "persist" else 1)

    def test_context_failure_remains_owned(self):
        self.check_recovery("context")

    def test_classifier_failure_remains_owned(self):
        self.check_recovery("classify")

    def test_pool_failure_remains_owned(self):
        self.check_recovery("persist")

    def test_reply_generation_failure_does_not_repeat_persistence(self):
        self.check_recovery("generate")

    def test_confirmation_follows_persistence_and_replay_is_suppressed(self):
        bot, message, events, attempts = self.setup_bot()
        self.direct(bot, message)
        self.assertIn("persist", events)
        self.assertLess(events.index("persist"), events.index("reply"))
        self.direct(bot, message)
        self.assertEqual(attempts["reply"], 1)
        self.assertEqual(attempts["persist"], 1)

    def test_unknown_reply_send_does_not_resend_completed_observation(self):
        bot, message, events, attempts = self.setup_bot("reply")
        self.direct(bot, message)
        self.assertFalse(getattr(bot, "_direct_pending", {}))
        self.direct(bot, message)
        self.assertEqual(attempts["reply"], 1)
        self.assertEqual(attempts["persist"], 1)


    def test_polling_worker_retries_owned_direct_feedback(self):
        bot, message, events, attempts = self.setup_bot("classify")
        self.direct(bot, message)
        self.assertTrue(bot._direct_pending)
        bot.interval, bot.buffer = 1, []
        bot.is_closed = lambda: False
        polls = []

        class StopControl(BaseException):
            pass

        async def sleep(seconds):
            polls.append(seconds)
            if len(polls) > 1:
                raise StopControl()

        bot._classify_loop.__globals__["asyncio"].sleep = sleep
        with self.assertRaises(StopControl):
            finish(bot._classify_loop())
        self.assertFalse(bot._direct_pending)
        self.assertEqual(attempts["classify"], 2)
        self.assertEqual(attempts["persist"], 1)
        self.assertEqual(attempts["reply"], 1)

    def test_overlapping_direct_events_share_one_owner(self):
        bot, message, events, attempts = self.setup_bot()
        context = bot._gather_context

        class Pause:
            def __await__(self):
                yield "context-paused"

        async def paused_context(message, **kwargs):
            await Pause()
            return await context(message, **kwargs)

        bot._gather_context = paused_context
        first = bot._direct_reply(message, CASES["message"]["text"], CASES["message"]["ah"])
        try:
            self.assertEqual(first.send(None), "context-paused")
            self.assertEqual(self.direct(bot, message)["status"], "pending")
            self.assertEqual(events, [])
            self.assertEqual(finish(first)["status"], "complete")
        finally:
            first.close()
        self.assertEqual(attempts["persist"], 1)
        self.assertEqual(attempts["reply"], 1)

    def test_activity_note_failure_does_not_replay_completed_feedback(self):
        bot, message, events, attempts = self.setup_bot("note")
        with self.assertRaises(RuntimeError):
            self.direct(bot, message)
        self.assertFalse(bot._direct_pending)
        self.assertEqual(self.direct(bot, message)["status"], "complete")
        self.assertEqual(attempts["persist"], 1)
        self.assertEqual(attempts["reply"], 1)
        self.assertEqual(attempts["note"], 1)

    def test_non_demand_reply_receives_unsaved_storage_state(self):
        bot, message, events, attempts = self.setup_bot()
        scope = bot._complete_direct.__globals__
        scope["classify_batch"] = lambda *args: [{"is_demand": False}]
        prompts = []

        def generate(text, instructions, context):
            prompts.append(instructions)
            return CASES["reply"]

        scope["gen_reply"] = generate
        self.direct(bot, message)
        self.assertNotIn("persist", events)
        self.assertIn("No product demand was saved.", prompts[0])
        self.assertIn("Do not say the message was logged or recorded.", prompts[0])
