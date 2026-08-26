"""
core.py — Difference CFG algorithm
===================================
Location: extensions/sd-webui-DifferenceCFG/sd_webui_difference_cfg/core.py

Based on:
    Extraltodeus/Skimmed_CFG (current upstream, comfy_api.latest rewrite) --
    the DifferenceCFG_PreCFG node family.

Difference CFG is a mask-free, reference-scale-based global re-adjustment of
the unconditional prediction. Unlike Skimmed CFG it does NOT select individual
"over-influenced" elements via a skimming mask; instead it either:

  * absolute_sum : matches the whole guidance residual's L1 norm at the
                   session CFG scale to what it would be at a lower reference
                   scale, deriving a single global fallback weight, then
                   interpolates the entire uncond toward that reference; or
  * *_distance   : builds a per-element soft weight from the normalized
                   absolute difference between the CFG residual at the session
                   scale and at the reference scale, then interpolates each
                   uncond element between "uncond re-scaled to the reference
                   scale" (where the residuals agree) and the original uncond
                   (where they diverge). linear / squared / root shape that
                   soft weight differently.

Because it shares none of Skimmed CFG's skimming machinery
(get_skimming_mask / skimmed_CFG), it is maintained as its own extension
rather than as a Skimmed CFG mode.

This module is intentionally self-contained: the small amount of backend
infrastructure it needs (backend detection, priority-ordered post-CFG
insertion, TCFG stash read, sigma helpers, percent_to_sigma) is duplicated
here rather than imported from any sibling extension, so each extension's
core stands alone.

Backend-adaptive hooking (same pattern as sd-webui-SkimmedCFG / sd-webui-TCFG):
    * reForge / Forge Classic -> Pre-CFG  (dict args, "conds_out" style)
    * Forge Neo               -> Post-CFG (dict args, "denoised" style;
                                  Forge Neo's pre-CFG runs before model
                                  evaluation, so cond/uncond predictions are
                                  not available there)

Both backends provide "sigma" in the hook args dict, so the end_at gating
works identically on both paths.

Composition with TCFG on Forge Neo:
    TCFG (priority 13.0) stashes its damped uncond into
    model_options["_tcfg_damped_uncond"]. This extension reads that key when
    present and falls back to the raw uncond_denoised otherwise, so behaviour
    is identical whether or not TCFG runs alongside it. Priority 14.2 keeps it
    after SkimmedCFG (14.0) when both are active (a supported combination: use
    SkimmedCFG to level a high session CFG, then Difference CFG to re-adjust
    the leveled uncond).
"""

import logging
import os
import sys

import torch

logger = logging.getLogger(__name__)

MARKER = "sd_webui_difference_cfg_v1"

# Mirrors DifferenceCFGScript.sorting_priority in
# scripts/sd_webui_difference_cfg.py. Kept in sync manually; used only to order
# this extension's hook within Forge Neo's sampler_post_cfg_function list
# relative to other SETI extensions (TCFG=13.0 and SkimmedCFG=14.0 run before
# this, MaHiRo=15.5 runs after).
_PRIORITY = 14.2

# Suite-wide debug convention: 0 = off, 1 = apply-time settings + chain dump.
DEBUG_ENV_VAR = "SD_WEBUI_SETI_DEBUG"

# One chain dump per sampling pass. Reset by apply_difference_cfg().
_CHAIN_DUMPED = False


def _debug_level():
    try:
        return int(os.environ.get(DEBUG_ENV_VAR, "0"))
    except Exception:
        return 0


def _emit(level, fmt, *args):
    """Emit to both logging and stderr; some forks suppress module loggers."""
    if _debug_level() < level:
        return
    try:
        msg = (fmt % args) if args else fmt
    except Exception:
        msg = str(fmt)
    text = "[DifferenceCFG] " + msg
    logger.warning(text)
    try:
        print(text, file=sys.stderr, flush=True)
    except Exception:
        pass


def _describe_chain(fns):
    """Render a hook list as 'name(priority)' in actual execution order."""
    parts = []
    for fn in fns or []:
        name = getattr(fn, "__name__", None) or type(fn).__name__
        prio = getattr(fn, "_sd_webui_priority", None)
        parts.append("%s(%s)" % (name, "-" if prio is None else prio))
    return " -> ".join(parts) if parts else "(empty)"


def _maybe_dump_chain(args) -> None:
    """Emit the pre-CFG chain once per pass, from inside the hook, so what is
    printed is the list as the sampler actually holds it at call time. The
    suite's post-CFG dump (sd-webui-FreSca) reads sampler_post_cfg_function
    and cannot see this list."""
    global _CHAIN_DUMPED
    if _CHAIN_DUMPED or _debug_level() < 1:
        return
    _CHAIN_DUMPED = True
    try:
        opts = args.get("model_options") or {}
        _emit(1, "pre-CFG chain: %s",
              _describe_chain(opts.get("sampler_pre_cfg_function")))
    except Exception as exc:
        _emit(1, "pre-CFG chain dump failed: %r", exc)

# Sentinel returned by percent_to_sigma for percent <= 0 (ComfyUI convention).
_SIGMA_SENTINEL_MAX = 999999999.9

_DIFF_METHODS = ("linear_distance", "squared_distance", "root_distance", "absolute_sum")


# ---------------------------------------------------------------------------
# Backend detection (duplicated; identical logic to sd-webui-TCFG/SkimmedCFG)
# ---------------------------------------------------------------------------

_BACKEND_IS_NEO = None  # cached


def _is_forge_neo_backend() -> bool:
    """
    Return True if the active backend is Forge Neo.

    Forge Neo's sampler_pre_cfg_function is called BEFORE model evaluation, so
    denoised predictions are not available there. On reForge / Forge Classic
    the pre-CFG hook receives a single dict whose "conds_out" already holds the
    predictions.

    Detection: Forge Neo ships backend.sampling.sampling_function with
    sampling_function_inner and calc_cond_uncond_batch; reForge / Classic use
    ldm_patched.modules.samplers instead.
    """
    global _BACKEND_IS_NEO
    if _BACKEND_IS_NEO is not None:
        return _BACKEND_IS_NEO

    is_neo = False
    try:
        from backend.sampling import sampling_function as _sf
        is_neo = (
            hasattr(_sf, "sampling_function_inner")
            and hasattr(_sf, "calc_cond_uncond_batch")
        )
    except Exception:
        is_neo = False

    _BACKEND_IS_NEO = is_neo
    logger.debug("[DifferenceCFG] backend detected: %s", "Forge Neo" if is_neo else "reForge / Forge Classic")
    return is_neo


# ---------------------------------------------------------------------------
# Priority-ordered insertion for Forge Neo's post-cfg list (duplicated)
# ---------------------------------------------------------------------------

def _priority_insert_post_cfg(unet, fn) -> None:
    """
    Insert fn into unet.model_options["sampler_post_cfg_function"] at the
    position that keeps SETI-suite hooks (those carrying a _sd_webui_priority
    attribute) in ascending priority order -- e.g. TCFG (13.0) before
    SkimmedCFG (14.0) before DifferenceCFG (14.2) before MaHiRo (15.5) --
    regardless of the order in which their apply_*() functions happened to run
    this call. Third-party hooks without that attribute are left exactly where
    they already are; only the new fn's position relative to them is decided
    (inserted before the first tracked hook with a strictly greater priority,
    otherwise appended at the end).
    """
    key = "sampler_post_cfg_function"
    existing = unet.model_options.get(key, [])
    priority = fn._sd_webui_priority

    insert_at = len(existing)
    for i, other in enumerate(existing):
        other_priority = getattr(other, "_sd_webui_priority", None)
        if other_priority is not None and other_priority > priority:
            insert_at = i
            break

    unet.model_options[key] = existing[:insert_at] + [fn] + existing[insert_at:]


# ---------------------------------------------------------------------------
# Priority-ordered insertion for the reForge / Forge Classic pre-cfg list
# ---------------------------------------------------------------------------

def _priority_insert_pre_cfg(unet, fn, disable_cfg1_optimization: bool = False) -> None:
    """
    Twin of _priority_insert_post_cfg for the pre-CFG list. Identical
    semantics, different key.

    Replaces the plain append that set_model_sampler_pre_cfg_function
    performs. That append made execution order follow extension LOAD order
    instead of _sd_webui_priority. Because extensions load alphabetically
    (APG, DifferenceCFG, SkimmedCFG, TCFG), the reForge pre-CFG chain ran in
    exactly the reverse of the documented order
    TCFG (13.0) -> SkimmedCFG (14.0) -> DifferenceCFG (14.2) -> APG (14.5).
    Forge Neo was unaffected: that path already used
    _priority_insert_post_cfg.

    disable_cfg1_optimization mirrors the flag that
    set_model_sampler_pre_cfg_function sets, so callers relying on it keep
    working.

    A new list is built rather than mutating in place, matching the backend
    helper's semantics, so a cloned unet never leaks the change into its
    source. Duplicated deliberately: each extension carries its own copy so
    no cross-extension import dependency exists.
    """
    key = "sampler_pre_cfg_function"
    existing = unet.model_options.get(key, [])
    priority = fn._sd_webui_priority

    insert_at = len(existing)
    for i, other in enumerate(existing):
        other_priority = getattr(other, "_sd_webui_priority", None)
        if other_priority is not None and other_priority > priority:
            insert_at = i
            break

    unet.model_options[key] = existing[:insert_at] + [fn] + existing[insert_at:]

    if disable_cfg1_optimization:
        unet.model_options["disable_cfg1_optimization"] = True


def _stashed_tcfg_uncond(args: dict):
    """Return TCFG's damped uncond from model_options if TCFG ran earlier
    in this same post-cfg call, else None."""
    model_options = args.get("model_options")
    if not isinstance(model_options, dict):
        return None
    return model_options.get("_tcfg_damped_uncond")


# ---------------------------------------------------------------------------
# Sigma helpers (duplicated)
# ---------------------------------------------------------------------------

def _sigma_scalar(sigma) -> float:
    """Extract a Python float from the hook args' "sigma" entry.

    Both backends pass the timestep tensor (shape [batch]); the upstream node
    reads element 0, reproduced here with a reshape so 0-dim tensors are also
    tolerated.
    """
    if isinstance(sigma, torch.Tensor):
        return sigma.reshape(-1)[0].item()
    return float(sigma)


def _percent_to_sigma(unet, percent: float):
    """ComfyUI-compatible percent -> sigma conversion for the active backend.

    Boundary convention (short-circuited here so the gating default is inert
    on any backend): percent <= 0 -> 999999999.9, percent >= 1 -> 0.0.

    Mid-range values are resolved through the first available provider:
      1. unet.get_model_object("model_sampling")  (reForge / Forge Classic)
      2. unet.model.predictor                     (Forge Neo)
      3. unet.model.model_sampling                (defensive fallback)

    Returns None if no provider exposing percent_to_sigma can be located; the
    caller then substitutes a value that disables the gate.
    """
    if percent <= 0.0:
        return _SIGMA_SENTINEL_MAX
    if percent >= 1.0:
        return 0.0

    candidates = []

    get_obj = getattr(unet, "get_model_object", None)
    if callable(get_obj):
        try:
            candidates.append(get_obj("model_sampling"))
        except Exception:
            pass

    inner_model = getattr(unet, "model", None)
    if inner_model is not None:
        candidates.append(getattr(inner_model, "predictor", None))
        candidates.append(getattr(inner_model, "model_sampling", None))

    for obj in candidates:
        if obj is not None and hasattr(obj, "percent_to_sigma"):
            try:
                return float(obj.percent_to_sigma(percent))
            except Exception:
                logger.warning("[DifferenceCFG] percent_to_sigma call failed", exc_info=True)
                return None

    return None


# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------

@torch.no_grad()
def interpolated_scales(
    x_orig, cond, uncond, cond_scale, small_scale, squared=False, root_dist=False
):
    """Verbatim port of the upstream helper used by the *_distance methods.

    Builds a per-element soft weight from the normalized absolute difference
    between the CFG residual at cond_scale and at small_scale, then
    interpolates each uncond element between "uncond re-scaled to small_scale"
    (where the difference is small) and the original uncond (where the
    difference is large).
    """
    deltacfg_normal = x_orig - cond_scale * cond - (cond_scale - 1) * uncond
    deltacfg_small = x_orig - small_scale * cond - (small_scale - 1) * uncond
    absdiff = (deltacfg_normal - deltacfg_small).abs()

    # Fix division by zero (upstream comment)
    diff_range = absdiff.max() - absdiff.min()
    if diff_range > 0:
        absdiff = (absdiff - absdiff.min()) / diff_range
    else:
        absdiff = torch.zeros_like(absdiff)

    if squared:
        absdiff = absdiff ** 2
    elif root_dist:
        absdiff = absdiff ** 0.5

    new_scale = (small_scale - 1) / (cond_scale - 1) if cond_scale > 1 else 0.0
    smaller_uncond = cond * (1 - new_scale) + uncond * new_scale
    new_uncond = smaller_uncond * (1 - absdiff) + uncond * absdiff
    return new_uncond


# ---------------------------------------------------------------------------
# Pre-CFG factory (reForge / Forge Classic)
# ---------------------------------------------------------------------------

def _make_difference_fn(reference_cfg: float, method: str, end_at_sigma: float):
    """Difference CFG — Pre-CFG (dict / conds_out style).

    Verbatim port of the upstream DifferenceCFG_PreCFG.execute.pre_cfg_patch.
    The whole uncond tensor is re-derived, either globally (absolute_sum:
    L1-norm matching against the reference scale) or per element with a
    continuous weight (linear/squared/root distance via interpolated_scales).
    Applies only while sigma > end_at_sigma (upstream default
    end_at_percentage = 0.80, i.e. the last 20 percent of the schedule is left
    untouched).
    """
    @torch.no_grad()
    def _fn(args):
        _maybe_dump_chain(args)
        conds_out  = args["conds_out"]
        cond_scale = args["cond_scale"]
        x_orig     = args["input"]
        sigma      = _sigma_scalar(args["sigma"])

        if not torch.any(conds_out[1]) or sigma <= end_at_sigma:
            return conds_out

        if method == "absolute_sum":
            ref_norm = (
                conds_out[0] * reference_cfg - conds_out[1] * (reference_cfg - 1)
            ).norm(p=1)
            cfg_norm = (
                conds_out[0] * cond_scale - conds_out[1] * (cond_scale - 1)
            ).norm(p=1)

            # Fix: Prevent division by zero (upstream comment)
            if cfg_norm == 0:
                return conds_out

            new_scale = cond_scale * ref_norm / cfg_norm

            # Fix: Prevent division by zero (upstream comment)
            if cond_scale <= 1:
                return conds_out

            fallback_weight = (new_scale - 1) / (cond_scale - 1)
            conds_out[1] = (
                conds_out[0] * (1 - fallback_weight)
                + conds_out[1] * fallback_weight
            )
        elif method in ("linear_distance", "squared_distance", "root_distance"):
            conds_out[1] = interpolated_scales(
                x_orig,
                conds_out[0],
                conds_out[1],
                cond_scale,
                reference_cfg,
                method == "squared_distance",
                method == "root_distance",
            )
        return conds_out

    _fn.__name__ = "_differencecfg_pre_cfg_fn"
    _fn._sd_webui_difference_cfg_marker = MARKER
    # Ordering tag read by _priority_insert_pre_cfg. Previously only the
    # Forge Neo post-CFG factory carried this, so the reForge pre-CFG hook
    # was invisible to priority-based insertion.
    _fn._sd_webui_priority = _PRIORITY
    return _fn


# ---------------------------------------------------------------------------
# Post-CFG factory (Forge Neo)
# ---------------------------------------------------------------------------
# Forge Neo post-CFG args dict keys:
#   "denoised"         — current CFG result (x0 estimate)
#   "cond_denoised"    — positive prediction
#   "uncond_denoised"  — negative prediction (None when CFG=1 / uncond off)
#   "cond_scale"       — CFG scale
#   "input"            — x_t (noisy latent)
#   "sigma"            — timestep tensor
#   "model_options"    — shared dict; read for TCFG's stashed damped uncond
#
# Mirrors the Pre-CFG factory: re-derive the whole uncond, then recompute CFG
# linearly:
#   denoised = uncond_new + cond_scale * (cond - uncond_new)
#
# If TCFG (or SkimmedCFG) ran earlier in this same post-cfg list, TCFG's damped
# uncond (when stashed) is used as the starting point instead of the raw
# uncond_denoised. Early returns hand back args["denoised"] unchanged, which
# preserves the output of any earlier hook in the list.
# ---------------------------------------------------------------------------

def _make_difference_post_fn(reference_cfg: float, method: str, end_at_sigma: float):
    """Difference CFG — Post-CFG (Forge Neo).

    Mirrors _make_difference_fn. Working copies are cloned for safety even
    though no in-place masked writes are performed here.
    """
    @torch.no_grad()
    def _fn(args):
        uncond_denoised = args.get("uncond_denoised")
        if uncond_denoised is None or not torch.any(uncond_denoised):
            return args["denoised"]

        sigma = _sigma_scalar(args["sigma"])
        if sigma <= end_at_sigma:
            return args["denoised"]

        x_orig     = args["input"]
        cond_scale = args["cond_scale"]
        cond   = args["cond_denoised"]
        tcfg_uncond = _stashed_tcfg_uncond(args)
        uncond = (tcfg_uncond if tcfg_uncond is not None else uncond_denoised).clone()

        if method == "absolute_sum":
            ref_norm = (
                cond * reference_cfg - uncond * (reference_cfg - 1)
            ).norm(p=1)
            cfg_norm = (
                cond * cond_scale - uncond * (cond_scale - 1)
            ).norm(p=1)

            if cfg_norm == 0:
                return args["denoised"]
            new_scale = cond_scale * ref_norm / cfg_norm
            if cond_scale <= 1:
                return args["denoised"]

            fallback_weight = (new_scale - 1) / (cond_scale - 1)
            uncond = cond * (1 - fallback_weight) + uncond * fallback_weight
        elif method in ("linear_distance", "squared_distance", "root_distance"):
            uncond = interpolated_scales(
                x_orig,
                cond,
                uncond,
                cond_scale,
                reference_cfg,
                method == "squared_distance",
                method == "root_distance",
            )
        else:
            return args["denoised"]

        return uncond + cond_scale * (cond - uncond)

    _fn.__name__ = "_differencecfg_post_cfg_fn"
    _fn._sd_webui_difference_cfg_marker = MARKER
    _fn._sd_webui_priority = _PRIORITY
    return _fn


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def _is_difference_cfg_fn(fn) -> bool:
    return getattr(fn, "_sd_webui_difference_cfg_marker", None) == MARKER


def remove_difference_cfg_patches(unet) -> None:
    """Remove all DifferenceCFG patches from both pre- and post-CFG lists.

    Only this extension's own hooks (identified by MARKER) are removed, so
    other extensions' pre/post-CFG functions are left untouched.
    """
    for key in ("sampler_pre_cfg_function", "sampler_post_cfg_function"):
        existing = unet.model_options.get(key)
        if isinstance(existing, list):
            unet.model_options[key] = [fn for fn in existing if not _is_difference_cfg_fn(fn)]


def apply_difference_cfg(unet, reference_cfg: float, method: str, end_at_percentage: float):
    """
    Register Difference CFG on unet, choosing the correct hook for the backend.

      * Forge Neo               -> Post-CFG, priority-ordered so it runs after
                                    TCFG / SkimmedCFG and before MaHiRo.
      * reForge / Forge Classic -> Pre-CFG.

    Parameters:
      reference_cfg     : target lower CFG scale to re-adjust uncond toward
      method            : one of _DIFF_METHODS
      end_at_percentage : schedule fraction after which the patch is inert
                          (upstream default 0.80)
    """
    remove_difference_cfg_patches(unet)

    if method not in _DIFF_METHODS:
        logger.warning("[DifferenceCFG] unknown method %r; falling back to linear_distance", method)
        method = "linear_distance"

    end_at_sigma = _percent_to_sigma(unet, float(end_at_percentage))
    if end_at_sigma is None:
        logger.warning("[DifferenceCFG] percent_to_sigma unavailable; end_at gating disabled")
        end_at_sigma = 0.0

    logger.info(
        "[DifferenceCFG] method: %s / reference scale: %s / end at sigma: %s",
        method, reference_cfg, round(end_at_sigma, 2),
    )

    global _CHAIN_DUMPED
    _CHAIN_DUMPED = False   # one chain dump per sampling pass

    pre_fn  = _make_difference_fn(reference_cfg, method, end_at_sigma)
    post_fn = _make_difference_post_fn(reference_cfg, method, end_at_sigma)

    if _is_forge_neo_backend():
        _priority_insert_post_cfg(unet, post_fn)
        logger.debug("[DifferenceCFG] registered post-CFG hook (Forge Neo backend)")
    else:
        # v1.1: priority-ordered insertion replaces the plain append that
        # set_model_sampler_pre_cfg_function performs. See
        # _priority_insert_pre_cfg for why.
        _priority_insert_pre_cfg(unet, pre_fn)
        _emit(1, "registered pre-CFG hook (reForge / Forge Classic), "
                 "priority=%s", _PRIORITY)

    return unet
