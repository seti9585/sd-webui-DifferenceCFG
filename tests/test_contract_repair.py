"""Reproduce the inference defect and test each independent contract guard."""
from collections import Counter
from contextlib import ExitStack, redirect_stdout
import io
import types
import unittest
import weakref
from unittest.mock import patch

import torch
import test_auto_reference as t
import test_diagnostics as d
from sd_webui_difference_cfg import auto_reference as auto
from sd_webui_difference_cfg.diagnostics import AutoDiagnostics, CONTRACT_KEY, GUARD_KEY
from sd_webui_skimmed_cfg.observation import observe_hook

OLD_AUTO = t.load_file("sd_webui_difference_cfg._pre_repair",
                       t.ROOT / "tests/baseline_auto_reference_pre_repair.py")
OLD_OBS = t.load_file("old_observation", t.ROOT / "tests/baseline_observation_pre_repair.py")


def pair(old=False, old_observer=False, diagnostics=None):
    model = t.Model()
    stats = diagnostics if diagnostics is not None else AutoDiagnostics()
    factory = OLD_AUTO.make_auto_hook if old else auto.make_auto_hook
    consumer = factory(t.diff._make_difference_fn(7, "linear_distance", 0),
                       t.diff.interpolated_scales, "linear_distance", 0,
                       0.1, auto.V2_ALGORITHM, stats)
    observer_factory = OLD_OBS.observe_hook if old or old_observer else observe_hook
    producer = observer_factory(t.skim._make_lin_interp_fn(3), "lin_interp",
                                {"lin_interp_cfg": 3}, (0, 0))
    model.model_options["sampler_pre_cfg_function"] = [producer, consumer]
    return model, producer, consumer, stats


class ContractRepairTests(unittest.TestCase):
    def setUp(self):
        t.diff._BACKEND_IS_NEO = False
        t.skim._BACKEND_IS_NEO = False

    def test_reproduces_16_invalid_14_gate_and_repairs_same_inputs(self):
        for old in (True, False):
            model, producer, consumer, stats = pair(old)
            with torch.inference_mode():
                for step in range(30):
                    args = t.inputs(model.model_options, step, scale=20, sigma=5 if step < 16 else 0)
                    producer(args)
                    if step < 16:
                        self.assertEqual(model.model_options[auto.KEY]["state"], "ERROR" if old else "APPLIED")
                    consumer(args)
            self.assertEqual(stats.get("DIFF_GATE_OFF"), 14)
            if old:
                self.assertEqual(stats.get("AUTO_CONTRACT_INVALID"), 16)
                self.assertEqual(stats.applied_count, 0)
            else:
                self.assertEqual(stats.get("AUTO_CONTRACT_INVALID", 0), 0)
                self.assertEqual(stats.get("AUTO_APPLIED"), 16)
                self.assertEqual(stats.applied_count, 16)
                self.assertEqual(stats.guard_counts, {"INFERENCE_SNAPSHOT": 16})
            print("Synthetic 16-active/14-gated inference replay, old=%s: %s" % (old, dict(stats)))

    def test_inference_outputs_equal_versioned_path(self):
        cases = 0
        for dtype in (torch.float16, torch.bfloat16, torch.float32, torch.float64):
            for seed in range(8):
                outputs = []
                refs = []
                for inference in (False, True):
                    with torch.inference_mode(inference):
                        model, producer, consumer, stats = pair()
                        args = t.inputs(model.model_options, seed, dtype, scale=20)
                        producer(args)
                        consumer(args)
                        self.assertNotEqual(consumer._auto_last_status, "AUTO_CONTRACT_INVALID")
                        outputs.append(args["conds_out"])
                        refs.append(consumer._auto_last_reference)
                self.assertEqual(refs[0], refs[1])
                for a, b in zip(*outputs):
                    self.assertTrue(torch.equal(a, b))
                cases += 1
        print("Inference/versioned output equivalence cases:", cases)

    def test_inference_mutations_are_rejected_not_ignored(self):
        for i, name in enumerate(("COND", "UNCOND")):
            with torch.inference_mode():
                model, producer, consumer, stats = pair()
                args = t.inputs(model.model_options, scale=20)
                producer(args)
                args["conds_out"][i].add_(0.25)
                prior = [x.clone() for x in args["conds_out"]]
                consumer(args)
                self.assertEqual(consumer._auto_last_contract_reason, name + "_CONTENT_CHANGED")
                self.assertEqual(stats.applied_count, 0)
                for a, b in zip(prior, args["conds_out"]):
                    self.assertTrue(torch.equal(a, b))

    def test_inference_snapshot_required_and_must_not_alias(self):
        for malformed in ("missing", "alias", "shape", "dtype"):
            with torch.inference_mode():
                model, producer, consumer, stats = pair()
                args = t.inputs(model.model_options, scale=20)
                producer(args)
                rec = model.model_options[auto.KEY]
                first, second = rec["snapshots"]
                if malformed == "missing":
                    first = None
                elif malformed == "alias":
                    first = args["conds_out"][0].view_as(args["conds_out"][0])
                elif malformed == "shape":
                    first = first.reshape(-1)
                else:
                    first = first.double()
                rec["snapshots"] = (first, second)
                consumer(args)
                self.assertEqual(consumer._auto_last_contract_reason,
                                 "COND_SNAPSHOT_MISSING" if malformed == "missing" else "COND_SNAPSHOT_INVALID")

    def test_legacy_observer_error_is_visible(self):
        with torch.inference_mode():
            model, producer, consumer, stats = pair(old_observer=True)
            args = t.inputs(model.model_options, scale=20)
            producer(args)
            consumer(args)
            self.assertEqual(consumer._auto_last_contract_reason, "OBSERVATION_ERROR")
            self.assertIn("RuntimeError", stats.contract_summary())

    def test_observer_preserves_exception_phase_and_message(self):
        model, producer, consumer, stats = pair()
        args = t.inputs(model.model_options, scale=20)
        with patch("sd_webui_skimmed_cfg.observation.torch.is_inference",
                   side_effect=RuntimeError("guard probe failure")):
            producer(args)
        record = model.model_options[auto.KEY]
        self.assertEqual(record["state"], "ERROR")
        self.assertEqual(record["error_stage"], "mutation_guard")
        self.assertIn("guard probe failure", record["error_message"])
        consumer(args)
        self.assertIn("mutation_guard: RuntimeError: guard probe failure", stats.contract_summary())

    def test_individual_contract_reason_codes(self):
        reasons = (
            "CHAIN_CONSUMER_MISSING", "CHAIN_PRODUCER_COUNT", "CHAIN_ORDER",
            "RECORD_MISSING", "RECORD_VERSION", "PASS_TOKEN", "PRODUCER_IDENTITY",
            "SEQUENCE_CURRENT", "SEQUENCE_REPLAY", "OUTPUTS_IDENTITY", "SCALE_MISMATCH",
            "SIGMA_MISMATCH", "INPUT_IDENTITY", "MODE_MISMATCH", "STATE_INVALID",
            "COND_IDENTITY", "UNCOND_IDENTITY", "SHAPE_RECORDED", "SHAPE_PAIR",
            "SHAPE_INPUT", "DIMENSION", "EMPTY_PREDICTION", "DTYPE_RECORDED",
            "DTYPE_UNCOND", "DTYPE_INPUT", "DEVICE_RECORDED", "DEVICE_UNCOND",
            "DEVICE_INPUT", "GUARD_VERSIONS_MISSING", "GUARD_SNAPSHOTS_INVALID",
            "COND_VERSION", "UNCOND_VERSION")
        for reason in reasons:
            with self.subTest(reason=reason):
                model, producer, consumer, stats = pair()
                args = t.inputs(model.model_options, scale=20)
                producer(args)
                rec = model.model_options[auto.KEY]
                c, u = args["conds_out"]
                keep_alive = []
                def set_output(index, tensor):
                    args["conds_out"][index] = tensor
                    rec["cond_ref" if index == 0 else "uncond_ref"] = weakref.ref(tensor)
                    return tensor
                if reason == "CHAIN_CONSUMER_MISSING":
                    model.model_options["sampler_pre_cfg_function"] = [producer]
                elif reason == "CHAIN_PRODUCER_COUNT":
                    model.model_options["sampler_pre_cfg_function"].insert(0, producer)
                elif reason == "CHAIN_ORDER":
                    model.model_options["sampler_pre_cfg_function"].reverse()
                elif reason == "RECORD_MISSING":
                    model.model_options.pop(auto.KEY)
                elif reason == "RECORD_VERSION":
                    rec["version"] = 999
                elif reason == "PASS_TOKEN":
                    rec["token"] = object()
                elif reason == "PRODUCER_IDENTITY":
                    rec["producer"] = None
                elif reason == "SEQUENCE_CURRENT":
                    rec["sequence"] += 1
                elif reason == "SEQUENCE_REPLAY":
                    consumer(args)
                    model.model_options[auto.KEY] = rec
                elif reason == "OUTPUTS_IDENTITY":
                    args["conds_out"] = list(args["conds_out"])
                elif reason == "SCALE_MISMATCH":
                    rec["scale"] = 21
                elif reason == "SIGMA_MISMATCH":
                    rec["sigma_value"] = 6
                elif reason == "INPUT_IDENTITY":
                    args["input"] = args["input"].clone()
                elif reason == "MODE_MISMATCH":
                    rec["mode"] = "single_scale"
                elif reason == "STATE_INVALID":
                    rec["state"] = "UNKNOWN"
                elif reason in ("COND_IDENTITY", "UNCOND_IDENTITY"):
                    args["conds_out"][0 if reason.startswith("COND") else 1] = c.clone()
                elif reason == "SHAPE_RECORDED":
                    rec["shape"] = (4,)
                elif reason == "SHAPE_PAIR":
                    set_output(1, u.reshape(-1))
                elif reason == "SHAPE_INPUT":
                    args["input"] = args["input"].reshape(-1)
                    rec["input_ref"] = weakref.ref(args["input"])
                elif reason in ("DIMENSION", "EMPTY_PREDICTION"):
                    shape = (256,) if reason == "DIMENSION" else (1, 4, 0, 8)
                    new_c = set_output(0, torch.zeros(shape))
                    set_output(1, torch.zeros(shape))
                    rec["shape"] = tuple(new_c.shape)
                    args["input"] = torch.zeros(shape)
                    rec["input_ref"] = weakref.ref(args["input"])
                elif reason == "DTYPE_RECORDED":
                    rec["dtype"] = torch.float64
                elif reason == "DTYPE_UNCOND":
                    set_output(1, u.double())
                elif reason == "DTYPE_INPUT":
                    args["input"] = args["input"].double()
                    rec["input_ref"] = weakref.ref(args["input"])
                elif reason == "DEVICE_RECORDED":
                    rec["device"] = torch.device("meta")
                elif reason == "DEVICE_UNCOND":
                    set_output(1, torch.empty_like(u, device="meta"))
                elif reason == "DEVICE_INPUT":
                    args["input"] = torch.empty_like(args["input"], device="meta")
                    rec["input_ref"] = weakref.ref(args["input"])
                elif reason == "GUARD_VERSIONS_MISSING":
                    rec["versions"] = None
                elif reason == "GUARD_SNAPSHOTS_INVALID":
                    rec["snapshots"] = None
                elif reason == "COND_VERSION":
                    c.add_(1)
                elif reason == "UNCOND_VERSION":
                    u.add_(1)
                consumer(args)
                self.assertEqual(consumer._auto_last_status, "AUTO_CONTRACT_INVALID")
                self.assertEqual(consumer._auto_last_contract_reason, reason)
                self.assertEqual(stats.contract_reasons.get(reason), 1)
        print("Individually exercised contract checks:", len(reasons))

    def test_no_extra_host_transfer_for_snapshot_check(self):
        counts_by_mode = []
        for inference in (False, True):
            with torch.inference_mode(inference):
                model, producer, consumer, stats = pair()
                args = t.inputs(model.model_options, scale=20)
                counts = Counter()
                with ExitStack() as stack:
                    for name in ("item", "tolist", "cpu", "numpy", "__float__", "__bool__"):
                        original = getattr(torch.Tensor, name)
                        def wrapper(tensor, *a, _name=name, _original=original, **kw):
                            counts[_name] += 1
                            return _original(tensor, *a, **kw)
                        stack.enter_context(patch.object(torch.Tensor, name, wrapper))
                    producer(args)
                    consumer(args)
                counts_by_mode.append(counts)
        self.assertEqual(counts_by_mode[0], counts_by_mode[1])
        print("Host scalar/transfer calls, versioned == inference:", dict(counts_by_mode[0]))

    def test_contract_details_console_png_and_request_reset(self):
        ui = d.script_module()
        script = ui.DifferenceCFGScript()
        p = types.SimpleNamespace(extra_generation_params={})
        config = (True, 7, "linear_distance", 1, True, 0.1, auto.ALGORITHM)
        script.process(p, *config)
        script.auto_diagnostics.record_contract("OBSERVATION_ERROR", "mutation_guard: RuntimeError: example")
        script.auto_diagnostics.record_guard("INFERENCE_SNAPSHOT")
        script.postprocess_batch(p)
        self.assertIn("OBSERVATION_ERROR=1", p.extra_generation_params[CONTRACT_KEY])
        self.assertIn("INFERENCE_SNAPSHOT=1", p.extra_generation_params[GUARD_KEY])
        output = io.StringIO()
        with redirect_stdout(output):
            script.postprocess(p, None)
        self.assertIn("[DifferenceCFG Auto Contract]", output.getvalue())
        self.assertIn("mutation_guard: RuntimeError", output.getvalue())
        script.process(p, *config)
        self.assertNotIn(CONTRACT_KEY, p.extra_generation_params)
        self.assertNotIn(GUARD_KEY, p.extra_generation_params)
        self.assertEqual(script.auto_diagnostics.contract_reasons, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
