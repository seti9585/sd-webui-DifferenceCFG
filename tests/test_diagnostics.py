"""Verify diagnostic values, lifecycle, output equivalence and sync calls."""

from collections import Counter
from contextlib import ExitStack, redirect_stdout
import ast
import io
import math
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import torch

import test_auto_reference as t
from sd_webui_difference_cfg import auto_reference as auto
from sd_webui_difference_cfg.diagnostics import AutoDiagnostics, STATS_KEY

BASELINE = t.load_file("baseline_auto_reference_v2", t.ROOT / "tests" / "baseline_auto_reference_v2.py")


def script_module():
    modules = types.ModuleType("modules")
    modules.scripts = types.SimpleNamespace(Script=object, AlwaysVisible=True)
    modules.script_callbacks = types.SimpleNamespace(on_before_ui=lambda fn: None)
    with patch.dict(sys.modules, {"modules": modules, "gradio": types.ModuleType("gradio")}):
        return t.load_file("diagnostic_test_ui", t.ROOT / "scripts" / "sd_webui_difference_cfg.py")


def build(factory, diagnostics, gamma=1, method="linear_distance", algorithm=auto.V2_ALGORITHM):
    model = t.Model()
    manual = t.diff._make_difference_fn(7, method, 0)
    hook = factory(manual, t.diff.interpolated_scales, method, 0, gamma, algorithm, diagnostics)
    model.model_options["sampler_pre_cfg_function"] = [hook]
    t.skim.apply_skimmed_cfg(model, "lin_interp", lin_interp_cfg=3)
    return model, hook


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        t.diff._BACKEND_IS_NEO = False
        t.skim._BACKEND_IS_NEO = False

    def test_exact_summary_and_floor_denominator(self):
        stats = AutoDiagnostics()
        for value, floor in ((3, 3), (9, 3), (6, 6)):
            stats.record_reference(value, floor)
        stats["MANUAL_FALLBACK_UNSUPPORTED"] = 20
        stats["AUTO_NO_CORRECTION"] = 10
        self.assertEqual(stats.reference_min, 3)
        self.assertEqual(stats.reference_mean, 6)
        self.assertEqual(stats.reference_max, 9)
        self.assertEqual(stats.applied_count, 3)
        self.assertEqual(stats.floor_hits, 2)
        self.assertEqual(stats.reference_summary(),
                         "count=3; min=3; mean=6; max=9; floor_hits=2; floor_rate=66.666666667%")

    def test_empty_and_invalid_values_do_not_sync(self):
        stats = AutoDiagnostics()
        class ConversionTrap:
            def __float__(self):
                raise AssertionError("A diagnostic must not convert tensor-like values")
        for reference in (torch.tensor(3), ConversionTrap(), float("nan"), float("inf")):
            stats.record_reference(reference, 3)
        self.assertEqual(stats.applied_count, 0)
        self.assertIn("floor_rate=N/A", stats.reference_summary())
        self.assertIn("min=N/A", stats.reference_summary())

    def test_floor_tolerance_and_per_call_floor(self):
        stats = AutoDiagnostics()
        stats.record_reference(3.0000000005, 3)
        stats.record_reference(3.00000001, 3)
        stats.record_reference(7, 7)
        self.assertEqual(stats.floor_hits, 2)
        self.assertEqual(stats.applied_count, 3)

    def test_only_auto_applied_contributes(self):
        for gamma, method, algorithm, expected in (
            (0, "linear_distance", auto.ALGORITHM, "AUTO_NO_CORRECTION"),
            (1, "squared_distance", auto.V2_ALGORITHM, "MANUAL_FALLBACK_UNSUPPORTED"),
            (1, "linear_distance", "unknown", "AUTO_ALGORITHM_UNSUPPORTED"),
        ):
            stats = AutoDiagnostics()
            model, hook = build(auto.make_auto_hook, stats, gamma, method, algorithm)
            t.run_chain(model, t.inputs(model.model_options, scale=20))
            self.assertEqual(hook._auto_last_status, expected)
            self.assertEqual(stats.applied_count, 0)
        for failure in ("missing", "nan", "gate"):
            stats = AutoDiagnostics()
            model, hook = build(auto.make_auto_hook, stats)
            args = t.inputs(model.model_options, scale=20)
            model.model_options["sampler_pre_cfg_function"][0](args)
            if failure == "missing":
                model.model_options.pop(auto.KEY)
            elif failure == "nan":
                model.model_options[auto.KEY]["delta"].fill_(float("nan"))
            else:
                args["sigma"] = torch.tensor([0.0])
            hook(args)
            self.assertNotEqual(hook._auto_last_status, "AUTO_APPLIED")
            self.assertEqual(stats.applied_count, 0)

    def test_outputs_and_statistics_match_prechange_auto(self):
        all_cases = 0
        for dtype in (torch.float16, torch.bfloat16, torch.float32, torch.float64):
            for gamma in (0, 0.1, 1):
                stats = AutoDiagnostics()
                current, current_hook = build(auto.make_auto_hook, stats, gamma)
                original, old_hook = build(BASELINE.make_auto_hook, {}, gamma)
                references = []
                floors = 0
                for seed in range(8):
                    current_args = t.inputs(current.model_options, seed, dtype, scale=20)
                    original_args = t.inputs(original.model_options, seed, dtype, scale=20)
                    t.run_chain(current, current_args)
                    t.run_chain(original, original_args)
                    self.assertEqual(current_hook._auto_last_status, old_hook._auto_last_status)
                    self.assertEqual(current_hook._auto_last_reference, old_hook._auto_last_reference)
                    for actual, expected in zip(current_args["conds_out"], original_args["conds_out"]):
                        self.assertTrue(torch.equal(actual, expected))
                    if old_hook._auto_last_status == "AUTO_APPLIED":
                        references.append(old_hook._auto_last_reference)
                        floors += math.isclose(old_hook._auto_last_reference, 3,
                                               rel_tol=0, abs_tol=0.000000001)
                    all_cases += 1
                self.assertEqual(stats.applied_count, len(references))
                self.assertEqual(stats.floor_hits, floors)
                if references:
                    self.assertEqual(stats.reference_min, min(references))
                    self.assertEqual(stats.reference_max, max(references))
                    self.assertAlmostEqual(stats.reference_mean, sum(references) / len(references), places=12)
        print("Diagnostic before/after output equivalence cases:", all_cases)

    def test_no_additional_tensor_scalar_or_transfer_calls(self):
        def measure(factory, diagnostics, gamma):
            model, hook = build(factory, diagnostics, gamma)
            args = t.inputs(model.model_options, scale=20)
            counts = Counter()
            with ExitStack() as stack:
                for name in ("item", "tolist", "cpu", "numpy", "to", "__float__", "__bool__"):
                    original = getattr(torch.Tensor, name)
                    def wrapper(tensor, *args, _name=name, _original=original, **kwargs):
                        counts[_name] += 1
                        return _original(tensor, *args, **kwargs)
                    stack.enter_context(patch.object(torch.Tensor, name, wrapper))
                t.run_chain(model, args)
            return counts
        for gamma in (0, 0.1, 1):
            before = measure(BASELINE.make_auto_hook, {}, gamma)
            after = measure(auto.make_auto_hook, AutoDiagnostics(), gamma)
            self.assertEqual(before, after)
            print("Tensor conversion/transfer call counts unchanged, budget", gamma, ":", dict(after))

    def test_solver_ast_and_constants_unchanged(self):
        current = ast.parse(Path(auto.__file__).read_text(encoding="utf-8-sig"))
        original = ast.parse((t.ROOT / "tests" / "baseline_auto_reference_v2.py").read_text(encoding="utf-8"))
        def function(tree, name):
            return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        self.assertEqual(ast.dump(function(current, "budget_reference")),
                         ast.dump(function(original, "budget_reference")))
        self.assertEqual(auto.DEFAULT_BUDGET, 0.10)
        self.assertEqual(auto.V2_ALGORITHM, BASELINE.ALGORITHM)
        self.assertEqual(auto.EPSILON, BASELINE.EPSILON)
        self.assertEqual(auto.MAX_CFG, BASELINE.MAX_CFG)

    def test_generation_reset_and_batch_metadata_snapshot(self):
        ui = script_module()
        script = ui.DifferenceCFGScript()
        p = types.SimpleNamespace(extra_generation_params={},
                                  sd_model=types.SimpleNamespace(forge_objects=types.SimpleNamespace(unet=t.Model())))
        config = (True, 7, "linear_distance", 1, True, 1, auto.ALGORITHM)
        script.process(p, *config)
        old_stats = script.auto_diagnostics
        for batch_index in range(2):
            script.process_before_every_sampling(p, *config)
            model = p.sd_model.forge_objects.unet
            t.skim.apply_skimmed_cfg(model, "lin_interp", lin_interp_cfg=3)
            t.run_chain(model, t.inputs(model.model_options, batch_index, scale=20))
            script.postprocess_batch(p, batch_number=batch_index)
            snapshot = p.extra_generation_params[STATS_KEY]
            self.assertIn("count=%d;" % (batch_index + 1), snapshot)
            if batch_index == 0:
                first_png_snapshot = snapshot
        self.assertIn("count=1;", first_png_snapshot)
        output = io.StringIO()
        with redirect_stdout(output):
            script.postprocess(p, None, *config)
        self.assertIn("[DifferenceCFG Auto Reference] " + snapshot, output.getvalue())
        self.assertEqual(old_stats.applied_count, 2)
        # Reusing a processing object must not preserve old PNG diagnostic keys.
        script.process(p, *config)
        self.assertIsNot(old_stats, script.auto_diagnostics)
        self.assertEqual(script.auto_diagnostics.applied_count, 0)
        self.assertNotIn(STATS_KEY, p.extra_generation_params)
        self.assertNotIn("DiffCFG Auto Status", p.extra_generation_params)
        script.process_before_every_sampling(p, *config)
        model = p.sd_model.forge_objects.unet
        t.skim.apply_skimmed_cfg(model, "lin_interp", lin_interp_cfg=3)
        t.run_chain(model, t.inputs(model.model_options, 99, scale=20))
        script.postprocess_batch(p)
        self.assertIn("count=1;", p.extra_generation_params[STATS_KEY])

    def test_zero_applied_prints_na_and_manual_does_not_add_stats(self):
        ui = script_module()
        script = ui.DifferenceCFGScript()
        p = types.SimpleNamespace(extra_generation_params={},
                                  sd_model=types.SimpleNamespace(forge_objects=types.SimpleNamespace(unet=t.Model())))
        config = (True, 7, "linear_distance", 1, True, 0, auto.ALGORITHM)
        script.process(p, *config)
        script.process_before_every_sampling(p, *config)
        model = p.sd_model.forge_objects.unet
        t.skim.apply_skimmed_cfg(model, "lin_interp", lin_interp_cfg=3)
        t.run_chain(model, t.inputs(model.model_options, scale=20))
        script.postprocess_batch(p)
        self.assertIn("count=0; min=N/A", p.extra_generation_params[STATS_KEY])
        output = io.StringIO()
        with redirect_stdout(output):
            script.postprocess(p, None, *config)
        self.assertIn("count=0; min=N/A", output.getvalue())
        manual = (True, 7, "linear_distance", 1, False, 0.1, auto.ALGORITHM)
        script.process(p, *manual)
        script.process_before_every_sampling(p, *manual)
        script.postprocess_batch(p)
        output = io.StringIO()
        with redirect_stdout(output):
            script.postprocess(p, None, *manual)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(STATS_KEY, p.extra_generation_params)

    def test_interrupted_generation_and_neo_do_not_leak(self):
        ui = script_module()
        script = ui.DifferenceCFGScript()
        p = types.SimpleNamespace(extra_generation_params={},
                                  sd_model=types.SimpleNamespace(forge_objects=types.SimpleNamespace(unet=t.Model())))
        config = (True, 7, "linear_distance", 1, True, 1, auto.ALGORITHM)
        script.process(p, *config)
        script.auto_diagnostics.record_reference(3, 3)
        # No postprocess occurred for the interrupted request.
        script.process(p, *config)
        self.assertEqual(script.auto_diagnostics.applied_count, 0)
        t.diff._BACKEND_IS_NEO = True
        script.process_before_every_sampling(p, *config)
        script.postprocess_batch(p)
        self.assertFalse(script.reference_stats_active)
        self.assertNotIn(STATS_KEY, p.extra_generation_params)


if __name__ == "__main__":
    unittest.main(verbosity=2)
