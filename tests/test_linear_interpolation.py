"""Focused CPU tests for CFG 20 with SkimmedCFG Linear Interpolation 3."""

from collections import Counter
import itertools
import unittest

import torch

import test_auto_reference as t
from sd_webui_difference_cfg.auto_reference import ALGORITHM, LEGACY_ALGORITHM, KEY


class LinearInterpolationTests(unittest.TestCase):
    def setUp(self):
        t.diff._BACKEND_IS_NEO = False
        t.skim._BACKEND_IS_NEO = False

    def pair(self, auto=True, method="linear_distance", algorithm=ALGORITHM,
             gamma=0.1, skim_scale=3):
        model = t.Model()
        t.diff.apply_difference_cfg(model, 5, method, 1, auto_reference=auto,
                                    auto_algorithm=algorithm, auto_budget=gamma)
        t.skim.apply_skimmed_cfg(model, "lin_interp", lin_interp_cfg=skim_scale)
        return model, model.model_options["sampler_pre_cfg_function"][-1]

    def test_manual_cfg20_li3_all_methods(self):
        cases = 0
        for method, dtype, seed in itertools.product(
                t.diff._DIFF_METHODS,
                (torch.float16, torch.bfloat16, torch.float32, torch.float64), range(8)):
            model, fn = self.pair(auto=False, method=method)
            args = t.inputs(model.model_options, seed, dtype, scale=20)
            old = t.duplicate(args)
            t.OLD_SKIM._make_lin_interp_fn(3)(old)
            t.OLD_DIFF._make_difference_fn(5, method, 0)(old)
            t.run_chain(model, args)
            for actual, expected in zip(args["conds_out"], old["conds_out"]):
                self.assertTrue(torch.equal(actual, expected))
            self.assertNotIn(KEY, model.model_options)
            cases += 1
        print("Linear Interpolation manual exact cases:", cases)

    def test_observation_preserves_both_mask_passes(self):
        for dtype, seed in itertools.product(
                (torch.float16, torch.bfloat16, torch.float32, torch.float64), range(8)):
            model, fn = self.pair()
            args = t.inputs(model.model_options, seed, dtype, scale=20)
            old = t.duplicate(args)
            calc_dtype = torch.float64 if dtype == torch.float64 else torch.float32
            c, u = old["conds_out"]
            before = u.to(calc_dtype) + 20 * (c.to(calc_dtype) - u.to(calc_dtype))
            t.OLD_SKIM._make_lin_interp_fn(3)(old)
            producer = model.model_options["sampler_pre_cfg_function"][0]
            producer(args)
            rec = model.model_options[KEY]
            self.assertEqual(rec["state"], "APPLIED")
            self.assertEqual(rec["skimming_cfg"], 3)
            self.assertEqual(rec["mode"], "lin_interp")
            c, u = old["conds_out"]
            after = u.to(calc_dtype) + 20 * (c.to(calc_dtype) - u.to(calc_dtype))
            self.assertTrue(torch.equal(rec["delta"], after - before))
            for actual, expected in zip(args["conds_out"], old["conds_out"]):
                self.assertTrue(torch.equal(actual, expected))

    def test_auto_cfg20_li3_actual_length_and_alignment_budget(self):
        counts = Counter()
        for dtype, seed, gamma in itertools.product(
                (torch.float16, torch.bfloat16, torch.float32, torch.float64),
                range(12), (0.1, 0.25, 0.5)):
            model, fn = self.pair(gamma=gamma)
            args = t.inputs(model.model_options, seed, dtype, scale=20)
            model.model_options["sampler_pre_cfg_function"][0](args)
            ds = model.model_options[KEY]["delta"]
            before = args["conds_out"][1].clone()
            fn(args)
            counts[fn._auto_last_status] += 1
            self.assertNotIn(KEY, model.model_options)
            self.assertTrue(torch.isfinite(args["conds_out"][1]).all())
            if fn._auto_last_status == "AUTO_APPLIED":
                self.assertTrue(3 <= fn._auto_last_reference <= 20)
                actual = -19 * (args["conds_out"][1].to(ds.dtype) - before.to(ds.dtype))
                norm = float(ds.norm())
                tol = 0.000000000001 + gamma * norm * 0.00001
                self.assertLessEqual(float(actual.norm()), gamma * norm + tol)
                self.assertLessEqual(float((actual * ds).sum()), gamma * norm * norm + tol * norm)
            else:
                self.assertIn(fn._auto_last_status, ("AUTO_NUMERIC_INVALID", "AUTO_NO_CORRECTION"))
                self.assertTrue(torch.equal(before, args["conds_out"][1]))
        self.assertGreater(counts["AUTO_APPLIED"], 0)
        print("Linear Interpolation Auto CFG20/LI3 cases=144:", dict(counts))

    def test_prior_algorithm_keeps_linear_interpolation_manual(self):
        model, fn = self.pair(algorithm=LEGACY_ALGORITHM)
        args = t.inputs(model.model_options, scale=20)
        old = t.duplicate(args)
        t.OLD_SKIM._make_lin_interp_fn(3)(old)
        t.OLD_DIFF._make_difference_fn(5, "linear_distance", 0)(old)
        t.run_chain(model, args)
        self.assertEqual(fn._auto_last_status, "MANUAL_FALLBACK_UNSUPPORTED")
        self.assertTrue(torch.equal(args["conds_out"][1], old["conds_out"][1]))

    def test_unknown_or_mismatched_mode_is_not_accepted(self):
        model, fn = self.pair()
        args = t.inputs(model.model_options, scale=20)
        model.model_options["sampler_pre_cfg_function"][0](args)
        model.model_options[KEY]["mode"] = "single_scale"
        prior = args["conds_out"][1].clone()
        fn(args)
        self.assertEqual(fn._auto_last_status, "AUTO_CONTRACT_INVALID")
        self.assertTrue(torch.equal(prior, args["conds_out"][1]))

    def test_linear_interpolation_has_no_single_scale_gate(self):
        model, fn = self.pair()
        t.skim.apply_skimmed_cfg(model, "lin_interp", lin_interp_cfg=3,
                                start_at_percentage=0.9, end_at_percentage=0.1,
                                skimming_cfg=99, full_skim_negative=True)
        args = t.inputs(model.model_options, scale=20)
        model.model_options["sampler_pre_cfg_function"][0](args)
        self.assertEqual(model.model_options[KEY]["state"], "APPLIED")
        self.assertEqual(model.model_options[KEY]["skimming_cfg"], 3)

    def test_zero_budget_and_targets_at_or_above_cfg(self):
        for gamma, target in ((0, 3), (0.1, 20), (0.1, 30)):
            model, fn = self.pair(gamma=gamma, skim_scale=target)
            args = t.inputs(model.model_options, scale=20)
            model.model_options["sampler_pre_cfg_function"][0](args)
            before = args["conds_out"][1].clone()
            fn(args)
            self.assertEqual(fn._auto_last_status, "AUTO_NO_CORRECTION")
            self.assertTrue(torch.equal(before, args["conds_out"][1]))

    def test_missing_uncond_and_nonfinite_record(self):
        model, fn = self.pair()
        args = t.inputs(model.model_options, scale=20)
        args["conds_out"][1] = None
        t.run_chain(model, args)
        self.assertEqual(fn._auto_last_status, "MANUAL_FALLBACK_SKIM_SKIPPED")
        model, fn = self.pair()
        args = t.inputs(model.model_options, scale=20)
        model.model_options["sampler_pre_cfg_function"][0](args)
        model.model_options[KEY]["delta"].fill_(float("nan"))
        before = args["conds_out"][1].clone()
        fn(args)
        self.assertEqual(fn._auto_last_status, "AUTO_NUMERIC_INVALID")
        self.assertTrue(torch.equal(before, args["conds_out"][1]))

    def test_single_scale_v1_and_v2_remain_identical(self):
        results = []
        for algorithm in (LEGACY_ALGORITHM, ALGORITHM):
            model = t.Model()
            t.diff.apply_difference_cfg(model, 5, "linear_distance", 1,
                                        auto_reference=True, auto_algorithm=algorithm)
            t.skim.apply_skimmed_cfg(model, "single_scale", **t.settings())
            args = t.inputs(model.model_options, scale=20)
            t.run_chain(model, args)
            results.append(args["conds_out"])
        for v1, v2 in zip(*results):
            self.assertTrue(torch.equal(v1, v2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
