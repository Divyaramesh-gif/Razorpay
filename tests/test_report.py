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


# --- 9 & 10. benchmark and limitations -------------------------------------

def test_report_carries_throughput(frozen):
    t = frozen.throughput
    assert t["valid_records"] == 480
    assert t["elapsed_seconds"] > 0
    assert t["records_per_second"] > 0
    assert set(t["stage_seconds"]) >= {"match", "rules", "audit_log"}


def test_render_includes_the_benchmark_table(frozen):
    text = R.render(frozen)
    assert "9. BENCHMARK" in text
    assert "records/second" in text
    assert "match" in text


def test_render_includes_limitations(frozen):
    text = R.render(frozen)
    assert "10. LIMITATIONS" in text
    for phrase in ("SYNTHETIC DATA", "NOT a", "SINGLE BATCH",
                   "NOT A FILING TOOL", "THROUGHPUT IS INDICATIVE"):
        assert phrase in text, phrase


def test_limitations_warn_against_quoting_the_numbers(frozen):
    text = R.render(frozen)
    assert "read before quoting any number" in text.lower()
    assert "production forecast" in text


def test_report_states_ai_status(frozen):
    """A reader must be able to tell whether the batch was AI-normalised."""
    text = R.render(frozen)
    assert "AI-assisted normalisation" in text
    assert "not requested" in text


def test_report_warns_when_the_ai_half_fell_back_entirely(result, splits, tmp_path):
    from src import pipeline as Pp

    class Boom:
        messages = property(lambda self: self)

        def create(self, **kwargs):
            raise RuntimeError("no credentials")

    failed = Pp.run(db_path=str(tmp_path / "f.sqlite"), now=FIXED_NOW,
                    ai_client=Boom())
    text = R.render(R.build_report(failed, splits, R.FROZEN_TEST))
    assert "EVERY AI CALL FAILED" in text
    assert "NOT AI-normalised" in text


def test_to_dict_carries_benchmark_and_limitations(frozen):
    payload = R.to_dict(frozen)
    assert payload["benchmark"]["valid_records"] == 480
    assert payload["limitations"]
    assert payload["ai"]["requested"] is False


# --- 0. executive summary / positioning ------------------------------------
# The claim and its limits must stay on the same page. These guard against a
# future edit keeping the marketing line and dropping the qualifier.

def test_executive_summary_carries_the_positioning(frozen):
    text = R.render(frozen)
    assert "0. EXECUTIVE SUMMARY" in text
    assert "GST-specific finance controller" in text
    assert "estimated ITC exposure" in text
    assert "safely escalates uncertainty" in text


def test_executive_summary_carries_the_supporting_line(frozen):
    assert ("AI-assisted normalisation. Deterministic decisions. "
            "Auditable human review.") in R.render(frozen)


def test_the_claim_never_appears_without_its_scope_qualifier(frozen):
    """Synthetic data, no GSTN, not tax advice — stated wherever the claim is."""
    text = R.render(frozen)
    assert "synthetic GSTR-2B-style data" in text
    assert "No live GSTN connectivity" in text
    assert "Not tax advice" in text


def test_the_report_claims_no_live_gstn_access(frozen):
    text = R.render(frozen).lower()
    for forbidden in ("live gstn", "connects to the gstn", "gstn api",
                      "files your return", "tax advice from"):
        if forbidden == "live gstn":
            assert "no live gstn" in text        # only ever in the negative
        else:
            assert forbidden not in text


def test_positioning_is_stated_once_not_repeated(frozen):
    """'Do not repeat the tagline throughout' — one statement, one place."""
    text = R.render(frozen)
    assert text.count("GST-specific finance controller") == 1
    assert text.count("Auditable human review") == 1


def test_itc_exposure_is_quantified_not_just_claimed(frozen):
    """The positioning says the tool quantifies ITC exposure, so the report
    must actually carry a number."""
    assert frozen.itc_exposure_total > 0
    assert 0 < frozen.itc_exposure_breaching <= frozen.itc_exposure_total
    assert f"{frozen.itc_exposure_total:,.2f}" in R.render(frozen)


def test_itc_exposure_sums_the_rule_engine_variance(result, splits):
    report = R.build_report(result, splits, R.FROZEN_TEST)
    assert report.itc_exposure_total == pytest.approx(
        round(sum(result.batch.itc_variance_by_gstin.values()), 2))


def test_to_dict_carries_positioning_and_exposure(frozen):
    payload = R.to_dict(frozen)
    assert payload["positioning"]["statement"] == R.POSITIONING
    assert payload["positioning"]["supporting_line"] == R.SUPPORTING_LINE
    assert "synthetic GSTR-2B-style data" in payload["positioning"]["scope"]
    assert payload["itc_exposure"]["total"] == frozen.itc_exposure_total
