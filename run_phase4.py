#!/usr/bin/env python3
"""Phase 4 sample run: confidence -> three-way gate -> audit log.

Runs the whole pipeline as built so far (Phases 2-4). Temporary scaffolding —
src/pipeline.py (build step 8) is the real orchestrator.

Reads the frozen threshold from src/rules/calibration_v2026_04.yaml. It never
opens ground_truth.csv: calibration is calibrate.py's job, and by this point
the threshold is just a number.

Phase 4 stops at the audit log. The §2.7 evaluation report is Phase 5.

    python3 run_phase4.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import confidence as C
from src import evidence as E
from src import gate as G
from src import matcher as M
from src import normalization as N
from src import validation as V
from src.audit_log import AuditLog
from src.quarantine_log import QuarantineLog
from src.rule_engine import (
    RuleEngine,
    STATUS_BREACHED,
    STATUS_OUTSIDE_WINDOW,
    STATUS_WITHIN_WINDOW,
)
from src.source_records import (
    REPO_ROOT,
    SOURCE_GSTR2B,
    SOURCE_PURCHASE_REGISTER,
    load_source,
)

OUT_DIR = os.path.join(REPO_ROOT, "out")
DB_PATH = os.path.join(OUT_DIR, "exception_ledger.sqlite")


def heading(title):
    print(f"\n{title}\n{'-' * len(title)}")


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    print("Exception Ledger — Phase 4 (confidence -> gate -> audit log)")

    # --- Phases 2-3 -------------------------------------------------------
    heading("Stages 1-3  validation, quarantine, normalisation (§2.1, §2.2)")
    normalised, quarantined_ids = {}, []
    with QuarantineLog(DB_PATH) as qlog:
        qlog.clear()
        for source in (SOURCE_PURCHASE_REGISTER, SOURCE_GSTR2B):
            records = load_source(source)
            valid, invalid = V.partition(records)
            qlog.quarantine_all(invalid)
            normalised[source] = [N.normalize_deterministic(r.record) for r in valid]
            print(f"  {source:<20} {len(records):>4} read  "
                  f"{len(valid):>4} valid  {len(invalid):>3} quarantined")
        quarantined_ids = [e.record_id for e in qlog.entries()]
        quarantine_count = qlog.count()

    heading("Stages 4-6  matching, evidence, rules (§2.3, §2.4, §2.5)")
    result = M.match_records(normalised[SOURCE_PURCHASE_REGISTER],
                             normalised[SOURCE_GSTR2B])
    evidences = E.compare_all(result.matches)
    engine = RuleEngine()
    batch = engine.evaluate_batch(result.matches, evidences)
    print(f"  matched {len(result.matched)}  no_candidate_found "
          f"{len(result.no_candidate)}  unmatched 2B {len(result.unmatched_2b)}")
    print(f"  classification rules fired on {len(batch.classification_table())} "
          f"record(s); rules v{batch.rules_version}")

    # --- §2.6 confidence --------------------------------------------------
    heading("Stage 7a  confidence scoring (§2.6)")
    calibration = C.load_calibration()
    threshold = C.load_threshold()
    scores = [C.score_evidence(ev) for ev in evidences]

    print(f"  source            src/rules/calibration_v2026_04.yaml "
          f"(v{calibration['version']})")
    print(f"  threshold         {threshold:g}   "
          f"(plateau {calibration['confidence_gate']['plateau'][0]:g}.."
          f"{calibration['confidence_gate']['plateau'][1]:g}, midpoint)")
    print(f"  field weights     " + ", ".join(
        f"{k}={v:g}" for k, v in C.FIELD_WEIGHTS.items()))
    print(f"  clean-match set   {', '.join(C.CLEAN_MATCH_FIELDS)}")
    print(f"\n  score distribution:")
    for value, n in sorted(Counter(s.value for s in scores).items(), reverse=True):
        bar = "#" * min(52, n // 6 + 1)
        side = ">= threshold" if value >= threshold else "<  threshold"
        print(f"    {value:6.1f}  {side}  {bar} {n}")

    # --- §2.6 gate --------------------------------------------------------
    heading("Stage 7b  three-way confidence gate (§2.6)")
    decisions = G.decide_batch(batch.evaluations, evidences, threshold,
                               quarantined_record_ids=quarantined_ids)
    tally = G.counts(decisions)
    total = len(decisions)
    for outcome in G.OUTCOMES:
        n = tally[outcome]
        print(f"  {outcome:<24} {n:>4}  ({100 * n / total:5.1f}% of scored)")
    print(f"  {'-' * 24} {'-' * 4}")
    print(f"  {'scored (passed §2.1)':<24} {total:>4}")
    print(f"  {'quarantined (§2.1 exit)':<24} {quarantine_count:>4}  "
          f"reported separately — NOT folded into any outcome above")

    by_category = Counter(d.category for d in decisions
                          if d.outcome == G.CLASSIFIED_EXCEPTION)
    print("\n  classified exceptions by named category:")
    for category, n in sorted(by_category.items()):
        print(f"    {category:<32} {n:>4}")

    review = [d for d in decisions if d.needs_human_review]
    print(f"\n  routed to human review: {len(review)}")

    # --- §2.7 audit log ---------------------------------------------------
    heading("Stage 8a  audit log (§2.7)")
    with AuditLog(DB_PATH) as alog:
        alog.clear()
        alog.record_all(decisions, evidences)
        print(f"  rows written      {alog.count():>4}  "
              f"(one per record that passed validation)")
        print(f"  by action         " + ", ".join(
            f"{k}={v}" for k, v in sorted(alog.counts_by_action().items())))
        print(f"  by rule fired     " + ", ".join(
            f"{k}={v}" for k, v in sorted(alog.counts_by_rule().items())))
        print(f"  pending review    {len(alog.pending_review()):>4}  "
              f"(reviewer_decision still null)")
        print(f"  tables in db      {', '.join(alog.tables())}")

        sample = alog.entries(G.INDETERMINATE)[0]
        print(f"\n  Sample audit row — an indeterminate record:")
        print(f"    record_id          {sample.record_id}")
        print(f"    confidence_score   {sample.confidence_score}")
        print(f"    rule_id_fired      {sample.rule_id_fired or '(none)'}")
        print(f"    action             {sample.action}")
        print(f"    reviewer_decision  {sample.reviewer_decision}")
        print(f"    timestamp          {sample.timestamp}")
        snapshot = sample.evidence()
        matched = [k for k, v in snapshot["fields"].items() if v["match"] is True]
        differing = [k for k, v in snapshot["fields"].items() if v["match"] is False]
        print(f"    evidence_snapshot  matched: {', '.join(matched) or 'none'}")
        print(f"                       differing: {', '.join(differing) or 'none'}")

    # --- worked example ---------------------------------------------------
    heading("Worked example — one record of each outcome")
    for outcome in G.OUTCOMES:
        decision = next(d for d in decisions if d.outcome == outcome)
        print(f"\n  [{outcome}]  {decision.record_id}  {decision.invoice_id}")
        print(f"    confidence  {decision.confidence.explain()}")
        print(f"    clean match {decision.confidence.clean_match}")
        print(f"    reason      {decision.reason}")

    # --- outputs ----------------------------------------------------------
    decisions_csv = os.path.join(OUT_DIR, "phase4_decisions.csv")
    with open(decisions_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["record_id", "invoice_id", "confidence_score", "threshold",
                    "clean_match", "rule_id_fired", "category", "action",
                    "needs_human_review", "reason"])
        for d in decisions:
            w.writerow([d.record_id, d.invoice_id, f"{d.confidence.value:.2f}",
                        f"{d.threshold:g}", d.confidence.clean_match,
                        d.rule_id or "", d.category or "", d.outcome,
                        d.needs_human_review, d.reason])

    summary = {
        "threshold": threshold,
        "calibration_version": calibration["version"],
        "scored_records": total,
        "quarantined_records": quarantine_count,
        "gate_outcomes": tally,
        "classified_by_category": {k: v for k, v in sorted(by_category.items())},
        "needs_human_review": len(review),
        "operational_checks": {
            "OPS-88D": {
                "within_window": sum(1 for e in batch.evaluations
                                     if e.flag("OPS-88D").status == STATUS_WITHIN_WINDOW),
                "outside_window": sum(1 for e in batch.evaluations
                                      if e.flag("OPS-88D").status == STATUS_OUTSIDE_WINDOW),
            },
            "OPS-DRC01C": {
                "breached_records": sum(1 for e in batch.evaluations
                                        if e.flag("OPS-DRC01C").status == STATUS_BREACHED),
            },
        },
    }
    summary_path = os.path.join(OUT_DIR, "phase4_summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")

    heading("Files written")
    for path in (decisions_csv, summary_path, DB_PATH):
        print(f"  {os.path.relpath(path, REPO_ROOT):<34} "
              f"{os.path.getsize(path):>9,} bytes")

    print("\n  Phase 4 stops here. The §2.7 evaluation report — match rate broken")
    print("  out exact/fuzzy/rule-classified, the two operational tables, and the")
    print("  frozen-test numbers — is Phase 5 (src/report.py).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
