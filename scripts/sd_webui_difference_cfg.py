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

logger = logging.getLogger(__name__)

_DIFF_METHODS = ["linear_distance", "squared_distance", "root_distance", "absolute_sum"]


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
    }


# ---------------------------------------------------------------------------
# Script
# ---------------------------------------------------------------------------

class DifferenceCFGScript(scripts.Script):

    sorting_priority = 14.2

    def __init__(self):
        self.enabled = False

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
        ]

        return [enabled, reference_cfg, method, end_at]

    # ------------------------------------------------------------------
    # Effective configuration (UI args + XYZ Grid override)
    # ------------------------------------------------------------------

    def _resolve(self, p, args):
        if len(args) < 4:
            return None
        (enabled, reference_cfg, method, end_at) = args[:4]

        xyz = getattr(p, "_difference_cfg_xyz", {})
        if "enabled" in xyz:
            enabled = (xyz["enabled"] == "True")
        if "method" in xyz:
            method = xyz["method"]

        return {
            "enabled":       bool(enabled),
            "reference_cfg": float(reference_cfg),
            "method":        str(method),
            "end_at":        float(end_at),
        }

    # ------------------------------------------------------------------
    # Metadata write (runs once before sampling so create_infotext captures it)
    # ------------------------------------------------------------------

    def process(self, p, *args):
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
        )

        p.sd_model.forge_objects.unet = unet
        logger.debug("[DifferenceCFG] applied: method=%s", cfg["method"])


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

    if not any(x.label.startswith("(Difference CFG)") for x in xyz_grid.axis_options):
        xyz_grid.axis_options.extend(new_axes)


def _on_before_ui() -> None:
    try:
        _register_xyz()
    except Exception:
        print(f"[sd-webui-DifferenceCFG] XYZ Grid error:\n{traceback.format_exc()}")


script_callbacks.on_before_ui(_on_before_ui)
