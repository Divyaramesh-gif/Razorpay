"""§2.3 matching. The one-to-one property is the whole point of this stage,
so it is asserted directly, adversarially, and over the real batch.

§5 step 4: "write test_matcher.py alongside it immediately, asserting no record
appears in more than one pair. This is the stage most likely to silently
misbehave if not tested directly."
"""

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import matcher as M
from src import validation as V
from src.normalization import NormalizedRecord, normalize_deterministic
from src.source_records import (
    SOURCE_GSTR2B,
    SOURCE_PURCHASE_REGISTER,
    SourceRecord,
    load_source,
)

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

BASE = {
    "record_id": "PR-0001",
    "invoice_id": "INV-2604-00001",
    "vendor_gstin": "27AAPFU0939F1ZV",
    "vendor_name": "Acme Industries Private Limited",
    "invoice_date": "2026-04-15",
    "taxable_value": "100000.00",
    "cgst": "9000.00", "sgst": "9000.00", "igst": "0.00",
    "total_tax": "18000.00", "invoice_value": "118000.00",
}


def rec(source, record_id, row=1, **overrides) -> NormalizedRecord:
    raw = dict(BASE)
    raw["record_id"] = record_id
    raw.update(overrides)
    return normalize_deterministic(SourceRecord(source, record_id, row, raw))


def pr(record_id="PR-0001", **o):
    return rec(SOURCE_PURCHASE_REGISTER, record_id, **o)


def b2(record_id="2B-0001", **o):
    return rec(SOURCE_GSTR2B, record_id, **o)


@pytest.fixture(scope="module")
def real_batch():
    """The real Phase 1 batch, validated and normalised — matcher input."""
    pr_valid, _ = V.partition(load_source(SOURCE_PURCHASE_REGISTER))
    b2_valid, _ = V.partition(load_source(SOURCE_GSTR2B))
    return (
        [normalize_deterministic(r.record) for r in pr_valid],
        [normalize_deterministic(r.record) for r in b2_valid],
    )


@pytest.fixture(scope="module")
def real_result(real_batch):
    return M.match_records(*real_batch)


# ==========================================================================
# THE ONE-TO-ONE PROPERTY  (§2.3 step 3)
# ==========================================================================

def test_no_2b_record_is_claimed_twice(real_result):
    claimed = [m.b2_id for m in real_result.matched]
    assert len(claimed) == len(set(claimed)), "a 2B record was claimed twice"


def test_no_pr_record_appears_in_two_pairs(real_result):
    pr_ids = [m.pr_id for m in real_result.matches]
    assert len(pr_ids) == len(set(pr_ids))


def test_every_pr_record_appears_exactly_once(real_batch, real_result):
    pr_records, _ = real_batch
    assert len(real_result.matches) == len(pr_records)
    assert {m.pr_id for m in real_result.matches} == {r.source_id for r in pr_records}


def test_matched_and_unmatched_2b_partition_the_2b_side(real_batch, real_result):
    _, b2_records = real_batch
    claimed = {m.b2_id for m in real_result.matched}
    unclaimed = {r.source_id for r in real_result.unmatched_2b}
    assert claimed.isdisjoint(unclaimed)
    assert claimed | unclaimed == {r.source_id for r in b2_records}


def test_one_to_one_holds_when_every_record_is_identical():
    """The adversarial case: 5 indistinguishable PR records and 5
    indistinguishable 2B records all score the maximum against each other.
    A broken matcher would give every PR the same 2B row."""
    prs = [pr(f"PR-{i:04d}") for i in range(1, 6)]
    b2s = [b2(f"2B-{i:04d}") for i in range(1, 6)]
    result = M.match_records(prs, b2s)

    claimed = [m.b2_id for m in result.matched]
    assert len(claimed) == len(set(claimed)) == 5
    assert not result.unmatched_2b


def test_one_to_one_holds_when_2b_side_is_scarce():
    """10 PR records competing for 2 identical 2B rows: exactly 2 win, the
    other 8 are no_candidate_found."""
    prs = [pr(f"PR-{i:04d}") for i in range(1, 11)]
    b2s = [b2("2B-0001"), b2("2B-0002")]
    result = M.match_records(prs, b2s)

    assert len(result.matched) == 2
    assert len(result.no_candidate) == 8
    assert len({m.b2_id for m in result.matched}) == 2


def test_greedy_assignment_never_double_claims_on_random_matrices():
    """Property test: 200 random score matrices, none may violate one-to-one."""
    rng = random.Random(4242)
    for _ in range(200):
        n_pr, n_b2 = rng.randint(1, 12), rng.randint(1, 12)
        matrix = [
            M.CandidateScore(f"pr{i}", f"b{j}", rng.choice([50.0, 60.0, 75.0, 90.0]), {})
            for i in range(n_pr) for j in range(n_b2)
            if rng.random() < 0.7
        ]
        assignment = M.greedy_assign(matrix)
        assigned_2b = [c.b2_id for c in assignment.values()]
        assert len(assigned_2b) == len(set(assigned_2b))
        assert len(assignment) == len(set(assignment))
        assert len(assignment) <= min(n_pr, n_b2)


def test_greedy_takes_the_highest_scoring_pair_first():
    matrix = [
        M.CandidateScore("pr1", "b1", 60.0, {}),
        M.CandidateScore("pr1", "b2", 90.0, {}),   # best pair overall
        M.CandidateScore("pr2", "b2", 80.0, {}),
    ]
    assignment = M.greedy_assign(matrix)
    assert assignment["pr1"].b2_id == "b2"
    assert "pr2" not in assignment       # b2 was taken; pr2 has nothing left


def test_assignment_is_deterministic_under_input_reordering():
    """Ties must not resolve differently just because the matrix arrived in a
    different order — otherwise the pipeline is not reproducible."""
    rng = random.Random(7)
    matrix = [
        M.CandidateScore(f"pr{i}", f"b{j}", 70.0, {})
        for i in range(6) for j in range(6)
    ]
    baseline = {k: v.b2_id for k, v in M.greedy_assign(matrix).items()}
    for _ in range(10):
        shuffled = list(matrix)
        rng.shuffle(shuffled)
        assert {k: v.b2_id for k, v in M.greedy_assign(shuffled).items()} == baseline


def test_full_run_is_reproducible(real_batch):
    pr_records, b2_records = real_batch
    a = M.match_records(pr_records, b2_records)
    b = M.match_records(pr_records, b2_records)
    assert [(m.pr_id, m.b2_id, m.score) for m in a.matches] == \
           [(m.pr_id, m.b2_id, m.score) for m in b.matches]


# ==========================================================================
# no_candidate_found is a valid output  (§2.3 step 4)
# ==========================================================================

def test_no_candidate_is_a_status_not_an_error():
    result = M.match_records([pr()], [])
    match = result.matches[0]
    assert match.status == M.NO_CANDIDATE_FOUND
    assert match.candidate_found is False
    assert match.b2_record is None
    assert match.b2_id is None


def test_unrelated_2b_records_do_not_become_candidates():
    """A record that shares nothing must not be dragged into a pair."""
    result = M.match_records(
        [pr()],
        [b2("2B-0001", invoice_id="INV-2604-07777",
            vendor_gstin="29AAGCB7383J1Z4", vendor_name="Zenith Polymers LLP",
            invoice_date="2026-04-02", taxable_value="7500.00")],
    )
    assert not result.matches[0].candidate_found


def test_empty_inputs_are_handled():
    assert M.match_records([], []).matches == []
    assert M.match_records([], [b2()]).unmatched_2b


# ==========================================================================
# Scoring: exact highest, fuzzy lower but non-zero  (§2.3 step 1)
# ==========================================================================

def test_identical_records_score_the_maximum():
    assert M.score_pair(pr(), b2()).score == pytest.approx(M.MAX_SCORE)


def test_exact_fields_outrank_fuzzy_fields():
    exact = M.W_INVOICE_EXACT + M.W_GSTIN_EXACT + M.W_AMOUNT_EXACT
    fuzzy = M.W_NAME_FUZZY + M.W_DATE_FUZZY
    assert exact > fuzzy * 3


@pytest.mark.parametrize("delta,expected", [
    ("100000.00", M.W_AMOUNT_EXACT),        # identical
    ("100000.99", M.W_AMOUNT_EXACT),        # within Rs.1 — §2.3's exact band
    ("101000.00", M.W_AMOUNT_NEAR),         # 1% off
    ("105000.00", M.W_AMOUNT_LOOSE),        # 5% off
    ("300000.00", 0.0),                     # unrelated
])
def test_amount_scoring_bands(delta, expected):
    assert M.score_amount(pr(), b2(taxable_value=delta)) == pytest.approx(expected)


@pytest.mark.parametrize("other,expected", [
    ("2026-04-15", M.W_DATE_FUZZY),
    ("2026-04-17", M.W_DATE_TOLERANCE),
    ("2026-04-20", M.W_DATE_WIDE),
    ("2026-05-10", M.W_DATE_LOOSE),
    ("2026-07-30", 0.0),
])
def test_date_scoring_bands(other, expected):
    assert M.score_date(pr(), b2(invoice_date=other)) == pytest.approx(expected)


def test_fuzzy_vendor_name_scores_lower_but_non_zero():
    score = M.score_vendor_name(pr(), b2(vendor_name="ACME INDS. PVT LTD"))
    assert 0 < score < M.W_GSTIN_EXACT


def test_ocr_damaged_name_still_scores():
    score = M.score_vendor_name(pr(), b2(vendor_name="Ac0me 1ndustries Private Limited"))
    assert score > 0


def test_unrelated_vendor_name_scores_zero():
    assert M.score_vendor_name(pr(), b2(vendor_name="Zenith Polymers LLP")) == 0.0


def test_same_pan_different_state_keeps_supplier_evidence():
    """The §2.5 GSTIN-header case must survive matching to be classified."""
    score = M.score_gstin(pr(), b2(vendor_gstin="07AAPFU0939F1ZH"))
    assert score == M.W_GSTIN_SAME_PAN
    assert 0 < score < M.W_GSTIN_EXACT


def test_different_pan_scores_zero():
    assert M.score_gstin(pr(), b2(vendor_gstin="29AAGCB7383J1Z4")) == 0.0


def test_garbled_invoice_number_scores_lower_but_non_zero():
    score = M.score_invoice_number(pr(), b2(invoice_id="INV-2604-00010"))
    assert 0 < score < M.W_INVOICE_EXACT


# ==========================================================================
# The matcher makes no decisions  (steps 5-7 own those)
# ==========================================================================

def test_match_exposes_no_category_or_confidence():
    import dataclasses
    for cls in (M.Match, M.CandidateScore, M.MatchResult):
        names = {f.name for f in dataclasses.fields(cls)}
        assert not (names & {"confidence", "category", "classification",
                             "rule_id", "outcome", "action", "verdict"})


def test_matcher_does_not_import_later_stages():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "matcher.py")).read()
    for module in ("rule_engine", "confidence", "gate", "report", "audit_log"):
        assert f"from .{module}" not in src and f"import {module}" not in src


# ==========================================================================
# Accuracy against ground truth
# ==========================================================================
# ground_truth.csv is an EVALUATION input, never a pipeline input — the matcher
# above ran without it. These tests grade the result after the fact.

@pytest.fixture(scope="module")
def graded(real_result):
    import csv
    gt_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "ground_truth.csv")
    gt = {r["pr_record_id"]: r for r in csv.DictReader(open(gt_path))}
    rows = []
    for m in real_result.matches:
        g = gt[m.pr_record.record_id]
        expected = f"gstr2b:{g['gstr2b_record_id']}" if g["gstr2b_record_id"] else None
        rows.append((g["case_type"], g["split"], expected, m.b2_id))
    return rows


def test_no_pair_is_assigned_the_wrong_partner_on_solved_cases(graded):
    """A wrong partner is worse than no partner: it manufactures evidence.
    On every case type with an unambiguous answer, there must be none."""
    solved = {"clean_exact_match", "fuzzy_vendor_name_variant", "fuzzy_ocr_artifact",
              "fuzzy_date_tolerance", "fuzzy_amount_rounding",
              "gstin_header_mismatch", "credit_note_netting"}
    wrong = [(c, e, g) for c, _, e, g in graded
             if c in solved and e is not None and g is not None and g != e]
    assert not wrong, wrong[:5]


@pytest.mark.parametrize("case_type", [
    "clean_exact_match", "fuzzy_vendor_name_variant", "fuzzy_ocr_artifact",
    "fuzzy_date_tolerance", "fuzzy_amount_rounding",
    "gstin_header_mismatch", "credit_note_netting",
])
def test_well_defined_case_types_match_perfectly(graded, case_type):
    rows = [(e, g) for c, _, e, g in graded if c == case_type]
    correct = sum(1 for e, g in rows if e == g)
    assert correct == len(rows), f"{case_type}: {correct}/{len(rows)}"


def test_overall_accuracy(graded):
    correct = sum(1 for _, _, e, g in graded if e == g)
    assert correct / len(graded) >= 0.95, f"{correct}/{len(graded)}"


def test_frozen_split_accuracy_is_reported_not_tuned(graded):
    """MIN_CANDIDATE_SCORE was swept on the calibration split only (§2.6).
    The frozen split is checked once, at a loose bound, and never tuned against."""
    frozen = [(e, g) for _, s, e, g in graded if s == "frozen_test"]
    correct = sum(1 for e, g in frozen if e == g)
    assert correct / len(frozen) >= 0.90, f"{correct}/{len(frozen)}"


def test_ambiguous_records_are_the_only_weak_spot(graded):
    """Everything the matcher gets wrong should be a deliberately ambiguous or
    absent record — not a case with a clean answer."""
    misses = {c for c, _, e, g in graded if e != g}
    assert misses <= {"indeterminate_ambiguous", "late_filed_supplier",
                      "invoice_removed_post_claim"}, misses
