"""§3.3 quarantine log: invalid rows are captured whole and stop there."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import validation as V
from src.normalization import normalize_deterministic
from src.quarantine_log import QuarantineLog
from src.source_records import (
    SOURCE_GSTR2B,
    SOURCE_PURCHASE_REGISTER,
    SourceRecord,
    load_source,
)

FIXED_TS = "2026-06-10T00:00:00+00:00"


@pytest.fixture
def log(tmp_path):
    with QuarantineLog(str(tmp_path / "test.sqlite"), now=FIXED_TS) as q:
        yield q


def bad_record(**overrides) -> SourceRecord:
    raw = {
        "record_id": "PR-0001",
        "invoice_id": "INV-2604-00001",
        "vendor_gstin": "27AAPFU0939F1ZV",
        "vendor_name": "Acme Industries Private Limited",
        "invoice_date": "2026-04-15",
        "taxable_value": "-500.00",
        "cgst": "0.00", "sgst": "0.00", "igst": "90.00",
    }
    raw.update(overrides)
    return SourceRecord(SOURCE_PURCHASE_REGISTER, raw["record_id"], 7, raw)


# --- what a quarantine row must carry --------------------------------------

def test_entry_carries_source_row_record_id_error_type_and_message(log):
    record = bad_record()
    entry = log.quarantine(V.validate_record(record))

    assert entry.record_id == "purchase_register:PR-0001"     # independent id
    assert entry.source == SOURCE_PURCHASE_REGISTER
    assert entry.source_record_id == "PR-0001"
    assert entry.source_row_number == 7                        # findable in the CSV
    assert entry.validation_error == V.NON_NUMERIC_OR_NEGATIVE_AMOUNT
    assert "negative" in entry.validation_message
    assert entry.error_field == "taxable_value"
    assert entry.timestamp == FIXED_TS


def test_raw_snapshot_is_the_complete_untouched_source_row(log):
    record = bad_record()
    log.quarantine(V.validate_record(record))
    snapshot = log.snapshot("purchase_register:PR-0001")
    assert snapshot == record.raw
    assert snapshot["taxable_value"] == "-500.00"   # the bad value is preserved


def test_snapshot_is_valid_json(log):
    log.quarantine(V.validate_record(bad_record()))
    entry = log.entries()[0]
    assert json.loads(entry.raw_record_snapshot) == bad_record().raw


# --- the table is separate from the audit log ------------------------------

def test_quarantine_is_its_own_table(log):
    assert "quarantine_log" in log.tables()
    assert "audit_log" not in log.tables()


# --- a valid record can never be quarantined -------------------------------

def test_quarantining_a_valid_record_is_refused(log):
    good = bad_record(taxable_value="500.00")
    result = V.validate_record(good)
    assert result.is_valid
    with pytest.raises(ValueError, match="passed validation"):
        log.quarantine(result)


# --- quarantined records do not continue -----------------------------------

def test_quarantined_records_never_reach_normalisation(log):
    """The §2.1 boundary: only the valid partition is normalised."""
    records = load_source(SOURCE_PURCHASE_REGISTER)
    valid, invalid = V.partition(records)
    log.quarantine_all(invalid)

    normalised_ids = {normalize_deterministic(r.record).source_id for r in valid}
    quarantined_ids = {e.record_id for e in log.entries()}

    assert quarantined_ids                       # there are some
    assert not (normalised_ids & quarantined_ids)
    assert len(normalised_ids) + len(quarantined_ids) == len(records)


# --- batch behaviour over the real Phase 1 data ----------------------------

def test_full_batch_counts_and_error_breakdown(log):
    for source in (SOURCE_PURCHASE_REGISTER, SOURCE_GSTR2B):
        _, invalid = V.partition(load_source(source))
        log.quarantine_all(invalid)

    assert log.count() == 20
    assert log.counts_by_error() == {
        V.MISSING_REQUIRED_FIELD: 6,
        V.INVALID_GSTIN_FORMAT: 6,
        V.UNPARSEABLE_OR_IMPLAUSIBLE_DATE: 4,
        V.NON_NUMERIC_OR_NEGATIVE_AMOUNT: 4,
    }
    assert all(e.source == SOURCE_PURCHASE_REGISTER for e in log.entries())


def test_every_error_type_carries_a_distinct_message(log):
    for source in (SOURCE_PURCHASE_REGISTER, SOURCE_GSTR2B):
        _, invalid = V.partition(load_source(source))
        log.quarantine_all(invalid)
    for entry in log.entries():
        assert entry.validation_error in V.ERROR_TYPES
        assert entry.validation_message.strip()
        assert entry.validation_message != entry.validation_error


def test_reruns_do_not_duplicate_rows(log):
    _, invalid = V.partition(load_source(SOURCE_PURCHASE_REGISTER))
    log.quarantine_all(invalid)
    log.quarantine_all(invalid)
    assert log.count() == 20


def test_clear_empties_the_table(log):
    _, invalid = V.partition(load_source(SOURCE_PURCHASE_REGISTER))
    log.quarantine_all(invalid)
    log.clear()
    assert log.count() == 0
