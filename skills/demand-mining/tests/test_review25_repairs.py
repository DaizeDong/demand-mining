"""Generated stored-pool compatibility controls for Demand25."""
import copy
import json
import unittest

from test_review19_repairs import generator
from test_review22_repairs import Review22Harness

CASES = generator.review25_cases()
PATH = "synthetic-pool"


def encoded(rows):
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")


def decoded(h):
    return [json.loads(line) for line in h.files[PATH].decode("utf-8").splitlines()]


def expected_rows():
    # The expected migration changes only the two canonical null author fields.
    rows = copy.deepcopy(CASES["rows"])
    for index in (0, 1, 3):
        for field in ("evidence", "observation_index"):
            del rows[index][field][0]["author_hash"]
    return rows


def put_alias(row, location, alias, value):
    if location == "root":
        row[alias] = value
    elif location == "metadata":
        row.setdefault("metadata", {})[alias] = value
    else:
        row[location][0][alias] = value


class Review25Tests(unittest.TestCase):
    def fixture(self):
        h = Review22Harness()
        lib, _, _, bot = h.ready()
        pool = h.pool()
        h.files[PATH] = encoded(CASES["rows"])
        h.atomic_calls = []
        original = h.modules["data_safety"].atomic_bytes
        def capture(path, payload):
            h.atomic_calls.append((path, payload))
            original(path, payload)
        h.modules["data_safety"].atomic_bytes = capture
        return h, lib, bot, pool

    def demand(self, h, product=None, key=None):
        demand = h.demand(CASES["authors"][5])
        demand["product_id"] = product or CASES["products"][0]
        demand["canonical_key"] = key or CASES["new_key"]
        return demand

    def assert_no_write(self, h, before):
        self.assertEqual(h.files[PATH], before)
        self.assertEqual(h.atomic_calls, [])

    def test_load_normalizes_view_without_persisting_or_changing_product_selection(self):
        h, _, _, pool = self.fixture()
        before = h.files[PATH]
        expected = expected_rows()
        for product in CASES["products"]:
            with self.subTest(product=product):
                self.assertEqual(pool.load(PATH, product_id=product),
                                 [row for row in expected if row.get("product_id") == product])
        self.assert_no_write(h, before)

    def test_unrelated_insert_in_each_product_persists_all_legacy_rows_without_recounting(self):
        for product in CASES["products"]:
            with self.subTest(product=product):
                h, lib, bot, pool = self.fixture()
                demand = self.demand(h, product=product)
                original = copy.deepcopy(demand)
                action, created = pool.upsert(PATH, demand, bot._rescorer(lib.DEFAULT_CONFIG))
                self.assertEqual(action, "new")
                self.assertEqual(demand, original)
                self.assertEqual(decoded(h), expected_rows() + [created])
                self.assertEqual(len(h.atomic_calls), 1)
                self.assertEqual(created["product_id"], product)
                self.assertEqual(h.redactor.safe_data(decoded(h)), decoded(h))

    def test_unrelated_merge_preserves_other_products_and_unassigned_history(self):
        h, lib, bot, pool = self.fixture()
        expected = expected_rows()
        target = expected[2]
        action, merged = pool.upsert(
            PATH, self.demand(h, key=target["canonical_key"]),
            bot._rescorer(lib.DEFAULT_CONFIG))
        self.assertEqual(action, "merged")
        expected[2] = merged
        self.assertEqual(decoded(h), expected)
        self.assertEqual(merged["reach"], 2)
        self.assertEqual(merged["observation_count"], 2)
        self.assertEqual(merged["internal_mentions"], 2)
        self.assertEqual(len(h.atomic_calls), 1)

    def test_unknown_target_keeps_unknown_identity_and_replay_is_byte_stable(self):
        h, lib, bot, pool = self.fixture()
        demand = self.demand(h, key=CASES["rows"][0]["canonical_key"])
        action, merged = pool.upsert(PATH, demand, bot._rescorer(lib.DEFAULT_CONFIG))
        self.assertEqual(action, "merged")
        unknown_id = CASES["rows"][0]["observation_index"][0]["observation_id"]
        for field in ("evidence", "observation_index"):
            unknown = [item for item in merged[field] if item["observation_id"] == unknown_id]
            self.assertEqual(len(unknown), 1)
            self.assertNotIn("author_hash", unknown[0])
        self.assertEqual({author["author_hash"] for author in merged["authors"]},
                         {CASES["authors"][0], CASES["authors"][5]})
        self.assertEqual(merged["reach"], 2)
        self.assertEqual(merged["observation_count"], 2)
        self.assertEqual(merged["internal_mentions"], 1)
        expected = expected_rows()
        expected[0] = merged
        self.assertEqual(decoded(h), expected)
        before = h.files[PATH]
        _, replay = pool.upsert(PATH, demand, bot._rescorer(lib.DEFAULT_CONFIG))
        self.assertEqual(replay, merged)
        self.assertEqual(h.files[PATH], before)
        self.assertEqual(len(h.atomic_calls), 2)

    def test_status_writes_nullable_and_unrelated_targets_with_exact_product_isolation(self):
        for target_index in (0, 1, 2):
            with self.subTest(target_index=target_index):
                h, _, _, pool = self.fixture()
                expected = expected_rows()
                target = expected[target_index]
                self.assertTrue(pool.set_status(PATH, target["canonical_key"], "planned",
                                                product_id=target["product_id"]))
                target["status"] = "planned"
                target["last_seen"] = CASES["timestamp"]
                self.assertEqual(decoded(h), expected)
                self.assertEqual(len(h.atomic_calls), 1)

    def test_legacy_attribution_persists_all_rows_and_preserves_unknown_observations(self):
        for product in CASES["products"]:
            with self.subTest(product=product):
                h, _, _, pool = self.fixture()
                expected = expected_rows()
                self.assertTrue(pool.attribute_legacy(
                    PATH, expected[3]["canonical_key"], product_id=product))
                expected[3]["product_id"] = product
                self.assertEqual(decoded(h), expected)
                self.assertEqual(pool.load(PATH, product_id=product),
                                 [row for row in expected if row.get("product_id") == product])
                self.assertEqual(len(h.atomic_calls), 1)

    def test_no_matching_status_or_attribution_leaves_original_nullable_bytes(self):
        h, _, _, pool = self.fixture()
        before = h.files[PATH]
        self.assertFalse(pool.set_status(PATH, CASES["missing_key"], "ack",
                                         product_id=CASES["products"][0]))
        self.assertFalse(pool.attribute_legacy(PATH, CASES["missing_key"],
                                               product_id=CASES["products"][0]))
        self.assert_no_write(h, before)

    def test_new_null_aliases_remain_strict_in_every_location(self):
        for alias in CASES["aliases"]:
            for location in CASES["new_alias_locations"]:
                with self.subTest(alias=alias, location=location):
                    h, _, _, pool = self.fixture()
                    before = h.files[PATH]
                    demand = self.demand(h)
                    put_alias(demand, location, alias, None)
                    original = copy.deepcopy(demand)
                    with self.assertRaisesRegex(ValueError, "invalid author identity type"):
                        pool.upsert(PATH, demand)
                    self.assertEqual(demand, original)
                    self.assert_no_write(h, before)

    def test_rescore_cannot_introduce_null_aliases_on_new_or_merged_rows(self):
        for target in CASES["rescore_targets"]:
            for alias in CASES["aliases"]:
                for location in ("root", "evidence", "observation_index"):
                    with self.subTest(target=target, alias=alias, location=location):
                        h, _, _, pool = self.fixture()
                        before = h.files[PATH]
                        key = (CASES["rows"][2 if target == "known" else 0]["canonical_key"]
                               if target != "new" else CASES["new_key"])
                        def rescore(row):
                            put_alias(row, location, alias, None)
                            return row
                        with self.assertRaisesRegex(ValueError, "invalid author identity type"):
                            pool.upsert(PATH, self.demand(h, key=key), rescore)
                        self.assert_no_write(h, before)

    def test_stored_unidentified_or_malformed_observation_does_not_gain_legacy_exception(self):
        for field in ("evidence", "observation_index"):
            for identity in CASES["invalid_ids"]:
                with self.subTest(field=field, identity=identity):
                    h, _, _, pool = self.fixture()
                    rows = copy.deepcopy(CASES["rows"])
                    item = rows[0][field][0]
                    if identity is None:
                        del item["observation_id"]
                    else:
                        item["observation_id"] = identity
                    h.files[PATH] = encoded(rows)
                    before = h.files[PATH]
                    with self.assertRaisesRegex(ValueError, "invalid author identity type"):
                        pool.set_status(PATH, rows[2]["canonical_key"], "ack",
                                        product_id=CASES["products"][0])
                    self.assert_no_write(h, before)

    def test_stored_null_alias_outside_canonical_slots_remains_strict(self):
        locations = ("root", "metadata", "authors", "evidence", "observation_index")
        for location in locations:
            for alias in CASES["aliases"]:
                if location in ("evidence", "observation_index") and alias == "author_hash":
                    continue
                with self.subTest(location=location, alias=alias):
                    h, _, _, pool = self.fixture()
                    rows = copy.deepcopy(CASES["rows"])
                    put_alias(rows[0], location, alias, None)
                    h.files[PATH] = encoded(rows)
                    before = h.files[PATH]
                    with self.assertRaisesRegex(ValueError, "invalid author identity type"):
                        pool.set_status(PATH, rows[2]["canonical_key"], "ack",
                                        product_id=CASES["products"][0])
                    self.assert_no_write(h, before)

    def test_stored_competing_alias_cannot_replace_unknown_attribution(self):
        for field in ("evidence", "observation_index"):
            for alias in ("author", "user_id"):
                with self.subTest(field=field, alias=alias):
                    h, _, _, pool = self.fixture()
                    rows = copy.deepcopy(CASES["rows"])
                    rows[0][field][0][alias] = CASES["authors"][2]
                    h.files[PATH] = encoded(rows)
                    before = h.files[PATH]
                    with self.assertRaisesRegex(ValueError, "invalid author identity type"):
                        pool.set_status(PATH, rows[2]["canonical_key"], "ack",
                                        product_id=CASES["products"][0])
                    self.assert_no_write(h, before)

    def test_unsupported_stored_author_types_are_not_normalized(self):
        for field in ("evidence", "observation_index"):
            for value in CASES["unsupported_authors"]:
                with self.subTest(field=field, value=value):
                    h, _, _, pool = self.fixture()
                    rows = copy.deepcopy(CASES["rows"])
                    rows[0][field][0]["author_hash"] = copy.deepcopy(value)
                    h.files[PATH] = encoded(rows)
                    before = h.files[PATH]
                    with self.assertRaisesRegex(ValueError, "invalid author identity type"):
                        pool.set_status(PATH, rows[2]["canonical_key"], "ack",
                                        product_id=CASES["products"][0])
                    self.assert_no_write(h, before)

    def test_both_canonical_typed_id_formats_preserve_unknown_and_counts(self):
        for identity in CASES["typed_ids"]:
            with self.subTest(identity=identity):
                h, _, _, pool = self.fixture()
                rows = copy.deepcopy(CASES["rows"])
                for field in ("evidence", "observation_index"):
                    rows[0][field][0]["observation_id"] = identity
                h.files[PATH] = encoded(rows)
                expected = expected_rows()
                for field in ("evidence", "observation_index"):
                    expected[0][field][0]["observation_id"] = identity
                self.assertTrue(pool.set_status(PATH, rows[2]["canonical_key"], "ack",
                                                product_id=CASES["products"][0]))
                expected[2]["status"] = "ack"
                self.assertEqual(decoded(h), expected)

    def test_conflicting_legacy_attribution_preserves_original_bytes(self):
        for conflict in ("duplicate-legacy", "attributed"):
            with self.subTest(conflict=conflict):
                h, _, _, pool = self.fixture()
                rows = copy.deepcopy(CASES["rows"])
                extra = copy.deepcopy(rows[3])
                if conflict == "attributed":
                    extra["product_id"] = CASES["products"][0]
                rows.append(extra)
                h.files[PATH] = encoded(rows)
                before = h.files[PATH]
                with self.assertRaisesRegex(ValueError, "legacy attribution conflicts"):
                    pool.attribute_legacy(PATH, rows[3]["canonical_key"],
                                          product_id=CASES["products"][0])
                self.assert_no_write(h, before)


if __name__ == "__main__":
    unittest.main()
