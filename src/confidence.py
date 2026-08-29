"""§2.6 Calibrated confidence scoring — build-order step 7a.

    "Confidence score is built from the evidence object directly — count of
     matching fields, weighted by field importance (GSTIN and amount matter
     more than date formatting) — NOT an LLM's self-reported confidence
     number."

So the score here is a weighted count of matching fields and nothing else. It
is a pure function of the §2.4 Evidence object: no model is consulted, no
network call is made, and the same evidence always yields the same number. A
record's confidence can be re-derived by hand from its audit-log evidence
snapshot, which is the point — an auditor must be able to check the arithmetic.

CALIBRATION PROTOCOL (§2.6), and the discipline it imposes:

  1. The 70/30 split was assigned in data/generate_dataset.py at generation
     time — "before any tuning". It lives in ground_truth.csv and is never
     re-derived.
  2. `sweep_thresholds()` below picks the threshold that best separates true
     matches from true mismatches. It takes (score, label) pairs as ARGUMENTS.
     It does not open ground_truth.csv, and neither does anything else in
     src/ — supplying labels is the evaluation script's job, and only the
     calibration split may be supplied.
  3. The frozen 30% is touched exactly once, at the end, to produce the
     reported numbers. No re-tuning after that run.

The chosen threshold is frozen into src/rules/calibration_v2026_04.yaml. The
pipeline reads that number; it never recalculates it and never sees a label.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

from .evidence import Evidence

RULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")
DEFAULT_CALIBRATION_PATH = os.path.join(RULES_DIR, "calibration_v2026_04.yaml")

# ---------------------------------------------------------------------------
# Field weights — "GSTIN and amount matter more than date formatting"
# ---------------------------------------------------------------------------
# Hand-set from what each field says about identity and value, NOT fitted to
# the data. Only the gate threshold is calibrated; these are fixed inputs to
# the sweep. They sum to 100 so a score reads directly as a percentage.

FIELD_WEIGHTS: Dict[str, float] = {
    "gstin": 30.0,           # supplier identity — the strongest single signal
    "amount": 30.0,          # the money; a difference here is the whole point
    "invoice_number": 20.0,  # document identity
    "date": 10.0,            # useful, but formatting noise is common
    "vendor_name": 5.0,      # free text, already fuzzy by the time it gets here
    "tax_heads": 5.0,        # CGST/SGST vs IGST allocation
}

MAX_CONFIDENCE = sum(FIELD_WEIGHTS.values())

# Fields that must agree for evidence to count as "a clean match" (§2.6's
# second auto-reconcile condition). A date that differs by a day or a vendor
# name spelled differently is cosmetic; a different GSTIN, amount or invoice
# number is not.
CLEAN_MATCH_FIELDS: Tuple[str, ...] = ("gstin", "amount", "invoice_number")


@dataclass(frozen=True)
class ConfidenceScore:
    """A score plus the arithmetic that produced it, so it can be audited."""

    value: float                                  # 0..100
    matched_weight: float
    total_weight: float
    contributions: Dict[str, float] = field(default_factory=dict)
    clean_match: bool = False

    @property
    def fraction(self) -> float:
        return self.value / 100.0

    def explain(self) -> str:
        parts = [f"{name}={weight:g}"
                 for name, weight in sorted(self.contributions.items())
                 if weight > 0]
        return (f"{self.matched_weight:g}/{self.total_weight:g} = {self.value:.1f} "
                f"[{', '.join(parts) if parts else 'no field matched'}]")


def score_evidence(evidence: Evidence,
                   weights: Optional[Dict[str, float]] = None) -> ConfidenceScore:
    """Weighted count of matching fields. Pure function of the evidence.

    A `no_candidate_found` singleton has every field recorded as not matching,
    so it scores 0 — which is correct: there is no counterpart to be confident
    about.
    """
    weights = weights or FIELD_WEIGHTS
    contributions: Dict[str, float] = {}
    matched_weight = 0.0
    total_weight = 0.0

    for name, weight in weights.items():
        total_weight += weight
        matched = evidence.is_match(name)
        contributions[name] = weight if matched is True else 0.0
        if matched is True:
            matched_weight += weight

    value = round(100.0 * matched_weight / total_weight, 4) if total_weight else 0.0
    clean = evidence.candidate_found and all(
        evidence.is_match(name) is True for name in CLEAN_MATCH_FIELDS
    )

    return ConfidenceScore(
        value=value,
        matched_weight=matched_weight,
        total_weight=total_weight,
        contributions=contributions,
        clean_match=clean,
    )


# ---------------------------------------------------------------------------
# Threshold sweep (§2.6 step 2) — evaluation-side, calibration split only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SweepPoint:
    threshold: float
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int

    @property
    def sensitivity(self) -> float:
        pos = self.true_positives + self.false_negatives
        return self.true_positives / pos if pos else 0.0

    @property
    def specificity(self) -> float:
        neg = self.true_negatives + self.false_positives
        return self.true_negatives / neg if neg else 0.0

    @property
    def youden_j(self) -> float:
        """Sensitivity + specificity - 1. Robust to the class imbalance here
        (far more true matches than true mismatches), unlike raw accuracy."""
        return self.sensitivity + self.specificity - 1.0

    @property
    def accuracy(self) -> float:
        total = (self.true_positives + self.false_positives
                 + self.true_negatives + self.false_negatives)
        return (self.true_positives + self.true_negatives) / total if total else 0.0


@dataclass(frozen=True)
class CalibrationResult:
    threshold: float
    sweep: List[SweepPoint]
    n_samples: int
    n_positive: int
    plateau: Tuple[float, float]

    @property
    def best(self) -> SweepPoint:
        return min(self.sweep, key=lambda p: abs(p.threshold - self.threshold))


def evaluate_threshold(scored_labels: Sequence[Tuple[float, bool]],
                       threshold: float) -> SweepPoint:
    """Confusion counts at one threshold. label=True means 'a true match'."""
    tp = fp = tn = fn = 0
    for score, is_true_match in scored_labels:
        predicted = score >= threshold
        if is_true_match and predicted:
            tp += 1
        elif is_true_match and not predicted:
            fn += 1
        elif not is_true_match and predicted:
            fp += 1
        else:
            tn += 1
    return SweepPoint(threshold, tp, fp, tn, fn)


def sweep_thresholds(scored_labels: Sequence[Tuple[float, bool]],
                     start: float = 0.0, stop: float = 100.0,
                     step: float = 0.5) -> CalibrationResult:
    """§2.6 step 2: sweep thresholds, pick the best separator.

    `scored_labels` must be the CALIBRATION SPLIT ONLY. This function takes the
    data as an argument and reads no file — keeping ground truth out of src/
    entirely and making it impossible to calibrate on the frozen split by
    accident.

    Selection: maximise Youden's J. Because confidence is discrete, several
    thresholds usually tie; the widest tied run is a plateau, and the MIDPOINT
    of that plateau is chosen. Picking an endpoint would sit flush against a
    cluster of scores and generalise worse.
    """
    if not scored_labels:
        raise ValueError("cannot calibrate on an empty sample")

    sweep: List[SweepPoint] = []
    value = start
    while value <= stop + 1e-9:
        sweep.append(evaluate_threshold(scored_labels, round(value, 6)))
        value += step

    best_j = max(point.youden_j for point in sweep)
    tied = [point.threshold for point in sweep
            if abs(point.youden_j - best_j) < 1e-9]

    # Widest contiguous run of tied thresholds.
    runs: List[List[float]] = [[tied[0]]]
    for threshold in tied[1:]:
        if abs(threshold - runs[-1][-1] - step) < 1e-9:
            runs[-1].append(threshold)
        else:
            runs.append([threshold])
    widest = max(runs, key=len)
    plateau = (widest[0], widest[-1])
    chosen = round((plateau[0] + plateau[1]) / 2.0, 4)

    return CalibrationResult(
        threshold=chosen,
        sweep=sweep,
        n_samples=len(scored_labels),
        n_positive=sum(1 for _, label in scored_labels if label),
        plateau=plateau,
    )


# ---------------------------------------------------------------------------
# The frozen artifact the pipeline actually reads
# ---------------------------------------------------------------------------


def load_calibration(path: str = DEFAULT_CALIBRATION_PATH) -> dict:
    """Read the frozen calibration. The pipeline reads a NUMBER, never a label."""
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_threshold(path: str = DEFAULT_CALIBRATION_PATH) -> float:
    return float(load_calibration(path)["confidence_gate"]["threshold"])
