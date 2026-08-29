"""§2.6 three-way gate: auto-reconcile / classified exception / indeterminate."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import confidence as C
from src import evidence as E
from src import gate as G
from src.matcher import Match
from src.normalization import normalize_deterministic
from src.rule_engine import Classification, RuleEvaluation
from src.source_records import SOURCE_GSTR2B, SOURCE_PURCHASE_REGISTER, SourceRecord

THRESHOLD = 80.25

BASE = {
    "record_id": "PR-0001", "invoice_id": "INV-2604-00001",
    "vendor_gstin": "27AAPFU0939F1ZV",
    "vendor_name": "Acme Industries Private Limited",
    "invoice_date": "2026-04-15", "taxable_value": "100000.00",
    "cgst": "9000.00", "sgst": "9000.00", "igst": "0.00",
    "total_tax": "18000.00", "invoice_value": "118000.00",
}


def rec(source, rid, **o):
    raw = dict(BASE); raw["record_id"] = rid; raw.update(o)
    return normalize_deterministic(SourceRecord(source, rid, 1, raw))


def evidence(**b2):
    return E.compare(Match(rec(SOURCE_PURCHASE_REGISTER, "PR-0001"),
                           rec(SOURCE_GSTR2B, "2B-0001", **b2), 100.0))


def evidence_absent():
    return E.compare(Match(rec(SOURCE_PURCHASE_REGISTER, "PR-0001"), None))


def evaluation(rule_id=None, category=None, record_id="purchase_register:PR-0001"):
    return RuleEvaluation(
        record_id=record_id, invoice_id="INV-2604-00001", candidate_found=True,
        classification=Classification(rule_id, category, "test", "2026-04"),
        operational_flags=[],
    )


# --- outcome 1: auto-reconcile ---------------------------------------------

def test_clean_high_confidence_match_auto_reconciles():
    decision = G.decide(evaluation(), evidence(), THRESHOLD)
    assert decision.outcome == G.AUTO_RECONCILE
    assert decision.confidence.value == 100.0


@pytest.mark.parametrize("overrides", [
    {"invoice_date": "2026-04-17"},             # date tolerance
    {"vendor_name": "ACME INDS. PVT LTD"},      # name variant
    {"vendor_name": "Ac0me 1ndustries Private Limited"},   # OCR damage
    {"taxable_value": "100000.75"},             # sub-rupee rounding
])
def test_fuzzy_but_sound_matches_auto_reconcile(overrides):
    assert G.decide(evaluation(), evidence(**overrides), THRESHOLD).outcome == \
        G.AUTO_RECONCILE


def test_auto_reconcile_needs_both_conditions():
    """§2.6: confidence >= threshold AND evidence shows a clean match.
    A high score alone is not enough."""
    ev = evidence(taxable_value="100500.00")     # -amount(30) -> 70
    score = C.score_evidence(ev)
    assert score.clean_match is False
    assert G.decide(evaluation("CLS-002", "credit_note_netting"), ev,
                    50.0).outcome != G.AUTO_RECONCILE


def test_a_clean_match_below_threshold_does_not_auto_reconcile():
    decision = G.decide(evaluation(), evidence(), 100.5)
    assert decision.outcome != G.AUTO_RECONCILE


# --- outcome 2: classified exception ---------------------------------------

@pytest.mark.parametrize("rule_id,category,overrides", [
    ("CLS-001", "gstin_header_mismatch",
     {"vendor_gstin": "07AAPFU0939F1ZH", "cgst": "0.00", "sgst": "0.00",
      "igst": "18000.00"}),
    ("CLS-002", "credit_note_netting", {"taxable_value": "90000.00"}),
])
def test_below_threshold_with_a_named_category_is_a_classified_exception(
        rule_id, category, overrides):
    decision = G.decide(evaluation(rule_id, category), evidence(**overrides),
                        THRESHOLD)
    assert decision.outcome == G.CLASSIFIED_EXCEPTION
    assert decision.category == category
    assert decision.rule_id == rule_id


def test_absence_cases_are_classified_exceptions_not_indeterminate():
    """no_candidate_found scores 0, but the rule engine names it — so it is a
    classified exception, not a puzzle for a human."""
    decision = G.decide(evaluation("CLS-004", "late_filed_supplier"),
                        evidence_absent(), THRESHOLD)
    assert decision.outcome == G.CLASSIFIED_EXCEPTION
    assert decision.confidence.value == 0.0


# --- outcome 3: indeterminate ----------------------------------------------

def test_no_rule_and_low_confidence_is_indeterminate():
    ev = evidence(invoice_id="INV-2604-09999", taxable_value="83271.44",
                  invoice_date="2026-04-24", vendor_name="Ac,me Ind0stries")
    decision = G.decide(evaluation(), ev, THRESHOLD)
    assert decision.outcome == G.INDETERMINATE
    assert decision.needs_human_review is True


def test_indeterminate_is_driven_by_the_rule_engine_not_the_score():
    """§2.6: 'Rule engine cannot confidently assign any category'."""
    ev = evidence(taxable_value="83271.44")
    assert G.decide(evaluation(), ev, THRESHOLD).outcome == G.INDETERMINATE
    assert G.decide(evaluation("CLS-002", "credit_note_netting"), ev,
                    THRESHOLD).outcome == G.CLASSIFIED_EXCEPTION


def test_only_indeterminate_needs_human_review():
    assert G.NEEDS_HUMAN_REVIEW == {G.INDETERMINATE}
    assert not G.decide(evaluation(), evidence(), THRESHOLD).needs_human_review


# --- quarantined records stay out (§2.1) -----------------------------------

def test_gate_refuses_a_quarantined_record():
    with pytest.raises(ValueError, match="quarantined records must not reach"):
        G.decide_batch([evaluation()], [evidence()], THRESHOLD,
                       quarantined_record_ids=["purchase_register:PR-0001"])


def test_gate_accepts_a_batch_with_unrelated_quarantined_ids():
    decisions = G.decide_batch([evaluation()], [evidence()], THRESHOLD,
                               quarantined_record_ids=["purchase_register:PR-9999"])
    assert len(decisions) == 1


def test_quarantine_is_not_one_of_the_gate_outcomes():
    """§2.1: a quarantined record is a data-quality problem, not a
    reconciliation outcome. It must not be expressible here."""
    assert set(G.OUTCOMES) == {G.AUTO_RECONCILE, G.CLASSIFIED_EXCEPTION,
                               G.INDETERMINATE}
    assert "quarantine" not in G.OUTCOMES


# --- batch behaviour --------------------------------------------------------

def test_counts_always_report_every_outcome_including_zeroes():
    tally = G.counts([G.decide(evaluation(), evidence(), THRESHOLD)])
    assert set(tally) == set(G.OUTCOMES)
    assert tally[G.AUTO_RECONCILE] == 1
    assert tally[G.INDETERMINATE] == 0


def test_batch_rejects_mismatched_inputs():
    with pytest.raises(ValueError, match="one to one"):
        G.decide_batch([evaluation()], [], THRESHOLD)


def test_every_record_gets_exactly_one_outcome():
    evals = [evaluation(record_id=f"purchase_register:PR-{i:04d}") for i in range(5)]
    evs = [evidence() for _ in range(5)]
    decisions = G.decide_batch(evals, evs, THRESHOLD)
    assert len(decisions) == 5
    assert all(d.outcome in G.OUTCOMES for d in decisions)


# --- determinism and provenance --------------------------------------------

def test_decision_is_deterministic():
    ev, ep = evidence(taxable_value="90000.00"), evaluation("CLS-002",
                                                            "credit_note_netting")
    assert len({G.decide(ep, ev, THRESHOLD).outcome for _ in range(50)}) == 1


def test_gate_makes_no_api_call():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "gate.py")).read()
    for token in ("anthropic", "messages.create", "clean_text_with_claude"):
        assert token not in src


def test_decision_records_the_threshold_it_used():
    assert G.decide(evaluation(), evidence(), THRESHOLD).threshold == THRESHOLD


def test_reason_explains_the_decision():
    auto = G.decide(evaluation(), evidence(), THRESHOLD)
    assert "100.0" in auto.reason and "clean match" in auto.reason

    exc = G.decide(evaluation("CLS-002", "credit_note_netting"),
                   evidence(taxable_value="90000.00"), THRESHOLD)
    assert "CLS-002" in exc.reason and "credit_note_netting" in exc.reason

    ind = G.decide(evaluation(), evidence(taxable_value="83271.44"), THRESHOLD)
    assert "no rule could assign a category" in ind.reason
    assert "human review" in ind.reason


# ==========================================================================
# End to end over the real batch, graded against ground truth
# ==========================================================================
# ground_truth.csv is an EVALUATION input. The gate below ran on a frozen
# threshold and never saw a label.

@pytest.fixture(scope="module")
def graded():
    import csv
    from src import matcher as M, normalization as Nz, validation as Vd
    from src.rule_engine import RuleEngine
    from src.source_records import load_source

    pr_valid, pr_invalid = Vd.partition(load_source(SOURCE_PURCHASE_REGISTER))
    b2_valid, _ = Vd.partition(load_source(SOURCE_GSTR2B))
    result = M.match_records(
        [Nz.normalize_deterministic(r.record) for r in pr_valid],
        [Nz.normalize_deterministic(r.record) for r in b2_valid],
    )
    evidences = E.compare_all(result.matches)
    batch = RuleEngine().evaluate_batch(result.matches, evidences)
    decisions = G.decide_batch(batch.evaluations, evidences, C.load_threshold())

    gt_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "ground_truth.csv")
    gt = {r["pr_record_id"]: r for r in csv.DictReader(open(gt_path))}
    return [(gt[d.record_id.split(":")[1]], d) for d in decisions], len(pr_invalid)


def test_every_record_that_passed_validation_is_gated(graded):
    rows, quarantined = graded
    assert len(rows) == 480
    assert quarantined == 20
    assert len(rows) + quarantined == 500


def test_no_quarantined_record_reaches_the_gate(graded):
    """§2.1: a data-quality failure must never appear as a gate outcome."""
    rows, _ = graded
    assert all(g["expected_outcome"] != "quarantine" for g, _ in rows)


def test_nothing_is_wrongly_auto_reconciled(graded):
    """The safety-critical direction. Wrongly auto-reconciling silently
    accepts a mismatch; wrongly flagging one only costs a review."""
    rows, _ = graded
    wrong = [(g["pr_record_id"], g["case_type"]) for g, d in rows
             if d.outcome == G.AUTO_RECONCILE
             and g["expected_outcome"] != "auto_reconcile"]
    assert not wrong, wrong[:5]


def test_every_true_match_is_auto_reconciled(graded):
    rows, _ = graded
    missed = [(g["pr_record_id"], g["case_type"], d.outcome) for g, d in rows
              if g["expected_outcome"] == "auto_reconcile"
              and d.outcome != G.AUTO_RECONCILE]
    assert not missed, missed[:5]


def test_overall_outcome_accuracy(graded):
    rows, _ = graded
    correct = sum(1 for g, d in rows if g["expected_outcome"] == d.outcome)
    assert correct / len(rows) >= 0.95, f"{correct}/{len(rows)}"


def test_frozen_split_accuracy_is_reported_not_tuned(graded):
    """The threshold was chosen on the calibration split. The frozen split is
    checked at a loose bound and never tuned against."""
    rows, _ = graded
    frozen = [(g, d) for g, d in rows if g["split"] == "frozen_test"]
    correct = sum(1 for g, d in frozen if g["expected_outcome"] == d.outcome)
    assert correct / len(frozen) >= 0.90, f"{correct}/{len(frozen)}"


def test_all_outcome_errors_trace_to_a_matcher_miss(graded):
    """The gate should not be independently wrong: every disagreement must be
    the right decision applied to a pairing Phase 3 got wrong."""
    rows, _ = graded
    for g, d in rows:
        if g["expected_outcome"] == d.outcome:
            continue
        assert g["case_type"] in {"indeterminate_ambiguous", "late_filed_supplier",
                                  "invoice_removed_post_claim"}, \
            (g["pr_record_id"], g["case_type"], g["expected_outcome"], d.outcome)


def test_scores_separate_cleanly_at_the_frozen_threshold(graded):
    """A wide gap between the two score populations is what makes the
    calibrated threshold meaningful rather than arbitrary."""
    rows, _ = graded
    threshold = C.load_threshold()
    true_match = [d.confidence.value for g, d in rows
                  if g["expected_outcome"] == "auto_reconcile"]
    other = [d.confidence.value for g, d in rows
             if g["expected_outcome"] != "auto_reconcile"]
    assert min(true_match) > max(other)
    assert max(other) < threshold < min(true_match)


def test_every_indeterminate_is_flagged_for_review(graded):
    rows, _ = graded
    for _, d in rows:
        assert d.needs_human_review == (d.outcome == G.INDETERMINATE)


def test_every_classified_exception_carries_a_named_category(graded):
    rows, _ = graded
    for _, d in rows:
        if d.outcome == G.CLASSIFIED_EXCEPTION:
            assert d.category and d.rule_id
        if d.outcome == G.AUTO_RECONCILE:
            assert d.confidence.clean_match is True
