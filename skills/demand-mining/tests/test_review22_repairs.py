"""Generated real-redactor controls for Demand22; external effects are memory-only."""
import builtins
import contextlib
import copy
import io
import json
import types
import unittest

from test_review19_repairs import generator
from test_review21_repairs import Review21Harness

CASES = generator.review22_cases()


class Review22Harness(Review21Harness):
    def __init__(self):
        super().__init__()
        append_open = self.open
        def memory_open(path, mode="r", **kwargs):
            if mode == "r":
                return io.StringIO(self.files[str(path)].decode(kwargs.get("encoding", "utf-8")))
            return append_open(path, mode, **kwargs)
        self.open = memory_open
        self.os.path.isfile = lambda path: str(path) in self.files
        self.os.environ["DEMAND_MINING_NOW"] = CASES["timestamp"]
        self.redactor = self.load("redact")
        self.redactor._EPHEMERAL_SALT = CASES["salt"].encode()

    def importer(self, name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"collections", "hmac", "unicodedata"}:
            return builtins.__import__(name, globals, locals, fromlist, level)
        return super().importer(name, globals, locals, fromlist, level)

    def pool(self):
        module = self.load("demand_pool")
        module._file_lock = lambda path: contextlib.nullcontext()
        def write(path, payload):
            if not isinstance(payload, bytes):
                raise AssertionError("serializer did not produce bytes")
            self.files[str(path)] = payload
        self.modules["data_safety"].atomic_bytes = write
        return module

    def collected(self, count=2):
        self.ready()
        pull = self.load("pull_discord")
        batches = copy.deepcopy(CASES["messages"])
        pull._get = lambda channel, token, before: batches.pop(str(channel), [])
        return pull.pull(CASES["channels"][:count], "synthetic-token",
                         source_window=CASES["window"])

    def proposed(self, corpus, candidate=None):
        candidate = copy.deepcopy(candidate or CASES["candidate"])
        return self.proposer([candidate]).propose(corpus, self.modules["lib"].DEFAULT_CONFIG)[0]

    def demand(self, author, *, text=None, source="feedback"):
        return self.modules["demand_bot"]._demand_from_verdict(
            CASES["verdict"], text or CASES["text"], author, source, CASES["product"],
            timestamp=CASES["timestamp"])

    def matched(self, card):
        dd = self.modules["dedup"]
        return {"ext": {
            dd.EXT + "last_score": card["final_score"],
            dd.EXT + "source_set": card["source_set"],
            dd.EXT + "tier": card.get("tier"),
            dd.EXT + "external_corroboration": {"external_origin_count": 0},
        }}


class Review22Tests(unittest.TestCase):
    def test_f13_real_collector_redactor_and_proposer_preserve_single_author(self):
        h = Review22Harness()
        corpus = h.collected(1)
        row = corpus["channels"]["feedback"][0]
        raw = CASES["messages"][CASES["channels"][0]["id"]][0]
        expected = h.redactor.pseudonymize(raw["author"]["id"])
        self.assertEqual(row["author_hash"], expected)
        self.assertNotIn("author", row)
        clean = h.redactor.safe_data(corpus)
        self.assertEqual(clean, corpus)
        proposed = h.proposed(corpus)
        self.assertEqual([a["author_hash"] for a in proposed["authors"]], [expected])
        self.assertEqual(proposed["reach"], 1)
        self.assertEqual(proposed["internal_mentions"], 1)

    def test_f13_multi_source_scheduled_and_live_observations_agree(self):
        h = Review22Harness()
        corpus = h.collected(2)
        candidate = copy.deepcopy(CASES["candidate"])
        candidate["evidence"].append({**candidate["evidence"][0], "channel": "support"})
        proposed = h.proposed(corpus, candidate)
        observed = []
        for channel in CASES["channels"]:
            raw = CASES["messages"][channel["id"]][0]
            row = corpus["channels"][channel["name"]][0]
            message = types.SimpleNamespace(id=raw["id"], channel=types.SimpleNamespace(id=channel["id"]))
            info = h.modules["demand_bot"]._message_observation(message)
            self.assertEqual(info["source_id"], row["source_id"])
            self.assertEqual(info["observation_id"], row["observation_id"])
            live = h.modules["demand_bot"]._demand_from_verdict(
                CASES["verdict"], row["text"], row["author_hash"], channel["name"],
                CASES["product"], source_id=info["source_id"], observation_id=info["observation_id"],
                timestamp=row["ts"])
            observed.extend(live["observation_index"])
        self.assertEqual({x["observation_id"] for x in proposed["observation_index"]},
                         {x["observation_id"] for x in observed})
        self.assertEqual(proposed["reach"], 2)
        self.assertEqual(proposed["independent_source_count"], 2)

    def test_f13_legacy_author_alias_is_idempotent_and_conflicts_are_rejected(self):
        h = Review22Harness()
        corpus = h.collected(1)
        row = corpus["channels"]["feedback"][0]
        row["author"] = row.pop("author_hash")
        expected = row["author"]
        self.assertEqual(h.proposed(corpus)["authors"][0]["author_hash"], expected)
        first = h.redactor.safe_data(corpus)
        self.assertEqual(h.redactor.safe_data(first), first)
        with self.assertRaisesRegex(ValueError, "conflicting author"):
            h.redactor.safe_data({"author": CASES["authors"][0], "author_hash": CASES["authors"][1]})

    def test_f14_typed_identifiers_survive_but_free_text_and_credentials_are_screened(self):
        h = Review22Harness()
        lib, _, _, _ = h.ready()
        identity = lib.observation_identity("feedback", CASES["authors"][0],
                                            CASES["timestamp"], CASES["text"])
        typed = {"observation_id": identity, "source_id": identity}
        self.assertEqual(h.redactor.safe_data(typed), typed)
        for field in ("text", "summary", "untyped_metadata"):
            screened = h.redactor.safe_data({field: identity})
            self.assertNotEqual(screened[field], identity)
        self.assertNotEqual(h.redactor.safe_data({"source_id": CASES["raw_token"]})["source_id"],
                            CASES["raw_token"])
        self.assertEqual(h.redactor.safe_data({"observation_id": CASES["authors"][0]})["observation_id"],
                         CASES["authors"][0])

    def test_f14_generated_fallbacks_survive_actual_pool_serialization_reload_and_replay(self):
        h = Review22Harness()
        lib, _, _, bot = h.ready()
        pool = h.pool()
        ids = set()
        for i, author in enumerate(CASES["authors"][:3]):
            demand = h.demand(author, text=CASES["text"] + " " + str(i))
            ids.add(demand["observation_index"][0]["observation_id"])
            _, row = pool.upsert("synthetic-pool", demand, bot._rescorer(lib.DEFAULT_CONFIG))
            self.assertEqual(pool.load("synthetic-pool", product_id=CASES["product"]), [row])
        self.assertEqual(len(ids), 3)
        self.assertEqual({x["observation_id"] for x in row["observation_index"]}, ids)
        before = copy.deepcopy(row)
        _, replay = pool.upsert("synthetic-pool", demand, bot._rescorer(lib.DEFAULT_CONFIG))
        self.assertEqual(before, replay)
        self.assertEqual(len(pool.load("synthetic-pool", product_id=CASES["product"])), 1)

    def test_f14_actual_discord_namespace_cap_and_replay_are_preserved(self):
        h = Review22Harness()
        lib, _, _, bot = h.ready()
        pool = h.pool()
        all_ids = set()
        for i in range(12):
            channel = CASES["channels"][i % 2]
            message = types.SimpleNamespace(id=str(840000000000000000 + i),
                                           channel=types.SimpleNamespace(id=channel["id"]))
            info = bot._message_observation(message)
            demand = bot._demand_from_verdict(CASES["verdict"], CASES["text"],
                CASES["authors"][i % 4], channel["name"], CASES["product"],
                source_id=info["source_id"], observation_id=info["observation_id"],
                timestamp=CASES["timestamp"])
            all_ids.add(info["observation_id"])
            _, row = pool.upsert("synthetic-pool", demand, bot._rescorer(lib.DEFAULT_CONFIG))
        self.assertEqual(len(row["evidence"]), 8)
        self.assertEqual(row["observation_count"], 12)
        self.assertEqual(row["reach"], 4)
        self.assertEqual(row["independent_source_count"], 2)
        self.assertEqual({x["observation_id"] for x in row["observation_index"]}, all_ids)
        _, replay = pool.upsert("synthetic-pool", demand, bot._rescorer(lib.DEFAULT_CONFIG))
        self.assertEqual(replay, row)

    def test_f15_legacy_authors_survive_three_updates_with_or_without_old_snippets(self):
        for has_snippets in (False, True):
            with self.subTest(has_snippets=has_snippets):
                h = Review22Harness()
                lib, _, _, bot = h.ready()
                pool = h.pool()
                incoming = h.demand(CASES["authors"][3])
                legacy = {k: copy.deepcopy(v) for k, v in incoming.items()
                          if k not in {"evidence", "observation_index", "authors"}}
                legacy.update(authors=[{"author_hash": a, "urgency": "need", "segment": "free"}
                                       for a in CASES["authors"][:3]],
                              reach=CASES["legacy_numeric_claim"], observation_count=CASES["legacy_numeric_claim"])
                legacy["evidence"] = ([{k: v for k, v in incoming["evidence"][0].items()
                                        if k not in {"observation_id", "source_id", "author_hash"}}]
                                      if has_snippets else [])
                h.files["synthetic-pool"] = (json.dumps(legacy) + "\n").encode()
                for i, author in enumerate(CASES["authors"][3:6], 1):
                    _, row = pool.upsert("synthetic-pool", h.demand(author), bot._rescorer(lib.DEFAULT_CONFIG))
                    self.assertEqual(row["canonical_key"], incoming["canonical_key"])
                    self.assertEqual(row["reach"], 3 + i)
                    self.assertEqual({a["author_hash"] for a in row["authors"]}, set(CASES["authors"][:3 + i]))
                    self.assertEqual(len(pool.load("synthetic-pool", product_id=CASES["product"])), 1)
                    self.assertEqual(row["observation_count"], i + int(has_snippets))
                _, replay = pool.upsert("synthetic-pool", h.demand(CASES["authors"][5]),
                                        bot._rescorer(lib.DEFAULT_CONFIG))
                self.assertEqual(replay, row)

    def test_f15_retained_authors_survive_existing_index_without_numeric_invention(self):
        h = Review22Harness()
        lib, _, _, _ = h.ready()
        current = h.demand(CASES["authors"][3])
        current["authors"] += [{"author_hash": a} for a in CASES["authors"][:3]]
        current["reach"] = CASES["legacy_numeric_claim"]
        for author in CASES["authors"][4:6]:
            current.update(lib.merge_observations(current, h.demand(author)))
        self.assertEqual(current["reach"], 6)
        self.assertEqual(current["internal_mentions"], 3)
        self.assertEqual(current["observation_count"], 3)

    def test_f16_grounding_projection_and_dedup_reject_unobserved_evolution_claims(self):
        h = Review22Harness()
        corpus = h.collected(1)
        candidate = copy.deepcopy(CASES["candidate"])
        candidate.update(external_corroboration=CASES["external_claim"],
                         competitor_status="shipped", competitor_ref="invented", velocity=99)
        proposed = h.proposed(corpus, candidate)
        self.assertEqual(proposed["external_corroboration"]["external_origin_count"], 0)
        for key in ("competitor_status", "competitor_ref", "velocity"):
            self.assertNotIn(key, proposed)
        lib, run, dd = h.modules["lib"], h.modules["run"], h.modules["dedup"]
        card = run.build_card(proposed, lib.DEFAULT_CONFIG, "synthetic-run")
        self.assertEqual(card["external_corroboration"]["external_origin_count"], 0)
        self.assertEqual(dd.decide(card, h.matched(card), lib.DEFAULT_CONFIG)["branch"], dd.SUPPRESS)

    def test_f16_direct_count_and_incomplete_external_events_cannot_resurface(self):
        h = Review22Harness()
        lib, _, run, _ = h.ready()
        dd = h.modules["dedup"]
        card = run.build_card(CASES["candidate"], lib.DEFAULT_CONFIG, "synthetic-run")
        matched = h.matched(card)
        card["external_corroboration"] = CASES["external_claim"]
        self.assertEqual(dd.decide(card, matched, lib.DEFAULT_CONFIG)["branch"], dd.SUPPRESS)
        for key, invalid in (("ts", "not-a-date"), ("source", ""), ("redacted_snippet", "")):
            broken = {**CASES["external"], key: invalid}
            card["evidence"] = [broken]
            decision = dd.decide(card, matched, lib.DEFAULT_CONFIG)
            self.assertFalse(decision["delta"]["external_corroboration_new"])
            self.assertEqual(decision["branch"], dd.SUPPRESS)

    def test_f16_real_external_event_resurfaces_once(self):
        h = Review22Harness()
        lib, _, run, _ = h.ready()
        dd = h.modules["dedup"]
        candidate = copy.deepcopy(CASES["candidate"])
        candidate["evidence"].append(copy.deepcopy(CASES["external"]))
        card = run.build_card(candidate, lib.DEFAULT_CONFIG, "synthetic-run")
        matched = h.matched(card)
        self.assertEqual(card["external_corroboration"]["external_origin_count"], 1)
        decision = dd.decide(card, matched, lib.DEFAULT_CONFIG)
        self.assertEqual(decision["branch"], dd.RESURFACE)
        self.assertTrue(decision["delta"]["external_corroboration_new"])
        matched["ext"][dd.EXT + "external_corroboration"] = card["external_corroboration"]
        self.assertEqual(dd.decide(card, matched, lib.DEFAULT_CONFIG)["branch"], dd.SUPPRESS)

    def test_f17_both_json_integer_overflows_keep_neighbor_and_blocked_diagnostics(self):
        h = Review22Harness()
        lib, _, _, _ = h.ready()
        cards = [copy.deepcopy(CASES["valid_card"])]
        for value in CASES["huge_scores"]:
            cards.append({**copy.deepcopy(CASES["valid_card"]), "final_score": value})
        cards = json.loads(json.dumps(cards))
        result = h.modules["verify_gate"].gate_batch(cards, lib.DEFAULT_CONFIG)
        self.assertEqual(result["passed"], cards[:1])
        self.assertEqual(len(result["blocked"]), 2)
        self.assertTrue(all("final_score missing/non-numeric" in item["errors"] for item in result["blocked"]))


if __name__ == "__main__":
    unittest.main()
