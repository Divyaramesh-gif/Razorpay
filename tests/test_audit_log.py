"""§2.7 audit log: one row per record that PASSED validation."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import evidence as E
from src import gate as G
from src import validation as V
from src.audit_log import AuditLog, QuarantinedRecordError
from src.audit_log import TABLE as AUDIT_TABLE
from src.matcher import Match
from src.normalization import normalize_deterministic
from src.quarantine_log import QuarantineLog
from src.rule_engine import Classification, RuleEvaluation
from src.source_records import SOURCE_GSTR2B, SOURCE_PURCHASE_REGISTER, SourceRecord

FIXED_TS = "2026-06-10T00:00:00+00:00"
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


def evidence(rid="PR-0001", **b2):
    return E.compare(Match(rec(SOURCE_PURCHASE_REGISTER, rid),
                           rec(SOURCE_GSTR2B, "2B-0001", **b2), 100.0))


def evaluation(rule_id=None, category=None, rid="PR-0001"):
    return RuleEvaluation(
        record_id=f"purchase_register:{rid}", invoice_id="INV-2604-00001",
        candidate_found=True,
        classification=Classification(rule_id, category, "test", "2026-04"),
        operational_flags=[],
    )


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "test.sqlite")


@pytest.fixture
def log(db):
    with AuditLog(db, now=FIXED_TS) as audit:
        yield audit


def decide(rule_id=None, category=None, rid="PR-0001", **b2):
    ev = evidence(rid, **b2)
    return G.decide(evaluation(rule_id, category, rid), ev, THRESHOLD), ev


# --- the §2.7 column set ----------------------------------------------------

def test_row_carries_every_column_the_architecture_names(log):
    decision, ev = decide()
    entry = log.record(decision, ev)

    assert entry.record_id == "purchase_register:PR-0001"
    assert entry.evidence_snapshot
    assert entry.rule_id_fired is None
    assert entry.confidence_score == 100.0
    assert entry.action == G.AUTO_RECONCILE
    assert entry.reviewer_decision is None          # nullable, starts null
    assert entry.timestamp == FIXED_TS


def test_rule_id_fired_is_recorded_when_a_rule_fires(log):
    decision, ev = decide("CLS-002", "credit_note_netting",
                          taxable_value="90000.00")
    entry = log.record(decision, ev)
    assert entry.rule_id_fired == "CLS-002"
    assert entry.category == "credit_note_netting"
    assert entry.action == G.CLASSIFIED_EXCEPTION


def test_evidence_snapshot_is_the_full_diff_verbatim(log):
    decision, ev = decide(taxable_value="90000.00")
    entry = log.record(decision, ev)
    snapshot = entry.evidence()
    assert snapshot["fields"] == ev.field_map()
    assert snapshot["candidate_found"] is True
    assert snapshot["b2_record_id"] == "gstr2b:2B-0001"


def test_snapshot_is_valid_json(log):
    decision, ev = decide()
    json.loads(log.record(decision, ev).evidence_snapshot)


def test_confidence_can_be_re_derived_from_the_snapshot(log):
    """The point of storing both: an auditor must be able to check the sum."""
    from src.confidence import FIELD_WEIGHTS
    decision, ev = decide(taxable_value="90000.00")
    entry = log.record(decision, ev)
    fields = entry.evidence()["fields"]
    recomputed = sum(w for name, w in FIELD_WEIGHTS.items()
                     if fields[name]["match"] is True)
    assert recomputed == entry.confidence_score


# --- separate table from quarantine (§3.3 / §2.7) --------------------------

def test_audit_is_its_own_table(log):
    assert AUDIT_TABLE in log.tables()


def test_both_logs_coexist_as_separate_tables(db):
    record = SourceRecord(SOURCE_PURCHASE_REGISTER, "PR-0002", 2,
                          dict(BASE, record_id="PR-0002", taxable_value="-1.00"))
    with QuarantineLog(db, now=FIXED_TS) as q:
        q.quarantine(V.validate_record(record))
    with AuditLog(db, now=FIXED_TS) as audit:
        decision, ev = decide()
        audit.record(decision, ev)
        assert {"audit_log", "quarantine_log"} <= set(audit.tables())
        assert audit.count() == 1                 # counted separately,
    with QuarantineLog(db) as q:
        assert q.count() == 1                     # never summed together


# --- quarantined records stay out, enforced --------------------------------

def test_a_quarantined_record_cannot_be_audited(db):
    """§2.1: it never reached the gate, so it must not appear here."""
    bad = SourceRecord(SOURCE_PURCHASE_REGISTER, "PR-0001", 1,
                       dict(BASE, taxable_value="-1.00"))
    with QuarantineLog(db, now=FIXED_TS) as q:
        q.quarantine(V.validate_record(bad))

    with AuditLog(db, now=FIXED_TS) as audit:
        decision, ev = decide()
        with pytest.raises(QuarantinedRecordError, match="was quarantined"):
            audit.record(decision, ev)
        assert audit.count() == 0


def test_the_guard_is_by_record_id_not_by_luck(db):
    with QuarantineLog(db, now=FIXED_TS) as q:
        q.quarantine(V.validate_record(SourceRecord(
            SOURCE_PURCHASE_REGISTER, "PR-9999", 9,
            dict(BASE, record_id="PR-9999", taxable_value="-1.00"))))
    with AuditLog(db, now=FIXED_TS) as audit:
        decision, ev = decide()
        audit.record(decision, ev)        # different record — allowed
        assert audit.count() == 1


def test_evidence_must_belong_to_the_decision(log):
    decision, _ = decide(rid="PR-0001")
    with pytest.raises(ValueError, match="evidence is for"):
        log.record(decision, evidence("PR-0002"))


# --- reviewer_decision is the human's column -------------------------------

def test_reviewer_decision_starts_null_and_can_be_set(log):
    decision, ev = decide(taxable_value="83271.44")
    log.record(decision, ev)
    assert log.entry("purchase_register:PR-0001").reviewer_decision is None

    log.record_reviewer_decision("purchase_register:PR-0001", "accepted_2b_value")
    assert log.entry("purchase_register:PR-0001").reviewer_decision == \
        "accepted_2b_value"


def test_reviewer_decision_on_an_unknown_record_raises(log):
    with pytest.raises(KeyError):
        log.record_reviewer_decision("purchase_register:PR-9999", "x")


def test_pending_review_lists_unresolved_indeterminates(log):
    ind, ind_ev = decide(taxable_value="83271.44")
    assert ind.outcome == G.INDETERMINATE
    log.record(ind, ind_ev)

    auto, auto_ev = decide(rid="PR-0002")
    log.record(auto, auto_ev)

    pending = log.pending_review()
    assert [e.record_id for e in pending] == ["purchase_register:PR-0001"]

    log.record_reviewer_decision("purchase_register:PR-0001", "resolved")
    assert log.pending_review() == []


def test_the_pipeline_never_writes_reviewer_decision(log):
    """It is set only by the explicit reviewer call."""
    for rid in ("PR-0001", "PR-0002", "PR-0003"):
        decision, ev = decide(rid=rid, taxable_value="83271.44")
        log.record(decision, ev)
    assert all(e.reviewer_decision is None for e in log.entries())


# --- batch behaviour --------------------------------------------------------

def test_one_row_per_record(log):
    pairs = [decide(rid=f"PR-{i:04d}") for i in range(1, 6)]
    log.record_all([d for d, _ in pairs], [e for _, e in pairs])
    assert log.count() == 5


def test_reruns_do_not_duplicate_rows(log):
    pairs = [decide(rid=f"PR-{i:04d}") for i in range(1, 4)]
    for _ in range(2):
        log.record_all([d for d, _ in pairs], [e for _, e in pairs])
    assert log.count() == 3


def test_counts_by_action_and_rule(log):
    cases = [
        decide(rid="PR-0001"),
        decide("CLS-002", "credit_note_netting", rid="PR-0002",
               taxable_value="90000.00"),
        decide(rid="PR-0003", taxable_value="83271.44"),
    ]
    log.record_all([d for d, _ in cases], [e for _, e in cases])
    assert log.counts_by_action() == {
        G.AUTO_RECONCILE: 1, G.CLASSIFIED_EXCEPTION: 1, G.INDETERMINATE: 1}
    assert log.counts_by_rule() == {"(none)": 2, "CLS-002": 1}


def test_entries_can_be_filtered_by_action(log):
    cases = [decide(rid="PR-0001"),
             decide(rid="PR-0002", taxable_value="83271.44")]
    log.record_all([d for d, _ in cases], [e for _, e in cases])
    assert len(log.entries(G.INDETERMINATE)) == 1
    assert len(log.entries()) == 2


def test_batch_rejects_mismatched_inputs(log):
    decision, _ = decide()
    with pytest.raises(ValueError, match="one to one"):
        log.record_all([decision], [])


def test_clear_empties_the_table(log):
    decision, ev = decide()
    log.record(decision, ev)
    log.clear()
    assert log.count() == 0
