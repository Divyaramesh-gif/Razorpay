#!/usr/bin/env python3
"""§2.6 calibration: sweep the confidence threshold on the 70% split, then freeze it.

This is the EVALUATION script, not the pipeline. It is the only program in the
repository permitted to open ground_truth.csv, and §2.6 is explicit about the
discipline:

  1. The 70/30 split was assigned at dataset-generation time, before any
     tuning. This script reads it; it never re-derives it.
  2. The sweep sees the CALIBRATION SPLIT ONLY. Frozen-split rows are filtered
     out before a single threshold is evaluated.
  3. The frozen 30% is touched exactly once, at the end, to produce the
     reported numbers — and only when --report-frozen is passed. No re-tuning
     after that run.

Output: src/rules/calibration_v2026_04.yaml, which the pipeline reads as a
plain number. Re-running overwrites it, so re-running after seeing frozen
results would BE the re-tuning §2.6 forbids. Don't.

    python3 calibrate.py                    # sweep on the calibration split
    python3 calibrate.py --report-frozen    # ...and score the frozen 30% once
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import confidence as C
from src import evidence as E
from src import matcher as M
from src import normalization as N
from src import validation as V
from src.rule_engine import RuleEngine
from src.source_records import (
    REPO_ROOT,
    SOURCE_GSTR2B,
    SOURCE_PURCHASE_REGISTER,
    load_source,
)

GROUND_TRUTH_CSV = os.path.join(REPO_ROOT, "data", "ground_truth.csv")
CALIBRATION_SPLIT = "calibration"
FROZEN_SPLIT = "frozen_test"

# The label the gate is being calibrated against: ground truth says this record
# is a true match that should auto-reconcile.
TRUE_MATCH_OUTCOME = "auto_reconcile"


def build_scores():
    """Run the pipeline (Phases 2-3), then score every record's evidence."""
    pr_valid, _ = V.partition(load_source(SOURCE_PURCHASE_REGISTER))
    b2_valid, _ = V.partition(load_source(SOURCE_GSTR2B))
    result = M.match_records(
        [N.normalize_deterministic(r.record) for r in pr_valid],
        [N.normalize_deterministic(r.record) for r in b2_valid],
    )
    evidences = E.compare_all(result.matches)
    batch = RuleEngine().evaluate_batch(result.matches, evidences)
    return [
        (evaluation.record_id.split(":")[1], C.score_evidence(ev), evaluation, ev)
        for evaluation, ev in zip(batch.evaluations, evidences)
    ]


def load_labels():
    with open(GROUND_TRUTH_CSV, encoding="utf-8") as fh:
        return {row["pr_record_id"]: row for row in csv.DictReader(fh)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Exception Ledger — §2.6 calibration")
    ap.add_argument("--report-frozen", action="store_true",
                    help="score the frozen 30%% once, after calibrating")
    ap.add_argument("--out", default=C.DEFAULT_CALIBRATION_PATH)
    args = ap.parse_args(argv)

    print("Exception Ledger — §2.6 confidence-gate calibration\n")

    scored = build_scores()
    labels = load_labels()

    calibration, frozen = [], []
    for record_id, score, _evaluation, _ev in scored:
        row = labels[record_id]
        is_true_match = row["expected_outcome"] == TRUE_MATCH_OUTCOME
        bucket = calibration if row["split"] == CALIBRATION_SPLIT else frozen
        bucket.append((score.value, is_true_match))

    print(f"  split (assigned at generation time, before any tuning)")
    print(f"    calibration      {len(calibration):>4} records  "
          f"({sum(1 for _, l in calibration if l)} true matches)")
    print(f"    frozen_test      {len(frozen):>4} records  "
          f"(withheld)\n")

    # ---- §2.6 step 2: sweep, calibration split ONLY --------------------
    result = C.sweep_thresholds(calibration)
    best = result.best
    print(f"  sweep 0..100 step 0.5 on the CALIBRATION SPLIT ONLY")
    print(f"    best Youden's J  {best.youden_j:.4f} over plateau "
          f"{result.plateau[0]:g}..{result.plateau[1]:g}")
    print(f"    chosen threshold {result.threshold:g}  (plateau midpoint)")
    print(f"    at that threshold: sensitivity {best.sensitivity:.4f}  "
          f"specificity {best.specificity:.4f}  accuracy {best.accuracy:.4f}")
    print(f"    confusion:  TP {best.true_positives}  FP {best.false_positives}  "
          f"TN {best.true_negatives}  FN {best.false_negatives}\n")

    # ---- freeze it -----------------------------------------------------
    artifact = {
        "version": "2026-04",
        "calibrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": (
            "Architecture v2 §2.6. Threshold swept on the 70% calibration "
            "split only; the frozen 30% is touched once, at the end, and never "
            "tuned against. The split itself was assigned in "
            "data/generate_dataset.py before any tuning."
        ),
        "confidence_gate": {
            "threshold": float(result.threshold),
            "scale": "0-100, weighted count of matching evidence fields",
            "selection": "maximum Youden's J; midpoint of the widest tied plateau",
            "plateau": [float(result.plateau[0]), float(result.plateau[1])],
        },
        "field_weights": {k: float(v) for k, v in C.FIELD_WEIGHTS.items()},
        "clean_match_fields": list(C.CLEAN_MATCH_FIELDS),
        "calibration_split": {
            "n_records": result.n_samples,
            "n_true_matches": result.n_positive,
            "sensitivity": round(best.sensitivity, 6),
            "specificity": round(best.specificity, 6),
            "accuracy": round(best.accuracy, 6),
            "true_positives": best.true_positives,
            "false_positives": best.false_positives,
            "true_negatives": best.true_negatives,
            "false_negatives": best.false_negatives,
        },
    }

    if args.report_frozen:
        # §2.6 step 3: exactly once, at the end. Reported, never optimised.
        point = C.evaluate_threshold(frozen, result.threshold)
        artifact["frozen_test_split"] = {
            "n_records": len(frozen),
            "n_true_matches": sum(1 for _, l in frozen if l),
            "sensitivity": round(point.sensitivity, 6),
            "specificity": round(point.specificity, 6),
            "accuracy": round(point.accuracy, 6),
            "true_positives": point.true_positives,
            "false_positives": point.false_positives,
            "true_negatives": point.true_negatives,
            "false_negatives": point.false_negatives,
            "note": "Touched once at the stated threshold. Not re-tuned.",
        }
        print(f"  FROZEN 30% — scored ONCE at threshold {result.threshold:g}")
        print(f"    sensitivity {point.sensitivity:.4f}  "
              f"specificity {point.specificity:.4f}  "
              f"accuracy {point.accuracy:.4f}")
        print(f"    confusion:  TP {point.true_positives}  FP {point.false_positives}  "
              f"TN {point.true_negatives}  FN {point.false_negatives}\n")

    with open(args.out, "w", encoding="utf-8") as fh:
        yaml.safe_dump(artifact, fh, sort_keys=False, default_flow_style=False)

    print(f"  frozen to {os.path.relpath(args.out, REPO_ROOT)}")
    print("  The pipeline reads this number and never sees a label.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
