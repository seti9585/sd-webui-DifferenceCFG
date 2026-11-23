"""CPU contract tests. Run from the extension root with unittest discovery."""

import importlib.util
import itertools
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "sd-webui-SkimmedCFG"))
from sd_webui_difference_cfg import core as diff
from sd_webui_difference_cfg.auto_reference import ALGORITHM, KEY, budget_reference, restore_enabled
from sd_webui_skimmed_cfg import core as skim


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD_DIFF = load_file("old_diff", ROOT / "tests" / "baseline_difference.py")
OLD_SKIM = load_file("old_skim", ROOT / "tests" / "baseline_skimmed.py")


class Model:
    def __init__(self):
        self.model_options = {}

    def clone(self):
        result = Model()
        result.model_options = dict(self.model_options)
        return result

    def get_model_object(self, name):
        return types.SimpleNamespace(percent_to_sigma=lambda p: 10 * (1 - p))


def settings(**extra):
    result = dict(skimming_cfg=3.0, full_skim_negative=False,
                  disable_flipping_filter=False, start_at_percentage=0.0,
                  end_at_percentage=1.0, flip_at_percentage=0.0)
    result.update(extra)
    return result


def inputs(options, seed=42, dtype=torch.float32, scale=9.0, sigma=5.0):
    gen = torch.Generator().manual_seed(seed)
    tensors = [torch.randn((1, 4, 8, 8), generator=gen, dtype=dtype) for _ in range(3)]
    return dict(conds_out=tensors[:2], input=tensors[2], cond_scale=scale,
                sigma=torch.tensor([sigma]), model_options=options)


def duplicate(args):
    return dict(args, conds_out=[t.clone() if t is not None else None for t in args["conds_out"]])


def run_chain(model, args):
    for fn in model.model_options["sampler_pre_cfg_function"]:
        args["conds_out"] = fn(args)
    return args["conds_out"]


class AutoTests(unittest.TestCase):
    def setUp(self):
        diff._BACKEND_IS_NEO = False
        skim._BACKEND_IS_NEO = False

    def pair(self, mode="single_scale", method="linear_distance", **kwargs):
        model = Model()
        diff.apply_difference_cfg(model, 5, method, 1, auto_reference=True, **kwargs)
        skim.apply_skimmed_cfg(model, mode, **settings(), lin_interp_cfg=3,
                               skimming_cfg_positive=3, skimming_cfg_negative=3)
        return model, model.model_options["sampler_pre_cfg_function"][-1]

    def test_manual_all_methods_exact(self):
        for method, use_skim, dtype, seed in itertools.product(
                diff._DIFF_METHODS, (False, True),
                (torch.float16, torch.bfloat16, torch.float32, torch.float64), range(4)):
            with self.subTest(method=method, skim=use_skim, dtype=dtype, seed=seed):
                model = Model()
                diff.apply_difference_cfg(model, 5, method, 1)
                if use_skim:
                    skim.apply_skimmed_cfg(model, "single_scale", **settings())
                args = inputs(model.model_options, seed, dtype)
                baseline = duplicate(args)
                if use_skim:
                    OLD_SKIM._make_single_scale_fn(3, False, False, 999999999.9, 0, 999999999.9, 0)(baseline)
                OLD_DIFF._make_difference_fn(5, method, 0)(baseline)
                result = run_chain(model, args)
                for actual, expected in zip(result, baseline["conds_out"]):
                    self.assertTrue(torch.equal(actual, expected))
                self.assertNotIn(KEY, model.model_options)

    def test_observation_is_exact_for_settings_and_gates(self):
        for full, disable, flip, current, sigma in itertools.product(
                (False, True), (False, True), (0.0, 0.5), (False, True), (1.0, 5.0, 9.0)):
            model = Model()
            cfg = settings(full_skim_negative=full, disable_flipping_filter=disable,
                           flip_at_percentage=flip, skimming_cfg=-1 if current else 3,
                           start_at_percentage=0.2, end_at_percentage=0.8)
            skim.apply_skimmed_cfg(model, "single_scale", **cfg)
            observer = model.model_options["sampler_pre_cfg_function"][0]
            def consumer(args):
                return args["conds_out"]
            consumer._difference_auto_token = object()
            model.model_options["sampler_pre_cfg_function"].append(consumer)
            args = inputs(model.model_options, sigma=sigma)
            baseline = duplicate(args)
            y0 = baseline["conds_out"][1] + 9 * (baseline["conds_out"][0] - baseline["conds_out"][1])
            OLD_SKIM._make_single_scale_fn(-1 if current else 3, full, disable, 8, 2,
                                           5 if flip else 999999999.9, flip)(baseline)
            observer(args)
            for actual, expected in zip(args["conds_out"], baseline["conds_out"]):
                self.assertTrue(torch.equal(actual, expected))
            rec = model.model_options[KEY]
            if sigma == 5:
                self.assertEqual(rec["state"], "APPLIED")
                c, u = baseline["conds_out"]
                self.assertTrue(torch.equal(rec["delta"], u + 9 * (c - u) - y0))
            else:
                self.assertEqual(rec["state"], "SKIPPED_GATE")

    def test_auto_bounds_and_actual_budget(self):
        applied = 0
        rejected = 0
        for dtype, scale, seed in itertools.product(
                (torch.float16, torch.bfloat16, torch.float32, torch.float64), (2.0, 9.0, 30.0), range(10)):
            model, fn = self.pair(auto_budget=0.1)
            args = inputs(model.model_options, seed, dtype, scale)
            producer = model.model_options["sampler_pre_cfg_function"][0]
            producer(args)
            ds = model.model_options[KEY]["delta"]
            prior = args["conds_out"][1].clone()
            fn(args)
            self.assertNotIn(KEY, model.model_options)
            if fn._auto_last_status == "AUTO_APPLIED":
                applied += 1
                self.assertTrue(0 <= fn._auto_last_reference <= scale)
                actual = (1 - scale) * (args["conds_out"][1].to(ds.dtype) - prior.to(ds.dtype))
                self.assertLessEqual(float(actual.norm()), 0.1 * float(ds.norm()) * 1.000011 + 0.000000001)
            elif fn._auto_last_status == "AUTO_NUMERIC_INVALID":
                rejected += 1
                self.assertTrue(torch.equal(args["conds_out"][1], prior))
            else:
                self.assertEqual(fn._auto_last_status, "AUTO_NO_CORRECTION")
        self.assertGreater(applied, 0)
        print("Auto matrix: applied=%s, safe-rejected=%s, cases=120" % (applied, rejected))

    def test_no_skim_and_old_skim(self):
        for old in (False, True):
            model = Model()
            diff.apply_difference_cfg(model, 5, "linear_distance", 1, auto_reference=True)
            fn = model.model_options["sampler_pre_cfg_function"][-1]
            if old:
                model.model_options["sampler_pre_cfg_function"].insert(0, OLD_SKIM._make_replace_fn())
            args = inputs(model.model_options)
            baseline = duplicate(args)
            OLD_DIFF._make_difference_fn(5, "linear_distance", 0)(baseline)
            fn(args)
            self.assertEqual(fn._auto_last_status, "MANUAL_FALLBACK_NO_SKIM")
            self.assertTrue(torch.equal(args["conds_out"][1], baseline["conds_out"][1]))

    def test_unsupported_modes_methods(self):
        for mode, method in itertools.product(("replace", "dual_scales"), diff._DIFF_METHODS):
            model, fn = self.pair(mode, method)
            run_chain(model, inputs(model.model_options))
            self.assertEqual(fn._auto_last_status, "MANUAL_FALLBACK_UNSUPPORTED")

    def test_contract_rejects_stale_mismatched_and_reordered(self):
        for damage in ("missing", "sequence", "token", "scale", "sigma", "outputs", "mutated", "order", "input", "pass"):
            model, fn = self.pair()
            args = inputs(model.model_options)
            producer = model.model_options["sampler_pre_cfg_function"][0]
            producer(args)
            if damage == "missing":
                model.model_options.pop(KEY)
            elif damage == "sequence":
                model.model_options[KEY]["sequence"] = 0
            elif damage == "token":
                model.model_options[KEY]["token"] = object()
            elif damage == "scale":
                args["cond_scale"] = 10
            elif damage == "sigma":
                args["sigma"] = torch.tensor([6.0])
            elif damage == "outputs":
                args["conds_out"] = list(args["conds_out"])
            elif damage == "mutated":
                args["conds_out"][0].add_(1)
            elif damage == "order":
                model.model_options["sampler_pre_cfg_function"].reverse()
            elif damage == "input":
                args["input"] = args["input"].clone()
            elif damage == "pass":
                rec = model.model_options[KEY]
                diff.apply_difference_cfg(model, 5, "linear_distance", 1, auto_reference=True)
                fn = model.model_options["sampler_pre_cfg_function"][-1]
                model.model_options[KEY] = rec
            prior = args["conds_out"][1].clone()
            fn(args)
            self.assertEqual(fn._auto_last_status, "AUTO_CONTRACT_INVALID", damage)
            self.assertTrue(torch.equal(prior, args["conds_out"][1]))

    def test_consumed_record_cannot_be_replayed(self):
        model, fn = self.pair()
        args = inputs(model.model_options)
        model.model_options["sampler_pre_cfg_function"][0](args)
        rec = model.model_options[KEY]
        fn(args)
        model.model_options[KEY] = rec
        fn(args)
        self.assertEqual(fn._auto_last_status, "AUTO_CONTRACT_INVALID")

    def test_nonfinite_and_unknown_algorithm(self):
        for value in (float("nan"), float("inf"), 1000000000000000000000000000000.0):
            model, fn = self.pair()
            args = inputs(model.model_options)
            args["input"].fill_(value)
            run_chain(model, args)
            self.assertIn(fn._auto_last_status, ("AUTO_NUMERIC_INVALID", "AUTO_NO_CORRECTION"))
        model, fn = self.pair(auto_algorithm="unknown-v999")
        run_chain(model, inputs(model.model_options))
        self.assertEqual(fn._auto_last_status, "AUTO_ALGORITHM_UNSUPPORTED")

    def test_zero_constant_and_current_cfg(self):
        for value in (0.0, 1.0):
            model, fn = self.pair()
            args = inputs(model.model_options)
            for tensor in args["conds_out"]:
                tensor.fill_(value)
            run_chain(model, args)
            self.assertTrue(all(torch.isfinite(t).all() for t in args["conds_out"]))
        model = Model()
        diff.apply_difference_cfg(model, 5, "linear_distance", 1, auto_reference=True)
        skim.apply_skimmed_cfg(model, "single_scale", **settings(skimming_cfg=-1))
        run_chain(model, inputs(model.model_options))
        self.assertEqual(model.model_options["sampler_pre_cfg_function"][-1]._auto_last_status, "AUTO_NO_CORRECTION")

    def test_gate_consumes_record(self):
        model = Model()
        diff.apply_difference_cfg(model, 5, "linear_distance", 0.1, auto_reference=True)
        skim.apply_skimmed_cfg(model, "single_scale", **settings())
        run_chain(model, inputs(model.model_options))
        self.assertNotIn(KEY, model.model_options)
        self.assertEqual(model.model_options["sampler_pre_cfg_function"][-1]._auto_last_status, "DIFF_GATE_OFF")

    def test_skim_gate_and_cfg_one(self):
        model, fn = self.pair()
        run_chain(model, inputs(model.model_options, scale=1.0))
        self.assertEqual(fn._auto_last_status, "MANUAL_FALLBACK_UNSUPPORTED")
        model = Model()
        diff.apply_difference_cfg(model, 5, "linear_distance", 1, auto_reference=True)
        skim.apply_skimmed_cfg(model, "single_scale", **settings(end_at_percentage=0.1))
        fn = model.model_options["sampler_pre_cfg_function"][-1]
        run_chain(model, inputs(model.model_options))
        self.assertEqual(fn._auto_last_status, "MANUAL_FALLBACK_SKIM_SKIPPED")

    def test_budget_scalar_boundaries(self):
        self.assertEqual(budget_reference(30, 3, 0.1, 10, 2, 1), 29.5)
        self.assertEqual(budget_reference(9, 3, 0.1, 0, 2, 1), 9)
        self.assertEqual(budget_reference(9, 30, 0.1, 10, 2, 1), 9)
        for scale, skim_scale, gamma in ((1, 3, 0.1), (9, -1, 0.1), (9, 3, -1), (9, 3, float("nan"))):
            with self.assertRaises(ValueError):
                budget_reference(scale, skim_scale, gamma, 10, 2, 1)

    def test_old_png_defaults_off(self):
        self.assertFalse(restore_enabled({"DiffCFG Method": "linear_distance"}))
        self.assertFalse(restore_enabled({"DiffCFG Auto Reference": "False"}))
        self.assertTrue(restore_enabled({"DiffCFG Auto Reference": "True"}))

    def test_disable_clears_records_and_hooks(self):
        model, fn = self.pair()
        args = inputs(model.model_options)
        model.model_options["sampler_pre_cfg_function"][0](args)
        diff.remove_difference_cfg_patches(model)
        self.assertNotIn(KEY, model.model_options)
        self.assertNotIn(fn, model.model_options["sampler_pre_cfg_function"])
        run_chain(model, inputs(model.model_options))
        self.assertNotIn(KEY, model.model_options)

    def test_missing_uncond_and_empty_tensor(self):
        for missing in (None, torch.empty(0)):
            model, fn = self.pair()
            args = inputs(model.model_options)
            args["conds_out"][1] = missing
            run_chain(model, args)
            self.assertEqual(fn._auto_last_status, "MANUAL_FALLBACK_SKIM_SKIPPED")
            self.assertIs(args["conds_out"][1], missing)

    def test_observation_failure_preserves_original(self):
        model, fn = self.pair()
        args = inputs(model.model_options)
        baseline = duplicate(args)
        OLD_SKIM._make_single_scale_fn(3, False, False, 999999999.9, 0, 999999999.9, 0)(baseline)
        with patch("sd_webui_skimmed_cfg.observation.weakref.ref", side_effect=RuntimeError("test")):
            run_chain(model, args)
        self.assertEqual(fn._auto_last_status, "AUTO_CONTRACT_INVALID")
        for result, expected in zip(args["conds_out"], baseline["conds_out"]):
            self.assertTrue(torch.equal(result, expected))

    def test_neo_reports_manual_fallback(self):
        diff._BACKEND_IS_NEO = True
        model = Model()
        counts = {}
        diff.apply_difference_cfg(model, 5, "linear_distance", 1, auto_reference=True, diagnostics=counts)
        self.assertEqual(counts, {"MANUAL_FALLBACK_UNSUPPORTED": 1})
        self.assertNotIn("sampler_pre_cfg_function", model.model_options)

    def test_linear_relation_and_equal_reference(self):
        for constant in (False, True):
            args = inputs({}, dtype=torch.float64)
            c, u = args["conds_out"]
            if constant:
                c.fill_(2)
                u.fill_(1)
            x = args["input"]
            for reference in (0.0, 3.0, 8.5, 9.0):
                actual = diff.interpolated_scales(x, c, u, 9, reference)
                expected = OLD_DIFF.interpolated_scales(x, c, u, 9, reference)
                self.assertTrue(torch.equal(actual, expected))
                if reference == 9:
                    self.assertTrue(torch.equal(actual, u))
                else:
                    difference = ((x - 9 * c - 8 * u) - (x - reference * c - (reference - 1) * u)).abs()
                    span = difference.max() - difference.min()
                    weight = (difference - difference.min()) / span if span > 0 else torch.zeros_like(c)
                    predicted = -(9 - reference) * (1 - weight) * (c - u)
                    torch.testing.assert_close(-8 * (actual - u), predicted)

    def test_ui_metadata_roundtrip_with_stub_host(self):
        class Component:
            def __init__(self, *args, **kwargs):
                self.value = kwargs.get("value")
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        gr = types.ModuleType("gradio")
        gr.update = lambda **kwargs: dict(kwargs, __type__="update")
        for name in ("Accordion", "HTML", "Checkbox", "Slider", "Dropdown", "Textbox"):
            setattr(gr, name, Component)
        modules = types.ModuleType("modules")
        modules.scripts = types.SimpleNamespace(Script=object, AlwaysVisible=True)
        modules.script_callbacks = types.SimpleNamespace(on_before_ui=lambda callback: None)
        with patch.dict(sys.modules, {"gradio": gr, "modules": modules}):
            ui = load_file("test_difference_ui", ROOT / "scripts" / "sd_webui_difference_cfg.py")
            script = ui.DifferenceCFGScript()
            components = script.ui(False)
            p = types.SimpleNamespace(extra_generation_params={})
            script.process(p, True, 5, "linear_distance", 0.8, True, 0.25, ALGORITHM)
            data = {key: str(value) for key, value in p.extra_generation_params.items()}
            restored = [reader(data) if callable(reader) else data.get(reader, component.value)
                        for component, reader in script.infotext_fields]
            restored = [value["value"] if isinstance(value, dict) else value for value in restored]
            # Infotext order differs from the API argument order.
            cfg = script._resolve(p, [restored[0], restored[2], restored[1], restored[3], *restored[4:]])
            self.assertTrue(cfg["auto_reference"])
            self.assertEqual(cfg["auto_budget"], 0.25)
            self.assertEqual(cfg["reference_cfg"], 5)
            self.assertEqual(cfg["auto_algorithm"], ALGORITHM)
            self.assertEqual(len(components), 7)
            legacy = script._resolve(p, [True, 5, "linear_distance", 0.8])
            self.assertFalse(legacy["auto_reference"])
            model = Model()
            diff.apply_difference_cfg(model, 5, "linear_distance", 1, auto_reference=True)
            p.sd_model = types.SimpleNamespace(forge_objects=types.SimpleNamespace(unet=model))
            script.process_before_every_sampling(p, False, 5, "linear_distance", 0.8)
            self.assertEqual(p.sd_model.forge_objects.unet.model_options["sampler_pre_cfg_function"], [])
if __name__ == "__main__":
    unittest.main(verbosity=2)
