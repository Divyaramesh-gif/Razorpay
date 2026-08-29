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

    with QuarantineLog(db_path, now=now) as qlog:
        qlog.clear()
        for source in sources:
            records = load_source(source)
            records_read[source] = len(records)
            valid, invalid = V.partition(records)
            qlog.quarantine_all(invalid)
            normalised[source] = [
                N.normalize(result.record, ai_client) for result in valid
            ]
        quarantined = qlog.entries()

    quarantined_ids = [entry.record_id for entry in quarantined]

    # ---- §2.3 matching ---------------------------------------------------
    match_result = M.match_records(normalised[SOURCE_PURCHASE_REGISTER],
                                   normalised[SOURCE_GSTR2B])

    # ---- §2.4 evidence ---------------------------------------------------
    evidences = E.compare_all(match_result.matches)

    # ---- §2.5 rules ------------------------------------------------------
    engine = RuleEngine(rules_path) if rules_path else RuleEngine()
    batch = engine.evaluate_batch(match_result.matches, evidences)

    # ---- §2.6 confidence + gate -----------------------------------------
    if threshold is None:
        threshold = (C.load_threshold(calibration_path) if calibration_path
                     else C.load_threshold())
    decisions = decide_batch(batch.evaluations, evidences, threshold,
                             quarantined_record_ids=quarantined_ids)

    # ---- §2.7 audit log --------------------------------------------------
    with AuditLog(db_path, now=now) as alog:
        alog.clear()
        audit_entries = alog.record_all(decisions, evidences)

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
    args = ap.parse_args(argv)

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

    print(f"  matched {len(result.match_result.matched)}  "
          f"no_candidate_found {len(result.match_result.no_candidate)}  "
          f"unmatched 2B {len(result.match_result.unmatched_2b)}")
    print(f"  rules v{result.rules_version}  threshold {result.threshold:g}\n")

    for outcome, n in result.outcome_counts().items():
        print(f"  {outcome:<24} {n:>4}")
    print(f"\n  audit rows {len(result.audit_entries)}  in {args.db}")
    print(f"  fingerprint {fingerprint(result)}")

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
