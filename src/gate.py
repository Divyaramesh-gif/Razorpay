"""§2.6 Three-way confidence gate — build-order step 7b.

    Outcome                        Condition
    ---------------------------    --------------------------------------------
    Auto-reconcile                 Confidence >= threshold, evidence shows a
                                   clean match
    Classified exception           Confidence below threshold, but the rule
                                   engine confidently assigns a named category
    Indeterminate -> human review  Rule engine cannot confidently assign any
                                   category

Note what "indeterminate" is NOT: it is not a bucket for bad input. A record
that failed §2.1 validation was quarantined and never reached this stage at
all, and §2.1 is explicit that a quarantined record "is not counted in the
match rate, the exception count, or the indeterminate count". Folding the two
together would misrepresent what the exception report says — a data-quality
problem is not a reconciliation problem. `decide_batch()` therefore refuses
any record carrying a validation error.

The gate is the only place in the pipeline that reaches a verdict. Everything
upstream observes; this decides. It does so from two deterministic inputs — a
confidence number derived from evidence, and whether a named rule fired — and
from nothing else. No model is consulted here either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from .confidence import ConfidenceScore, score_evidence
from .evidence import Evidence
from .rule_engine import Classification, RuleEvaluation

AUTO_RECONCILE = "auto_reconcile"
CLASSIFIED_EXCEPTION = "classified_exception"
INDETERMINATE = "indeterminate"

OUTCOMES = (AUTO_RECONCILE, CLASSIFIED_EXCEPTION, INDETERMINATE)

# Outcomes that need a person. Kept as a set so the report can ask the
# question without re-deriving the rule.
NEEDS_HUMAN_REVIEW = frozenset({INDETERMINATE})


@dataclass(frozen=True)
class GateDecision:
    """One record's outcome, with the reasoning that produced it."""

    record_id: str
    invoice_id: str
    outcome: str
    confidence: ConfidenceScore
    threshold: float
    rule_id: Optional[str]
    category: Optional[str]
    reason: str

    @property
    def needs_human_review(self) -> bool:
        return self.outcome in NEEDS_HUMAN_REVIEW


def decide(evaluation: RuleEvaluation, evidence: Evidence,
           threshold: float) -> GateDecision:
    """Apply the §2.6 condition table to one record."""
    confidence = score_evidence(evidence)
    classification: Classification = evaluation.classification

    if confidence.value >= threshold and confidence.clean_match:
        outcome = AUTO_RECONCILE
        reason = (f"confidence {confidence.value:.1f} >= threshold "
                  f"{threshold:g} and evidence is a clean match "
                  f"({', '.join(evidence.matched_fields())})")
    elif classification.fired:
        outcome = CLASSIFIED_EXCEPTION
        reason = (f"confidence {confidence.value:.1f} vs threshold "
                  f"{threshold:g}; rule {classification.rule_id} assigns "
                  f"'{classification.category}' — {classification.reason}")
    else:
        outcome = INDETERMINATE
        detail = (", ".join(evidence.mismatched_fields())
                  or "no counterpart to compare")
        reason = (f"confidence {confidence.value:.1f} vs threshold "
                  f"{threshold:g}; no rule could assign a category "
                  f"(differing: {detail}) — routed to human review")

    return GateDecision(
        record_id=evaluation.record_id,
        invoice_id=evaluation.invoice_id,
        outcome=outcome,
        confidence=confidence,
        threshold=threshold,
        rule_id=classification.rule_id,
        category=classification.category,
        reason=reason,
    )


def decide_batch(evaluations: Sequence[RuleEvaluation],
                 evidences: Sequence[Evidence],
                 threshold: float,
                 quarantined_record_ids: Sequence[str] = ()) -> List[GateDecision]:
    """Gate a whole batch.

    Refuses to score a quarantined record: §2.1 puts those on a separate exit
    path, and letting one through here would silently fold a data-quality
    failure into the exception or indeterminate count.
    """
    if len(evaluations) != len(evidences):
        raise ValueError("evaluations and evidences must correspond one to one")

    blocked = set(quarantined_record_ids)
    if blocked:
        intruders = sorted({e.record_id for e in evaluations} & blocked)
        if intruders:
            raise ValueError(
                "quarantined records must not reach the confidence gate "
                f"(§2.1); found {len(intruders)}: {intruders[:5]}"
            )

    return [decide(evaluation, evidence, threshold)
            for evaluation, evidence in zip(evaluations, evidences)]


def counts(decisions: Sequence[GateDecision]) -> dict:
    """Outcome tally. Every outcome key is always present, including zeroes,
    so a report never silently omits an empty bucket."""
    tally = {outcome: 0 for outcome in OUTCOMES}
    for decision in decisions:
        tally[decision.outcome] += 1
    return tally
