"""Experimental correction budgets with versioned PNG behavior."""

import math
from .diagnostics import AutoDiagnostics

import torch

KEY = "_seti_skimmedcfg_auto_v1"
ALGORITHM = "skim-budget-v3-experimental"
V2_ALGORITHM = "skim-budget-v2-experimental"
LEGACY_ALGORITHM = "skim-budget-v1-experimental"
ALGORITHMS = (ALGORITHM, V2_ALGORITHM, LEGACY_ALGORITHM)
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

        def reject(reason, detail=""):
            hook._auto_last_contract_reason = reason
            hook._auto_last_contract_detail = detail
            if isinstance(counts, AutoDiagnostics):
                counts.record_contract(reason, detail)
            return skip("AUTO_CONTRACT_INVALID")

        def fallback(name):
            status(name)
            # The legacy helper assumes a present, nonempty uncond tensor.
            if len(outputs) != 2 or outputs[1] is None or outputs[1].numel() == 0:
                return outputs
            return manual(args)

        phase = "runtime"
        hook._auto_last_contract_reason = None
        hook._auto_last_contract_detail = ""
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
            supported_methods = ("linear_distance", "squared_distance", "root_distance", "absolute_sum") if algorithm == ALGORITHM else ("linear_distance",)
            if method not in supported_methods or scale <= 1 or options.get("sampler_cfg_function"):
                return fallback("MANUAL_FALLBACK_UNSUPPORTED")
            chain = options.get("sampler_pre_cfg_function", [])
            producers = [fn for fn in chain if getattr(fn, "_sd_webui_skimmed_cfg_marker", None)]
            if not producers or not any(getattr(fn, "_skim_auto_version", None) == 1 for fn in producers):
                return fallback("MANUAL_FALLBACK_NO_SKIM")
            phase = "chain"
            if hook not in chain:
                return reject("CHAIN_CONSUMER_MISSING")
            index = chain.index(hook)
            if len(producers) != 1:
                return reject("CHAIN_PRODUCER_COUNT")
            if index == 0 or chain[index - 1] is not producers[0]:
                names = " -> ".join(getattr(fn, "__name__", type(fn).__name__) for fn in chain)
                return reject("CHAIN_ORDER", names[:240])
            producer = producers[0]
            phase = "record_identity"
            if not isinstance(record, dict):
                return reject("RECORD_MISSING")
            if record.get("version") != 1:
                return reject("RECORD_VERSION")
            if record.get("token") is not token:
                return reject("PASS_TOKEN")
            if record.get("producer") is not producer:
                return reject("PRODUCER_IDENTITY")
            sequence = record.get("sequence", 0)
            if sequence != producer._auto_sequence:
                return reject("SEQUENCE_CURRENT")
            if sequence <= last_sequence.get(producer, 0):
                return reject("SEQUENCE_REPLAY")
            if record.get("outputs_id") != id(outputs):
                return reject("OUTPUTS_IDENTITY")
            if record.get("scale") != scale:
                return reject("SCALE_MISMATCH")
            if record.get("sigma_value") != sigma:
                return reject("SIGMA_MISMATCH")
            if record.get("input_ref", lambda: None)() is not args["input"]:
                return reject("INPUT_IDENTITY")
            last_sequence[producer] = sequence
            mode = record.get("mode")
            if mode != getattr(producer, "_skim_auto_mode", None):
                return reject("MODE_MISMATCH")
            if mode not in hook._difference_auto_modes:
                return fallback("MANUAL_FALLBACK_UNSUPPORTED")
            state = record.get("state")
            if state in ("SKIPPED_GATE", "SKIPPED_UNCOND"):
                return fallback("MANUAL_FALLBACK_SKIM_SKIPPED")
            if state == "UNSUPPORTED":
                return fallback("MANUAL_FALLBACK_UNSUPPORTED")
            if state == "ERROR":
                detail = "%s: %s: %s" % (
                    record.get("error_stage", "unknown"),
                    record.get("error", "unknown"), record.get("error_message", ""))
                return reject("OBSERVATION_ERROR", detail)
            if state != "APPLIED":
                return reject("STATE_INVALID", str(state))
            phase = "prediction_metadata"
            c, u = outputs
            x = args["input"]
            if record.get("cond_ref", lambda: None)() is not c:
                return reject("COND_IDENTITY")
            if record.get("uncond_ref", lambda: None)() is not u:
                return reject("UNCOND_IDENTITY")
            if tuple(c.shape) != record.get("shape"):
                return reject("SHAPE_RECORDED")
            if c.shape != u.shape:
                return reject("SHAPE_PAIR")
            if x.shape != c.shape:
                return reject("SHAPE_INPUT")
            if c.ndim != 4:
                return reject("DIMENSION")
            if c.numel() == 0:
                return reject("EMPTY_PREDICTION")
            if c.dtype != record.get("dtype"):
                return reject("DTYPE_RECORDED")
            if u.dtype != c.dtype:
                return reject("DTYPE_UNCOND")
            if x.dtype != c.dtype:
                return reject("DTYPE_INPUT")
            if c.device != record.get("device"):
                return reject("DEVICE_RECORDED")
            if u.device != c.device:
                return reject("DEVICE_UNCOND")
            if x.device != c.device:
                return reject("DEVICE_INPUT")
            phase = "mutation_guard"
            versions = record.get("versions")
            snapshots = record.get("snapshots", (None, None))
            if not isinstance(versions, tuple) or len(versions) != 2:
                return reject("GUARD_VERSIONS_MISSING")
            if not isinstance(snapshots, tuple) or len(snapshots) != 2:
                return reject("GUARD_SNAPSHOTS_INVALID")
            content_checks = []
            for i, (name, tensor) in enumerate((("COND", c), ("UNCOND", u))):
                if torch.is_inference(tensor):
                    snapshot = snapshots[i]
                    if versions[i] is not None or not torch.is_tensor(snapshot):
                        return reject(name + "_SNAPSHOT_MISSING")
                    if (snapshot is tensor or snapshot.data_ptr() == tensor.data_ptr()
                            or snapshot.shape != tensor.shape or snapshot.dtype != tensor.dtype
                            or snapshot.device != tensor.device):
                        return reject(name + "_SNAPSHOT_INVALID")
                    content_checks.append((name, tensor, snapshot))
                elif versions[i] != tensor._version:
                    return reject(name + "_VERSION")
            phase = "auto_computation"
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
            if method == "squared_distance":
                weight = weight ** 2
            elif method == "root_distance":
                weight = weight ** 0.5
            v = -(1 - weight.to(ds.dtype)) * (c.to(ds.dtype) - u.to(ds.dtype))
            if method == "absolute_sum":
                # The reverse triangle inequality bounds the change in the
                # native L1 ratio for every reference between skim and scale.
                # Use the absolute alignment: the L1 curve can turn around.
                cfg_norm = (c * scale - u * (scale - 1)).norm(p=1)
                direction = c.to(ds.dtype) - u.to(ds.dtype)
                sensitivity = scale * direction.norm(p=1) / cfg_norm.to(ds.dtype).clamp_min(EPSILON)
                v = -sensitivity * direction
            packed_stats = [ds.norm(), v.norm(), (ds * v).sum(),
                            torch.isfinite(x).all().to(ds.dtype),
                            torch.isfinite(normal).all().to(ds.dtype),
                            torch.isfinite(target).all().to(ds.dtype)]
            # Reuse the existing packed transfer; do not synchronize for equality.
            packed_stats.extend(torch.eq(tensor, snapshot).all().to(ds.dtype)
                                for _, tensor, snapshot in content_checks)
            stats = torch.stack(packed_stats).tolist()
            if method == "absolute_sum":
                stats[2] = abs(stats[2])
            if stats[3:6] != [1.0, 1.0, 1.0]:
                return skip("AUTO_NUMERIC_INVALID")
            for (name, _, _), unchanged in zip(content_checks, stats[6:]):
                if unchanged != 1.0:
                    return reject(name + "_CONTENT_CHANGED")
            if isinstance(counts, AutoDiagnostics):
                counts.record_guard("INFERENCE_SNAPSHOT" if content_checks else "VERSION_COUNTER")
            reference = budget_reference(scale, skim, float(gamma), *stats[:3])
            if reference == scale:
                status("AUTO_NO_CORRECTION", reference)
                return outputs
            if method == "absolute_sum":
                ref_norm = (c * reference - u * (reference - 1)).norm(p=1)
                new_scale = scale * ref_norm / cfg_norm
                fallback_weight = (new_scale - 1) / (scale - 1)
                candidate = c * (1 - fallback_weight) + u * fallback_weight
            else:
                candidate = interpolate(x, c, u, scale, reference,
                                        method == "squared_distance", method == "root_distance")
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
        except Exception as exc:
            return reject("CONTRACT_EXCEPTION",
                          "%s: %s: %s" % (phase, type(exc).__name__, str(exc)[:160]))

    hook.__name__ = manual.__name__
    hook.__dict__.update(manual.__dict__)
    hook._difference_auto_token = token
    hook._difference_auto_modes = (
        ("single_scale", "lin_interp") if algorithm in (ALGORITHM, V2_ALGORITHM) else ("single_scale",)
    )
    hook._auto_counts = counts
    hook._auto_last_status = None
    hook._auto_last_reference = None
    hook._auto_last_contract_reason = None
    hook._auto_last_contract_detail = ""
    return hook
