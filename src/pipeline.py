"""End-to-end orchestration — build-order step 8.

Wires every stage together in the §1 order:

    purchase_register.csv + gstr2b.csv
            |
            v
      §2.1 validation ---[FAILS]---> §3.3 quarantine log  (exits; never scored)
            |
         [PASSES]
            v
      §2.2 normalisation (deterministic; AI half optional)
            v
      §2.3 exact + fuzzy ONE-TO-ONE matching
            v
      §2.4 field-by-field evidence comparison
            v
      §2.5 versioned GST rules + operational checks
            v
      §2.6 confidence scoring -> three-way gate
            v
      §2.7 audit log

**This module is label-blind.** It never opens ground_truth.csv and never sees
a split. It reads a calibrated threshold as a plain number and produces the two
logs plus an in-memory `PipelineResult`. §2.6 is explicit that the labels exist
"for the evaluation script" alone — so grading this output is src/report.py's
job, and the two are deliberately separate entry points rather than one call
chain. If the pipeline could reach a label, "the pipeline never sees them"
would be a convention rather than a fact.

The other boundary this module keeps is §2.1's: a record that fails validation
goes to the quarantine log and no further. It is not normalised, not matched,
not scored, and not counted in the match rate, the exception count or the
indeterminate count. `run()` passes the quarantined ids to the gate and the
audit log precisely so both can refuse them.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import confidence as C
from . import evidence as E
from . import matcher as M
from . import normalization as N
from . import validation as V
from .audit_log import AuditEntry, AuditLog
from .evidence import Evidence
from .gate import GateDecision, counts as gate_counts, decide_batch
from .matcher import Match, MatchResult
from .normalization import NormalizedRecord
from .quarantine_log import QuarantineEntry, QuarantineLog
from .rule_engine import BatchEvaluation, RuleEngine, RuleEvaluation
from .source_records import (
    MATCHING_SOURCES,
    REPO_ROOT,
    SOURCE_GSTR2B,
    SOURCE_PURCHASE_REGISTER,
    load_source,
)

DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "out", "exception_ledger.sqlite")


@dataclass(frozen=True)
class PipelineResult:
    """Everything one run produced. Carries no labels and no split."""

    # §2.1 / §3.3
    records_read: Dict[str, int]
    quarantined: List[QuarantineEntry]
    # §2.2
    normalised: Dict[str, List[NormalizedRecord]]
    # §2.3
    match_result: MatchResult
    # §2.4
    evidences: List[Evidence]
    # §2.5
    batch: BatchEvaluation
    # §2.6
    threshold: float
    decisions: List[GateDecision]
    # §2.7
    audit_entries: List[AuditEntry]
    rules_version: str
    db_path: str
    ai_assisted: bool = False
    # §2.2 AI-half outcomes. Diagnostic only — the AI half can never change a
    # decision the deterministic half would not also reach, so these counters
    # stay out of fingerprint(). They exist so a run where every call failed is
    # distinguishable from one where nothing needed repairing.
    ai_stats: Dict[str, int] = field(default_factory=dict)
    # Wall-clock instrumentation. Measurement only — nothing here feeds a
    # decision, and stage_seconds is excluded from fingerprint() so a slow
    # machine never looks like a different result.
    elapsed_seconds: float = 0.0
    stage_seconds: Dict[str, float] = field(default_factory=dict)

    @property
    def valid_records(self) -> int:
        """Records that passed §2.1 — the throughput denominator."""
        return len(self.decisions)

    @property
    def records_per_second(self) -> float:
        """Valid records processed per second of wall clock."""
        if self.elapsed_seconds <= 0:
            return 0.0
        return round(self.valid_records / self.elapsed_seconds, 1)

    @property
    def total_records_per_second(self) -> float:
        """All source rows read per second, quarantined ones included."""
        if self.elapsed_seconds <= 0:
            return 0.0
        return round(self.total_read / self.elapsed_seconds, 1)

    def throughput(self) -> Dict[str, float]:
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "valid_records": self.valid_records,
            "records_read": self.total_read,
            "quarantined": self.quarantined_count,
            "records_per_second": self.records_per_second,
            "total_records_per_second": self.total_records_per_second,
            "stage_seconds": {k: round(v, 3) for k, v in self.stage_seconds.items()},
        }

    @property
    def ai_attempted(self) -> int:
        return self.ai_stats.get(N.AI_ATTEMPTED, 0)

    @property
    def ai_applied(self) -> int:
        return self.ai_stats.get(N.AI_APPLIED, 0)

    @property
    def ai_failed(self) -> int:
        """Calls that raised, plus responses rejected by the §2.2 contract."""
        return (self.ai_stats.get(N.AI_CALL_FAILED, 0)
                + self.ai_stats.get(N.AI_CONTRACT_VIOLATION, 0))

    @property
    def ai_fell_back_entirely(self) -> bool:
        """The AI half was asked for, tried, and produced nothing usable."""
        return (self.ai_assisted and self.ai_attempted > 0
                and self.ai_failed == self.ai_attempted)

    @property
    def scored(self) -> int:
        """Records that passed §2.1 and reached the gate."""
        return len(self.decisions)

    @property
    def quarantined_count(self) -> int:
        return len(self.quarantined)

    @property
    def total_read(self) -> int:
        return sum(self.records_read.values())

    @property
    def matches(self) -> List[Match]:
        return self.match_result.matches

    @property
    def evaluations(self) -> List[RuleEvaluation]:
        return self.batch.evaluations

    def outcome_counts(self) -> Dict[str, int]:
        return gate_counts(self.decisions)

    def by_record(self) -> Dict[str, Dict[str, Any]]:
        """Everything about one record, keyed by its independent source id."""
        joined: Dict[str, Dict[str, Any]] = {}
        for match, ev, evaluation, decision in zip(
            self.matches, self.evidences, self.evaluations, self.decisions
        ):
            joined[decision.record_id] = {
                "match": match, "evidence": ev,
                "evaluation": evaluation, "decision": decision,
            }
        return joined


def run(db_path: str = DEFAULT_DB_PATH,
        threshold: Optional[float] = None,
        rules_path: Optional[str] = None,
        calibration_path: Optional[str] = None,
        ai_client: Any = None,
        now: Optional[str] = None,
        sources: Sequence[str] = MATCHING_SOURCES) -> PipelineResult:
    """Run every stage end to end.

    `now` pins both logs' timestamps, which is what makes a run byte-for-byte
    reproducible. `ai_client` enables the §2.2 AI-assisted half; the default of
    None keeps the whole pipeline offline and deterministic.
    """
    # ---- §2.1 validation + §3.3 quarantine, §2.2 normalisation ----------
    records_read: Dict[str, int] = {}
    normalised: Dict[str, List[NormalizedRecord]] = {}
    quarantined: List[QuarantineEntry] = []
    ai_stats: Counter = Counter()
    stage_seconds: Dict[str, float] = {}
    _t0 = time.perf_counter()
    _mark = _t0

    with QuarantineLog(db_path, now=now) as qlog:
        qlog.clear()
        for source in sources:
            records = load_source(source)
            records_read[source] = len(records)
            valid, invalid = V.partition(records)
            qlog.quarantine_all(invalid)
            normalised[source] = [
                N.normalize(result.record, ai_client, ai_stats)
                for result in valid
            ]
        quarantined = qlog.entries()
    stage_seconds["validate_quarantine_normalise"] = time.perf_counter() - _mark
    _mark = time.perf_counter()

    quarantined_ids = [entry.record_id for entry in quarantined]

    # ---- §2.3 matching ---------------------------------------------------
    match_result = M.match_records(normalised[SOURCE_PURCHASE_REGISTER],
                                   normalised[SOURCE_GSTR2B])
    stage_seconds["match"] = time.perf_counter() - _mark
    _mark = time.perf_counter()

    # ---- §2.4 evidence ---------------------------------------------------
    evidences = E.compare_all(match_result.matches)
    stage_seconds["evidence"] = time.perf_counter() - _mark
    _mark = time.perf_counter()

    # ---- §2.5 rules ------------------------------------------------------
    engine = RuleEngine(rules_path) if rules_path else RuleEngine()
    batch = engine.evaluate_batch(match_result.matches, evidences)
    stage_seconds["rules"] = time.perf_counter() - _mark
    _mark = time.perf_counter()

    # ---- §2.6 confidence + gate -----------------------------------------
    if threshold is None:
        threshold = (C.load_threshold(calibration_path) if calibration_path
                     else C.load_threshold())
    decisions = decide_batch(batch.evaluations, evidences, threshold,
                             quarantined_record_ids=quarantined_ids)
    stage_seconds["confidence_gate"] = time.perf_counter() - _mark
    _mark = time.perf_counter()

    # ---- §2.7 audit log --------------------------------------------------
    with AuditLog(db_path, now=now) as alog:
        alog.clear()
        audit_entries = alog.record_all(decisions, evidences)
    stage_seconds["audit_log"] = time.perf_counter() - _mark

    return PipelineResult(
        records_read=records_read,
        quarantined=quarantined,
        normalised=normalised,
        match_result=match_result,
        evidences=evidences,
        batch=batch,
        threshold=threshold,
        decisions=decisions,
        audit_entries=audit_entries,
        rules_version=batch.rules_version,
        db_path=db_path,
        ai_assisted=ai_client is not None,
        ai_stats=dict(ai_stats),
        elapsed_seconds=time.perf_counter() - _t0,
        stage_seconds=stage_seconds,
    )


def fingerprint(result: PipelineResult) -> str:
    """A stable digest of everything a run decided, excluding timestamps.

    Two runs over the same inputs must produce the same fingerprint. Used by
    the reproducibility check; timestamps are excluded so a real difference in
    a decision is not masked by the clock.
    """
    import hashlib
    import json

    payload = {
        "threshold": result.threshold,
        "rules_version": result.rules_version,
        "records_read": result.records_read,
        "quarantined": sorted(
            (e.record_id, e.validation_error, e.raw_record_snapshot)
            for e in result.quarantined
        ),
        "decisions": [
            (d.record_id, d.invoice_id, d.outcome, d.confidence.value,
             d.rule_id or "", d.category or "", d.reason)
            for d in result.decisions
        ],
        "pairs": [(m.pr_id, m.b2_id or "", m.score) for m in result.matches],
        "unmatched_2b": sorted(r.source_id for r in result.match_result.unmatched_2b),
        "evidence": [
            (ev.pr_record_id, ev.candidate_found, sorted(ev.matched_fields()),
             sorted(ev.mismatched_fields()))
            for ev in result.evidences
        ],
        "operational": [
            (e.record_id, tuple(sorted((f.check_id, f.status)
                                       for f in e.operational_flags)))
            for e in result.evaluations
        ],
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def compare_deterministic_vs_ai(db_path: str, ai_client: Any,
                                now: Optional[str] = None) -> Dict[str, Any]:
    """Run the pipeline both ways and diff the results.

    Measurement, not a stage. The AI half may only rewrite free text, so the
    interesting questions are: did any normalised value change, and did any
    DECISION change as a result?
    """
    baseline = run(db_path=db_path + ".det", now=now, ai_client=None)
    assisted = run(db_path=db_path + ".ai", now=now, ai_client=ai_client)

    base_norm = {r.source_id: r.normalized
                 for recs in baseline.normalised.values() for r in recs}
    ai_norm = {r.source_id: r.normalized
               for recs in assisted.normalised.values() for r in recs}
    changed_fields = sorted({
        (rid, field)
        for rid, values in ai_norm.items()
        for field, value in values.items()
        if base_norm.get(rid, {}).get(field) != value
    })

    base_dec = {d.record_id: (d.outcome, d.category, d.confidence.value)
                for d in baseline.decisions}
    changed_decisions = sorted(
        rid for d in assisted.decisions
        for rid in [d.record_id]
        if base_dec.get(rid) != (d.outcome, d.category, d.confidence.value)
    )

    return {
        "deterministic": {
            "fingerprint": fingerprint(baseline),
            "outcomes": baseline.outcome_counts(),
            "throughput": baseline.throughput(),
        },
        "ai_assisted": {
            "fingerprint": fingerprint(assisted),
            "outcomes": assisted.outcome_counts(),
            "throughput": assisted.throughput(),
            "ai_stats": assisted.ai_stats,
            "fell_back_entirely": assisted.ai_fell_back_entirely,
        },
        "normalised_fields_changed": changed_fields,
        "decisions_changed": changed_decisions,
        "identical_decisions": fingerprint(baseline) == fingerprint(assisted),
    }


def _compare_ai(args) -> int:
    client = N.build_client()
    if client is None:
        print("  !! no Anthropic client available; comparison would be vacuous")
        return 1
    d = compare_deterministic_vs_ai(args.db, client, args.now)

    print("Deterministic-only vs AI-assisted\n")
    print(f"  {'':<26} {'deterministic':>16} {'AI-assisted':>16}")
    for key in ("auto_reconcile", "classified_exception", "indeterminate"):
        print(f"  {key:<26} {d['deterministic']['outcomes'][key]:>16} "
              f"{d['ai_assisted']['outcomes'][key]:>16}")
    print(f"  {'elapsed (s)':<26} "
          f"{d['deterministic']['throughput']['elapsed_seconds']:>16.3f} "
          f"{d['ai_assisted']['throughput']['elapsed_seconds']:>16.3f}")
    print(f"  {'records/second':<26} "
          f"{d['deterministic']['throughput']['records_per_second']:>16.1f} "
          f"{d['ai_assisted']['throughput']['records_per_second']:>16.1f}")
    print(f"\n  AI outcomes          {d['ai_assisted']['ai_stats']}")
    print(f"  normalised fields changed by the AI half: "
          f"{len(d['normalised_fields_changed'])}")
    print(f"  DECISIONS changed:   {len(d['decisions_changed'])}")
    print(f"  identical decisions: {d['identical_decisions']}")
    if d["ai_assisted"]["fell_back_entirely"]:
        print("\n  !! every AI call failed — this comparison shows the fallback "
              "path,\n     not a working AI half.")
    return 0


def _main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python3 -m src.pipeline",
        description="Run the Exception Ledger pipeline end to end (label-blind).")
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--now", default=None,
                    help="pin log timestamps (makes a run byte-reproducible)")
    ap.add_argument("--ai", action="store_true",
                    help="enable the §2.2 AI-assisted normalisation half")
    ap.add_argument("--verify-reproducible", action="store_true",
                    help="run twice and compare decision fingerprints")
    ap.add_argument("--compare-ai", action="store_true",
                    help="run deterministic-only and AI-assisted, and diff them")
    args = ap.parse_args(argv)

    if args.compare_ai:
        return _compare_ai(args)

    client = N.build_client() if args.ai else None
    if args.ai and client is None:
        print("  !! --ai requested but no Anthropic client is available\n")

    result = run(db_path=args.db, ai_client=client, now=args.now)

    print("Exception Ledger — pipeline run\n")
    for source, n in result.records_read.items():
        print(f"  {source:<22} {n:>4} read")
    print(f"  {'-' * 22} {'-' * 4}")
    print(f"  {'quarantined (§2.1)':<22} {result.quarantined_count:>4}  "
          f"exits the pipeline, never scored")
    print(f"  {'scored':<22} {result.scored:>4}\n")

    # §2.2 AI half. Reported unconditionally: a run where every call failed
    # must not look like a run where nothing needed repairing. The pipeline is
    # correct either way — the deterministic value stands — but the operator
    # needs to know which of the two happened before treating a batch as
    # AI-normalised.
    if not args.ai:
        print(f"  {'AI-assisted normalisation':<22}  not requested "
              f"(deterministic half only)")
    else:
        print(f"  {'AI-assisted normalisation':<22}  "
              f"{result.ai_attempted} field(s) sent, "
              f"{result.ai_applied} repaired, "
              f"{result.ai_stats.get(N.AI_UNCHANGED, 0)} unchanged, "
              f"{result.ai_failed} failed")
        if result.ai_fell_back_entirely:
            print("  !! EVERY AI CALL FAILED — the deterministic result stands for")
            print("     all of them. This batch is NOT AI-normalised. Check")
            print("     credentials before treating it as such.")
        elif result.ai_attempted == 0:
            print("     (nothing was messy enough to send)")
    print()

    print(f"  matched {len(result.match_result.matched)}  "
          f"no_candidate_found {len(result.match_result.no_candidate)}  "
          f"unmatched 2B {len(result.match_result.unmatched_2b)}")
    print(f"  rules v{result.rules_version}  threshold {result.threshold:g}\n")

    for outcome, n in result.outcome_counts().items():
        print(f"  {outcome:<24} {n:>4}")
    print(f"\n  audit rows {len(result.audit_entries)}  in {args.db}")

    t = result.throughput()
    print(f"\n  THROUGHPUT")
    print(f"    elapsed            {t['elapsed_seconds']:>8.3f} s")
    print(f"    valid records      {t['valid_records']:>8}")
    print(f"    records/second     {t['records_per_second']:>8.1f}  (valid only)")
    print(f"    rows/second        {t['total_records_per_second']:>8.1f}  "
          f"(all {t['records_read']} source rows)")
    for stage, seconds in result.stage_seconds.items():
        share = 100 * seconds / result.elapsed_seconds if result.elapsed_seconds else 0
        print(f"      {stage:<32} {seconds:>7.3f} s  {share:>5.1f}%")

    print(f"\n  fingerprint {fingerprint(result)}")

    if args.verify_reproducible:
        second = run(db_path=args.db, ai_client=client, now=args.now)
        a, b = fingerprint(result), fingerprint(second)
        print(f"\n  REPRODUCIBILITY  run 1 {a[:16]}")
        print(f"                   run 2 {b[:16]}")
        print(f"                   -> {'IDENTICAL' if a == b else 'DIVERGED'}")
        return 0 if a == b else 1
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main())
