#!/usr/bin/env python3
"""Phase 3 sample run: matching -> evidence -> rules.

Temporary scaffolding, NOT part of the locked §4 layout — src/pipeline.py
(build step 8) is the real orchestrator. Runs Phase 2 first (validation,
quarantine, normalisation) because Phase 3 consumes its output.

Stops at the rule engine. There is deliberately NO confidence score and NO
auto-reconcile / exception / indeterminate outcome here: that is the §2.6 gate,
build step 7, which does not exist yet.

    python3 run_phase3.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import evidence as E
from src import matcher as M
from src import normalization as N
from src import validation as V
from src.quarantine_log import QuarantineLog
from src.rule_engine import (
    RuleEngine,
    STATUS_BREACHED,
    STATUS_NOT_APPLICABLE,
    STATUS_OUTSIDE_WINDOW,
    STATUS_WITHIN_THRESHOLD,
    STATUS_WITHIN_WINDOW,
)
from src.source_records import (
    REPO_ROOT,
    SOURCE_GSTR2B,
    SOURCE_PURCHASE_REGISTER,
    load_source,
)

OUT_DIR = os.path.join(REPO_ROOT, "out")


def rule(title):
    print(f"\n{title}\n{'-' * len(title)}")


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    print("Exception Ledger — Phase 3 (matching -> evidence -> rules)")

    # --- Phase 2 (already built) -----------------------------------------
    rule("Stage 1-3  validation, quarantine, normalisation (§2.1, §2.2)")
    normalised = {}
    with QuarantineLog(os.path.join(OUT_DIR, "exception_ledger.sqlite")) as log:
        log.clear()
        for source in (SOURCE_PURCHASE_REGISTER, SOURCE_GSTR2B):
            records = load_source(source)
            valid, invalid = V.partition(records)
            log.quarantine_all(invalid)
            normalised[source] = [N.normalize_deterministic(r.record) for r in valid]
            print(f"  {source:<20} {len(records):>4} read  "
                  f"{len(valid):>4} valid  {len(invalid):>3} quarantined")
        quarantined = log.count()

    pr_records = normalised[SOURCE_PURCHASE_REGISTER]
    b2_records = normalised[SOURCE_GSTR2B]

    # --- §2.3 matching ----------------------------------------------------
    rule("Stage 4  exact + fuzzy ONE-TO-ONE matching (§2.3)")
    result = M.match_records(pr_records, b2_records)
    print(f"  score matrix           {len(pr_records)} x {len(b2_records)} = "
          f"{result.matrix_size:,} cells evaluated")
    print(f"  candidate pairs        {result.candidate_pairs:,} scored >= "
          f"{M.MIN_CANDIDATE_SCORE:g}")
    print(f"  matched                {len(result.matched):>4}")
    print(f"  no_candidate_found     {len(result.no_candidate):>4}  "
          f"(valid output, feeds the rule engine as an absence case)")
    print(f"  unmatched 2B rows      {len(result.unmatched_2b):>4}  "
          f"(claimed by nobody — the one-to-one guarantee)")

    claimed = [m.b2_id for m in result.matched]
    print(f"\n  ONE-TO-ONE CHECK       {len(claimed)} pairs, "
          f"{len(set(claimed))} distinct 2B records  -> "
          f"{'OK' if len(claimed) == len(set(claimed)) else 'VIOLATED'}")

    # --- §2.4 evidence ----------------------------------------------------
    rule("Stage 5  field-by-field evidence comparison (§2.4)")
    evidences = E.compare_all(result.matches)
    field_mismatches = Counter()
    for ev in evidences:
        if ev.candidate_found:
            field_mismatches.update(ev.mismatched_fields())
    print(f"  evidence objects       {len(evidences):>4}")
    print(f"  mismatching fields across matched pairs:")
    for name, n in field_mismatches.most_common():
        print(f"      {name:<18} {n:>4}")
    if not field_mismatches:
        print("      (none)")

    # --- §2.5 rules -------------------------------------------------------
    engine = RuleEngine()
    batch = engine.evaluate_batch(result.matches, evidences)

    rule(f"Stage 6a  CLASSIFICATION RULES — what happened "
         f"(rules v{batch.rules_version})")
    by_rule = Counter((e.classification.rule_id, e.classification.category)
                      for e in batch.evaluations if e.classification.fired)
    for (rule_id, category), n in sorted(by_rule.items()):
        print(f"  {rule_id:<10} {category:<30} {n:>4}")
    unclassified = len(batch.unclassified())
    print(f"  {'—':<10} {'no rule fired':<30} {unclassified:>4}  "
          f"(input to the §2.6 gate, not a verdict)")

    rule("Stage 6b  OPERATIONAL CHECKS — what to do, and by when")
    print("  (reported as a SEPARATE table — §2.5: do not merge with the above)")
    w88d = Counter(e.flag("OPS-88D").status for e in batch.evaluations)
    print(f"\n  OPS-88D    Rule 88D 7-day response window")
    print(f"      within_window      {w88d[STATUS_WITHIN_WINDOW]:>4}")
    print(f"      outside_window     {w88d[STATUS_OUTSIDE_WINDOW]:>4}")
    print(f"      not_applicable     {w88d[STATUS_NOT_APPLICABLE]:>4}")

    drc = Counter(e.flag("OPS-DRC01C").status for e in batch.evaluations)
    threshold = engine.operational_checks["OPS-DRC01C"]["parameters"]["threshold_rupees"]
    breaching = {g for g, v in batch.itc_variance_by_gstin.items() if v > threshold}
    print(f"\n  OPS-DRC01C cumulative ITC variance vs Rs.{threshold:,.2f} trigger")
    print(f"      breached           {drc[STATUS_BREACHED]:>4} record(s) "
          f"across {len(breaching)} of {len(batch.itc_variance_by_gstin)} suppliers")
    print(f"      within_threshold   {drc[STATUS_WITHIN_THRESHOLD]:>4} record(s)")

    top = sorted(batch.itc_variance_by_gstin.items(), key=lambda kv: -kv[1])[:3]
    print("      highest-variance suppliers:")
    for gstin, value in top:
        mark = "BREACH" if value > threshold else "ok"
        print(f"          {gstin}  Rs.{value:>12,.2f}  {mark}")

    # --- worked example ---------------------------------------------------
    rule("Worked example — one classified exception, end to end")
    example = next(e for e in batch.evaluations
                   if e.classification.category == "gstin_header_mismatch")
    ev = next(x for x in evidences if x.pr_record_id == example.record_id)
    match = next(m for m in result.matches if m.pr_id == example.record_id)
    print(f"  record            {example.record_id}  invoice {example.invoice_id}")
    print(f"  matched to        {ev.b2_record_id}  (score {match.score:.1f} / "
          f"{M.MAX_SCORE:g})")
    print(f"  score components  " + ", ".join(
        f"{k}={v:g}" for k, v in match.components.items()))
    print(f"  evidence          matched: {', '.join(ev.matched_fields()) or 'none'}")
    print(f"                    differing: {', '.join(ev.mismatched_fields()) or 'none'}")
    print(f"  classification    {example.classification.rule_id} "
          f"{example.classification.category}")
    print(f"  reason            {example.classification.reason}")
    for flag in example.operational_flags:
        print(f"  {flag.check_id:<10}        {flag.status}: {flag.detail}")

    # --- outputs ----------------------------------------------------------
    matches_csv = os.path.join(OUT_DIR, "phase3_matches.csv")
    with open(matches_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["pr_record_id", "b2_record_id", "invoice_id", "status", "score",
                    "matched_fields", "mismatched_fields", "rule_id", "category",
                    "reason", "ops_88d_status", "ops_drc01c_status"])
        for m, ev, e in zip(result.matches, evidences, batch.evaluations):
            w.writerow([
                m.pr_id, m.b2_id or "", ev.invoice_id, m.status, f"{m.score:.2f}",
                "|".join(ev.matched_fields()), "|".join(ev.mismatched_fields()),
                e.classification.rule_id or "", e.classification.category or "",
                e.classification.reason,
                e.flag("OPS-88D").status, e.flag("OPS-DRC01C").status,
            ])

    evidence_json = os.path.join(OUT_DIR, "phase3_evidence.json")
    with open(evidence_json, "w", encoding="utf-8") as fh:
        json.dump([{"invoice_id": ev.invoice_id, "pr_record_id": ev.pr_record_id,
                    "b2_record_id": ev.b2_record_id,
                    "candidate_found": ev.candidate_found,
                    "fields": ev.field_map()} for ev in evidences],
                  fh, indent=2, sort_keys=True)
        fh.write("\n")

    summary = {
        "rules_version": batch.rules_version,
        "quarantined": quarantined,
        "matching": {
            "matrix_cells": result.matrix_size,
            "candidate_pairs": result.candidate_pairs,
            "min_candidate_score": M.MIN_CANDIDATE_SCORE,
            "matched": len(result.matched),
            "no_candidate_found": len(result.no_candidate),
            "unmatched_2b": len(result.unmatched_2b),
            "one_to_one_holds": len(claimed) == len(set(claimed)),
        },
        "classification_rules": {f"{r}|{c}": n for (r, c), n in sorted(by_rule.items())},
        "no_rule_fired": unclassified,
        "operational_checks": {
            "OPS-88D": dict(w88d),
            "OPS-DRC01C": dict(drc),
            "suppliers_breaching": len(breaching),
            "suppliers_total": len(batch.itc_variance_by_gstin),
        },
    }
    summary_path = os.path.join(OUT_DIR, "phase3_summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")

    rule("Files written")
    for path in (matches_csv, evidence_json, summary_path):
        print(f"  out/{os.path.basename(path):<28} {os.path.getsize(path):>9,} bytes")

    print("\n  Phase 3 stops here. No confidence score and no auto-reconcile /")
    print("  exception / indeterminate outcome — that is the §2.6 gate (step 7).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
