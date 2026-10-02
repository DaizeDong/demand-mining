"""Generated unknown historical attribution controls for Demand24."""
import copy
import json
import unittest

from test_review19_repairs import generator
from test_review22_repairs import Review22Harness

CASES = generator.review24_cases()


class Review24Tests(unittest.TestCase):
    def test_unknown_author_is_absent_and_repeated_screening_is_stable(self):
        h = Review22Harness()
        lib, _, _, _ = h.ready()
        original = copy.deepcopy(CASES["evidence"])
        fact = lib.evidence_observation(original)
        self.assertNotIn("author_hash", fact)
        self.assertTrue(fact["explicit"])
        self.assertEqual(h.redactor.safe_data(fact), fact)
        merged = lib.merge_observations({"evidence": [original], "authors": []})
        self.assertEqual(original, CASES["evidence"])
        self.assertEqual(merged["reach"], 0)
        self.assertEqual(merged["internal_mentions"], 0)
        self.assertEqual(merged["observation_count"], 1)
        self.assertEqual(merged["independent_source_count"], 1)
        for item in merged["evidence"] + merged["observation_index"]:
            self.assertNotIn("author_hash", item)
        self.assertEqual(h.redactor.safe_data(merged), merged)
        self.assertEqual(lib.merge_observations(merged), merged)

    def test_legacy_unknown_snippet_survives_serialization_updates_and_replay(self):
        for legacy_author_count in CASES["legacy_author_counts"]:
            with self.subTest(legacy_author_count=legacy_author_count):
                h = Review22Harness()
                lib, _, _, bot = h.ready()
                pool = h.pool()
                incoming = h.demand(CASES["authors"][3])
                legacy = {key: copy.deepcopy(value) for key, value in incoming.items()
                          if key not in {"evidence", "observation_index", "authors"}}
                known = CASES["authors"][:legacy_author_count]
                legacy.update(
                    authors=[{"author_hash": author, "urgency": "need", "segment": "free"}
                             for author in known],
                    evidence=[copy.deepcopy(CASES["evidence"])],
                    reach=CASES["legacy_numeric_claim"],
                    observation_count=CASES["legacy_numeric_claim"])
                h.files["synthetic-pool"] = (json.dumps(legacy) + "\n").encode()
                unknown_id = None
                for count, author in enumerate(CASES["authors"][3:6], 1):
                    demand = h.demand(author)
                    _, row = pool.upsert("synthetic-pool", demand,
                                         bot._rescorer(lib.DEFAULT_CONFIG))
                    unknown = [fact for fact in row["observation_index"]
                               if "author_hash" not in fact]
                    self.assertEqual(len(unknown), 1)
                    if unknown_id is None:
                        unknown_id = unknown[0]["observation_id"]
                    self.assertEqual(unknown[0]["observation_id"], unknown_id)
                    snippets = [item for item in row["evidence"]
                                if item["observation_id"] == unknown_id]
                    self.assertEqual(len(snippets), 1)
                    self.assertNotIn("author_hash", snippets[0])
                    for key, value in CASES["evidence"].items():
                        self.assertEqual(snippets[0][key], value)
                    self.assertEqual(row["reach"], legacy_author_count + count)
                    self.assertEqual({item["author_hash"] for item in row["authors"]},
                                     set(known + CASES["authors"][3:3 + count]))
                    self.assertEqual(row["internal_mentions"], count)
                    self.assertEqual(row["observation_count"], count + 1)
                    self.assertEqual(h.redactor.safe_data(row), row)
                    self.assertEqual(pool.load("synthetic-pool", product_id=CASES["product"]), [row])
                before = copy.deepcopy(row)
                _, replay = pool.upsert("synthetic-pool", demand,
                                        bot._rescorer(lib.DEFAULT_CONFIG))
                self.assertEqual(replay, before)

    def test_legacy_nullable_index_normalizes_without_reassigning_unknown_author(self):
        h = Review22Harness()
        lib, _, _, _ = h.ready()
        fact = lib.evidence_observation(CASES["evidence"])
        fact["author_hash"] = None
        snippet = {**copy.deepcopy(CASES["evidence"]),
                   "observation_id": fact["observation_id"],
                   "source_id": fact["source_id"], "author_hash": None}
        legacy = {"authors": [{"author_hash": CASES["authors"][0]}],
                  "observation_index": [fact], "evidence": [snippet]}
        before = copy.deepcopy(legacy)
        merged = lib.merge_observations(legacy)
        self.assertEqual(legacy, before)
        self.assertEqual(merged["authors"], legacy["authors"])
        self.assertEqual(merged["reach"], 1)
        self.assertEqual(merged["internal_mentions"], 0)
        self.assertEqual(merged["observation_count"], 1)
        for item in merged["evidence"] + merged["observation_index"]:
            self.assertNotIn("author_hash", item)
            self.assertEqual(item["observation_id"], fact["observation_id"])
        self.assertEqual(h.redactor.safe_data(merged), merged)
        self.assertEqual(lib.merge_observations(merged), merged)

    def test_unique_legacy_author_and_explicit_known_author_are_preserved(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                h = Review22Harness()
                lib, _, _, _ = h.ready()
                authors = CASES["authors"][:2 if explicit else 1]
                evidence = copy.deepcopy(CASES["evidence"])
                expected = authors[-1]
                if explicit:
                    evidence["author_hash"] = expected
                record = {"authors": [{"author_hash": author} for author in authors],
                          "evidence": [evidence]}
                before = copy.deepcopy(record)
                merged = lib.merge_observations(record)
                self.assertEqual(record, before)
                self.assertEqual(merged["reach"], len(authors))
                self.assertEqual(merged["internal_mentions"], 1)
                self.assertEqual(merged["observation_count"], 1)
                self.assertEqual(merged["observation_index"][0]["author_hash"], expected)
                self.assertEqual(merged["evidence"][0]["author_hash"], expected)
                self.assertEqual(h.redactor.safe_data(merged), merged)
                self.assertEqual(lib.merge_observations(merged), merged)


if __name__ == "__main__":
    unittest.main()
