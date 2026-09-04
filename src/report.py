"""§2.7 Evaluation report — build-order step 8.

    Evaluation report (frozen test set only):
      - Overall match rate (exact / fuzzy / rule-classified, broken out
        separately)
      - Classification-rule exceptions, listed by named category
      - Operational-check flags (Rule 88D window status, DRC-01C threshold
        status), listed SEPARATELY from the classification exceptions
      - Indeterminate count
      - Quarantined-record count, reported as its own line, not folded into
        any of the above

THIS IS THE EVALUATION SCRIPT, NOT THE PIPELINE. §2.6 reserves the
ground_truth.csv labels for exactly one consumer, and this is it (alongside
calibrate.py). src/pipeline.py never imports this module and never sees a
split; grading happens here, afterwards, on a finished PipelineResult.

`build_report()` takes the split map as an ARGUMENT rather than reading it, so
the frozen-set restriction is supplied explicitly by the caller and the
function itself has no way to widen its own scope. `load_splits()` is the
separate helper that touches the file.

Two report boundaries the architecture states in the negative, and which this
module keeps:

  * Classification exceptions and operational flags are two tables, not one
    blended list. A record can appear in both; they answer different questions
    ("what happened" vs "what to do, and by when").
  * The quarantined count is its own line. It is never added into the match
    rate, the exception count or the indeterminate count — a quarantined
    record was never scored, and folding it in would misstate what the report
    says.
"""

from __future__ import annotations

import csv
import os
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .confidence import FIELD_WEIGHTS
from .evidence import Evidence
from .gate import AUTO_RECONCILE, CLASSIFIED_EXCEPTION, INDETERMINATE
from .pipeline import PipelineResult
from .rule_engine import (
    STATUS_BREACHED,
    STATUS_NOT_APPLICABLE,
    STATUS_OUTSIDE_WINDOW,
    STATUS_WITHIN_THRESHOLD,
    STATUS_WITHIN_WINDOW,
)
from .source_records import REPO_ROOT

GROUND_TRUTH_CSV = os.path.join(REPO_ROOT, "data", "ground_truth.csv")

FROZEN_TEST = "frozen_test"
CALIBRATION = "calibration"

# §2.7's three match-rate buckets, derived from the pipeline's OWN output —
# never from a label.
MATCH_EXACT = "exact"
MATCH_FUZZY = "fuzzy"
MATCH_RULE_CLASSIFIED = "rule_classified"
MATCH_NONE = "none"

MATCH_TYPES = (MATCH_EXACT, MATCH_FUZZY, MATCH_RULE_CLASSIFIED)


def classify_match_type(evidence: Evidence, outcome: str) -> str:
    """Which §2.7 bucket a resolved record falls into.

    Derived from the evidence and the gate outcome, so the report describes
    what the pipeline actually did rather than what the answer key says.
    """
    if outcome == AUTO_RECONCILE:
        return MATCH_EXACT if not evidence.mismatched_fields() else MATCH_FUZZY
    if outcome == CLASSIFIED_EXCEPTION:
        return MATCH_RULE_CLASSIFIED
    return MATCH_NONE


# ---------------------------------------------------------------------------
# Report structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchRate:
    exact: int = 0
    fuzzy: int = 0
    rule_classified: int = 0
    unresolved: int = 0

    @property
    def resolved(self) -> int:
        return self.exact + self.fuzzy + self.rule_classified

    @property
    def total(self) -> int:
        return self.resolved + self.unresolved

    @property
    def rate(self) -> float:
        return self.resolved / self.total if self.total else 0.0

    def pct(self, n: int) -> float:
        return 100.0 * n / self.total if self.total else 0.0


@dataclass(frozen=True)
class ConfidenceSummary:
    mean: float
    minimum: float
    maximum: float
    threshold: float
    by_outcome: Dict[str, Dict[str, float]] = field(default_factory=dict)
    distribution: Dict[float, int] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditSummary:
    rows: int
    by_action: Dict[str, int]
    by_rule: Dict[str, int]
    pending_review: int
    db_path: str
    table: str = "audit_log"


@dataclass(frozen=True)
class Accuracy:
    """Graded against ground truth. Reported, never fed back into tuning."""

    correct: int
    total: int
    disagreements: List[tuple] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class EvaluationReport:
    split: str
    scored: int
    match_rate: MatchRate
    exceptions_by_category: Dict[str, int]
    operational_88d: Dict[str, int]
    operational_drc01c: Dict[str, int]
    indeterminate: int
    quarantined: int
    quarantined_by_error: Dict[str, int]
    confidence: ConfidenceSummary
    audit: AuditSummary
    accuracy: Optional[Accuracy]
    rules_version: str
    dataset_seed: int
    suppliers_breaching_drc01c: int
    suppliers_total: int
    itc_exposure_total: float = 0.0
    itc_exposure_breaching: float = 0.0
    # ITC exposure and supplier counts are WHOLE BATCH, not split-scoped:
    # DRC-01C variance is cumulative per supplier across the batch.
    itc_exposure_scope: str = "whole_batch"
    batch_scored: int = 0
    throughput: Dict[str, float] = field(default_factory=dict)
    ai_stats: Dict[str, int] = field(default_factory=dict)
    ai_assisted: bool = False
    ai_fell_back_entirely: bool = False


# ---------------------------------------------------------------------------
# Labels (evaluation-side only)
# ---------------------------------------------------------------------------


def load_splits(path: str = GROUND_TRUTH_CSV) -> Dict[str, str]:
    """record_id -> split. The one place this module reads the answer key."""
    with open(path, encoding="utf-8") as fh:
        return {f"purchase_register:{row['pr_record_id']}": row["split"]
                for row in csv.DictReader(fh)}


def load_expected_outcomes(path: str = GROUND_TRUTH_CSV) -> Dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        return {f"purchase_register:{row['pr_record_id']}": row["expected_outcome"]
                for row in csv.DictReader(fh)}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_report(result: PipelineResult,
                 splits: Dict[str, str],
                 split: str = FROZEN_TEST,
                 expected_outcomes: Optional[Dict[str, str]] = None,
                 dataset_seed: int = 20260401) -> EvaluationReport:
    """§2.7 report, restricted to one split.

    `splits` and `expected_outcomes` are arguments, not file reads: the caller
    decides what this report is allowed to see.
    """
    joined = result.by_record()
    in_split = [rid for rid in joined if splits.get(rid) == split]

    match_counts = Counter()
    exceptions = Counter()
    ops_88d = Counter()
    ops_drc = Counter()
    scores: List[float] = []
    by_outcome_scores: Dict[str, List[float]] = {}
    indeterminate = 0

    for record_id in in_split:
        row = joined[record_id]
        decision, evidence, evaluation = (
            row["decision"], row["evidence"], row["evaluation"])

        match_counts[classify_match_type(evidence, decision.outcome)] += 1
        if decision.outcome == CLASSIFIED_EXCEPTION:
            exceptions[decision.category] += 1
        if decision.outcome == INDETERMINATE:
            indeterminate += 1

        ops_88d[evaluation.flag("OPS-88D").status] += 1
        ops_drc[evaluation.flag("OPS-DRC01C").status] += 1

        scores.append(decision.confidence.value)
        by_outcome_scores.setdefault(decision.outcome, []).append(
            decision.confidence.value)

    match_rate = MatchRate(
        exact=match_counts[MATCH_EXACT],
        fuzzy=match_counts[MATCH_FUZZY],
        rule_classified=match_counts[MATCH_RULE_CLASSIFIED],
        unresolved=match_counts[MATCH_NONE],
    )

    # Quarantine: reported as its own line. Note it is NOT filtered by split —
    # a quarantined record was never scored, so it has no place in a scored
    # split. It is counted for the whole batch and stated as such.
    quarantined_by_error = Counter(e.validation_error for e in result.quarantined)

    confidence = ConfidenceSummary(
        mean=round(sum(scores) / len(scores), 2) if scores else 0.0,
        minimum=min(scores) if scores else 0.0,
        maximum=max(scores) if scores else 0.0,
        threshold=result.threshold,
        by_outcome={
            outcome: {
                "n": len(values),
                "mean": round(sum(values) / len(values), 2),
                "min": min(values),
                "max": max(values),
            }
            for outcome, values in sorted(by_outcome_scores.items())
        },
        distribution=dict(sorted(Counter(scores).items(), reverse=True)),
    )

    split_ids = set(in_split)
    audit_rows = [e for e in result.audit_entries if e.record_id in split_ids]
    audit = AuditSummary(
        rows=len(audit_rows),
        by_action=dict(sorted(Counter(e.action for e in audit_rows).items())),
        by_rule=dict(sorted(Counter(e.rule_id_fired or "(none)"
                                    for e in audit_rows).items())),
        pending_review=sum(1 for e in audit_rows
                           if e.action == INDETERMINATE
                           and e.reviewer_decision is None),
        db_path=result.db_path,
    )

    accuracy = None
    if expected_outcomes:
        correct, disagreements = 0, []
        for record_id in in_split:
            expected = expected_outcomes.get(record_id)
            actual = joined[record_id]["decision"].outcome
            if expected == actual:
                correct += 1
            else:
                disagreements.append((record_id, expected, actual))
        accuracy = Accuracy(correct, len(in_split), sorted(disagreements))

    # Suppliers over the DRC-01C trigger, derived from the flags the rule
    # engine already set. Re-applying the threshold here would duplicate a
    # value that lives in rules_v2026_04.yaml and silently drift from it.
    breaching_suppliers = {
        row["match"].pr_record.value("vendor_gstin")
        for row in joined.values()
        if row["evaluation"].flag("OPS-DRC01C").status == STATUS_BREACHED
    }

    return EvaluationReport(
        split=split,
        scored=len(in_split),
        match_rate=match_rate,
        exceptions_by_category=dict(sorted(exceptions.items())),
        operational_88d=dict(sorted(ops_88d.items())),
        operational_drc01c=dict(sorted(ops_drc.items())),
        indeterminate=indeterminate,
        quarantined=result.quarantined_count,
        quarantined_by_error=dict(sorted(quarantined_by_error.items())),
        confidence=confidence,
        audit=audit,
        accuracy=accuracy,
        rules_version=result.rules_version,
        dataset_seed=dataset_seed,
        batch_scored=result.scored,
        itc_exposure_total=round(sum(result.batch.itc_variance_by_gstin.values()), 2),
        itc_exposure_breaching=round(sum(
            v for g, v in result.batch.itc_variance_by_gstin.items()
            if g in breaching_suppliers), 2),
        suppliers_breaching_drc01c=len(breaching_suppliers),
        suppliers_total=len(result.batch.itc_variance_by_gstin),
        throughput=result.throughput(),
        ai_stats=dict(result.ai_stats),
        ai_assisted=result.ai_assisted,
        ai_fell_back_entirely=result.ai_fell_back_entirely,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

BAR = "=" * 76
RULE = "-" * 76

# Product positioning. Stated once, at the top of the report, alongside the
# scope qualifier — a claim and its limits belong on the same page.
POSITIONING = (
    "A GST-specific finance controller that reconciles evidence, quantifies "
    "estimated ITC exposure and safely escalates uncertainty."
)
SUPPORTING_LINE = (
    "AI-assisted normalisation. Deterministic decisions. Auditable human review."
)
SCOPE_QUALIFIER = [
    "Runs on synthetic GSTR-2B-style data. No live GSTN connectivity.",
    "Not tax advice. See section 10 before quoting any figure.",
]

# Stated in the report itself, not only the README: a number is quoted far more
# often than the document that qualifies it.
LIMITATIONS = [
    "* SYNTHETIC DATA. Every figure comes from data/generate_dataset.py",
    "  (seed 20260401). The injected defects damage high-weight evidence",
    "  fields while the fuzzy cases damage only low-weight ones, so the two",
    "  confidence populations cannot overlap. Real filings do overlap. Treat",
    "  these numbers as evidence the protocol is wired correctly, NOT as a",
    "  production forecast.",
    "",
    "* SINGLE BATCH, SINGLE PERIOD. One return period (2026-04) against one",
    "  prior-period snapshot. No multi-period carry-forward, no amendment",
    "  tracking, no incremental or streaming runs.",
    "",
    "* THROUGHPUT IS INDICATIVE. Single-threaded, one process, no cache, on",
    "  one machine. Matching is O(PR x 2B), so seconds/record grows with the",
    "  square of batch size, not linearly.",
    "",
    "* SYNTHETIC DRC-01C TRIGGER. Rs.75,000 cumulative variance, not the",
    "  statutory Rs.25 lakh test, which no invoice in this batch approaches.",
    "",
    "* NOT A FILING TOOL AND NOT TAX ADVICE. Nothing here files or amends a",
    "  return or talks to the GSTN. Rule IDs are a triage aid; the rules",
    "  recognise the defect patterns this dataset contains, not the full",
    "  statutory surface.",
    "",
    "* HUMAN REVIEW IS A QUEUE, NOT A WORKFLOW. A local read-only dashboard",
    "  (python3 -m src.dashboard) shows the queue and the evidence behind each",
    "  record: no upload, no reviewer write-back, no assignment, no escalation.",
    "  Reviewer decisions are appended through",
    "  audit_log.record_reviewer_decision() outside the UI.",
    "",
    "* MATCHER ACCURACY IS NOT PERFECT. Deliberately ambiguous records are",
    "  the weak spot; every outcome error traces to a matcher miss on one of",
    "  those rather than to a rule misfiring.",
]


def render(report: EvaluationReport) -> str:
    """The §2.7 report as text. Section order follows the architecture."""
    out: List[str] = []
    add = out.append

    add(BAR)
    add("EXCEPTION LEDGER — EVALUATION REPORT")
    add(BAR)
    add(f"  split               {report.split}  "
        f"({report.scored} records scored)")
    add(f"  rules version       {report.rules_version}")
    add(f"  dataset seed        {report.dataset_seed}")
    add(f"  confidence threshold {report.confidence.threshold:g}")
    add("")

    # -- 0. executive summary ---------------------------------------------
    add(RULE)
    add("0. EXECUTIVE SUMMARY")
    add(RULE)
    for line in textwrap.wrap(POSITIONING, width=72):
        add(f"  {line}")
    add("")
    add(f"  {SUPPORTING_LINE}")
    add("")
    for line in SCOPE_QUALIFIER:
        add(f"  {line}")
    add("")
    mr0 = report.match_rate
    add(f"  Reconciled          {mr0.resolved} of {mr0.total} records "
        f"({100 * mr0.rate:.1f}%) — {mr0.exact} exact, {mr0.fuzzy} fuzzy, "
        f"{mr0.rule_classified} rule-classified")
    add(f"  Escalated           {report.indeterminate} indeterminate record(s) "
        f"routed to human review")
    add(f"  Quarantined         {report.quarantined} record(s) rejected at input "
        f"validation, counted separately")
    add(f"  ITC exposure        Rs.{report.itc_exposure_total:,.2f} estimated "
        f"across {report.suppliers_total} suppliers")
    add(f"                      Rs.{report.itc_exposure_breaching:,.2f} of it "
        f"with the {report.suppliers_breaching_drc01c} supplier(s) over the "
        f"DRC-01C trigger")
    add(f"                      SCOPE: WHOLE BATCH ({report.batch_scored} scored "
        f"records), not the {report.split} split. DRC-01C is cumulative per")
    add(f"                      supplier, so a per-split figure would understate it.")
    add("")

    # -- 1. match rate, broken out ----------------------------------------
    add(RULE)
    add("1. OVERALL MATCH RATE")
    add(RULE)
    mr = report.match_rate
    add(f"  resolved            {mr.resolved:>4} / {mr.total}   "
        f"{100 * mr.rate:5.1f}%")
    add("")
    add(f"      exact           {mr.exact:>4}   {mr.pct(mr.exact):5.1f}%   "
        f"every compared field agreed")
    add(f"      fuzzy           {mr.fuzzy:>4}   {mr.pct(mr.fuzzy):5.1f}%   "
        f"auto-reconciled with tolerated differences")
    add(f"      rule-classified {mr.rule_classified:>4}   "
        f"{mr.pct(mr.rule_classified):5.1f}%   a named rule explained it")
    add("")
    add(f"      unresolved      {mr.unresolved:>4}   {mr.pct(mr.unresolved):5.1f}%   "
        f"indeterminate (see section 4)")

    # -- 2. classification exceptions -------------------------------------
    add("")
    add(RULE)
    add("2. CLASSIFICATION-RULE EXCEPTIONS — what happened")
    add(RULE)
    if report.exceptions_by_category:
        for category, n in report.exceptions_by_category.items():
            add(f"  {category:<40} {n:>4}")
        add(f"  {'-' * 40} {'-' * 4}")
        add(f"  {'total':<40} {sum(report.exceptions_by_category.values()):>4}")
    else:
        add("  (none)")

    # -- 3. operational checks, SEPARATE table ----------------------------
    add("")
    add(RULE)
    add("3. OPERATIONAL-CHECK FLAGS — what to do, and by when")
    add(RULE)
    add("   Reported separately from section 2, not merged into it: a record")
    add("   may appear in both, and they answer different questions.")
    add("")
    add("   OPS-88D    Rule 88D 7-day response window")
    for status in (STATUS_WITHIN_WINDOW, STATUS_OUTSIDE_WINDOW,
                   STATUS_NOT_APPLICABLE):
        add(f"       {status:<36} {report.operational_88d.get(status, 0):>4}")
    add("")
    add("   OPS-DRC01C cumulative ITC variance vs auto-notice trigger")
    add(f"       record counts below are {report.split}; the supplier totals")
    add(f"       and the ITC exposure are WHOLE BATCH — the variance is")
    add(f"       cumulative per supplier and does not split.")
    for status in (STATUS_BREACHED, STATUS_WITHIN_THRESHOLD):
        add(f"       {status:<36} {report.operational_drc01c.get(status, 0):>4}")
    add(f"       {'suppliers over the trigger (whole batch)':<36} "
        f"{report.suppliers_breaching_drc01c:>4} of {report.suppliers_total}")

    # -- 4. indeterminate --------------------------------------------------
    add("")
    add(RULE)
    add("4. INDETERMINATE — routed to human review")
    add(RULE)
    add(f"  {'no rule could assign a category':<40} {report.indeterminate:>4}")

    # -- 5. quarantine, its own line --------------------------------------
    add("")
    add(RULE)
    add("5. QUARANTINED RECORDS — reported separately (§2.1)")
    add(RULE)
    add("   A data-quality problem, not a reconciliation problem. NOT folded")
    add("   into the match rate, the exception count or the indeterminate")
    add("   count above. Counted across the whole batch, not per split —")
    add("   a quarantined record was never scored, so it belongs to no split.")
    add("")
    add(f"  {'quarantined':<40} {report.quarantined:>4}")
    for error, n in report.quarantined_by_error.items():
        add(f"      {error:<36} {n:>4}")

    # -- 6. confidence -----------------------------------------------------
    add("")
    add(RULE)
    add("6. CONFIDENCE")
    add(RULE)
    c = report.confidence
    add(f"  threshold           {c.threshold:g}   (calibrated on the 70% split, "
        f"frozen)")
    add(f"  weights             " + ", ".join(
        f"{k}={v:g}" for k, v in FIELD_WEIGHTS.items()))
    add(f"  mean / min / max    {c.mean:.1f} / {c.minimum:.1f} / {c.maximum:.1f}")
    add("")
    add(f"  {'outcome':<24} {'n':>4} {'mean':>7} {'min':>7} {'max':>7}")
    for outcome, stats in c.by_outcome.items():
        add(f"  {outcome:<24} {int(stats['n']):>4} {stats['mean']:>7.1f} "
            f"{stats['min']:>7.1f} {stats['max']:>7.1f}")
    add("")
    add("  distribution:")
    for value, n in c.distribution.items():
        side = ">=" if value >= c.threshold else "< "
        add(f"    {value:6.1f}  {side} threshold  {'#' * min(46, n // 3 + 1)} {n}")

    # -- 7. audit ----------------------------------------------------------
    add("")
    add(RULE)
    add("7. AUDIT LOG")
    add(RULE)
    add(f"  table               {report.audit.table} in "
        f"{os.path.basename(report.audit.db_path)}")
    add(f"  rows (this split)   {report.audit.rows:>4}")
    add(f"  by action           " + ", ".join(
        f"{k}={v}" for k, v in report.audit.by_action.items()))
    add(f"  by rule fired       " + ", ".join(
        f"{k}={v}" for k, v in report.audit.by_rule.items()))
    add(f"  pending review      {report.audit.pending_review:>4}  "
        f"(reviewer_decision still null)")

    # -- 8. accuracy -------------------------------------------------------
    if report.accuracy is not None:
        add("")
        add(RULE)
        add("8. ACCURACY vs GROUND TRUTH")
        add(RULE)
        add("   The frozen split is scored once and reported. It is never fed")
        add("   back into calibration (§2.6).")
        add("")
        a = report.accuracy
        add(f"  correct             {a.correct:>4} / {a.total}   "
            f"{100 * a.rate:5.1f}%")
        if a.disagreements:
            add("")
            add(f"  disagreements ({len(a.disagreements)}):")
            for record_id, expected, actual in a.disagreements:
                add(f"      {record_id:<28} expected {expected:<22} got {actual}")

    # -- 9. benchmark ------------------------------------------------------
    add("")
    add(RULE)
    add("9. BENCHMARK — throughput")
    add(RULE)
    t = report.throughput
    if t:
        add(f"  mode                {t.get('mode', 'unknown')}")
        b = t.get("batch_size", {})
        if b:
            add(f"  batch size          "
                f"{b.get('purchase_register_rows', 0)} register rows x "
                f"{b.get('gstr2b_rows', 0)} 2B rows")
            add(f"                      "
                f"{b.get('match_matrix_cells', 0):,} match-matrix cells")
        add(f"  elapsed             {t['elapsed_seconds']:>8.3f} s   "
            f"(whole batch, not just this split)")
        add(f"  valid records       {t['valid_records']:>8}")
        add(f"  records/second      {t['records_per_second']:>8.1f}   valid only")
        add(f"  rows/second         {t['total_records_per_second']:>8.1f}   "
            f"all {t['records_read']} source rows")
        add("")
        add(f"  {'stage':<34} {'seconds':>9} {'share':>7}")
        for stage, seconds in t.get("stage_seconds", {}).items():
            share = 100 * seconds / t["elapsed_seconds"] if t["elapsed_seconds"] else 0
            add(f"  {stage:<34} {seconds:>9.3f} {share:>6.1f}%")
        add("")
        add("  METHOD: wall clock (time.perf_counter) around each stage of one")
        add("  run, single-threaded, one process, no cache. For a stable figure")
        add("  use `python3 -m src.pipeline --benchmark N`, which repeats the")
        add("  run and reports the median with min/max spread.")
        add("")
        add("  Matching dominates: it scores the full PR x 2B cross product")
        add("  (§2.3 step 2), so cost is O(n^2) in batch size, not linear.")
    else:
        add("  (not measured)")

    add("")
    add(f"  AI-assisted normalisation (§2.2)")
    if not report.ai_assisted:
        add("      not requested — deterministic half only")
    else:
        add(f"      attempted {report.ai_stats.get('ai_attempted', 0)}  "
            f"applied {report.ai_stats.get('ai_applied', 0)}  "
            f"failed {report.ai_stats.get('ai_call_failed', 0)}  "
            f"contract-rejected {report.ai_stats.get('ai_contract_violation', 0)}")
        if report.ai_fell_back_entirely:
            add("      !! EVERY AI CALL FAILED — deterministic fallback used "
                "throughout.")
            add("         This batch is NOT AI-normalised.")

    # -- 10. limitations ---------------------------------------------------
    add("")
    add(RULE)
    add("10. LIMITATIONS — read before quoting any number above")
    add(RULE)
    for line in LIMITATIONS:
        add(f"  {line}")

    add("")
    add(BAR)
    return "\n".join(out)


def to_dict(report: EvaluationReport) -> dict:
    """Machine-readable form of the same report."""
    mr = report.match_rate
    payload = {
        "split": report.split,
        "scored": report.scored,
        "rules_version": report.rules_version,
        "dataset_seed": report.dataset_seed,
        "match_rate": {
            "resolved": mr.resolved,
            "total": mr.total,
            "rate": round(mr.rate, 6),
            "exact": mr.exact,
            "fuzzy": mr.fuzzy,
            "rule_classified": mr.rule_classified,
            "unresolved": mr.unresolved,
        },
        "exceptions_by_category": report.exceptions_by_category,
        "operational_checks": {
            "OPS-88D": report.operational_88d,
            "OPS-DRC01C": report.operational_drc01c,
            "suppliers_breaching": report.suppliers_breaching_drc01c,
            "suppliers_total": report.suppliers_total,
        },
        "indeterminate": report.indeterminate,
        "quarantined": {
            "count": report.quarantined,
            "by_error": report.quarantined_by_error,
            "note": "whole batch; never folded into the counts above",
        },
        "confidence": {
            "threshold": report.confidence.threshold,
            "mean": report.confidence.mean,
            "min": report.confidence.minimum,
            "max": report.confidence.maximum,
            "by_outcome": report.confidence.by_outcome,
        },
        "audit": {
            "rows": report.audit.rows,
            "by_action": report.audit.by_action,
            "by_rule": report.audit.by_rule,
            "pending_review": report.audit.pending_review,
        },
    }
    payload["positioning"] = {
        "statement": POSITIONING,
        "supporting_line": SUPPORTING_LINE,
        "scope": " ".join(SCOPE_QUALIFIER),
    }
    payload["itc_exposure"] = {
        "scope": report.itc_exposure_scope,
        "scope_note": (f"whole batch ({report.batch_scored} scored records); "
                       f"NOT the {report.split} split — DRC-01C variance is "
                       f"cumulative per supplier"),
        "batch_scored": report.batch_scored,
        "total": report.itc_exposure_total,
        "with_breaching_suppliers": report.itc_exposure_breaching,
        "suppliers_total": report.suppliers_total,
        "suppliers_breaching": report.suppliers_breaching_drc01c,
    }
    payload["benchmark"] = report.throughput
    payload["ai"] = {
        "requested": report.ai_assisted,
        "stats": report.ai_stats,
        "fell_back_entirely": report.ai_fell_back_entirely,
    }
    payload["limitations"] = [l for l in LIMITATIONS if l]
    if report.accuracy is not None:
        payload["accuracy"] = {
            "correct": report.accuracy.correct,
            "total": report.accuracy.total,
            "rate": round(report.accuracy.rate, 6),
            "disagreements": [list(d) for d in report.accuracy.disagreements],
        }
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv=None) -> int:
    import argparse
    import json

    from . import pipeline as P

    ap = argparse.ArgumentParser(
        prog="python3 -m src.report",
        description="Run the pipeline, then produce the §2.7 evaluation report.")
    ap.add_argument("--split", default=FROZEN_TEST,
                    choices=[FROZEN_TEST, CALIBRATION])
    ap.add_argument("--db", default=P.DEFAULT_DB_PATH)
    ap.add_argument("--now", default=None)
    ap.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "out"))
    ap.add_argument("--no-accuracy", action="store_true",
                    help="omit the ground-truth grading section")
    args = ap.parse_args(argv)

    result = P.run(db_path=args.db, now=args.now)
    report = build_report(
        result,
        splits=load_splits(),
        split=args.split,
        expected_outcomes=None if args.no_accuracy else load_expected_outcomes(),
    )

    text = render(report)
    print(text)

    os.makedirs(args.out_dir, exist_ok=True)
    txt_path = os.path.join(args.out_dir, f"evaluation_report_{args.split}.txt")
    json_path = os.path.join(args.out_dir, f"evaluation_report_{args.split}.json")
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(to_dict(report), fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"\n  written: {os.path.relpath(txt_path, REPO_ROOT)}")
    print(f"           {os.path.relpath(json_path, REPO_ROOT)}")
    print(f"  pipeline fingerprint: {P.fingerprint(result)}")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main())
