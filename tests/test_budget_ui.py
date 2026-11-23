"""Budget UI regression tests, including optional upstream host functions."""
import ast
from decimal import Decimal
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import test_auto_reference as t
from sd_webui_difference_cfg.auto_reference import DEFAULT_BUDGET, restore_enabled

REFERENCE = Path(os.environ["DIFFCFG_HOST_REFERENCE"]) if "DIFFCFG_HOST_REFERENCE" in os.environ else None


class Component:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get("value")
        self.visible = kwargs.get("visible", True)
        self.label = kwargs.get("label")
        self.step = kwargs.get("step")
        if type(self).__name__ == "Slider":
            self.minimum, self.maximum = args[:2]
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def load_ui():
    gr = types.ModuleType("gradio")
    gr.update = lambda **kwargs: dict(kwargs, __type__="update")
    for name in ("Accordion", "HTML", "Checkbox", "Slider", "Dropdown", "Textbox",
                 "Radio", "Number", "Button", "Tabs", "TabItem"):
        setattr(gr, name, type(name, (Component,), {}))
    modules = types.ModuleType("modules")
    modules.scripts = types.SimpleNamespace(Script=object, AlwaysVisible=True)
    modules.script_callbacks = types.SimpleNamespace(on_before_ui=lambda callback: None)
    with patch.dict(sys.modules, {"gradio": gr, "modules": modules}):
        ui = t.load_file("test_budget_ui_script", t.ROOT / "scripts/sd_webui_difference_cfg.py")
    return ui, gr


class BudgetUITests(unittest.TestCase):
    def setUp(self):
        self.ui, self.gr = load_ui()

    def test_fresh_ui_defaults_and_config_exclusion(self):
        for img2img in (False, True):
            script = self.ui.DifferenceCFGScript()
            fields = script.ui(img2img)
            slider = fields[5]
            self.assertEqual((slider.minimum, slider.maximum, slider.step, slider.value),
                             (0.0, 0.10, 0.005, 0.03))
            self.assertTrue(slider.do_not_save_to_config)
            self.assertEqual(len(fields), 7)
            self.assertEqual((fields[1].minimum, fields[1].maximum, fields[1].value), (0, 20, 5))
            self.assertFalse(fields[4].value)

    def test_legacy_large_budget_restore_without_clipping(self):
        for value in (0.15, 0.25, 0.50, 1.0):
            result = self.ui._restore_auto_budget({"DiffCFG Auto Budget": str(value)})
            self.assertEqual(result["value"], value)
            self.assertEqual(result["maximum"], value)
            self.assertEqual(result["minimum"], 0)
            self.assertEqual(result["step"], 0.005)
            self.assertEqual(result["__type__"], "update")

    def test_normal_restore_resets_expanded_bounds(self):
        self.ui._restore_auto_budget({"DiffCFG Auto Budget": "1.0"})
        for value in (0, 0.005, 0.03, 0.10):
            result = self.ui._restore_auto_budget({"DiffCFG Auto Budget": str(value)})
            self.assertEqual(result["value"], value)
            self.assertEqual(result["maximum"], 0.10)
            self.assertEqual(result["step"], 0.005)

    def test_preserve_legacy_values_between_new_steps(self):
        for value in ("0.0375", "0.123456", "0.1001", "0.00001"):
            result = self.ui._restore_auto_budget({"DiffCFG Auto Budget": value})
            self.assertEqual(result["value"], float(value))
            self.assertEqual(Decimal(value) % Decimal(str(result["step"])), 0)
            self.assertLessEqual(result["step"], 0.005)

    def test_missing_old_metadata_keeps_implicit_budget_and_auto_off(self):
        self.assertEqual(self.ui._restore_auto_budget({})["value"], DEFAULT_BUDGET)
        self.assertEqual(DEFAULT_BUDGET, 0.10)
        self.assertFalse(restore_enabled({}))
        self.assertFalse(restore_enabled({"DiffCFG Method": "linear_distance"}))

    def test_invalid_metadata_uses_safe_ui_default_with_warning(self):
        for value in ("nan", "inf", "-0.1", "1.1", None, "bad"):
            with self.assertLogs(self.ui.logger, level="WARNING"):
                result = self.ui._restore_auto_budget({"DiffCFG Auto Budget": value})
            self.assertEqual(result["value"], 0.03)
            self.assertEqual(result["maximum"], 0.10)

    def test_metadata_roundtrip_keeps_manual_and_legacy_values(self):
        for value in (0.03, 0.25, 1.0, 0.123456):
            script = self.ui.DifferenceCFGScript()
            p = types.SimpleNamespace(extra_generation_params={})
            args = (True, 7, "linear_distance", 0.8, True, value, self.ui.ALGORITHM)
            script.process(p, *args)
            self.assertEqual(p.extra_generation_params["DiffCFG Reference"], 7)
            update = self.ui._restore_auto_budget(p.extra_generation_params)
            restored = script._resolve(p, (*args[:5], update["value"], args[6]))
            self.assertEqual(restored["auto_budget"], value)
            self.assertEqual(restored["reference_cfg"], 7)

    def test_internal_api_range_and_implicit_default_unchanged(self):
        script = self.ui.DifferenceCFGScript()
        p = types.SimpleNamespace()
        for value in (0.25, 0.5, 1):
            cfg = script._resolve(p, (True, 7, "linear_distance", 0.8, True, value))
            self.assertEqual(cfg["auto_budget"], value)
        legacy = script._resolve(p, (True, 7, "linear_distance", 0.8))
        self.assertEqual(legacy["auto_budget"], 0.10)
        self.assertFalse(legacy["auto_reference"])

    @unittest.skipUnless(REFERENCE is not None, "Set DIFFCFG_HOST_REFERENCE to the fetched public host sources")
    def test_upstream_paste_function_passes_range_update(self):
        tree = ast.parse((REFERENCE / "infotext_utils.py").read_text(encoding="utf-8"))
        outer = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "connect_paste")
        paste = next(n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == "paste_func")
        script = self.ui.DifferenceCFGScript()
        fields = script.ui(False)
        params = {"DiffCFG Method": "linear_distance", "DiffCFG Reference": "7",
                  "DiffCFG Auto Reference": "True", "DiffCFG Auto Budget": "0.25"}
        errors = []
        namespace = dict(gr=self.gr, type_of_gr_update=dict,
                         paste_fields=script.infotext_fields,
                         parse_generation_parameters=lambda prompt: params,
                         script_callbacks=types.SimpleNamespace(infotext_pasted_callback=lambda *args: None),
                         errors=types.SimpleNamespace(report=lambda *args, **kwargs: errors.append(args)))
        exec(compile(ast.Module(body=[paste], type_ignores=[]), "upstream_paste", "exec"), namespace)
        updates = namespace["paste_func"]("nonempty saved infotext")
        self.assertEqual(updates[5]["value"], 0.25)
        self.assertEqual(updates[5]["maximum"], 0.25)
        self.assertEqual(updates[2]["value"], 7)
        self.assertEqual(errors, [])
        params["DiffCFG Auto Budget"] = "0.03"
        updates = namespace["paste_func"]("nonempty saved infotext")
        self.assertEqual(updates[5]["maximum"], 0.10)
        self.assertEqual(updates[5]["value"], 0.03)

    @unittest.skipUnless(REFERENCE is not None, "Set DIFFCFG_HOST_REFERENCE to the fetched public host sources")
    def test_upstream_ui_config_cannot_override_budget(self):
        tree = ast.parse((REFERENCE / "ui_loadsave.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "UiLoadsave")
        namespace = dict(gr=self.gr, InputAccordion=type("InputAccordion", (Component,), {}),
                         ToolButton=type("ToolButton", (Component,), {}))
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "upstream_loadsave", "exec"), namespace)
        host = namespace["UiLoadsave"].__new__(namespace["UiLoadsave"])
        host.finalized_ui = False
        host.component_mapping = {}
        path = "customscript/DifferenceCFG/Auto Correction Budget"
        host.ui_settings = {path + "/" + key: value for key, value in
                            (("value", 1), ("minimum", -1), ("maximum", 1), ("step", 0.01))}
        slider = self.ui.DifferenceCFGScript().ui(False)[5]
        host.add_component(path, slider)
        self.assertEqual((slider.minimum, slider.maximum, slider.step, slider.value),
                         (0, 0.10, 0.005, 0.03))
        self.assertNotIn(path + "/value", host.component_mapping)


if __name__ == "__main__":
    unittest.main(verbosity=2)
