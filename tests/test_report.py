"""§2.7 evaluation report: frozen test set only, five sections kept separate."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import gate as G
from src import pipeline as P
from src import report as R

FIXED_NOW = "2026-06-10T00:00:00+00:00"


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("report") / "ledger.sqlite")
    return P.run(db_path=db, now=FIXED_NOW)


@pytest.fixture(scope="module")
def splits():
    return R.load_splits()


@pytest.fixture(scope="module")
def frozen(result, splits):
    return R.build_report(result, splits, R.FROZEN_TEST,
                          expected_outcomes=R.load_expected_outcomes())


# --- frozen test set only --------------------------------------------------

def test_report_covers_only_the_frozen_split(frozen, splits, result):
    """The frozen split is 151 records by label, but 6 of them were
    quarantined at §2.1 and never scored — so the report covers 145. The
    quarantined 6 are counted on their own line instead, never here."""
    labelled = {k for k, v in splits.items() if v == R.FROZEN_TEST}
    quarantined = {e.record_id for e in result.quarantined}
    assert len(labelled) == 151
    assert len(labelled & quarantined) == 6
    assert frozen.scored == len(labelled - quarantined) == 145
    assert frozen.split == R.FROZEN_TEST


def test_no_quarantined_record_appears_in_a_split_report(frozen, result, splits):
    """Whichever split a quarantined record was labelled with, it must not be
    scored in that split's report."""
    quarantined = {e.record_id for e in result.quarantined}
    scored = {d.record_id for d in result.decisions
              if splits.get(d.record_id) == R.FROZEN_TEST}
    assert scored.isdisjoint(quarantined)
    assert len(scored) == frozen.scored


def test_calibration_split_is_a_separate_report(result, splits):
    calibration = R.build_report(result, splits, R.CALIBRATION)
    assert calibration.scored == 335
    assert calibration.scored + 145 == 480      # every scored record, once


def test_the_two_splits_partition_the_scored_records(result, splits):
    a = R.build_report(result, splits, R.FROZEN_TEST)
    b = R.build_report(result, splits, R.CALIBRATION)
    assert a.scored + b.scored == result.scored


def test_build_report_takes_splits_as_an_argument():
    """The caller decides what the report may see; the function cannot widen
    its own scope."""
    import inspect
    params = inspect.signature(R.build_report).parameters
    assert "splits" in params
    assert params["splits"].default is inspect.Parameter.empty


# --- 1. match rate, broken out separately ---------------------------------

def test_match_rate_is_broken_into_three_buckets(frozen):
    mr = frozen.match_rate
    assert mr.exact > 0 and mr.fuzzy > 0 and mr.rule_classified > 0
    assert mr.resolved == mr.exact + mr.fuzzy + mr.rule_classified


def test_match_rate_buckets_plus_unresolved_cover_every_record(frozen):
    mr = frozen.match_rate
    assert mr.total == frozen.scored
    assert mr.resolved + mr.unresolved == frozen.scored


def test_exact_means_every_field_agreed(result, splits):
    """The bucket is derived from the pipeline's own evidence, not a label."""
    joined = result.by_record()
    for record_id, row in joined.items():
        if splits.get(record_id) != R.FROZEN_TEST:
            continue
        bucket = R.classify_match_type(row["evidence"], row["decision"].outcome)
        if bucket == R.MATCH_EXACT:
            assert row["evidence"].mismatched_fields() == []
        elif bucket == R.MATCH_FUZZY:
            assert row["evidence"].mismatched_fields() != []
            assert row["decision"].outcome == G.AUTO_RECONCILE


def test_rule_classified_bucket_matches_the_classified_exceptions(frozen):
    assert frozen.match_rate.rule_classified == \
        sum(frozen.exceptions_by_category.values())


def test_unresolved_bucket_is_exactly_the_indeterminates(frozen):
    assert frozen.match_rate.unresolved == frozen.indeterminate


# --- 2. named exception categories ----------------------------------------

def test_exceptions_are_listed_by_named_category(frozen):
    assert set(frozen.exceptions_by_category) <= {
        "gstin_header_mismatch", "credit_note_netting",
        "invoice_removed_post_claim", "late_filed_supplier"}
    assert all(n > 0 for n in frozen.exceptions_by_category.values())


def test_every_classified_exception_has_a_category(frozen):
    assert None not in frozen.exceptions_by_category
    assert "" not in frozen.exceptions_by_category


# --- 3. operational checks, listed SEPARATELY -----------------------------

def test_operational_flags_are_separate_from_the_exception_list(frozen):
    """§2.7: 'listed separately from the classification exceptions'."""
    assert set(frozen.operational_88d) & set(frozen.exceptions_by_category) == set()
    assert set(frozen.operational_drc01c) & set(frozen.exceptions_by_category) == set()


def test_operational_counts_cover_every_scored_record(frozen):
    """Operational checks apply to all records, not only exceptions — which is
    why they cannot be a sub-list of section 2."""
    assert sum(frozen.operational_88d.values()) == frozen.scored
    assert sum(frozen.operational_drc01c.values()) == frozen.scored


def test_both_operational_checks_are_reported(frozen):
    assert "within_window" in frozen.operational_88d
    assert "outside_window" in frozen.operational_88d
    assert "breached" in frozen.operational_drc01c


def test_supplier_breach_count_is_derived_not_hardcoded(frozen):
    assert 0 < frozen.suppliers_breaching_drc01c < frozen.suppliers_total
    assert frozen.suppliers_total == 40


# --- 4 & 5. indeterminate and quarantine ----------------------------------

def test_indeterminate_is_reported_as_its_own_count(frozen):
    assert frozen.indeterminate == 8


def test_quarantine_is_its_own_line_not_folded_in(frozen):
    """§2.7: 'reported as its own line, not folded into any of the above'."""
    assert frozen.quarantined == 20
    assert frozen.quarantined not in (frozen.match_rate.total,)
    assert frozen.match_rate.total + frozen.quarantined != frozen.scored
    assert frozen.scored == frozen.match_rate.total    # quarantine excluded


def test_quarantine_is_batch_wide_not_split_scoped(result, splits):
    """A quarantined record was never scored, so it belongs to no split."""
    a = R.build_report(result, splits, R.FROZEN_TEST)
    b = R.build_report(result, splits, R.CALIBRATION)
    assert a.quarantined == b.quarantined == result.quarantined_count


def test_quarantine_breakdown_by_error_type(frozen):
    assert frozen.quarantined_by_error == {
        "invalid_gstin_format": 6,
        "missing_required_field": 6,
        "non_numeric_or_negative_amount": 4,
        "unparseable_or_implausible_date": 4,
    }
    assert sum(frozen.quarantined_by_error.values()) == frozen.quarantined


# --- 6 & 7. confidence and audit information ------------------------------

def test_confidence_section_reports_threshold_and_spread(frozen):
    c = frozen.confidence
    assert c.threshold == 80.25
    assert c.minimum == 0.0 and c.maximum == 100.0
    assert 0 < c.mean < 100
    assert sum(c.distribution.values()) == frozen.scored


def test_confidence_is_broken_down_by_outcome(frozen):
    by_outcome = frozen.confidence.by_outcome
    assert set(by_outcome) <= set(G.OUTCOMES)
    assert by_outcome[G.AUTO_RECONCILE]["min"] >= frozen.confidence.threshold


def test_audit_section_reports_rows_actions_and_pending_review(frozen):
    audit = frozen.audit
    assert audit.rows == frozen.scored
    assert sum(audit.by_action.values()) == frozen.scored
    assert audit.pending_review == frozen.indeterminate
    assert audit.table == "audit_log"


def test_audit_actions_agree_with_the_match_rate(frozen):
    assert frozen.audit.by_action[G.AUTO_RECONCILE] == \
        frozen.match_rate.exact + frozen.match_rate.fuzzy
    assert frozen.audit.by_action[G.CLASSIFIED_EXCEPTION] == \
        frozen.match_rate.rule_classified


# --- 8. accuracy, reported not tuned --------------------------------------

def test_accuracy_is_reported_when_labels_are_supplied(frozen):
    assert frozen.accuracy is not None
    assert frozen.accuracy.total == 145
    assert frozen.accuracy.rate >= 0.90


def test_accuracy_is_omitted_when_labels_are_withheld(result, splits):
    report = R.build_report(result, splits, R.FROZEN_TEST)
    assert report.accuracy is None


def test_disagreements_are_listed_individually(frozen):
    a = frozen.accuracy
    assert len(a.disagreements) == a.total - a.correct
    for record_id, expected, actual in a.disagreements:
        assert record_id.startswith("purchase_register:")
        assert expected != actual


# --- rendering --------------------------------------------------------------

def test_render_includes_every_required_section(frozen):
    text = R.render(frozen)
    for heading in ("OVERALL MATCH RATE", "CLASSIFICATION-RULE EXCEPTIONS",
                    "OPERATIONAL-CHECK FLAGS", "INDETERMINATE",
                    "QUARANTINED RECORDS", "CONFIDENCE", "AUDIT LOG"):
        assert heading in text


def test_render_states_the_provenance(frozen):
    text = R.render(frozen)
    assert str(frozen.dataset_seed) in text
    assert frozen.rules_version in text
    assert "frozen_test" in text


def test_render_says_quarantine_is_not_folded_in(frozen):
    assert "NOT folded" in R.render(frozen)


def test_to_dict_round_trips_the_key_numbers(frozen):
    payload = R.to_dict(frozen)
    assert payload["match_rate"]["exact"] == frozen.match_rate.exact
    assert payload["quarantined"]["count"] == frozen.quarantined
    assert payload["indeterminate"] == frozen.indeterminate
    assert payload["dataset_seed"] == frozen.dataset_seed


def test_report_is_deterministic(result, splits):
    a = R.render(R.build_report(result, splits, R.FROZEN_TEST))
    b = R.render(R.build_report(result, splits, R.FROZEN_TEST))
    assert a == b
