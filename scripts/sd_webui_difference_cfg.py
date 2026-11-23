"""
sd-webui-DifferenceCFG - Difference CFG for Forge-derived WebUIs
===============================================================
Location: extensions/sd-webui-DifferenceCFG/scripts/sd_webui_difference_cfg.py

Hook:  Pre-CFG (reForge / Forge Classic) / Post-CFG (Forge Neo)
Origin: https://github.com/Extraltodeus/Skimmed_CFG  (DifferenceCFG_PreCFG)

sorting_priority: 14.2
    TCFG (13.0) -> SkimmedCFG (14.0) -> DifferenceCFG (14.2) -> CFG -> MaHiRo (15.5)

Difference CFG is a mask-free, reference-scale-based global re-adjustment of
the unconditional prediction. Single algorithm, four method variants selected
from a dropdown -- no modes. Split out of sd-webui-SkimmedCFG because it shares
none of that extension's skimming machinery.
"""

import logging
import math
from decimal import Decimal
import os
import sys
import traceback
from functools import partial
from typing import Any

import gradio as gr
from modules import scripts, script_callbacks

# ---------------------------------------------------------------------------
# sys.path - ensure the extension root is importable
# ---------------------------------------------------------------------------
_EXT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _EXT_ROOT not in sys.path:
    sys.path.insert(0, _EXT_ROOT)
# ---------------------------------------------------------------------------

from sd_webui_difference_cfg import apply_difference_cfg, remove_difference_cfg_patches
from sd_webui_difference_cfg.auto_reference import ALGORITHM, DEFAULT_BUDGET, restore_enabled
from sd_webui_difference_cfg.diagnostics import AutoDiagnostics, STATS_KEY, CONTRACT_KEY, GUARD_KEY

logger = logging.getLogger(__name__)

_DIFF_METHODS = ["linear_distance", "squared_distance", "root_distance", "absolute_sum"]
UI_BUDGET_DEFAULT = 0.03
UI_BUDGET_MAXIMUM = 0.10
UI_BUDGET_STEP = 0.005


def _restore_auto_budget(data):
    """Restore legacy values without clipping them to the normal UI range."""
    # Keep the old implicit value for historical metadata lacking this field.
    raw = data.get("DiffCFG Auto Budget", DEFAULT_BUDGET)
    try:
        value = float(raw)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("Budget outside the existing internal range")
    except (TypeError, ValueError, OverflowError):
        logger.warning("[DifferenceCFG] Invalid saved Auto Budget %r; using UI default 0.03", raw)
        value = UI_BUDGET_DEFAULT
    step = UI_BUDGET_STEP
    decimal_value = Decimal(str(value))
    # Unusual legacy/API values must not be rounded to the new step size.
    if decimal_value % Decimal(str(step)) != 0:
        step = min(step, float(Decimal(1).scaleb(decimal_value.as_tuple().exponent)))
    return gr.update(value=value, minimum=0.0,
                     maximum=max(UI_BUDGET_MAXIMUM, value), step=step)



def _has_forge_backend(p) -> bool:
    return hasattr(p, "sd_model") and hasattr(p.sd_model, "forge_objects")


def _build_infotext_params(cfg: dict) -> dict:
    """Build the infotext key/value dict.

    Keys use the "DiffCFG" prefix (independent of the retired SkimmedCFG
    Difference mode keys; no backward compatibility is provided). Presence of
    "DiffCFG Method" means the extension was active, so it doubles as the
    enable marker on the read side.
    """
    return {
        "DiffCFG Method":    cfg["method"],
        "DiffCFG Reference": cfg["reference_cfg"],
        "DiffCFG End At":    cfg["end_at"],
        "DiffCFG Auto Reference": cfg.get("auto_reference", False),
        "DiffCFG Auto Algorithm": cfg.get("auto_algorithm", ALGORITHM),
        "DiffCFG Auto Budget": cfg.get("auto_budget", DEFAULT_BUDGET),
    }


# ---------------------------------------------------------------------------
# Script
# ---------------------------------------------------------------------------

class DifferenceCFGScript(scripts.Script):

    sorting_priority = 14.2

    def __init__(self):
        self.enabled = False
        self.auto_diagnostics = AutoDiagnostics()
        self.reference_stats_active = False

    def title(self) -> str:
        return "Difference CFG"

    def show(self, is_img2img: bool):
        return scripts.AlwaysVisible

    def ui(self, is_img2img: bool):
        with gr.Accordion(open=False, label=self.title()):
            gr.HTML(
                "<p><i>"
                "<b>Pre-CFG</b>: Mask-free global re-adjustment of the unconditional "
                "prediction toward a reference CFG scale. Below the session CFG "
                "this softens high-guidance artifacts; above it, it strengthens."
                "</i></p>"
            )
            enabled       = gr.Checkbox(label="Enable Difference CFG", value=False)
            reference_cfg = gr.Slider(0.0, 20.0, value=5.0, step=0.5,
                                      label="Reference CFG")
            method        = gr.Dropdown(_DIFF_METHODS, value="linear_distance",
                                        label="Difference Method")
            end_at        = gr.Slider(0.0, 1.0, value=0.80, step=0.01,
                                      label="End At Percentage")
            auto_reference = gr.Checkbox(label="Auto Reference CFG (experimental)", value=False)
            gr.HTML("<p>Uses measured SkimmedCFG correction to limit additional correction. "
                    "SkimmedCFG Linear Interpolation or Single Scale + any DifferenceCFG method on reForge (v3). Not image-quality calibrated. "
                    "Session CFG and SkimmedCFG scale remain adjustable; CFG 20 / interpolation 3 is a tested setup. "
                    "Reference CFG remains the manual fallback. Results appear in the generation parameters.</p>")
            auto_budget = gr.Slider(0.0, UI_BUDGET_MAXIMUM, value=UI_BUDGET_DEFAULT,
                                    step=UI_BUDGET_STEP,
                                    label="Auto Correction Budget (provisional default: 0.03)")
            gr.HTML("<p>0.03 is a provisional preference-based default, not a universal optimum. "
                    "The normal range is 0 to 0.10. Loading an older PNG may temporarily "
                    "extend the range or refine the step to preserve its exact Budget.</p>")
            auto_algorithm = gr.Textbox(value=ALGORITHM, label="Auto Algorithm", visible=False)
            auto_algorithm.do_not_save_to_config = True
            auto_budget.do_not_save_to_config = True

            # ui-config.json persists slider value/min/max/step by label
            # string and silently overrides the code-defined values above on
            # startup. Both sliders are excluded from that mechanism so a
            # future change to the value= defaults above always takes
            # effect. See the APG / SkimmedCFG extensions for the same
            # pattern.
            for slider in (reference_cfg, end_at):
                slider.do_not_save_to_config = True

        # Infotext round-trip (PNG Info -> Send to txt2img / img2img).
        # Metadata is written in process(). "DiffCFG Method" is written only
        # when active, so its presence means ON, absence OFF; Enable therefore
        # binds to a callable that forces OFF when the key is absent. The other
        # keys use plain strings (absent keys leave the component untouched).
        self.infotext_fields = [
            (enabled,       lambda d: "DiffCFG Method" in d),
            (method,        "DiffCFG Method"),
            (reference_cfg, "DiffCFG Reference"),
            (end_at,        "DiffCFG End At"),
            (auto_reference, restore_enabled),
            (auto_budget, _restore_auto_budget),
            (auto_algorithm, lambda d: d.get("DiffCFG Auto Algorithm", ALGORITHM)),
        ]

        return [enabled, reference_cfg, method, end_at, auto_reference, auto_budget, auto_algorithm]

    # ------------------------------------------------------------------
    # Effective configuration (UI args + XYZ Grid override)
    # ------------------------------------------------------------------

    def _resolve(self, p, args):
        if len(args) < 4:
            return None
        (enabled, reference_cfg, method, end_at) = args[:4]
        auto_reference = bool(args[4]) if len(args) > 4 else False
        auto_budget = args[5] if len(args) > 5 else DEFAULT_BUDGET
        auto_algorithm = str(args[6]) if len(args) > 6 else ALGORITHM

        xyz = getattr(p, "_difference_cfg_xyz", {})
        if "enabled" in xyz:
            enabled = (xyz["enabled"] == "True")
        if "method" in xyz:
            method = xyz["method"]
        if "auto_reference" in xyz:
            auto_reference = str(xyz["auto_reference"]).lower() == "true"
        if "auto_budget" in xyz:
            auto_budget = float(xyz["auto_budget"])
        if "auto_reference" in xyz or "auto_budget" in xyz:
            auto_algorithm = ALGORITHM

        return {
            "enabled":       bool(enabled),
            "reference_cfg": float(reference_cfg),
            "method":        str(method),
            "end_at":        float(end_at),
            "auto_reference": auto_reference,
            "auto_budget": float(auto_budget),
            "auto_algorithm": auto_algorithm,
        }

    # ------------------------------------------------------------------
    # Metadata write (runs once before sampling so create_infotext captures it)
    # ------------------------------------------------------------------

    def process(self, p, *args):
        self.auto_diagnostics = AutoDiagnostics()
        self.reference_stats_active = False
        # Remove request-local measurements even when the next request is manual.
        p.extra_generation_params.pop(STATS_KEY, None)
        p.extra_generation_params.pop(CONTRACT_KEY, None)
        p.extra_generation_params.pop(GUARD_KEY, None)
        p.extra_generation_params.pop("DiffCFG Auto Status", None)
        cfg = self._resolve(p, args)
        if cfg is None or not cfg["enabled"]:
            return
        p.extra_generation_params.update(_build_infotext_params(cfg))

    # ------------------------------------------------------------------
    # Hook application (correct timing for forge_objects.unet)
    # ------------------------------------------------------------------

    def process_before_every_sampling(self, p, *args, **kwargs):
        cfg = self._resolve(p, args)
        if cfg is None:
            logger.warning("[DifferenceCFG] process_before_every_sampling: missing args")
            return

        self.enabled = cfg["enabled"]

        if not cfg["enabled"]:
            if _has_forge_backend(p):
                unet = p.sd_model.forge_objects.unet.clone()
                remove_difference_cfg_patches(unet)
                p.sd_model.forge_objects.unet = unet
            return

        if not _has_forge_backend(p):
            logger.warning("[DifferenceCFG] Requires Forge backend.")
            return

        unet = p.sd_model.forge_objects.unet.clone()

        apply_difference_cfg(
            unet,
            reference_cfg=cfg["reference_cfg"],
            method=cfg["method"],
            end_at_percentage=cfg["end_at"],
            auto_reference=cfg["auto_reference"],
            auto_budget=cfg["auto_budget"],
            auto_algorithm=cfg["auto_algorithm"],
            diagnostics=self.auto_diagnostics,
        )

        # Only the reForge Pre-CFG hook contributes reference statistics.
        self.reference_stats_active = self.reference_stats_active or any(
            getattr(fn, "_auto_counts", None) is self.auto_diagnostics
            for fn in unet.model_options.get("sampler_pre_cfg_function", [])
        )
        p.sd_model.forge_objects.unet = unet
        logger.debug("[DifferenceCFG] applied: method=%s", cfg["method"])

    def _write_contract_diagnostics(self, p):
        if self.auto_diagnostics.contract_reasons:
            p.extra_generation_params[CONTRACT_KEY] = self.auto_diagnostics.contract_summary()
        if self.auto_diagnostics.guard_counts:
            p.extra_generation_params[GUARD_KEY] = self.auto_diagnostics.guard_summary()

    def postprocess_batch(self, p, *args, **kwargs):
        self._write_contract_diagnostics(p)
        # This callback precedes image infotext creation and PNG saving.
        if self.reference_stats_active:
            p.extra_generation_params[STATS_KEY] = self.auto_diagnostics.reference_summary()
        if self.auto_diagnostics:
            summary = "; ".join("%s=%s" % item for item in sorted(self.auto_diagnostics.items()))
            p.extra_generation_params["DiffCFG Auto Status"] = summary

    def postprocess(self, p, processed, *args):
        self._write_contract_diagnostics(p)
        if self.auto_diagnostics.contract_reasons:
            print("[DifferenceCFG Auto Contract] " + self.auto_diagnostics.contract_summary(), flush=True)
        if self.auto_diagnostics.guard_counts:
            print("[DifferenceCFG Auto Guard] " + self.auto_diagnostics.guard_summary(), flush=True)
        if self.reference_stats_active:
            summary = self.auto_diagnostics.reference_summary()
            p.extra_generation_params[STATS_KEY] = summary
            print("[DifferenceCFG Auto Reference] " + summary, flush=True)
        if self.auto_diagnostics:
            summary = "; ".join("%s=%s" % item for item in sorted(self.auto_diagnostics.items()))
            print("[DifferenceCFG Auto] " + summary, flush=True)


# ---------------------------------------------------------------------------
# XYZ Grid
# ---------------------------------------------------------------------------

def _set_xyz(p, x: Any, xs: Any, *, field: str) -> None:
    if not hasattr(p, "_difference_cfg_xyz"):
        p._difference_cfg_xyz = {}
    p._difference_cfg_xyz[field] = x


def _register_xyz() -> None:
    xyz_grid = None
    for script in scripts.scripts_data:
        if script.script_class.__module__ == "xyz_grid.py":
            xyz_grid = script.module
            break
    if xyz_grid is None:
        return

    new_axes = [
        xyz_grid.AxisOption(
            "(Difference CFG) Enabled",
            str,
            partial(_set_xyz, field="enabled"),
            choices=lambda: ["True", "False"],
        ),
        xyz_grid.AxisOption(
            "(Difference CFG) Method",
            str,
            partial(_set_xyz, field="method"),
            choices=lambda: _DIFF_METHODS,
        ),
    ]

    new_axes.extend([
        xyz_grid.AxisOption("(Difference CFG) Auto Reference", str,
                            partial(_set_xyz, field="auto_reference"),
                            choices=lambda: ["True", "False"]),
        xyz_grid.AxisOption("(Difference CFG) Auto Budget", float,
                            partial(_set_xyz, field="auto_budget")),
    ])
    existing_labels = {axis.label for axis in xyz_grid.axis_options}
    xyz_grid.axis_options.extend(axis for axis in new_axes if axis.label not in existing_labels)


def _on_before_ui() -> None:
    try:
        _register_xyz()
    except Exception:
        print(f"[sd-webui-DifferenceCFG] XYZ Grid error:\n{traceback.format_exc()}")


script_callbacks.on_before_ui(_on_before_ui)
