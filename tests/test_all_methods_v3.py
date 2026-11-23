"""Exercise v3 budgets, native-method outputs and legacy PNG behavior."""

from collections import Counter
import itertools
import unittest

import torch

import test_auto_reference as t
from sd_webui_difference_cfg import auto_reference as auto


class AllMethodsV3Tests(unittest.TestCase):
    def setUp(self):
        t.diff._BACKEND_IS_NEO = False
        t.skim._BACKEND_IS_NEO = False

    def pair(self, method, mode, gamma, algorithm=auto.ALGORITHM):
        model = t.Model()
        t.diff.apply_difference_cfg(model, 5, method, 1, auto_reference=True,
                                    auto_budget=gamma, auto_algorithm=algorithm)
        t.skim.apply_skimmed_cfg(model, mode, **t.settings(), lin_interp_cfg=3)
        return model, model.model_options["sampler_pre_cfg_function"][-1]

    def exercise(self, device="cpu", inference=False):
        counts = Counter()
        for method, mode, dtype, scale, gamma, seed in itertools.product(
                t.diff._DIFF_METHODS, ("single_scale", "lin_interp"),
                (torch.float16, torch.bfloat16, torch.float32, torch.float64),
                (7.0, 20.0, 30.0), (0.0, 0.03, 0.1, 1.0), range(4)):
            with self.subTest(method=method, mode=mode, dtype=dtype,
                              scale=scale, gamma=gamma, seed=seed, device=device):
                with torch.inference_mode(inference):
                    model, hook = self.pair(method, mode, gamma)
                    args = t.inputs(model.model_options, seed, dtype, scale)
                    args["conds_out"] = [tensor.to(device) for tensor in args["conds_out"]]
                    args["input"] = args["input"].to(device)
                    args["sigma"] = args["sigma"].to(device)
                    model.model_options["sampler_pre_cfg_function"][0](args)
                    delta = model.model_options[auto.KEY]["delta"].clone()
                    prior = [tensor.clone() for tensor in args["conds_out"]]
                    hook(args)
                    state = hook._auto_last_status
                    counts[(method, state)] += 1
                    self.assertIn(state, ("AUTO_APPLIED", "AUTO_NO_CORRECTION", "AUTO_NUMERIC_INVALID"))
                    self.assertNotIn(auto.KEY, model.model_options)
                    self.assertTrue(torch.equal(args["conds_out"][0], prior[0]))
                    if gamma == 0:
                        self.assertEqual(state, "AUTO_NO_CORRECTION")
                    if state != "AUTO_APPLIED":
                        self.assertTrue(torch.equal(args["conds_out"][1], prior[1]))
                        continue
                    reference = hook._auto_last_reference
                    self.assertGreaterEqual(reference, 3)
                    self.assertLess(reference, scale)
                    actual = (1 - scale) * (args["conds_out"][1].to(delta.dtype) - prior[1].to(delta.dtype))
                    allowance = gamma * float(delta.norm())
                    tolerance = auto.EPSILON + allowance * 0.00001
                    self.assertLessEqual(float(actual.norm()), allowance + tolerance)
                    self.assertLessEqual(float((delta * actual).sum()),
                                         gamma * float(delta.norm()) ** 2 + tolerance * float(delta.norm()))
                    # An applied candidate must be the selected native method
                    # at the recorded reference, not a blended substitute.
                    manual_args = dict(args, conds_out=prior)
                    t.OLD_DIFF._make_difference_fn(reference, method, 0)(manual_args)
                    self.assertTrue(torch.equal(args["conds_out"][1], manual_args["conds_out"][1]))
        for method in t.diff._DIFF_METHODS:
            self.assertGreater(counts[(method, "AUTO_APPLIED")], 0)
        print("V3 matrix", device, "inference=" + str(inference), dict(counts))

    def test_cpu_all_methods_budget_matrix(self):
        self.exercise()

    def test_cpu_inference_all_methods_budget_matrix(self):
        self.exercise(inference=True)

    @unittest.skipUnless(torch.cuda.is_available(), "CUDA unavailable")
    def test_cuda_inference_all_methods_budget_matrix(self):
        self.exercise(device="cuda", inference=True)

    def test_v1_v2_non_linear_methods_keep_manual_fallback(self):
        for algorithm, method, mode in itertools.product(
                (auto.LEGACY_ALGORITHM, auto.V2_ALGORITHM),
                ("squared_distance", "root_distance", "absolute_sum"),
                ("single_scale", "lin_interp")):
            model, hook = self.pair(method, mode, 0.03, algorithm)
            args = t.inputs(model.model_options, scale=20)
            model.model_options["sampler_pre_cfg_function"][0](args)
            expected = t.duplicate(args)
            t.OLD_DIFF._make_difference_fn(5, method, 0)(expected)
            hook(args)
            self.assertEqual(hook._auto_last_status, "MANUAL_FALLBACK_UNSUPPORTED")
            self.assertTrue(torch.equal(args["conds_out"][1], expected["conds_out"][1]))

    def test_v3_linear_matches_v2_exactly(self):
        for mode, dtype, seed in itertools.product(
                ("single_scale", "lin_interp"),
                (torch.float16, torch.bfloat16, torch.float32, torch.float64), range(8)):
            results = []
            for algorithm in (auto.V2_ALGORITHM, auto.ALGORITHM):
                model, hook = self.pair("linear_distance", mode, 0.03, algorithm)
                args = t.inputs(model.model_options, seed, dtype, scale=20)
                t.run_chain(model, args)
                results.append((args["conds_out"], hook._auto_last_status, hook._auto_last_reference))
            self.assertEqual(results[0][1:], results[1][1:])
            for left, right in zip(results[0][0], results[1][0]):
                self.assertTrue(torch.equal(left, right))

    def test_degenerate_nonfinite_and_invalid_budget_preserve_outputs(self):
        for method, damage in itertools.product(t.diff._DIFF_METHODS,
                                               ("zero", "nan", "gamma", "floor")):
            model, hook = self.pair(method, "lin_interp", -0.1 if damage == "gamma" else 0.03)
            args = t.inputs(model.model_options, scale=20)
            if damage == "zero":
                for tensor in args["conds_out"]:
                    tensor.zero_()
            model.model_options["sampler_pre_cfg_function"][0](args)
            record = model.model_options[auto.KEY]
            if damage == "nan":
                record["delta"].fill_(float("nan"))
            if damage == "floor":
                record["skimming_cfg"] = 30
            before = [tensor.clone() for tensor in args["conds_out"]]
            hook(args)
            self.assertNotEqual(hook._auto_last_status, "AUTO_APPLIED")
            for left, right in zip(before, args["conds_out"]):
                self.assertTrue(torch.equal(left, right))


if __name__ == "__main__":
    unittest.main(verbosity=2)
