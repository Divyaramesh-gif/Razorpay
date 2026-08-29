"""§2.6 confidence: derived from evidence, never self-reported by a model."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import confidence as C
from src import evidence as E
from src.matcher import Match
from src.normalization import normalize_deterministic
from src.source_records import SOURCE_GSTR2B, SOURCE_PURCHASE_REGISTER, SourceRecord

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


def ev_pair(**b2):
    return E.compare(Match(rec(SOURCE_PURCHASE_REGISTER, "PR-0001"),
                           rec(SOURCE_GSTR2B, "2B-0001", **b2), 100.0))


def ev_absent():
    return E.compare(Match(rec(SOURCE_PURCHASE_REGISTER, "PR-0001"), None))


# --- the score is a weighted count of matching fields ----------------------

def test_identical_records_score_100():
    assert C.score_evidence(ev_pair()).value == 100.0


def test_no_candidate_scores_zero():
    """Nothing matched, so there is nothing to be confident about."""
    assert C.score_evidence(ev_absent()).value == 0.0


def test_gstin_and_amount_outweigh_date_formatting():
    """§2.6 states this explicitly."""
    assert C.FIELD_WEIGHTS["gstin"] > C.FIELD_WEIGHTS["date"]
    assert C.FIELD_WEIGHTS["amount"] > C.FIELD_WEIGHTS["date"]
    assert C.FIELD_WEIGHTS["gstin"] > C.FIELD_WEIGHTS["vendor_name"]


def test_weights_sum_to_100_so_the_score_reads_as_a_percentage():
    assert C.MAX_CONFIDENCE == 100.0


@pytest.mark.parametrize("overrides,expected", [
    ({"invoice_date": "2026-04-17"}, 90.0),                       # -date(10)
    ({"vendor_name": "Zenith Polymers LLP"}, 95.0),               # -name(5)
    ({"taxable_value": "90000.00"}, 70.0),                        # -amount(30)
    ({"vendor_gstin": "29AAGCB7383J1Z4"}, 70.0),                  # -gstin(30)
    ({"invoice_id": "INV-2604-09999"}, 80.0),                     # -invoice(20)
])
def test_each_field_removes_exactly_its_weight(overrides, expected):
    assert C.score_evidence(ev_pair(**overrides)).value == expected


def test_losing_a_heavy_field_costs_more_than_a_light_one():
    heavy = C.score_evidence(ev_pair(vendor_gstin="29AAGCB7383J1Z4")).value
    light = C.score_evidence(ev_pair(invoice_date="2026-04-17")).value
    assert heavy < light


def test_score_exposes_its_own_arithmetic():
    score = C.score_evidence(ev_pair(invoice_date="2026-04-17"))
    assert score.matched_weight == 90.0
    assert score.total_weight == 100.0
    assert score.contributions["date"] == 0.0
    assert score.contributions["gstin"] == 30.0
    assert "gstin=30" in score.explain()


def test_score_is_reproducible():
    evidence = ev_pair(taxable_value="90000.00")
    assert len({C.score_evidence(evidence).value for _ in range(50)}) == 1


def test_confidence_makes_no_api_call():
    """§2.6: NOT an LLM's self-reported confidence number."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "confidence.py")).read()
    for token in ("anthropic", "messages.create", "normalize_ai_assisted",
                  "clean_text_with_claude"):
        assert token not in src


def test_confidence_only_opens_the_calibration_artifact():
    """The scoring and sweep halves must not read any file at all; only the
    artifact loaders at the bottom of the module may. (That src/ cannot
    ADDRESS ground_truth.csv is asserted in test_source_preservation.py, which
    parses the AST rather than grepping prose.)"""
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "confidence.py")
    before_loaders = open(path).read().split("def load_calibration")[0]
    assert "open(" not in before_loaders


# --- clean-match flag (§2.6's second auto-reconcile condition) -------------

def test_identical_records_are_a_clean_match():
    assert C.score_evidence(ev_pair()).clean_match is True


@pytest.mark.parametrize("overrides", [
    {"vendor_gstin": "29AAGCB7383J1Z4"},
    {"taxable_value": "90000.00"},
    {"invoice_id": "INV-2604-09999"},
])
def test_a_critical_field_mismatch_is_not_a_clean_match(overrides):
    assert C.score_evidence(ev_pair(**overrides)).clean_match is False


@pytest.mark.parametrize("overrides", [
    {"invoice_date": "2026-04-17"},
    {"vendor_name": "ACME INDS. PVT LTD"},
])
def test_cosmetic_differences_remain_a_clean_match(overrides):
    """A date off by a day or a name spelled differently is not a value
    dispute — the fuzzy cases must still be able to auto-reconcile."""
    assert C.score_evidence(ev_pair(**overrides)).clean_match is True


def test_sub_rupee_amount_difference_is_still_a_clean_match():
    assert C.score_evidence(ev_pair(taxable_value="100000.99")).clean_match is True


def test_no_candidate_is_never_a_clean_match():
    assert C.score_evidence(ev_absent()).clean_match is False


# --- the sweep (§2.6 step 2) -----------------------------------------------

def test_sweep_finds_a_perfect_separator():
    samples = [(95.0, True)] * 10 + [(40.0, False)] * 10
    result = C.sweep_thresholds(samples)
    assert 40.0 < result.threshold <= 95.0
    assert result.best.youden_j == 1.0


def test_sweep_picks_the_plateau_midpoint():
    """An endpoint would sit flush against a score cluster and generalise
    worse."""
    result = C.sweep_thresholds([(90.0, True)] * 5 + [(50.0, False)] * 5)
    low, high = result.plateau
    assert result.threshold == pytest.approx((low + high) / 2.0)
    assert 50.0 < result.threshold <= 90.0


def test_sweep_reports_the_sample_it_saw():
    result = C.sweep_thresholds([(90.0, True)] * 3 + [(10.0, False)] * 7)
    assert result.n_samples == 10
    assert result.n_positive == 3


def test_sweep_refuses_an_empty_sample():
    with pytest.raises(ValueError, match="empty sample"):
        C.sweep_thresholds([])


def test_sweep_takes_data_not_a_file():
    """Structural guarantee that calibration cannot touch the frozen split by
    accident: the function has no way to go and fetch more records."""
    import inspect
    params = inspect.signature(C.sweep_thresholds).parameters
    assert "scored_labels" in params
    assert not any("path" in p or "file" in p for p in params)


def test_evaluate_threshold_confusion_counts():
    point = C.evaluate_threshold(
        [(90.0, True), (85.0, True), (40.0, False), (95.0, False)], 80.0)
    assert (point.true_positives, point.false_negatives) == (2, 0)
    assert (point.false_positives, point.true_negatives) == (1, 1)
    assert point.sensitivity == 1.0
    assert point.specificity == 0.5


def test_youden_j_is_zero_for_a_useless_threshold():
    point = C.evaluate_threshold([(90.0, True), (90.0, False)], 80.0)
    assert point.youden_j == 0.0


# --- the frozen artifact ----------------------------------------------------

def test_calibration_artifact_exists_and_is_readable():
    data = C.load_calibration()
    assert data["version"] == "2026-04"
    assert 0.0 < data["confidence_gate"]["threshold"] < 100.0


def test_threshold_loads_as_a_plain_number():
    assert isinstance(C.load_threshold(), float)


def test_artifact_records_the_protocol_and_both_splits():
    data = C.load_calibration()
    assert "calibration split only" in data["protocol"].lower()
    assert data["calibration_split"]["n_records"] > 0
    assert "frozen_test_split" in data, "frozen split must be reported once"
    assert "not re-tuned" in data["frozen_test_split"]["note"].lower()


def test_artifact_weights_match_the_code():
    assert C.load_calibration()["field_weights"] == C.FIELD_WEIGHTS


def test_frozen_split_was_not_larger_than_the_calibration_split():
    data = C.load_calibration()
    assert data["frozen_test_split"]["n_records"] < \
        data["calibration_split"]["n_records"]
