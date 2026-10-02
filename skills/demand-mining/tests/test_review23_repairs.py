"""Generated synthetic count and author-alias controls for Demand23."""
import copy
import itertools
import json
import unittest

from test_review19_repairs import generator
from test_review22_repairs import Review22Harness

CASES = generator.review23_cases()


class Review23Tests(unittest.TestCase):
    def test_f18_extreme_json_counts_block_with_neighbor_and_diagnostic(self):
        for encoded_count in CASES["overflow_count_json"]:
            with self.subTest(encoded_count=encoded_count):
                h = Review22Harness()
                lib, _, _, _ = h.ready()
                valid = copy.deepcopy(CASES["valid_card"])
                broken = copy.deepcopy(valid)
                broken["independent_source_count"] = "SYNTHETIC_COUNT_PLACEHOLDER"
                wire = json.dumps([broken, valid]).replace(
                    '"SYNTHETIC_COUNT_PLACEHOLDER"', encoded_count)
                cards = json.loads(wire)
                before = copy.deepcopy(cards)
                result = h.modules["verify_gate"].gate_batch(cards, lib.DEFAULT_CONFIG)
                self.assertEqual(result["passed"], [valid])
                self.assertEqual(result["archivable"], [valid])
                self.assertFalse(result["empty_day"])
                self.assertEqual(len(result["blocked"]), 1)
                self.assertIn("independent_source_count non-numeric",
                              result["blocked"][0]["errors"])
                self.assertEqual(cards, before)

    def test_f18_ordinary_counts_keep_existing_batch_behavior(self):
        for case in CASES["ordinary_counts"]:
            with self.subTest(label=case["label"]):
                h = Review22Harness()
                lib, _, _, _ = h.ready()
                card = {**copy.deepcopy(CASES["valid_card"]),
                        "final_score": case["score"],
                        "independent_source_count": case["value"]}
                result = h.modules["verify_gate"].gate_batch([card], lib.DEFAULT_CONFIG)
                self.assertEqual(bool(result["passed"]), case["passes"])
                self.assertEqual(len(result["blocked"]), int(not case["passes"]))
                self.assertEqual(bool(result["pushable"]), case["pushable"])
                if case["error"]:
                    self.assertTrue(any(case["error"] in message
                                        for message in result["blocked"][0]["errors"]))

    def test_f19_all_alias_pairs_reject_malformed_values_in_both_orders(self):
        for good_key, bad_key in itertools.permutations(CASES["alias_keys"], 2):
            for malformed in CASES["malformed_authors"]:
                for reverse in (False, True):
                    with self.subTest(good_key=good_key, bad_key=bad_key,
                                      malformed=malformed["label"], reverse=reverse):
                        h = Review22Harness()
                        pairs = [(good_key, CASES["pseudonym"]),
                                 (bad_key, copy.deepcopy(malformed["value"]))]
                        if reverse:
                            pairs.reverse()
                        value = dict(pairs)
                        before = copy.deepcopy(value)
                        with self.assertRaisesRegex(ValueError, "invalid author identity type"):
                            h.redactor.safe_data(value)
                        self.assertEqual(value, before)

    def test_f19_standalone_and_nested_malformed_aliases_are_rejected(self):
        for key in CASES["alias_keys"]:
            for malformed in CASES["malformed_authors"]:
                for nested in (False, True):
                    with self.subTest(key=key, malformed=malformed["label"], nested=nested):
                        h = Review22Harness()
                        value = {key: copy.deepcopy(malformed["value"])}
                        if nested:
                            value = {"observations": [value]}
                        with self.assertRaisesRegex(ValueError, "invalid author identity type"):
                            h.redactor.safe_data(value)

    def test_f19_valid_scalar_aliases_stay_order_independent_and_idempotent(self):
        for raw in CASES["valid_author_scalars"]:
            h = Review22Harness()
            expected = (raw if raw == CASES["pseudonym"]
                        else h.redactor.pseudonymize(str(raw)))
            for keys in itertools.permutations(CASES["alias_keys"]):
                with self.subTest(raw=raw, keys=keys):
                    value = {key: raw for key in keys}
                    before = copy.deepcopy(value)
                    normalized = h.redactor.safe_data(value)
                    self.assertEqual(normalized, {"author_hash": expected})
                    self.assertEqual(h.redactor.safe_data(normalized), normalized)
                    self.assertEqual(value, before)


if __name__ == "__main__":
    unittest.main()
