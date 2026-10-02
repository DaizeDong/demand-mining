"""Inert regressions for complete private destinations and headline capacity."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import types
import unittest

ROOT = Path(__file__).parents[3]
SCRIPTS = ROOT / "skills/demand-mining/scripts"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


CASES = module("_review18_fixture_generator", ROOT / "tools/make_fixtures.py").review18_cases()
PRIOR = module("_review18_inert_support", Path(__file__).with_name("test_review17_repairs.py"))


def transport(case):
    safety = module("_review18_data_safety", SCRIPTS / "data_safety.py")
    root = ROOT.resolve()
    calls, visibility = [], []
    state = deepcopy(case)

    def git(directory, *args, **kwargs):
        if args == ("rev-parse", "--show-toplevel"):
            output = str(root)
        elif args == ("config", "--null", "--list"):
            output = "".join(key + "\n" + value + "\0" for key, value in state["config"])
        elif args[:2] == ("remote", "get-url"):
            name = args[-1]
            calls.append((name, "--push" in args))
            urls = state["urls"][name]
            output = urls[-1] if "--push" in args else urls[0]
        else:
            raise AssertionError("unexpected synthetic Git operation")
        return types.SimpleNamespace(stdout=output)

    def visible(repository):
        visibility.append(repository)
        if repository == state.get("unknown"):
            return "UNKNOWN"
        return "PUBLIC" if repository == "example-owner/public-store" else "PRIVATE"

    safety.git, safety._visibility = git, visible
    safety.os = types.SimpleNamespace(environ={})
    return safety, root, state, calls, visibility


class DestinationCoverageTests(unittest.TestCase):
    def test_all_configured_and_selected_destinations_require_private_proof(self):
        for case in CASES["transport"]:
            with self.subTest(case=case["name"]):
                safety, root, state, calls, visibility = transport(case)
                if case["accept"]:
                    proof = safety._transport_proof(root)
                    self.assertEqual(proof["repository"], "example-owner/private-store")
                    self.assertEqual(set(visibility), set(case.get("repositories", ["example-owner/private-store"])))
                    self.assertEqual(set(calls), {(name, direction) for name in case["urls"]
                                                  for direction in (False, True)})
                else:
                    with self.assertRaises(safety.DestinationError):
                        safety._transport_proof(root)

    def test_secondary_destination_change_invalidates_the_transport_binding(self):
        case = next(case for case in CASES["transport"] if case["name"] == "private-secondary")
        safety, root, state, calls, visibility = transport(case)
        before = safety._transport_proof(root)
        state["urls"]["archive"] = ["https://github.com/example-owner/private-mirror.git"]
        after = safety.require_private_push(root, before["repository"])
        self.assertNotEqual(after["sha256"], before["sha256"])
        self.assertIn("example-owner/private-mirror", visibility)


class HeadlineCapacityTests(unittest.TestCase):
    def product(self):
        support = PRIOR.BatchConservationTests()
        support.setUp()
        support.p.cfg["push"]["max_per_day"] = 1
        return support

    def candidates(self, kano):
        noise, useful = deepcopy(CASES["noise"]), deepcopy(CASES["useful"])
        noise["kano"] = kano
        return [noise, useful]

    def test_cut_cannot_consume_capacity_or_proposed_push_bookkeeping(self):
        for kano in CASES["cut_kano"]:
            for reverse in (False, True):
                with self.subTest(kano=kano, reverse=reverse):
                    support = self.product()
                    candidates = self.candidates(kano)
                    plan = support.prepare(list(reversed(candidates)) if reverse else candidates)
                    cards = {c["title"]: c for c in plan["cards"]}
                    noise, useful = candidates
                    self.assertEqual(cards[noise["title"]]["tier"], "cut")
                    self.assertGreater(cards[noise["title"]]["final_score"], cards[useful["title"]]["final_score"])
                    self.assertEqual(plan["result"]["proposed_push"], [useful["title"]])
                    self.assertIn(useful["title"], plan["headlines"])
                    self.assertNotIn(noise["title"], plan["headlines"])
                    self.assertEqual({row["card"]["title"]: row["pushed"] for row in plan["mutations"]},
                                     {noise["title"]: False, useful["title"]: True})

    def test_finalizer_receipt_push_counts_and_replay_match_rendered_selection(self):
        for kano in CASES["cut_kano"]:
            with self.subTest(kano=kano):
                support = self.product()
                candidates = self.candidates(kano)

                def invoke():
                    return support.p.final.finalize(candidates, support.p.cfg, support.ledger, False,
                        "synthetic-review18", "synthetic/archive", identity=support.identity,
                        attempt_id="synthetic-attempt")

                first = invoke()
                self.assertEqual(first["status"], "complete")
                self.assertEqual(first["pushed"], [candidates[1]["title"]])
                self.assertEqual(len(support.p.sends), 1)
                self.assertIn(candidates[1]["title"], support.p.sends[0])
                self.assertNotIn(candidates[0]["title"], support.p.sends[0])
                prefix = support.p.dd.EXT
                for candidate, count in zip(candidates, (0, 1)):
                    self.assertEqual(support.ledger.rows[candidate["canonical_key"]]["ext"][prefix + "push_count"], count)
                before = deepcopy((support.ledger.rows, support.ledger.writes,
                                   support.ledger.watermarks, support.p.sends))
                self.assertEqual(invoke()["pushed"], first["pushed"])
                self.assertEqual((support.ledger.rows, support.ledger.writes,
                                  support.ledger.watermarks, support.p.sends), before)

    def test_tier0_retains_priority_below_the_score_floor(self):
        support = self.product()
        urgent = deepcopy(CASES["urgent"])
        plan = support.prepare([deepcopy(CASES["useful"]), urgent])
        card = next(c for c in plan["cards"] if c["title"] == urgent["title"])
        self.assertEqual(card["tier"], "tier0")
        self.assertLess(card["final_score"], support.p.cfg["scoring"]["min_score_to_push"])
        self.assertEqual(plan["result"]["proposed_push"], [urgent["title"]])
        self.assertIn(urgent["title"], plan["headlines"])

    def test_all_cut_day_has_no_proposed_or_planned_push(self):
        support = self.product()
        noise = self.candidates("indifferent")[0]
        plan = support.prepare([noise])
        self.assertEqual(plan["result"]["proposed_push"], [])
        self.assertTrue(all(not row["pushed"] for row in plan["mutations"]))
        self.assertNotIn(noise["title"], plan["headlines"])


if __name__ == "__main__":
    unittest.main()
