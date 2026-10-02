"""Generated full-module controls for evidence conservation and strict validation."""
import asyncio
import copy
import contextlib
import hashlib
import json
from pathlib import Path
import types
import unittest

from test_review19_repairs import ROOT, SCRIPTS, generator
from test_review20_repairs import DomainHarness

CASES = generator.review21_cases()


class Review21Harness(DomainHarness):
    def __init__(self):
        super().__init__()
        self.effects["threading"] = types.SimpleNamespace(Lock=contextlib.nullcontext)
        self.effects["msvcrt"] = self.effects["fcntl"] = None
        self.effects["secrets"] = types.SimpleNamespace(token_hex=lambda size: "synthetic-attempt")
        self.effects["zoneinfo"].ZoneInfoNotFoundError = KeyError
        self.effects["redact"].pseudonymize = lambda value: "u_" + hashlib.sha256(value.encode()).hexdigest()[:16]
        self.effects["urllib.error"] = types.SimpleNamespace(HTTPError=RuntimeError)
        self.effects["urllib.request"] = types.SimpleNamespace(urlopen=self.forbidden)
        async def to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)
        self.effects["asyncio"] = types.SimpleNamespace(to_thread=to_thread)
        self.effects["shutil"] = types.SimpleNamespace(copy2=self.forbidden)
        self.effects["redact"].privacy_coverage = lambda: {}

        self.effects["extract"] = None
        self.pool_rows = []

    def importer(self, name, globals=None, locals=None, fromlist=(), level=0):
        if name in ("msvcrt", "fcntl"):
            raise ImportError(name)
        return super().importer(name, globals, locals, fromlist, level)

    def ready(self):
        lib, score, run = self.scoring()
        self.load("verify_gate")
        self.load("dedup")
        run.dd = self.modules["dedup"]
        run.gate_batch = self.modules["verify_gate"].gate_batch
        self.load("private_log")
        bot = self.load("demand_bot")
        return lib, score, run, bot

    def pool(self):
        module = self.load("demand_pool")
        module._file_lock = lambda path: contextlib.nullcontext()
        module._read_rows = lambda path: copy.deepcopy(self.pool_rows)
        def write(path, rows):
            self.pool_rows = copy.deepcopy(rows)
        module._atomic_write = write
        return module

    def proposer(self, candidates):
        self.load("extract")
        self.load("finalize")
        self.load("pull_discord")
        self.effects["llmcall"].call = lambda *a, **k: types.SimpleNamespace(
            error=None, data={"classification": "complete", "candidates": copy.deepcopy(candidates)})
        return self.load("scheduled")

    def corpus(self, observations):
        channels = {}
        for item in observations:
            channels.setdefault(item["channel"], []).append({
                "author": item["author"], "text": item["text"], "ts": item["ts"],
                "observation_id": item["id"], "source_id": "synthetic-source-" + item["channel"]})
        return {"channels": channels}

    def card(self):
        self.ready()
        tests = self.load("legacy_gate", ROOT / "skills/demand-mining/tests/test_gate_digest.py")
        return tests._card(), self.modules["lib"].DEFAULT_CONFIG


class Review21Tests(unittest.TestCase):
    def test_f7_live_score_outputs_match_card_axes(self):
        h = Review21Harness()
        lib, score, run, bot = h.ready()
        proposal = copy.deepcopy(CASES["candidate"])
        proposal.update(opportunity_score=-1, urgency_wsjf=-1, tier_reason="stale")
        expected = run.build_card(proposal, lib.DEFAULT_CONFIG, "synthetic-run")
        before = copy.deepcopy(proposal)
        actual = bot._rescorer(lib.DEFAULT_CONFIG)(proposal)
        for field in CASES["score_axes"]:
            with self.subTest(axis=field):
                self.assertEqual(actual[field], expected[field])
        for field in ("reach", "importance", "satisfaction", "effort_weeks"):
            self.assertEqual(actual[field], before[field])

    def test_f8_three_internal_authors_and_replay(self):
        h = Review21Harness()
        lib, _, _, bot = h.ready()
        pool = h.pool()
        for author in CASES["authors"][:3]:
            demand = bot._demand_from_verdict(CASES["verdict"], CASES["text"], author,
                                             "feedback", CASES["product"])
            _, row = pool.upsert("synthetic-pool", demand, bot._rescorer(lib.DEFAULT_CONFIG))
            _, replay = pool.upsert("synthetic-pool", demand, bot._rescorer(lib.DEFAULT_CONFIG))
            self.assertEqual({**row, "last_seen": None}, {**replay, "last_seen": None})
        self.assertEqual(row["reach"], 3)
        self.assertEqual(row.get("internal_mentions", 0), 3)
        self.assertEqual(row["rice_domain"]["confidence"], 0.8)

    def test_f8_sources_same_text_and_display_cap(self):
        h = Review21Harness()
        lib, _, _, bot = h.ready()
        pool = h.pool()
        for item in CASES["observations"]:
            message = types.SimpleNamespace(id=item["id"], channel=types.SimpleNamespace(id=item["channel"]))
            kwargs = bot._message_observation(message) if hasattr(bot, "_message_observation") else {}
            demand = bot._demand_from_verdict(CASES["verdict"], item["text"], item["author"],
                                             item["channel"], CASES["product"], **kwargs)
            _, row = pool.upsert("synthetic-pool", demand, bot._rescorer(lib.DEFAULT_CONFIG))
        self.assertEqual(row["independent_source_count"], 2)
        self.assertEqual(row["reach"], 4)
        self.assertEqual(row.get("internal_mentions", 0), 4)
        self.assertEqual(row.get("observation_count", 0), 12)
        self.assertEqual(len(row["evidence"]), pool._MAX_EVIDENCE)
        _, replay = pool.upsert("synthetic-pool", demand, bot._rescorer(lib.DEFAULT_CONFIG))
        self.assertEqual({**row, "last_seen": None}, {**replay, "last_seen": None})

    def test_f8_repeated_author_does_not_create_three_author_band(self):
        h = Review21Harness()
        lib, _, _, bot = h.ready()
        pool = h.pool()
        for i in range(4):
            demand = bot._demand_from_verdict(CASES["verdict"], CASES["text"] + str(i),
                                             CASES["authors"][0], "feedback", CASES["product"])
            _, row = pool.upsert("synthetic-pool", demand, bot._rescorer(lib.DEFAULT_CONFIG))
        self.assertEqual(row["reach"], 1)
        self.assertEqual(row.get("internal_mentions", 0), 1)
        self.assertEqual(row["rice_domain"]["confidence"], 0.5)

    def test_f8_direct_context_is_not_attributed_to_current_author(self):
        h = Review21Harness()
        lib, _, _, bot = h.ready()
        captured = []
        h.effects["demand_pool"].upsert = lambda path, demand, rescore: (captured.append(demand) or "new", demand)
        owner = object.__new__(bot.DemandBot)
        owner.poolp, owner.rescore = "synthetic-pool", bot._rescorer(lib.DEFAULT_CONFIG)
        owner._pool_product = lambda: CASES["product"]
        entry = {"m": types.SimpleNamespace(id="synthetic-message", channel=types.SimpleNamespace(
                    id="synthetic-channel", name="feedback")),
                 "text": CASES["text"], "ah": CASES["authors"][0],
                 "context": "Another author discussed an unrelated feature.",
                 "verdict": CASES["verdict"], "reply": "synthetic reply", "reply_attempted": True}
        coroutine = owner._complete_direct(entry)
        with self.assertRaises(StopIteration):
            coroutine.send(None)
        self.assertEqual(captured[0]["evidence"][0]["redacted_snippet"], CASES["text"])
        self.assertEqual(captured[0]["authors"][0]["author_hash"], CASES["authors"][0])

    def test_f9_model_counts_authors_and_origins_are_corpus_bound(self):
        h = Review21Harness()
        lib, _, run, _ = h.ready()
        candidate = copy.deepcopy(CASES["candidate"])
        corpus = h.corpus(CASES["observations"][:1])
        proposed = h.proposer([candidate]).propose(corpus, lib.DEFAULT_CONFIG)[0]
        self.assertEqual(proposed["reach"], 1)
        self.assertEqual(proposed["independent_source_count"], 1)
        self.assertEqual(proposed["internal_mentions"], 1)
        self.assertEqual([a["author_hash"] for a in proposed["authors"]], [CASES["authors"][0]])
        self.assertEqual(proposed["evidence"][0]["origin_type"], "internal")
        merged = run.merge_candidates([proposed, copy.deepcopy(proposed)])[0]
        card = run.build_card(merged, lib.DEFAULT_CONFIG, "synthetic-run")
        self.assertEqual(card["rice_domain"]["reach"], 1)
        self.assertEqual(card["independent_source_count"], 1)
        self.assertEqual(h.modules["verify_gate"].gate_batch([card], lib.DEFAULT_CONFIG)["pushable"], [])

    def test_f9_duplicate_corpus_rows_and_legitimate_multi_source(self):
        h = Review21Harness()
        lib, _, run, _ = h.ready()
        candidate = copy.deepcopy(CASES["candidate"])
        second = copy.deepcopy(candidate)
        second["evidence"][0]["channel"] = "support"
        observations = CASES["observations"][:2] + [CASES["observations"][3]]
        corpus = h.corpus(observations + observations)
        proposed = h.proposer([candidate, second]).propose(corpus, lib.DEFAULT_CONFIG)
        merged = run.merge_candidates(proposed + proposed)[0]
        self.assertEqual(merged["reach"], 3)
        self.assertEqual(merged["independent_source_count"], 2)
        self.assertEqual(merged["internal_mentions"], 3)
        self.assertEqual(len(merged["evidence"]), 3)
        self.assertEqual(merged.get("observation_count"), 3)
        self.assertEqual(run.build_card(merged, lib.DEFAULT_CONFIG, "synthetic-run")["rice_domain"]["confidence"], 1)

    def test_f9_unobserved_quote_and_identity_are_rejected(self):
        for field, value in (("redacted_snippet", "unobserved synthetic quote"),
                             ("observation_id", "invented-observation"),
                             ("channel", "invented-source")):
            with self.subTest(field=field):
                h = Review21Harness()
                lib, _, _, _ = h.ready()
                candidate = copy.deepcopy(CASES["candidate"])
                candidate["evidence"][0][field] = value
                with self.assertRaises(ValueError):
                    h.proposer([candidate]).propose(h.corpus(CASES["observations"][:1]), lib.DEFAULT_CONFIG)

    def test_f10_reject_empty_missing_and_nontextual_internal_snippets(self):
        h = Review21Harness()
        card, cfg = h.card()
        gate = h.modules["verify_gate"]
        self.assertTrue(gate.validate_card(card, cfg)[0])
        for origin in ("internal", None):
            for snippet in CASES["invalid_snippets"]:
                with self.subTest(origin=origin, snippet=snippet):
                    bad = copy.deepcopy(card)
                    bad["evidence"][0]["redacted_snippet"] = snippet
                    bad["evidence"][0]["origin_type"] = origin
                    self.assertFalse(gate.validate_card(bad, cfg)[0])
                    self.assertFalse(gate.gate_batch([bad], cfg)["passed"])

    def test_f10_blocked_card_cannot_complete_real_run_path(self):
        h = Review21Harness()
        lib, _, run, _ = h.ready()
        finalizer = h.load("finalize")
        calls = []
        finalizer.require_private = lambda path: {"path": str(path)}
        finalizer.file_lock = lambda *a, **k: contextlib.nullcontext()
        finalizer.atomic_json = lambda *a, **k: calls.append("write") or h.forbidden()
        ledger = types.SimpleNamespace(list_active=lambda: [], upsert=lambda *a, **k: calls.append("upsert"))
        candidate = copy.deepcopy(CASES["candidate"])
        candidate["evidence"][0].update(origin_type="internal", redacted_snippet="")
        cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
        cfg.update(product_id=CASES["product"], timezone="UTC")
        identity = {"product_id": CASES["product"], "date": "2026-01-15", "timezone": "UTC",
                    "source_window": {"start": "2026-01-15T00:00:00Z", "end": "2026-01-16T00:00:00Z"}}
        collection = {"status": "complete", "classification": "complete", "source_count": 1, "source_window": identity["source_window"]}
        with self.assertRaisesRegex(ValueError, "candidate validation failed"):
            finalizer.finalize([candidate], cfg, ledger=ledger, dry_run=False,
                identity=identity, collection=collection, archive_dir=h.root, attempt_id="synthetic")
        self.assertEqual(calls, [])

    def test_f11_malformed_score_keeps_batch_diagnostics(self):
        h = Review21Harness()
        card, cfg = h.card()
        bad = []
        for value in CASES["bad_scores"]:
            item = copy.deepcopy(card)
            item["final_score"] = value
            bad.append(item)
        result = h.modules["verify_gate"].gate_batch(bad + [card], cfg)
        self.assertEqual(len(result["blocked"]), len(bad))
        self.assertEqual(result["passed"], [card])
        self.assertTrue(all("final_score missing/non-numeric" in x["errors"] for x in result["blocked"]))

    def test_f12_high_similarity_requires_subject_and_entities(self):
        h = Review21Harness()
        lib, _, _, _ = h.ready()
        dd = h.modules["dedup"]
        words = CASES["dedup_words"]
        candidate = {"canonical_key": "alpha|export::integrations", "title": "alpha " + words}
        row = {"idempotency_key": "beta|export::integrations", "ext": {
            dd.EXT + "text": "beta " + words, dd.EXT + "simhash": 0}}
        self.assertGreaterEqual(lib.jaccard(dd._token_set(candidate["title"]),
                                           dd._token_set(row["ext"][dd.EXT + "text"])), 0.83)
        self.assertFalse(dd._subject_agree(candidate["title"], row["ext"][dd.EXT + "text"]))
        self.assertIsNone(dd.match_existing(candidate, [row], lib.DEFAULT_CONFIG))
        candidate["canonical_key"] = "other::unrelated"
        row["ext"][dd.EXT + "text"] = candidate["title"]
        self.assertIsNone(dd.match_existing(candidate, [row], lib.DEFAULT_CONFIG))
        candidate["canonical_key"] = row["idempotency_key"]
        self.assertIs(dd.match_existing(candidate, [row], lib.DEFAULT_CONFIG), row)

    def test_f12_original_rewrites_and_candidate_band_preserved(self):
        h = Review21Harness()
        lib, _, _, _ = h.ready()
        tests = h.load("old_dedup_tests", ROOT / "skills/demand-mining/tests/test_dedup_pool.py")
        tests.test_near_dup_rewrite_matches()
        dd = h.modules["dedup"]
        cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
        cfg["scoring"].update(candidate_merge_band=[0.5, 0.99], dedup_cosine_threshold=0.99)
        words = CASES["dedup_words"]
        candidate = {"canonical_key": "alpha|export::integrations", "title": "alpha " + words}
        text = "alpha " + words + " synthetic"
        row = {"idempotency_key": "alpha|export|synthetic::integrations", "ext": {
            dd.EXT + "text": text, dd.EXT + "simhash": lib.simhash(text)}}
        self.assertIsNotNone(dd.in_candidate_band(candidate, [row], cfg))
        self.assertIsNone(dd.match_existing(candidate, [row], cfg))


if __name__ == "__main__":
    unittest.main()
