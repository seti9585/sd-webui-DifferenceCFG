"""CPU-only statistics for successful Auto Reference CFG applications."""

import math

STATS_KEY = "DiffCFG Auto Reference Stats"
CONTRACT_KEY = "DiffCFG Auto Contract"
GUARD_KEY = "DiffCFG Auto Guard"
FLOOR_ABS_TOLERANCE = 0.000000001


def _decimal(value):
    """Use ordinary decimals for readable console and PNG metadata."""
    return format(value, ".9f").rstrip("0").rstrip(".") or "0"


class AutoDiagnostics(dict):
    """Keep legacy status counts and constant-size, request-local statistics."""

    def __init__(self):
        super().__init__()
        self.applied_count = 0
        self.reference_min = None
        self.reference_mean = 0.0
        self.reference_max = None
        self.floor_hits = 0
        self.contract_reasons = {}
        self.contract_details = {}
        self.guard_counts = {}

    def record_reference(self, reference, skim):
        """Accept existing Python scalars only; never convert a tensor."""
        if type(reference) not in (int, float) or type(skim) not in (int, float):
            return
        if not math.isfinite(reference) or not math.isfinite(skim):
            return
        self.applied_count += 1
        self.reference_min = reference if self.reference_min is None else min(self.reference_min, reference)
        self.reference_max = reference if self.reference_max is None else max(self.reference_max, reference)
        self.reference_mean += (reference - self.reference_mean) / self.applied_count
        if math.isclose(reference, skim, rel_tol=0.0, abs_tol=FLOOR_ABS_TOLERANCE):
            self.floor_hits += 1

    def record_contract(self, reason, detail=""):
        self.contract_reasons[reason] = self.contract_reasons.get(reason, 0) + 1
        if detail and reason not in self.contract_details:
            self.contract_details[reason] = detail.replace("\n", " ")[:240]

    def contract_summary(self):
        return "; ".join(
            "%s=%d%s" % (reason, count,
                         " [" + self.contract_details[reason] + "]"
                         if reason in self.contract_details else "")
            for reason, count in sorted(self.contract_reasons.items())
        )

    def record_guard(self, name):
        self.guard_counts[name] = self.guard_counts.get(name, 0) + 1

    def guard_summary(self):
        return "; ".join("%s=%d" % item for item in sorted(self.guard_counts.items()))

    def reference_summary(self):
        """Count denoiser calls, not images, steps or tensor elements."""
        if not self.applied_count:
            return "count=0; min=N/A; mean=N/A; max=N/A; floor_hits=0; floor_rate=N/A"
        rate = 100.0 * self.floor_hits / self.applied_count
        return (
            "count=%d; min=%s; mean=%s; max=%s; floor_hits=%d; floor_rate=%s%%"
            % (self.applied_count, _decimal(self.reference_min),
               _decimal(self.reference_mean), _decimal(self.reference_max),
               self.floor_hits, _decimal(rate))
        )
