"""§2.3 Exact + fuzzy ONE-TO-ONE matching — build-order step 4.

The architecture calls this "the stage that most needed fixing", and the whole
point of the rewrite is the one-to-one guarantee: **no GSTR-2B record may be
claimed by more than one purchase-register record, and vice versa.**

Algorithm, exactly as §2.3 specifies:

  1. For every purchase-register record, score it against every GSTR-2B record.
     Exact fields (invoice number, GSTIN, amount within Rs.1) score highest;
     fuzzy fields (vendor-name similarity via rapidfuzz, date within a
     tolerance window) score lower but non-zero.
  2. Build the full candidate score matrix.
  3. Greedy one-to-one assignment: sort all candidate pairs by score
     descending, assign the highest-scoring pair, remove BOTH records from
     further consideration, repeat.
  4. Any purchase-register record left unassigned is marked
     `no_candidate_found` — a valid, expected output, not an error. It feeds
     the rule engine as an "absence" case (§2.5).

Greedy is deliberate over an optimal bipartite solver (Hungarian): §2.3 wants a
result that is easy to explain and verify in review, and at this batch size the
outcome difference is negligible.

This module ranks candidates. It assigns no category, no confidence and no
reconciliation outcome — those are steps 5-7. Scoring is fully deterministic:
same inputs, same matrix, same assignment, every run. No LLM is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from rapidfuzz import fuzz

from .normalization import NormalizedRecord
from .validation import parse_amount, parse_date

NO_CANDIDATE_FOUND = "no_candidate_found"

# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------
# §2.3: exact fields score highest, fuzzy fields score "lower but non-zero".
# Exact evidence therefore dominates (100 of the 116 available points) while
# fuzzy evidence can still break a tie or rescue a partially-damaged record.

W_INVOICE_EXACT = 40.0      # exact  — the strongest single identity signal
W_GSTIN_EXACT = 30.0        # exact  — supplier identity
W_GSTIN_SAME_PAN = 20.0     # same legal entity, different state registration
W_AMOUNT_EXACT = 30.0       # exact  — "amount within Rs.1" per §2.3
W_INVOICE_FUZZY = 8.0       # fuzzy  — a garbled invoice number
W_NAME_FUZZY = 10.0         # fuzzy  — rapidfuzz vendor-name similarity
W_DATE_FUZZY = 6.0          # fuzzy  — date within a tolerance window

AMOUNT_EXACT_TOLERANCE = 1.00       # rupees — §2.3's "amount within Rs.1"
AMOUNT_NEAR_PCT = 0.02              # partial credit band
AMOUNT_LOOSE_PCT = 0.10             # weaker partial credit band
W_AMOUNT_NEAR = 12.0
W_AMOUNT_LOOSE = 6.0

DATE_EXACT_DAYS = 0
DATE_TOLERANCE_DAYS = 2             # the §2.3 tolerance window
DATE_WIDE_DAYS = 7
DATE_LOOSE_DAYS = 30
W_DATE_TOLERANCE = 4.0
W_DATE_WIDE = 2.0
W_DATE_LOOSE = 1.0

# Similarity floors below which fuzzy evidence counts for nothing. Without
# these, every pair scores a few points on coincidence and the matrix stops
# discriminating.
NAME_SIMILARITY_FLOOR = 0.60
INVOICE_SIMILARITY_FLOOR = 0.85

# A pair below this total is not a candidate at all. Supplier identity alone
# (30) is deliberately not enough: two unrelated invoices from the same vendor
# would otherwise pair up. A candidate needs identity PLUS corroboration.
#
# 45 sits just above GSTIN(30) + a perfect vendor name(10) + a weak date(2) =
# 42 — the exact profile of two unrelated invoices from the same supplier, which
# is the dominant false-pair shape in this data. Chosen by sweeping 20..60 on
# the CALIBRATION SPLIT ONLY (§2.6 discipline: the frozen 30% is not a tuning
# surface). Calibration accuracy by floor: 40 -> 95.5%, 45 -> 97.9%, 50 -> 96.1%.
MIN_CANDIDATE_SCORE = 45.0

MAX_SCORE = (W_INVOICE_EXACT + W_GSTIN_EXACT + W_AMOUNT_EXACT
             + W_NAME_FUZZY + W_DATE_FUZZY)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateScore:
    """One cell of the §2.3 score matrix."""

    pr_id: str
    b2_id: str
    score: float
    components: Dict[str, float]

    def sort_key(self) -> Tuple:
        """Descending score, then a stable tiebreak so the assignment is
        reproducible regardless of dict/iteration order."""
        return (-self.score, self.pr_id, self.b2_id)


@dataclass(frozen=True)
class Match:
    """A purchase-register record and the 2B record assigned to it, if any."""

    pr_record: NormalizedRecord
    b2_record: Optional[NormalizedRecord]
    score: float = 0.0
    components: Dict[str, float] = field(default_factory=dict)

    @property
    def candidate_found(self) -> bool:
        return self.b2_record is not None

    @property
    def status(self) -> str:
        return "matched" if self.candidate_found else NO_CANDIDATE_FOUND

    @property
    def pr_id(self) -> str:
        return self.pr_record.source_id

    @property
    def b2_id(self) -> Optional[str]:
        return self.b2_record.source_id if self.b2_record else None


@dataclass(frozen=True)
class MatchResult:
    """The full outcome of one matching run."""

    matches: List[Match]
    unmatched_2b: List[NormalizedRecord]
    matrix_size: int
    candidate_pairs: int

    @property
    def matched(self) -> List[Match]:
        return [m for m in self.matches if m.candidate_found]

    @property
    def no_candidate(self) -> List[Match]:
        return [m for m in self.matches if not m.candidate_found]


# ---------------------------------------------------------------------------
# Field-level scorers
# ---------------------------------------------------------------------------


def _amount(record: NormalizedRecord, field_name: str = "taxable_value") -> Optional[float]:
    return parse_amount(record.value(field_name))


def _date(record: NormalizedRecord, field_name: str = "invoice_date") -> Optional[date]:
    return parse_date(record.value(field_name))


def score_invoice_number(pr: NormalizedRecord, b2: NormalizedRecord) -> float:
    a, b = pr.value("invoice_id"), b2.value("invoice_id")
    if not a or not b:
        return 0.0
    if a == b:
        return W_INVOICE_EXACT
    ratio = fuzz.ratio(a, b) / 100.0
    if ratio <= INVOICE_SIMILARITY_FLOOR:
        return 0.0
    span = 1.0 - INVOICE_SIMILARITY_FLOOR
    return W_INVOICE_FUZZY * (ratio - INVOICE_SIMILARITY_FLOOR) / span


def score_gstin(pr: NormalizedRecord, b2: NormalizedRecord) -> float:
    a, b = pr.value("vendor_gstin"), b2.value("vendor_gstin")
    if not a or not b:
        return 0.0
    if a == b:
        return W_GSTIN_EXACT
    # Same PAN, different state prefix: the same legal entity registered
    # elsewhere. Strong supplier evidence, and precisely the shape the §2.5
    # GSTIN-header rule is looking for — so it must survive matching.
    if len(a) == 15 and len(b) == 15 and a[2:12] == b[2:12]:
        return W_GSTIN_SAME_PAN
    return 0.0


def score_amount(pr: NormalizedRecord, b2: NormalizedRecord) -> float:
    a, b = _amount(pr), _amount(b2)
    if a is None or b is None:
        return 0.0
    delta = abs(a - b)
    if delta <= AMOUNT_EXACT_TOLERANCE:
        return W_AMOUNT_EXACT
    base = max(abs(a), abs(b), 1.0)
    relative = delta / base
    if relative <= AMOUNT_NEAR_PCT:
        return W_AMOUNT_NEAR
    if relative <= AMOUNT_LOOSE_PCT:
        return W_AMOUNT_LOOSE
    return 0.0


def score_vendor_name(pr: NormalizedRecord, b2: NormalizedRecord) -> float:
    a, b = pr.value("vendor_name"), b2.value("vendor_name")
    if not a or not b:
        return 0.0
    ratio = fuzz.token_sort_ratio(a.upper(), b.upper()) / 100.0
    if ratio <= NAME_SIMILARITY_FLOOR:
        return 0.0
    span = 1.0 - NAME_SIMILARITY_FLOOR
    return W_NAME_FUZZY * (ratio - NAME_SIMILARITY_FLOOR) / span


def score_date(pr: NormalizedRecord, b2: NormalizedRecord) -> float:
    a, b = _date(pr), _date(b2)
    if a is None or b is None:
        return 0.0
    days = abs((a - b).days)
    if days <= DATE_EXACT_DAYS:
        return W_DATE_FUZZY
    if days <= DATE_TOLERANCE_DAYS:
        return W_DATE_TOLERANCE
    if days <= DATE_WIDE_DAYS:
        return W_DATE_WIDE
    if days <= DATE_LOOSE_DAYS:
        return W_DATE_LOOSE
    return 0.0


SCORERS = {
    "invoice_number": score_invoice_number,
    "gstin": score_gstin,
    "amount": score_amount,
    "vendor_name": score_vendor_name,
    "date": score_date,
}


def score_pair(pr: NormalizedRecord, b2: NormalizedRecord) -> CandidateScore:
    components = {name: fn(pr, b2) for name, fn in SCORERS.items()}
    return CandidateScore(
        pr_id=pr.source_id,
        b2_id=b2.source_id,
        score=round(sum(components.values()), 6),
        components=components,
    )


# ---------------------------------------------------------------------------
# §2.3 steps 2-4: matrix, greedy one-to-one assignment, no_candidate_found
# ---------------------------------------------------------------------------


def build_score_matrix(
    pr_records: Sequence[NormalizedRecord],
    b2_records: Sequence[NormalizedRecord],
    min_score: float = MIN_CANDIDATE_SCORE,
) -> List[CandidateScore]:
    """§2.3 step 2. Every purchase-register record against every 2B record.

    Pairs scoring below `min_score` are not candidates and are dropped — they
    would only add noise to the assignment. The full cross product is still
    evaluated; only the surviving cells are returned.
    """
    matrix: List[CandidateScore] = []
    for pr in pr_records:
        for b2 in b2_records:
            candidate = score_pair(pr, b2)
            if candidate.score >= min_score:
                matrix.append(candidate)
    return matrix


def greedy_assign(matrix: Sequence[CandidateScore]) -> Dict[str, CandidateScore]:
    """§2.3 step 3. Highest-scoring pair first, then remove BOTH records.

    This is the one-to-one guarantee, and it is enforced structurally rather
    than checked afterwards: a record lands in `claimed_pr` or `claimed_2b` the
    moment it is assigned, and every later pair touching it is skipped. There
    is no path through this loop that assigns either side twice.

    Ties are broken by (pr_id, b2_id) so the result is reproducible.
    """
    claimed_pr: set = set()
    claimed_2b: set = set()
    assignment: Dict[str, CandidateScore] = {}

    for candidate in sorted(matrix, key=CandidateScore.sort_key):
        if candidate.pr_id in claimed_pr or candidate.b2_id in claimed_2b:
            continue
        claimed_pr.add(candidate.pr_id)
        claimed_2b.add(candidate.b2_id)
        assignment[candidate.pr_id] = candidate

    return assignment


def match_records(
    pr_records: Sequence[NormalizedRecord],
    b2_records: Sequence[NormalizedRecord],
    min_score: float = MIN_CANDIDATE_SCORE,
) -> MatchResult:
    """Run §2.3 end to end: matrix -> greedy assignment -> no_candidate_found."""
    matrix = build_score_matrix(pr_records, b2_records, min_score)
    assignment = greedy_assign(matrix)

    b2_by_id = {r.source_id: r for r in b2_records}
    matches: List[Match] = []
    claimed_2b: set = set()

    for pr in pr_records:
        candidate = assignment.get(pr.source_id)
        if candidate is None:
            # §2.3 step 4: a valid, expected output — not an error. Feeds the
            # rule engine as an "absence" case (§2.5).
            matches.append(Match(pr_record=pr, b2_record=None))
            continue
        b2 = b2_by_id[candidate.b2_id]
        claimed_2b.add(candidate.b2_id)
        matches.append(
            Match(pr_record=pr, b2_record=b2, score=candidate.score,
                  components=candidate.components)
        )

    unmatched_2b = [r for r in b2_records if r.source_id not in claimed_2b]

    return MatchResult(
        matches=matches,
        unmatched_2b=unmatched_2b,
        matrix_size=len(pr_records) * len(b2_records),
        candidate_pairs=len(matrix),
    )
