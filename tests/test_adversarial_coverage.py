"""The frozen test set must still contain the hard cases.

A frozen split that quietly drifted towards clean records would inflate every
reported number without anything failing. The §2.6 split is stratified by case
type precisely to stop that, and these tests hold it to that promise: every
adversarial family the dataset injects must still be present in the frozen
30%, and the 2B-side matcher traps must still exist.

ground_truth.csv is read here as an EVALUATION input — this is a property of
the dataset, not of a pipeline run.
"""

import csv
import os
import sys
from collections import Counter

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from src.source_records import (
    SOURCE_GSTR2B,
    SOURCE_PRIOR_PERIOD,
    SOURCE_PURCHASE_REGISTER,
    load_source,
)

GROUND_TRUTH = os.path.join(REPO, "data", "ground_truth.csv")
FROZEN = "frozen_test"
CALIBRATION = "calibration"

# Every case type the generator injects that is adversarial in some way —
# i.e. anything a naive exact-key join would get wrong.
ADVERSARIAL_CASES = {
    "fuzzy_vendor_name_variant":  "abbreviation / casing variants",
    "fuzzy_ocr_artifact":         "OCR damage (O->0, I->1, S->5, stray punctuation)",
    "fuzzy_date_tolerance":       "invoice date off by 1-2 days",
    "fuzzy_amount_rounding":      "sub-rupee amount difference",
    "gstin_header_mismatch":      "CGST/SGST vs IGST filed under another state",
    "credit_note_netting":        "2B net of an unabsorbed credit note",
    "late_filed_supplier":        "absent from 2B and from the prior period",
    "invoice_removed_post_claim": "absent from 2B but present in the prior period",
    "indeterminate_ambiguous":    "garbled number, shifted date, odd delta",
    "quarantine_missing_field":   "required field blank",
    "quarantine_bad_gstin":       "bad length / checksum / state code / PAN",
    "quarantine_bad_date":        "impossible or implausible date",
    "quarantine_bad_amount":      "negative or non-numeric amount",
}


@pytest.fixture(scope="module")
def rows():
    with open(GROUND_TRUTH, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def frozen(rows):
    return [r for r in rows if r["split"] == FROZEN]


# --- every adversarial family survives into the frozen split --------------

@pytest.mark.parametrize("case_type", sorted(ADVERSARIAL_CASES))
def test_adversarial_case_is_present_in_the_frozen_split(frozen, case_type):
    n = sum(1 for r in frozen if r["case_type"] == case_type)
    assert n > 0, f"{case_type} ({ADVERSARIAL_CASES[case_type]}) absent from frozen set"


def test_every_case_type_appears_in_both_splits(rows):
    """The §2.6 stratification promise."""
    by_split = {s: Counter(r["case_type"] for r in rows if r["split"] == s)
                for s in (CALIBRATION, FROZEN)}
    missing = [c for c in ADVERSARIAL_CASES
               if not by_split[CALIBRATION][c] or not by_split[FROZEN][c]]
    assert not missing, missing


def test_frozen_split_is_not_mostly_clean(frozen):
    """If the hard cases drifted out, every reported number would inflate."""
    adversarial = sum(1 for r in frozen if r["case_type"] in ADVERSARIAL_CASES)
    assert adversarial / len(frozen) > 0.35, \
        f"only {adversarial}/{len(frozen)} frozen records are adversarial"


def test_all_six_defect_families_are_represented(frozen):
    families = {r["defect_family"] for r in frozen}
    assert families == {"clean", "fuzzy", "classification_exception",
                        "absence", "ambiguous", "invalid_input"}


def test_frozen_split_size_is_stable(frozen, rows):
    assert len(frozen) == 151
    assert len(rows) == 500


# --- the specific traps still bite ----------------------------------------

def test_quarantine_cases_span_all_four_validation_checks(frozen):
    """All four §2.1 failure modes must still be exercised after the split."""
    errors = {r["expected_validation_error"] for r in frozen
              if r["expected_outcome"] == "quarantine"}
    assert errors == {"missing_required_field", "invalid_gstin_format",
                      "unparseable_or_implausible_date",
                      "non_numeric_or_negative_amount"}


def test_absence_cases_cover_both_prior_period_directions(frozen):
    """CLS-003 and CLS-004 are separated only by the snapshot, so the frozen
    set needs both sides or the disambiguation is untested."""
    absent = [r for r in frozen if r["match_type"] == "none"]
    assert {r["in_prior_period_snapshot"] for r in absent} == {"true", "false"}


def test_rule_88d_window_has_both_sides_in_the_frozen_split(frozen):
    statuses = {r["expected_rule88d_within_window"] for r in frozen
                if r["expected_rule88d_within_window"]}
    assert statuses == {"true", "false"}


def test_drc01c_has_breaching_and_non_breaching_suppliers(frozen):
    assert {r["expected_drc01c_breach"] for r in frozen} == {"true", "false"}


def test_ocr_damage_is_genuinely_present_in_the_source_file(rows):
    """Not just labelled — the 2B file must actually contain damaged text."""
    frozen_ocr = {r["gstr2b_record_id"] for r in rows
                  if r["split"] == FROZEN and r["case_type"] == "fuzzy_ocr_artifact"}
    b2 = {r.record_id: r for r in load_source(SOURCE_GSTR2B)}
    names = [b2[rid].get("vendor_name") for rid in frozen_ocr if rid in b2]
    assert names
    assert any(any(ch.isdigit() for ch in name.split()[-1]) or
               "," in name or ";" in name or "  " in name for name in names), names


# --- the 2B-side matcher traps --------------------------------------------

def test_near_duplicate_decoys_still_exist(rows):
    """Unpaired 2B rows that share a supplier and date with a real pair — the
    trap the §2.3 one-to-one assignment must not fall for."""
    b2 = {r.record_id: r for r in load_source(SOURCE_GSTR2B)}
    claimed = {r["gstr2b_record_id"] for r in rows if r["gstr2b_record_id"]}
    paired_keys = {(b2[c].get("vendor_gstin"), b2[c].get("invoice_date"))
                   for c in claimed if c in b2}
    decoys = [rid for rid, row in b2.items()
              if rid not in claimed
              and (row.get("vendor_gstin"), row.get("invoice_date")) in paired_keys]
    assert len(decoys) >= 10, f"only {len(decoys)} decoys remain"


def test_unpaired_2b_rows_still_exist(rows):
    b2 = {r.record_id for r in load_source(SOURCE_GSTR2B)}
    claimed = {r["gstr2b_record_id"] for r in rows if r["gstr2b_record_id"]}
    assert len(b2 - claimed) == 25


def test_prior_period_snapshot_still_holds_the_removed_invoices(rows):
    snapshot = {r.get("invoice_id") for r in load_source(SOURCE_PRIOR_PERIOD)}
    removed = [r["invoice_id"] for r in rows
               if r["case_type"] == "invoice_removed_post_claim"]
    late = [r["invoice_id"] for r in rows
            if r["case_type"] == "late_filed_supplier"]
    assert removed and all(i in snapshot for i in removed)
    assert late and not any(i in snapshot for i in late)


def test_source_files_are_the_expected_size(rows):
    assert len(load_source(SOURCE_PURCHASE_REGISTER)) == 500
    assert len(load_source(SOURCE_GSTR2B)) == 490
    assert len(load_source(SOURCE_PRIOR_PERIOD)) == 75


# --- the full case-type inventory ------------------------------------------

ALL_CASE_TYPES = set(ADVERSARIAL_CASES) | {"clean_exact_match"}


def test_all_14_case_types_are_present_in_the_frozen_split(frozen):
    """The dataset injects 14 case types: 13 adversarial plus the clean
    baseline. Every one must survive the split, or some behaviour is untested
    on the numbers actually reported."""
    present = {r["case_type"] for r in frozen}
    assert len(ALL_CASE_TYPES) == 14
    assert present == ALL_CASE_TYPES, ALL_CASE_TYPES - present


def test_the_dataset_defines_exactly_those_14_case_types(rows):
    assert {r["case_type"] for r in rows} == ALL_CASE_TYPES


def test_thirteen_of_the_fourteen_are_adversarial():
    """clean_exact_match is the baseline, not a trap — stated explicitly so
    the 13/14 distinction is never mistaken for a missing case."""
    assert len(ADVERSARIAL_CASES) == 13
    assert "clean_exact_match" not in ADVERSARIAL_CASES
