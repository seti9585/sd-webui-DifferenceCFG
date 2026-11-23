"""Experimental skim-budget-v2: measured correction budget, not image optimization."""

import math
from .diagnostics import AutoDiagnostics

import torch

KEY = "_seti_skimmedcfg_auto_v1"
ALGORITHM = "skim-budget-v2-experimental"
LEGACY_ALGORITHM = "skim-budget-v1-experimental"
ALGORITHMS = (ALGORITHM, LEGACY_ALGORITHM)
DEFAULT_BUDGET = 0.10
EPSILON = 0.000000000001
MAX_CFG = 1000.0


def restore_enabled(data):
    """Old PNGs must explicitly turn Auto off."""
    return str(data.get("DiffCFG Auto Reference", False)).lower() == "true"


def budget_reference(scale, skim_scale, gamma, ds_norm, v_norm, alignment):
    """Solve the scalar bounds after a single packed device-to-host transfer."""
    values = (scale, skim_scale, gamma, ds_norm, v_norm, alignment)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Non-finite budget input")
    if not (1 < scale <= MAX_CFG and 0 <= skim_scale <= MAX_CFG and 0 <= gamma <= 1):
        raise ValueError("Budget input outside supported range")
    if ds_norm < 0 or v_norm < 0:
        raise ValueError("Negative norm")
    if ds_norm <= EPSILON or v_norm <= EPSILON or gamma == 0:
        return scale
    nominal = max(0.0, scale - skim_scale)
    length = gamma * ds_norm / max(v_norm, EPSILON)
    aligned = gamma * ds_norm * ds_norm / alignment if alignment > 0 else math.inf
    return scale - max(0.0, min(nominal, length, aligned))


def make_auto_hook(manual, interpolate, method, end_sigma, gamma, algorithm, diagnostics):
    """Wrap only the opt-in path; preserve the legacy manual factory verbatim."""
    token = object()
    last_sequence = {}
    counts = diagnostics if diagnostics is not None else AutoDiagnostics()

    def status(name, reference=None, skim=None):
        counts[name] = counts.get(name, 0) + 1
        hook._auto_last_status = name
        hook._auto_last_reference = reference
        if name == "AUTO_APPLIED" and isinstance(counts, AutoDiagnostics):
            counts.record_reference(reference, skim)

    @torch.no_grad()
    def hook(args):
        options = args.get("model_options", {})
        record = options.pop(KEY, None)
        outputs = args["conds_out"]

        def skip(name):
            status(name)
            return outputs

        def fallback(name):
            status(name)
            # The legacy helper assumes a present, nonempty uncond tensor.
            if len(outputs) != 2 or outputs[1] is None or outputs[1].numel() == 0:
                return outputs
            return manual(args)

        try:
            sigma = args["sigma"]
            sigma = float(sigma.reshape(-1)[0]) if torch.is_tensor(sigma) else float(sigma)
            scale = float(args["cond_scale"])
            if not math.isfinite(sigma) or not math.isfinite(scale):
                return skip("AUTO_NUMERIC_INVALID")
            if sigma <= end_sigma:
                return skip("DIFF_GATE_OFF")
            if algorithm not in ALGORITHMS:
                return skip("AUTO_ALGORITHM_UNSUPPORTED")
            if method != "linear_distance" or scale <= 1 or options.get("sampler_cfg_function"):
                return fallback("MANUAL_FALLBACK_UNSUPPORTED")
            chain = options.get("sampler_pre_cfg_function", [])
            producers = [fn for fn in chain if getattr(fn, "_sd_webui_skimmed_cfg_marker", None)]
            if not producers or not any(getattr(fn, "_skim_auto_version", None) == 1 for fn in producers):
                return fallback("MANUAL_FALLBACK_NO_SKIM")
            # Adjacency excludes an unobserved modifier between the two hooks.
            if hook not in chain:
                return skip("AUTO_CONTRACT_INVALID")
            index = chain.index(hook)
            if len(producers) != 1 or index == 0 or chain[index - 1] is not producers[0]:
                return skip("AUTO_CONTRACT_INVALID")
            producer = producers[0]
            if not isinstance(record, dict):
                return skip("AUTO_CONTRACT_INVALID")
            sequence = record.get("sequence", 0)
            if (record.get("version") != 1 or record.get("token") is not token
                    or record.get("producer") is not producer
                    or sequence != producer._auto_sequence
                    or sequence <= last_sequence.get(producer, 0)
                    or record.get("outputs_id") != id(outputs)
                    or record.get("scale") != scale or record.get("sigma_value") != sigma
                    or record.get("input_ref", lambda: None)() is not args["input"]):
                return skip("AUTO_CONTRACT_INVALID")
            last_sequence[producer] = sequence
            mode = record.get("mode")
            if mode != getattr(producer, "_skim_auto_mode", None):
                return skip("AUTO_CONTRACT_INVALID")
            if mode not in hook._difference_auto_modes:
                return fallback("MANUAL_FALLBACK_UNSUPPORTED")
            state = record.get("state")
            if state in ("SKIPPED_GATE", "SKIPPED_UNCOND"):
                return fallback("MANUAL_FALLBACK_SKIM_SKIPPED")
            if state == "UNSUPPORTED":
                return fallback("MANUAL_FALLBACK_UNSUPPORTED")
            if state != "APPLIED":
                return skip("AUTO_CONTRACT_INVALID")
            c, u = outputs
            x = args["input"]
            if (record["cond_ref"]() is not c or record["uncond_ref"]() is not u
                    or record["versions"] != (c._version, u._version)
                    or tuple(c.shape) != record["shape"] or c.shape != u.shape
                    or x.shape != c.shape or c.ndim != 4 or c.numel() == 0
                    or c.dtype != record["dtype"] or u.dtype != c.dtype or x.dtype != c.dtype
                    or c.device != record["device"] or u.device != c.device or x.device != c.device):
                return skip("AUTO_CONTRACT_INVALID")
            ds = record["delta"]
            skim = record["skimming_cfg"]
            nominal = max(0.0, scale - skim)
            # Match the upstream residual expression (C + U, not C - U).
            normal = x - scale * c - (scale - 1) * u
            target = x - (scale - nominal) * c - (scale - nominal - 1) * u
            difference = (normal - target).abs()
            span = difference.max() - difference.min()
            weight = (difference - difference.min()) / span.clamp_min(EPSILON)
            weight = torch.where(span > 0, weight, torch.zeros_like(weight))
            v = -(1 - weight.to(ds.dtype)) * (c.to(ds.dtype) - u.to(ds.dtype))
            stats = torch.stack((ds.norm(), v.norm(), (ds * v).sum(),
                                 torch.isfinite(x).all().to(ds.dtype),
                                 torch.isfinite(normal).all().to(ds.dtype),
                                 torch.isfinite(target).all().to(ds.dtype))).tolist()
            if stats[3:] != [1.0, 1.0, 1.0]:
                return skip("AUTO_NUMERIC_INVALID")
            reference = budget_reference(scale, skim, float(gamma), *stats[:3])
            if reference == scale:
                status("AUTO_NO_CORRECTION", reference)
                return outputs
            candidate = interpolate(x, c, u, scale, reference)
            # Check the actual legacy output, including low-precision rounding.
            actual = (1 - scale) * (candidate.to(ds.dtype) - u.to(ds.dtype))
            measured = torch.stack((actual.norm(), (ds * actual).sum(),
                                    torch.isfinite(candidate).all().to(ds.dtype))).tolist()
            allowance = float(gamma) * stats[0]
            tolerance = EPSILON + allowance * 0.00001
            if (not all(math.isfinite(value) for value in measured) or measured[2] != 1
                    or measured[0] > allowance + tolerance
                    or measured[1] > float(gamma) * stats[0] ** 2 + tolerance * stats[0]):
                return skip("AUTO_NUMERIC_INVALID")
            outputs[1] = candidate
            status("AUTO_APPLIED", reference, skim)
            return outputs
        except (ValueError, OverflowError):
            return skip("AUTO_NUMERIC_INVALID")
        except Exception:
            # Never misreport a failed contract or contaminate the skim output.
            return skip("AUTO_CONTRACT_INVALID")

    hook.__name__ = manual.__name__
    hook.__dict__.update(manual.__dict__)
    hook._difference_auto_token = token
    hook._difference_auto_modes = (
        ("single_scale", "lin_interp") if algorithm == ALGORITHM else ("single_scale",)
    )
    hook._auto_counts = counts
    hook._auto_last_status = None
    hook._auto_last_reference = None
    return hook
