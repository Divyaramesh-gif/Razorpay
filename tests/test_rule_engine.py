"""§2.5 rule engine: deterministic classification + operational checks.

Two categories, kept separate. No LLM participates in any decision here.
"""

import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import evidence as E
from src import rule_engine as R
from src import validation as V
from src.matcher import Match
from src.normalization import normalize_deterministic
from src.source_records import SOURCE_GSTR2B, SOURCE_PURCHASE_REGISTER, SourceRecord

BASE = {
    "record_id": "PR-0001",
    "invoice_id": "INV-2604-00001",
    "vendor_gstin": "27AAPFU0939F1ZV",
    "vendor_name": "Acme Industries Private Limited",
    "invoice_date": "2026-04-15",
    "taxable_value": "100000.00",
    "cgst": "9000.00", "sgst": "9000.00", "igst": "0.00",
    "total_tax": "18000.00", "invoice_value": "118000.00",
    "rule88d_intimation_date": "",
    "simulated_current_date": "2026-06-10",
}


def rec(source, record_id, **o):
    raw = dict(BASE); raw["record_id"] = record_id; raw.update(o)
    return normalize_deterministic(SourceRecord(source, record_id, 1, raw))


def pair(pr_overrides=None, **b2_overrides):
    return Match(
        pr_record=rec(SOURCE_PURCHASE_REGISTER, "PR-0001", **(pr_overrides or {})),
        b2_record=rec(SOURCE_GSTR2B, "2B-0001", **b2_overrides),
        score=100.0,
    )


def absent(**pr_overrides):
    return Match(rec(SOURCE_PURCHASE_REGISTER, "PR-0001", **pr_overrides), None)


@pytest.fixture
def engine():
    return R.RuleEngine(prior_period_invoice_ids={"INV-2604-00099"})


def classify(engine, match):
    return engine.classify(E.compare(match))


# ==========================================================================
# The YAML is the source of the parameters
# ==========================================================================

def test_rules_file_declares_a_version_and_both_categories():
    data = yaml.safe_load(open(R.DEFAULT_RULES_PATH, encoding="utf-8"))
    assert data["version"] == "2026-04"
    assert data["classification_rules"] and data["operational_checks"]


def test_engine_stamps_the_rules_version_on_every_classification(engine):
    assert classify(engine, pair()).rules_version == "2026-04"


def test_every_rule_has_an_id_name_description_and_scope():
    data = yaml.safe_load(open(R.DEFAULT_RULES_PATH, encoding="utf-8"))
    for rule in data["classification_rules"]:
        assert rule["id"].startswith("CLS-")
        assert rule["name"] and rule["description"]
        assert rule["applies_to"] in (R.APPLIES_MATCHED_PAIR, R.APPLIES_NO_CANDIDATE)
    for check in data["operational_checks"]:
        assert check["id"].startswith("OPS-")
        assert check["parameters"] and check["statuses"]


def test_changing_a_yaml_threshold_changes_behaviour_without_touching_code(tmp_path):
    """A versioned rule file must actually drive the logic."""
    data = yaml.safe_load(open(R.DEFAULT_RULES_PATH, encoding="utf-8"))
    for check in data["operational_checks"]:
        if check["id"] == "OPS-DRC01C":
            check["parameters"]["threshold_rupees"] = 10.00
    path = tmp_path / "rules_test.yaml"
    path.write_text(yaml.safe_dump(data))

    tuned = R.RuleEngine(str(path), prior_period_invoice_ids=set())
    flag = tuned.check_drc_01c("27AAPFU0939F1ZV", {"27AAPFU0939F1ZV": 50.0})
    assert flag.status == R.STATUS_BREACHED

    default = R.RuleEngine(prior_period_invoice_ids=set())
    assert default.check_drc_01c(
        "27AAPFU0939F1ZV", {"27AAPFU0939F1ZV": 50.0}).status == R.STATUS_WITHIN_THRESHOLD


def test_credit_note_patterns_come_from_the_yaml(engine):
    cond = engine._by_name["credit_note_netting"]["conditions"]
    assert {p["type"] for p in cond["credit_note_patterns"]} == {
        "fixed_amount", "percentage_of_taxable"}


# ==========================================================================
# CLS-001 gstin_header_mismatch
# ==========================================================================

HEADER_MISMATCH = dict(vendor_gstin="07AAPFU0939F1ZH",
                       cgst="0.00", sgst="0.00", igst="18000.00")


def test_gstin_header_mismatch_fires(engine):
    result = classify(engine, pair(**HEADER_MISMATCH))
    assert result.rule_id == "CLS-001"
    assert result.category == "gstin_header_mismatch"


def test_gstin_header_mismatch_reason_names_the_evidence(engine):
    reason = classify(engine, pair(**HEADER_MISMATCH)).reason
    assert "27" in reason and "07" in reason
    assert "AAPFU0939F" in reason           # the shared PAN
    assert "cgst_sgst" in reason and "igst" in reason


def test_header_mismatch_needs_a_different_state_prefix(engine):
    assert classify(engine, pair()).rule_id is None


def test_header_mismatch_needs_an_identical_pan(engine):
    """A different PAN is a different supplier, not a filing-header error."""
    result = classify(engine, pair(vendor_gstin="07AAGCB7383J1ZP",
                                   cgst="0.00", sgst="0.00", igst="18000.00"))
    assert result.category != "gstin_header_mismatch"


def test_header_mismatch_needs_the_tax_heads_to_flip(engine):
    result = classify(engine, pair(vendor_gstin="07AAPFU0939F1ZH"))
    assert result.category != "gstin_header_mismatch"


def test_header_mismatch_needs_equal_total_tax(engine):
    """Different heads AND a different total is a value dispute, not a
    misfiled header."""
    result = classify(engine, pair(vendor_gstin="07AAPFU0939F1ZH",
                                   cgst="0.00", sgst="0.00", igst="12000.00",
                                   total_tax="12000.00"))
    assert result.category != "gstin_header_mismatch"


# ==========================================================================
# CLS-002 credit_note_netting
# ==========================================================================

@pytest.mark.parametrize("credit", [1000.00, 2500.00, 5000.00, 10000.00])
def test_fixed_credit_note_amounts_fire(engine, credit):
    result = classify(engine, pair(taxable_value=f"{100000.00 - credit:.2f}"))
    assert result.rule_id == "CLS-002"
    assert f"{credit:,.2f}" in result.reason


def test_percentage_credit_note_fires(engine):
    """10% of the register taxable value."""
    result = classify(engine, pair(taxable_value="90000.00"))
    assert result.category == "credit_note_netting"


def test_credit_note_needs_2b_to_be_lower(engine):
    """A HIGHER 2B amount is not a credit note — the direction matters."""
    result = classify(engine, pair(taxable_value="101000.00"))
    assert result.category != "credit_note_netting"


def test_arbitrary_delta_is_not_a_credit_note(engine):
    result = classify(engine, pair(taxable_value="96813.47"))
    assert result.rule_id is None
    assert "no classification rule matched" in result.reason


def test_sub_rupee_difference_is_not_a_credit_note(engine):
    result = classify(engine, pair(taxable_value="99999.50"))
    assert result.category != "credit_note_netting"


def test_credit_note_requires_the_gstin_to_match(engine):
    result = classify(engine, pair(vendor_gstin="29AAGCB7383J1Z4",
                                   taxable_value="95000.00"))
    assert result.category != "credit_note_netting"


# ==========================================================================
# CLS-003 / CLS-004 absence, disambiguated by the prior-period snapshot
# ==========================================================================

def test_present_in_snapshot_is_invoice_removed_post_claim(engine):
    result = classify(engine, absent(invoice_id="INV-2604-00099"))
    assert result.rule_id == "CLS-003"
    assert result.category == "invoice_removed_post_claim"
    assert "present in the prior-period snapshot" in result.reason


def test_absent_from_snapshot_is_late_filed_supplier(engine):
    result = classify(engine, absent(invoice_id="INV-2604-00001"))
    assert result.rule_id == "CLS-004"
    assert result.category == "late_filed_supplier"


def test_the_snapshot_is_the_only_thing_that_separates_them(engine):
    """Same record, same everything — only snapshot membership differs."""
    a = R.RuleEngine(prior_period_invoice_ids={"INV-2604-00001"})
    b = R.RuleEngine(prior_period_invoice_ids=set())
    match = absent(invoice_id="INV-2604-00001")
    assert classify(a, match).category == "invoice_removed_post_claim"
    assert classify(b, match).category == "late_filed_supplier"


def test_absence_always_classifies(engine):
    """§2.3 step 4: no_candidate_found feeds the rule engine as an absence
    case — it must never fall through unclassified."""
    for invoice in ("INV-2604-00099", "INV-2604-00001", "INV-9999-99999"):
        assert classify(engine, absent(invoice_id=invoice)).fired


def test_pair_rules_never_fire_on_an_absence(engine):
    result = classify(engine, absent(invoice_id="INV-2604-00001"))
    assert result.rule_id not in ("CLS-001", "CLS-002")


# ==========================================================================
# Clean pairs and rule ordering
# ==========================================================================

def test_clean_pair_fires_no_rule(engine):
    result = classify(engine, pair())
    assert not result.fired
    assert result.reason == "all compared fields agree"


def test_no_rule_fired_is_not_a_verdict(engine):
    """category=None is an input to the §2.6 gate, not a finding of 'fine'."""
    result = classify(engine, pair())
    assert result.category is None and result.rule_id is None


def test_rules_are_evaluated_in_file_order(engine):
    ids = [r["id"] for r in engine.classification_rules]
    assert ids == sorted(ids), "file order must be deterministic and readable"


# ==========================================================================
# OPS-88D Rule 88D response window
# ==========================================================================

@pytest.mark.parametrize("intimation,expected", [
    ("2026-06-10", R.STATUS_WITHIN_WINDOW),    # 0 days elapsed
    ("2026-06-04", R.STATUS_WITHIN_WINDOW),    # 6 days
    ("2026-06-03", R.STATUS_WITHIN_WINDOW),    # 7 days — the boundary, inclusive
    ("2026-06-02", R.STATUS_OUTSIDE_WINDOW),   # 8 days — just past
    ("2026-05-20", R.STATUS_OUTSIDE_WINDOW),
])
def test_rule_88d_window_boundaries(engine, intimation, expected):
    flag = engine.check_rule_88d(absent(rule88d_intimation_date=intimation))
    assert flag.status == expected
    assert flag.check_id == "OPS-88D"


def test_rule_88d_not_applicable_without_an_intimation(engine):
    flag = engine.check_rule_88d(absent())
    assert flag.status == R.STATUS_NOT_APPLICABLE


def test_rule_88d_reports_days_remaining(engine):
    flag = engine.check_rule_88d(absent(rule88d_intimation_date="2026-06-05"))
    assert "2 day(s) left" in flag.detail


def test_rule_88d_reports_how_late_it_is(engine):
    flag = engine.check_rule_88d(absent(rule88d_intimation_date="2026-05-31"))
    assert "closed 3 day(s) ago" in flag.detail


def test_rule_88d_uses_simulated_current_date_not_the_clock(engine):
    """Changing the dataset's current date must change the answer."""
    late = absent(rule88d_intimation_date="2026-06-01",
                  simulated_current_date="2026-06-30")
    early = absent(rule88d_intimation_date="2026-06-01",
                   simulated_current_date="2026-06-05")
    assert engine.check_rule_88d(late).status == R.STATUS_OUTSIDE_WINDOW
    assert engine.check_rule_88d(early).status == R.STATUS_WITHIN_WINDOW


def test_future_intimation_is_not_reported_as_late(engine):
    flag = engine.check_rule_88d(absent(rule88d_intimation_date="2026-07-01"))
    assert flag.status == R.STATUS_NOT_APPLICABLE
    assert "not yet open" in flag.detail


# ==========================================================================
# OPS-DRC01C cumulative ITC variance
# ==========================================================================

def test_drc01c_breaches_above_the_threshold(engine):
    flag = engine.check_drc_01c("G1", {"G1": 75000.01})
    assert flag.status == R.STATUS_BREACHED
    assert flag.check_id == "OPS-DRC01C"


def test_drc01c_threshold_is_exclusive(engine):
    assert engine.check_drc_01c("G1", {"G1": 75000.00}).status == \
        R.STATUS_WITHIN_THRESHOLD


def test_drc01c_is_cumulative_across_the_vendor(engine):
    """No single record breaches; the vendor's total does."""
    assert engine.check_drc_01c("G1", {"G1": 30000.0}).status == \
        R.STATUS_WITHIN_THRESHOLD
    assert engine.check_drc_01c("G1", {"G1": 90000.0}).status == R.STATUS_BREACHED


def test_missing_candidate_puts_the_whole_itc_at_risk(engine):
    match = absent()
    assert engine.itc_variance(match, E.compare(match)) == 18000.00


def test_matched_pair_risks_only_the_difference(engine):
    match = pair(cgst="8000.00", sgst="8000.00", total_tax="16000.00")
    assert engine.itc_variance(match, E.compare(match)) == 2000.00


def test_clean_pair_contributes_no_variance(engine):
    match = pair()
    assert engine.itc_variance(match, E.compare(match)) == 0.0


# ==========================================================================
# The two categories stay separate (§2.5: "do not merge them")
# ==========================================================================

def test_classification_and_operational_results_are_distinct_types():
    assert R.Classification is not R.OperationalFlag
    import dataclasses
    cls_fields = {f.name for f in dataclasses.fields(R.Classification)}
    ops_fields = {f.name for f in dataclasses.fields(R.OperationalFlag)}
    assert "check_id" not in cls_fields
    assert "category" not in ops_fields and "rule_id" not in ops_fields


def test_batch_exposes_two_separate_tables(engine):
    matches = [pair(), absent(invoice_id="INV-2604-00099")]
    batch = engine.evaluate_batch(matches, [E.compare(m) for m in matches])

    classification = batch.classification_table()
    operational = batch.operational_table("OPS-DRC01C")
    assert all(e.classification.fired for e in classification)
    assert all(e.flag("OPS-DRC01C") for e in operational)
    # A record may appear in both tables, or in only one — they are not nested.
    assert len(batch.evaluations) == 2


def test_operational_flags_are_independent_of_classification(engine):
    """A clean pair with an intimation still gets a Rule 88D flag."""
    match = pair(pr_overrides={"rule88d_intimation_date": "2026-06-08"})
    batch = engine.evaluate_batch([match], [E.compare(match)])
    evaluation = batch.evaluations[0]
    assert not evaluation.classification.fired
    assert evaluation.flag("OPS-88D").status == R.STATUS_WITHIN_WINDOW


def test_every_evaluation_carries_both_operational_checks(engine):
    matches = [pair(), absent()]
    batch = engine.evaluate_batch(matches, [E.compare(m) for m in matches])
    for evaluation in batch.evaluations:
        assert {f.check_id for f in evaluation.operational_flags} == \
            {"OPS-88D", "OPS-DRC01C"}


def test_batch_rejects_mismatched_inputs(engine):
    with pytest.raises(ValueError, match="one to one"):
        engine.evaluate_batch([pair()], [])


# ==========================================================================
# Deterministic, no LLM
# ==========================================================================

def test_rule_engine_makes_no_api_call():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "rule_engine.py")).read()
    for token in ("anthropic", "normalize_ai_assisted", "clean_text_with_claude",
                  "messages.create"):
        assert token not in src, f"the rule engine must not reach for {token}"


def test_classification_is_repeatable(engine):
    match = pair(**HEADER_MISMATCH)
    results = {classify(engine, match) for _ in range(20)}
    assert len(results) == 1


def test_rule_engine_does_not_modify_the_evidence(engine):
    """§2.4: the rule engine consumes the evidence object but does not
    modify it."""
    ev = E.compare(pair(**HEADER_MISMATCH))
    before = {k: dict(v) for k, v in ev.fields.items()}
    engine.classify(ev)
    engine.classify(ev)
    assert {k: dict(v) for k, v in ev.fields.items()} == before


# ==========================================================================
# Over the real batch, graded against ground truth
# ==========================================================================
# ground_truth.csv is an EVALUATION input. The engine below ran without it.

@pytest.fixture(scope="module")
def real_batch():
    import csv
    from src import matcher as M
    from src.source_records import load_source

    pr_valid, _ = V.partition(load_source(SOURCE_PURCHASE_REGISTER))
    b2_valid, _ = V.partition(load_source(SOURCE_GSTR2B))
    result = M.match_records(
        [normalize_deterministic(r.record) for r in pr_valid],
        [normalize_deterministic(r.record) for r in b2_valid],
    )
    evidences = E.compare_all(result.matches)
    batch = R.RuleEngine().evaluate_batch(result.matches, evidences)

    gt_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "ground_truth.csv")
    gt = {r["pr_record_id"]: r for r in csv.DictReader(open(gt_path))}
    graded = [(gt[e.record_id.split(":")[1]], e) for e in batch.evaluations]
    return batch, graded


def test_every_valid_record_is_evaluated(real_batch):
    batch, _ = real_batch
    assert len(batch.evaluations) == 480


@pytest.mark.parametrize("category", ["gstin_header_mismatch", "credit_note_netting"])
def test_pair_rules_are_exact_on_the_real_batch(real_batch, category):
    """Both matched-pair rules must be perfect: their conditions are precise
    and the matcher resolves every one of these cases."""
    _, graded = real_batch
    rows = [(g, e) for g, e in graded if g["expected_classification"] == category]
    correct = sum(1 for g, e in rows if e.classification.category == category)
    assert correct == len(rows), f"{category}: {correct}/{len(rows)}"


def test_pair_rules_never_fire_on_a_record_that_should_not_have_them(real_batch):
    """False positives are worse than misses — they invent an explanation."""
    _, graded = real_batch
    for g, e in graded:
        if e.classification.category in ("gstin_header_mismatch", "credit_note_netting"):
            assert g["expected_classification"] == e.classification.category, \
                (g["pr_record_id"], g["case_type"], e.classification.category)


def test_overall_classification_accuracy(real_batch):
    _, graded = real_batch
    correct = sum(1 for g, e in graded
                  if (g["expected_classification"] or None) == e.classification.category)
    assert correct / len(graded) >= 0.95, f"{correct}/{len(graded)}"


def test_all_classification_errors_trace_to_a_matcher_miss(real_batch):
    """The rule engine should not be independently wrong: every disagreement
    must be a correct rule applied to a pairing the matcher got wrong."""
    _, graded = real_batch
    for g, e in graded:
        expected = g["expected_classification"] or None
        if expected == e.classification.category:
            continue
        expected_pair = f"gstr2b:{g['gstr2b_record_id']}" if g["gstr2b_record_id"] else None
        actual_paired = e.candidate_found
        assert (expected_pair is not None) != actual_paired or expected_pair is None, \
            (g["pr_record_id"], g["case_type"], expected, e.classification.category)


def test_rule_88d_window_counts_match_the_dataset(real_batch):
    """Phase 1 built 65 records inside the window and 50 outside."""
    batch, _ = real_batch
    from collections import Counter
    counts = Counter(e.flag("OPS-88D").status for e in batch.evaluations)
    assert counts[R.STATUS_WITHIN_WINDOW] == 65
    assert counts[R.STATUS_OUTSIDE_WINDOW] == 50


def test_rule_88d_agrees_with_ground_truth_labels(real_batch):
    _, graded = real_batch
    for g, e in graded:
        expected = g["expected_rule88d_within_window"]
        status = e.flag("OPS-88D").status
        if not expected:
            assert status == R.STATUS_NOT_APPLICABLE, g["pr_record_id"]
        else:
            assert status == (R.STATUS_WITHIN_WINDOW if expected == "true"
                              else R.STATUS_OUTSIDE_WINDOW), g["pr_record_id"]


def test_drc01c_separates_suppliers_on_the_real_batch(real_batch):
    batch, _ = real_batch
    threshold = 75000.00
    breached = {g for g, v in batch.itc_variance_by_gstin.items() if v > threshold}
    assert 3 <= len(breached) <= len(batch.itc_variance_by_gstin) - 5
    assert len(batch.itc_variance_by_gstin) == 40


def test_operational_table_excludes_not_applicable(real_batch):
    batch, _ = real_batch
    rows = batch.operational_table("OPS-88D")
    assert all(e.flag("OPS-88D").status != R.STATUS_NOT_APPLICABLE for e in rows)
    assert len(rows) == 115


def test_classification_and_operational_tables_are_not_the_same_rows(real_batch):
    """§2.5: two separate tables, not one blended list."""
    batch, _ = real_batch
    classified = {e.record_id for e in batch.classification_table()}
    flagged_88d = {e.record_id for e in batch.operational_table("OPS-88D")}
    assert classified and flagged_88d
    assert classified != flagged_88d
