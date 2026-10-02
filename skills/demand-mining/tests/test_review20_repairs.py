"""Generated full-module regressions for domain factors and direct CLI bootstrap."""
import argparse
import builtins
import copy
import io
from pathlib import Path
import types
import unittest

from test_review19_repairs import Harness, ROOT, SCRIPTS, CASES as OLD_CASES, generator

CASES = generator.review20_cases()


class DomainHarness(Harness):
    def importer(self, name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"html", "contextvars", "shlex"}:
            return builtins.__import__(name, globals, locals, fromlist, level)
        return super().importer(name, globals, locals, fromlist, level)

    def scoring(self):
        lib = self.load("lib")
        score = self.load("score")
        self.load("data_safety")
        self.load("digest")
        self.load("push_card")
        return lib, score, self.load("run")


class StartupHarness(Harness):
    def __init__(self, argv, *, missing=None, console=True, behavior="normal"):
        super().__init__(behavior)
        self.argv, self.missing = list(argv), missing
        self.import_attempts, self.parsed = [], None
        if not console:
            self.sys.stdout = self.sys.stderr = None
        self.original_streams = self.sys.stdout, self.sys.stderr
        h = self

        class RealParser(argparse.ArgumentParser):
            def __init__(self, **kwargs):
                super().__init__(prog="demand_bot.py", **kwargs)

            def parse_args(self, args=None, namespace=None):
                h.parsed = super().parse_args(h.argv if args is None else args, namespace)
                return h.parsed

            def _print_message(self, message, file=None):
                target = h.sys.stderr if file is argparse._sys.stderr else h.sys.stdout
                if target is not None:
                    target.write(message)

        class Loader:
            def exec_module(self, module):
                from test_review19_repairs import source_bytes
                module.__dict__["__builtins__"] = {
                    **vars(builtins), "__import__": h.importer, "open": h.open}
                h.modules[module.__name__] = module
                exec(compile(source_bytes(Path(module.__file__)), module.__file__, "exec"), module.__dict__)

        def spec_from_file_location(name, path):
            return types.SimpleNamespace(name=name, origin=path, loader=Loader())

        def module_from_spec(spec):
            module = types.ModuleType(spec.name)
            module.__file__ = spec.origin
            return module

        self.effects["argparse"] = types.SimpleNamespace(ArgumentParser=RealParser)
        self.effects["importlib.util"] = types.SimpleNamespace(
            spec_from_file_location=spec_from_file_location, module_from_spec=module_from_spec)

    def importer(self, name, globals=None, locals=None, fromlist=(), level=0):
        if name in ("discord", "llmcall"):
            self.import_attempts.append(name)
            if name == self.missing:
                raise ModuleNotFoundError(CASES["missing_message"] + " '" + name + "'", name=name)
        return super().importer(name, globals, locals, fromlist, level)

    def prepare(self):
        self.load("lib")
        self.load("data_safety")
        self.load("private_log")
        self.load("score")

    def direct(self):
        self.prepare()
        return self.load("__main__", SCRIPTS / "demand_bot.py")


class DomainFactorTests(unittest.TestCase):
    def test_generated_domain_pools_and_weighted_numeric_parity(self):
        h = DomainHarness()
        lib, score, run = h.scoring()
        digest, push = h.modules["digest"], h.modules["push_card"]
        for case in CASES["scores"]:
            with self.subTest(case=case["name"]):
                cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
                if case["weights_mode"] == "missing":
                    cfg["scoring"].pop("rice_weights", None)
                else:
                    cfg["scoring"]["rice_weights"] = copy.deepcopy(case["weights"])
                proposal = copy.deepcopy(case["proposal"])
                before = copy.deepcopy((proposal, cfg))
                scored = score.score_demand(proposal, cfg)
                card = run.build_card(proposal, cfg, "synthetic-review20")
                self.assertEqual(scored["rice_domain"], case["domain"])
                self.assertEqual(card["rice_domain"], case["domain"])
                self.assertEqual(card["rice_weights"], case["effective_weights"])
                self.assertEqual(scored["rice_weights"], case["effective_weights"])
                self.assertEqual(card["rice"], scored["rice"])
                self.assertEqual(card["rice"]["rice_raw"], case["rice_raw"])
                self.assertEqual(card["final_score"], case["final_score"])
                self.assertEqual(score._final_map([proposal], None, cfg)[proposal["id"]],
                                 case["final_score"])
                self.assertEqual(score._final_map([proposal], case["weights"], cfg)[proposal["id"]],
                                 case["final_score"])
                pools = digest.split_pools([card], cfg)
                self.assertEqual([key for key, values in pools.items() if values], [case["pool"]])
                queue = digest.iteration_queue([card], cfg)
                self.assertEqual(queue[0]["rice_domain"], case["domain"])
                self.assertEqual(queue[0]["rice"]["rice_raw"], case["rice_raw"])
                domain_text = score.domain_factor_text(card)
                report = digest.build_markdown([card], date=CASES["date"], cfg=cfg)
                text = push.render_text(card)
                embed = push.build_embed(card)
                fields = {field["name"]: field["value"] for field in embed["fields"]}
                self.assertEqual(fields[CASES["labels"]["domain"]], domain_text)
                for rendered in (report, text):
                    self.assertIn(domain_text, rendered)
                    self.assertIn(CASES["labels"]["weighted"], rendered)
                self.assertIn(str(case["rice_raw"]), fields[CASES["labels"]["weighted"]])
                self.assertEqual((proposal, cfg), before)

    def test_weights_do_not_change_domain_pool_membership(self):
        h = DomainHarness()
        lib, score, run = h.scoring()
        for case in CASES["scores"]:
            with self.subTest(case=case["name"]):
                cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
                if case["weights_mode"] == "missing":
                    cfg["scoring"].pop("rice_weights", None)
                else:
                    cfg["scoring"]["rice_weights"] = copy.deepcopy(case["weights"])
                card = run.build_card(case["proposal"], cfg, "synthetic-pool20")
                pools = h.modules["digest"].split_pools([card], cfg)
                self.assertEqual([name for name, cards in pools.items() if cards], [case["pool"]])

    def test_fixed_numeric_rank_controls_remain_identical(self):
        h = DomainHarness()
        lib, score, run = h.scoring()
        for case in CASES["scores"]:
            with self.subTest(case=case["name"]):
                cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
                if case["weights_mode"] == "missing":
                    cfg["scoring"].pop("rice_weights", None)
                else:
                    cfg["scoring"]["rice_weights"] = copy.deepcopy(case["weights"])
                scored = score.score_demand(case["proposal"], cfg)
                card = run.build_card(case["proposal"], cfg, "synthetic-rank20")
                self.assertEqual(scored["rice"]["rice_raw"], case["rice_raw"])
                self.assertEqual(scored["final_score"], case["final_score"])
                self.assertEqual(card["rice"], scored["rice"])
                self.assertEqual(card["final_score"], scored["final_score"])

    def test_live_rescorer_preserves_domain_and_weight_provenance(self):
        h = DomainHarness()
        lib, score, run = h.scoring()
        h.load("private_log")
        bot = h.load("demand_bot")
        for case in CASES["scores"]:
            with self.subTest(case=case["name"]):
                cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
                if case["weights_mode"] == "missing":
                    cfg["scoring"].pop("rice_weights", None)
                else:
                    cfg["scoring"]["rice_weights"] = copy.deepcopy(case["weights"])
                row = copy.deepcopy(case["proposal"])
                result = bot._rescorer(cfg)(row)
                self.assertIs(result, row)
                self.assertEqual(result["rice_domain"], case["domain"])
                self.assertEqual(result["rice_weights"], case["effective_weights"])
                self.assertEqual(result["rice"]["rice_raw"], case["rice_raw"])
                self.assertEqual(result["final_score"], case["final_score"])

    def test_legacy_requires_explicit_unweighted_provenance_without_losing_rank(self):
        h = DomainHarness()
        lib, score, run = h.scoring()
        digest, push = h.modules["digest"], h.modules["push_card"]
        cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
        for case in CASES["legacy_cases"]:
            with self.subTest(case=case):
                generated = next(item for item in CASES["scores"] if item["name"] == case["source"])
                card = run.build_card(generated["proposal"], cfg, "synthetic-legacy")
                card.pop("rice_domain", None)
                card.pop("rice_weights", None)
                card["kano"] = case["kano"]
                if case["provenance"]:
                    card["rice_factor_semantics"] = CASES["legacy_provenance"]
                before = copy.deepcopy(card)
                pools = digest.split_pools([card], cfg)
                self.assertEqual([key for key, values in pools.items() if values], [case["pool"]])
                queue = digest.iteration_queue([card], cfg)
                self.assertEqual(queue[0]["rice"]["final_score"], card["final_score"])
                if not case["provenance"]:
                    self.assertTrue(all(value is None for value in score.domain_factors(card).values()))
                    for rendered in (digest.build_markdown([card], date=CASES["date"], cfg=cfg),
                                     push.render_text(card), str(push.build_embed(card))):
                        self.assertIn(CASES["legacy_notice"], rendered)
                        self.assertIn("re-score", rendered)
                self.assertEqual(card, before)


class DirectBootstrapTests(unittest.TestCase):
    def test_missing_optional_dependencies_preserve_primary_error_and_private_diagnostic(self):
        for case in CASES["startup"]:
            with self.subTest(case=case):
                argv = list(CASES["normal_args"])
                if case["log"]:
                    argv += ["--log-file", CASES["log_path"]]
                h = StartupHarness(argv, missing=case["dependency"], console=case["console"])
                with self.assertRaises(ModuleNotFoundError) as caught:
                    h.direct()
                self.assertEqual(caught.exception.name, case["dependency"])
                self.assertIsNotNone(h.parsed)
                if case["log"]:
                    content = h.files[CASES["log_path"]].decode()
                    self.assertIn("ModuleNotFoundError", content)
                    self.assertIn(case["dependency"], content)
                    self.assertTrue(h.writes)
                    self.assertTrue(all(state == "PRIVATE" for _, _, state in h.writes))
                else:
                    self.assertFalse(h.writes)
                self.assertEqual((h.sys.stdout, h.sys.stderr), h.original_streams)

    def test_help_and_invalid_choices_are_parsed_before_optional_imports(self):
        for argv, expected in [(CASES["help_args"], 0), (CASES["invalid_args"], 2)]:
            for console in (True, False):
                with self.subTest(argv=argv, console=console):
                    h = StartupHarness(argv, missing="discord", console=console)
                    with self.assertRaises(SystemExit) as caught:
                        h.direct()
                    self.assertEqual(caught.exception.code, expected)
                    self.assertFalse(h.import_attempts)
                    self.assertFalse(h.writes)
                    self.assertEqual((h.sys.stdout, h.sys.stderr), h.original_streams)

    def test_direct_normal_modes_use_actual_parser_and_restore_streams(self):
        for mode in CASES["normal_modes"]:
            for console in (True, False):
                with self.subTest(mode=mode, console=console):
                    argv = ["--mode", mode, "--interval", "15", "--display-interval", "30",
                            "--log-file", CASES["log_path"]]
                    h = StartupHarness(argv, console=console)
                    with self.assertRaises(SystemExit) as caught:
                        h.direct()
                    self.assertEqual(caught.exception.code, 0)
                    self.assertEqual(h.parsed.mode, mode)
                    self.assertEqual((h.parsed.interval, h.parsed.display_interval), (15.0, 30.0))
                    content = h.files[CASES["log_path"]].decode()
                    self.assertTrue(all(message in content for message in OLD_CASES["log_messages"]))
                    self.assertEqual((h.sys.stdout, h.sys.stderr), h.original_streams)

    def test_import_preserves_public_api_without_parsing_or_logging(self):
        h = StartupHarness(CASES["invalid_args"])
        h.prepare()
        module = h.load("demand_bot")
        for name in CASES["public_api"]:
            self.assertTrue(callable(getattr(module, name)))
        self.assertIsNone(h.parsed)
        self.assertFalse(h.writes)
        self.assertEqual((h.sys.stdout, h.sys.stderr), h.original_streams)

    def test_refused_log_prevents_optional_imports(self):
        h = StartupHarness(CASES["normal_args"] + ["--log-file", CASES["log_path"]], console=False)
        h.visibility = "PUBLIC"
        with self.assertRaises(SystemExit):
            h.direct()
        self.assertFalse(h.import_attempts)
        self.assertFalse(h.writes)
        self.assertEqual((h.sys.stdout, h.sys.stderr), h.original_streams)

    def test_direct_revocation_prevents_next_append_and_restores_none_streams(self):
        h = StartupHarness(CASES["normal_args"] + ["--log-file", CASES["log_path"]],
                           console=False, behavior="direct-revoke")
        with self.assertRaises(SystemExit):
            h.direct()
        content = h.files[CASES["log_path"]].decode()
        self.assertIn(OLD_CASES["log_messages"][0], content)
        self.assertNotIn(OLD_CASES["log_messages"][1], content)
        self.assertTrue(all(state == "PRIVATE" for _, _, state in h.writes))
        self.assertTrue(all(state == "PRIVATE" for _, state in h.opens))
        self.assertEqual((h.sys.stdout, h.sys.stderr), h.original_streams)


if __name__ == "__main__":
    unittest.main()
