"""§2.4 evidence: a plain diff. No interpretation, no verdict."""

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import evidence as E
from src.matcher import Match
from src.normalization import normalize_deterministic
from src.source_records import SOURCE_GSTR2B, SOURCE_PURCHASE_REGISTER, SourceRecord

BASE = {
    "record_id": "PR-0001",
    "invoice_id": "INV-1042",
    "vendor_gstin": "27AAPFU0939F1ZV",
    "vendor_name": "Acme Industries Private Limited",
    "invoice_date": "2026-03-14",
    "taxable_value": "11800.00",
    "cgst": "1062.00", "sgst": "1062.00", "igst": "0.00",
    "total_tax": "2124.00", "invoice_value": "13924.00",
}


def rec(source, record_id, **o):
    raw = dict(BASE); raw["record_id"] = record_id; raw.update(o)
    return normalize_deterministic(SourceRecord(source, record_id, 1, raw))


def paired(**b2_overrides):
    return Match(
        pr_record=rec(SOURCE_PURCHASE_REGISTER, "PR-0001"),
        b2_record=rec(SOURCE_GSTR2B, "2B-0001", **b2_overrides),
        score=100.0,
    )


def singleton():
    return Match(pr_record=rec(SOURCE_PURCHASE_REGISTER, "PR-0001"), b2_record=None)


# --- the §2.4 shape ---------------------------------------------------------

def test_object_matches_the_architecture_shape():
    ev = E.compare(paired())
    assert ev.invoice_id == "INV-1042"
    assert ev.candidate_found is True
    assert {"amount", "gstin", "date", "invoice_number"} <= set(ev.fields)


def test_amount_entry_carries_pr_2b_delta_and_match():
    ev = E.compare(paired(taxable_value="10800.00"))
    assert ev.fields["amount"] == {
        "pr_value": 11800.00, "2b_value": 10800.00,
        "delta": 1000.00, "match": False,
    }


def test_gstin_entry_carries_both_values_and_match():
    ev = E.compare(paired(vendor_gstin="07AAPFU0939F1ZH"))
    entry = ev.fields["gstin"]
    assert entry["pr_value"] == "27AAPFU0939F1ZV"
    assert entry["2b_value"] == "07AAPFU0939F1ZH"
    assert entry["match"] is False


def test_matching_date_is_recorded_as_a_match():
    ev = E.compare(paired())
    assert ev.fields["date"] == {"pr_value": "2026-03-14", "2b_value": "2026-03-14",
                                 "delta": 0, "match": True}


def test_identical_records_match_on_every_field():
    ev = E.compare(paired())
    assert ev.mismatched_fields() == []
    assert set(ev.matched_fields()) == set(ev.fields)


# --- amounts ---------------------------------------------------------------

@pytest.mark.parametrize("b2_amount,expected_match", [
    ("11800.00", True),
    ("11800.99", True),      # within Rs.1
    ("11801.01", False),
    ("10800.00", False),
])
def test_amount_match_uses_the_one_rupee_tolerance(b2_amount, expected_match):
    ev = E.compare(paired(taxable_value=b2_amount))
    assert ev.is_match("amount") is expected_match


def test_amount_delta_is_signed_pr_minus_2b():
    assert E.compare(paired(taxable_value="10800.00")).delta("amount") == 1000.00
    assert E.compare(paired(taxable_value="12800.00")).delta("amount") == -1000.00


def test_date_delta_is_in_days():
    assert E.compare(paired(invoice_date="2026-03-12")).delta("date") == 2
    assert E.compare(paired(invoice_date="2026-03-16")).delta("date") == -2


# --- tax heads (the §2.5 header-mismatch signal) ---------------------------

def test_tax_head_profiles_are_recorded():
    ev = E.compare(paired(cgst="0.00", sgst="0.00", igst="2124.00"))
    entry = ev.fields["tax_heads"]
    assert entry["pr_value"] == "cgst_sgst"
    assert entry["2b_value"] == "igst"
    assert entry["match"] is False
    assert entry["pr_total_tax"] == entry["2b_total_tax"] == 2124.00


def test_same_heads_match():
    assert E.compare(paired()).is_match("tax_heads") is True


# --- vendor name ------------------------------------------------------------

def test_vendor_name_compares_case_insensitively():
    ev = E.compare(paired(vendor_name="ACME INDUSTRIES PRIVATE LIMITED"))
    assert ev.is_match("vendor_name") is True
    assert ev.b2_value("vendor_name") == "ACME INDUSTRIES PRIVATE LIMITED"


def test_different_vendor_name_is_a_mismatch():
    assert E.compare(paired(vendor_name="Zenith Polymers LLP")).is_match(
        "vendor_name") is False


# --- no_candidate_found singleton ------------------------------------------

def test_singleton_is_supported():
    ev = E.compare(singleton())
    assert ev.candidate_found is False
    assert ev.b2_record_id is None
    assert ev.invoice_id == "INV-1042"


def test_singleton_records_the_absence_field_by_field():
    """The absence must be visible per field, not only via the flag."""
    ev = E.compare(singleton())
    for name, entry in ev.fields.items():
        assert entry["2b_value"] is None, name
        assert entry["match"] is False, name
    assert ev.matched_fields() == []


def test_singleton_still_carries_the_register_values():
    ev = E.compare(singleton())
    assert ev.pr_value("amount") == 11800.00
    assert ev.pr_value("gstin") == "27AAPFU0939F1ZV"


# --- no interpretation, no verdict -----------------------------------------

FORBIDDEN = {"confidence", "score", "category", "classification", "rule_id",
             "outcome", "action", "verdict", "exception", "reconciled",
             "is_exception", "severity"}


def test_evidence_exposes_no_verdict_field():
    names = {f.name for f in dataclasses.fields(E.Evidence)}
    assert not (names & FORBIDDEN), names & FORBIDDEN


def test_field_entries_carry_only_observations():
    ev = E.compare(paired(taxable_value="10800.00"))
    for name, entry in ev.fields.items():
        assert not (set(entry) & FORBIDDEN), (name, entry)


def test_evidence_does_not_import_the_rule_engine():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "evidence.py")).read()
    for module in ("rule_engine", "confidence", "gate"):
        assert f"from .{module}" not in src and f"import {module}" not in src


def test_evidence_is_immutable():
    ev = E.compare(paired())
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.invoice_id = "TAMPERED"


def test_consumers_cannot_mutate_the_logged_evidence():
    """§2.4: the rule engine consumes the object but does not modify it."""
    ev = E.compare(paired())
    borrowed = ev.field_map()
    borrowed["amount"]["match"] = False
    borrowed["amount"]["injected"] = True
    assert ev.is_match("amount") is True
    assert "injected" not in ev.fields["amount"]


# --- over the real batch ----------------------------------------------------

def test_every_match_produces_evidence_with_the_full_field_set():
    from src import matcher as M, validation as V
    from src.source_records import load_source

    pr_valid, _ = V.partition(load_source(SOURCE_PURCHASE_REGISTER))
    b2_valid, _ = V.partition(load_source(SOURCE_GSTR2B))
    result = M.match_records(
        [normalize_deterministic(r.record) for r in pr_valid],
        [normalize_deterministic(r.record) for r in b2_valid],
    )
    evidences = E.compare_all(result.matches)
    assert len(evidences) == 480
    expected = {"invoice_number", "gstin", "amount", "date", "vendor_name",
                "tax_heads"}
    assert all(set(ev.fields) == expected for ev in evidences)
    assert sum(1 for ev in evidences if not ev.candidate_found) == \
        len(result.no_candidate)
