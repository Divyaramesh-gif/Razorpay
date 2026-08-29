"""§2.1 input validation."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))

import generate_dataset as gen           # Part 1 reference implementation
from src import validation as V
from src.source_records import (
    SOURCE_GSTR2B,
    SOURCE_PURCHASE_REGISTER,
    SourceRecord,
    load_source,
)


def make(**overrides) -> SourceRecord:
    """A record that passes every §2.1 check, before overrides."""
    raw = {
        "record_id": "PR-9999",
        "invoice_id": "INV-2604-09999",
        "vendor_gstin": "27AAPFU0939F1ZV",
        "vendor_name": "Acme Industries Private Limited",
        "invoice_date": "2026-04-15",
        "taxable_value": "100000.00",
        "cgst": "9000.00",
        "sgst": "9000.00",
        "igst": "0.00",
        "total_tax": "18000.00",
        "invoice_value": "118000.00",
    }
    raw.update(overrides)
    return SourceRecord(SOURCE_PURCHASE_REGISTER, raw["record_id"], 1, raw)


# --- baseline ---------------------------------------------------------------

def test_clean_record_passes():
    assert V.validate_record(make()).is_valid


# --- check 1: required fields ----------------------------------------------

@pytest.mark.parametrize("missing", ["invoice_id", "vendor_gstin", "invoice_date",
                                     "taxable_value"])
def test_each_required_field_is_required(missing):
    result = V.validate_record(make(**{missing: ""}))
    assert not result.is_valid
    assert result.error.error_type == V.MISSING_REQUIRED_FIELD
    assert result.error.field == missing


def test_all_tax_heads_blank_is_a_missing_field():
    result = V.validate_record(make(cgst="", sgst="", igst=""))
    assert result.error.error_type == V.MISSING_REQUIRED_FIELD


def test_one_tax_head_is_enough():
    assert V.validate_record(make(cgst="", sgst="", igst="18000.00")).is_valid


def test_whitespace_only_counts_as_missing():
    assert V.validate_record(make(invoice_id="   ")).error.error_type == \
        V.MISSING_REQUIRED_FIELD


# --- check 2: GSTIN ---------------------------------------------------------

def test_reference_gstin_validates():
    """A real, publicly documented GSTIN must pass."""
    assert V.gstin_is_valid("27AAPFU0939F1ZV")


@pytest.mark.parametrize("gstin,reason", [
    ("27AAPFU0939F1Z", "14 characters"),
    ("27AAPFU0939F1ZVX", "16 characters"),
    ("27AAPFU0939F1ZX", "wrong check digit"),
    ("40AAPFU0939F1ZV", "state code 40 does not exist"),
    ("00AAPFU0939F1ZV", "state code 00 does not exist"),
    ("2712345U0939F1ZV", "PAN block is not PAN-shaped"),
    ("27aapfu0939f1zv", "lowercase"),
    ("27AAPFU0939F1YV", "14th char is not Z"),
])
def test_invalid_gstins_are_rejected(gstin, reason):
    result = V.validate_record(make(vendor_gstin=gstin))
    assert not result.is_valid, reason
    assert result.error.error_type == V.INVALID_GSTIN_FORMAT


def test_gstin_error_message_names_the_expected_check_digit():
    result = V.validate_record(make(vendor_gstin="27AAPFU0939F1ZX"))
    assert "check digit" in result.error.message
    assert "'V'" in result.error.message


# --- check 3: date ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("2026-04-15", "2026-04-15"),
    ("15/04/2026", "2026-04-15"),
    ("15-04-2026", "2026-04-15"),
    ("2026/04/15", "2026-04-15"),
])
def test_accepted_date_layouts_parse(raw, expected):
    assert V.validate_record(make(invoice_date=raw)).is_valid
    assert V.parse_date(raw).isoformat() == expected


@pytest.mark.parametrize("raw", [
    "2026-02-31",     # February has no 31st
    "2026-13-05",     # no month 13
    "31/04/2026",     # April has no 31st
    "not-a-date",
    "15 April 2026",  # layout not accepted
])
def test_unparseable_dates_are_rejected(raw):
    result = V.validate_record(make(invoice_date=raw))
    assert result.error.error_type == V.UNPARSEABLE_OR_IMPLAUSIBLE_DATE


@pytest.mark.parametrize("raw", ["1899-01-01", "2199-06-30", "2017-06-30"])
def test_implausible_dates_are_rejected(raw):
    result = V.validate_record(make(invoice_date=raw))
    assert result.error.error_type == V.UNPARSEABLE_OR_IMPLAUSIBLE_DATE


def test_gst_epoch_boundary_is_inclusive():
    assert V.validate_record(make(invoice_date="2017-07-01")).is_valid


# --- check 4: numerics ------------------------------------------------------

@pytest.mark.parametrize("field", ["taxable_value", "cgst", "total_tax",
                                   "invoice_value"])
def test_negative_amounts_are_rejected(field):
    result = V.validate_record(make(**{field: "-1.00"}))
    assert result.error.error_type == V.NON_NUMERIC_OR_NEGATIVE_AMOUNT
    assert result.error.field == field


@pytest.mark.parametrize("raw", ["N/A", "12,345.67abc", "abc", "1.2.3"])
def test_non_numeric_amounts_are_rejected(raw):
    result = V.validate_record(make(taxable_value=raw))
    assert result.error.error_type == V.NON_NUMERIC_OR_NEGATIVE_AMOUNT


def test_thousands_separators_are_accepted():
    assert V.validate_record(make(taxable_value="1,00,000.00")).is_valid


def test_zero_is_not_negative():
    assert V.validate_record(make(cgst="0.00", sgst="0.00", igst="18000.00")).is_valid


# --- check ordering ---------------------------------------------------------

def test_first_failure_wins_in_architecture_order():
    """A record broken in every way reports the FIRST §2.1 check that fails."""
    result = V.validate_record(
        make(invoice_id="", vendor_gstin="bad", invoice_date="nope",
             taxable_value="-1")
    )
    assert result.error.error_type == V.MISSING_REQUIRED_FIELD

    result = V.validate_record(
        make(vendor_gstin="bad", invoice_date="nope", taxable_value="-1")
    )
    assert result.error.error_type == V.INVALID_GSTIN_FORMAT

    result = V.validate_record(make(invoice_date="nope", taxable_value="-1"))
    assert result.error.error_type == V.UNPARSEABLE_OR_IMPLAUSIBLE_DATE


# --- agreement with the Part 1 reference implementation ---------------------

def test_agrees_with_part1_reference_on_every_source_record():
    """README (Part 1) promised src/validation.py would agree with the
    generator's reference validator on every record. Hold it to that."""
    disagreements = []
    for source in (SOURCE_PURCHASE_REGISTER, SOURCE_GSTR2B):
        for record in load_source(source):
            mine = V.validate_record(record)
            theirs = gen.reference_validate(record.raw)
            mine_error = mine.error.error_type if mine.error else None
            if mine_error != theirs:
                disagreements.append((record.source_id, mine_error, theirs))
    assert not disagreements, disagreements[:5]


def test_expected_counts_on_the_phase1_batch():
    pr_valid, pr_invalid = V.partition(load_source(SOURCE_PURCHASE_REGISTER))
    b2_valid, b2_invalid = V.partition(load_source(SOURCE_GSTR2B))
    assert (len(pr_valid), len(pr_invalid)) == (480, 20)
    assert (len(b2_valid), len(b2_invalid)) == (490, 0)
