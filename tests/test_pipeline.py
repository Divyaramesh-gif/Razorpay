"""Integration: the whole pipeline, end to end (§1)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import confidence as C
from src import gate as G
from src import pipeline as P
from src.audit_log import AuditLog, QuarantinedRecordError
from src.quarantine_log import QuarantineLog
from src.source_records import SOURCE_GSTR2B, SOURCE_PURCHASE_REGISTER

FIXED_NOW = "2026-06-10T00:00:00+00:00"


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("pipeline") / "ledger.sqlite")
    return P.run(db_path=db, now=FIXED_NOW)


# --- the run completes and the numbers reconcile ---------------------------

def test_every_source_record_is_accounted_for(result):
    """Nothing may be silently dropped: read = scored + quarantined."""
    assert result.records_read == {SOURCE_PURCHASE_REGISTER: 500,
                                   SOURCE_GSTR2B: 490}
    pr_read = result.records_read[SOURCE_PURCHASE_REGISTER]
    assert result.scored + result.quarantined_count == pr_read


def test_expected_batch_shape(result):
    assert result.total_read == 990
    assert result.scored == 480
    assert result.quarantined_count == 20


def test_every_stage_produced_one_output_per_scored_record(result):
    n = result.scored
    assert len(result.matches) == n
    assert len(result.evidences) == n
    assert len(result.evaluations) == n
    assert len(result.decisions) == n
    assert len(result.audit_entries) == n


def test_stages_stay_aligned_record_by_record(result):
    """A silent off-by-one between stages would attach one record's evidence
    to another's decision."""
    for match, ev, evaluation, decision in zip(
        result.matches, result.evidences, result.evaluations, result.decisions
    ):
        assert match.pr_id == ev.pr_record_id == evaluation.record_id \
            == decision.record_id


def test_outcomes_sum_to_the_scored_count(result):
    assert sum(result.outcome_counts().values()) == result.scored


def test_pipeline_runs_offline_by_default(result):
    assert result.ai_assisted is False


# --- §2.1 boundary: quarantined records stay out --------------------------

def test_quarantined_records_are_never_scored(result):
    quarantined = {e.record_id for e in result.quarantined}
    scored = {d.record_id for d in result.decisions}
    assert quarantined
    assert quarantined.isdisjoint(scored)


def test_quarantined_records_are_never_normalised(result):
    quarantined = {e.record_id for e in result.quarantined}
    normalised = {r.source_id
                  for r in result.normalised[SOURCE_PURCHASE_REGISTER]}
    assert quarantined.isdisjoint(normalised)


def test_quarantined_records_are_never_audited(result):
    quarantined = {e.record_id for e in result.quarantined}
    audited = {e.record_id for e in result.audit_entries}
    assert quarantined.isdisjoint(audited)


def test_quarantine_is_not_a_gate_outcome(result):
    assert set(result.outcome_counts()) == set(G.OUTCOMES)
    assert "quarantine" not in result.outcome_counts()


def test_both_logs_are_written_as_separate_tables(result):
    with AuditLog(result.db_path) as alog:
        assert {"audit_log", "quarantine_log"} <= set(alog.tables())
        assert alog.count() == result.scored
    with QuarantineLog(result.db_path) as qlog:
        assert qlog.count() == result.quarantined_count


# --- §2.3 one-to-one survives full integration ----------------------------

def test_one_to_one_holds_end_to_end(result):
    claimed = [m.b2_id for m in result.match_result.matched]
    assert len(claimed) == len(set(claimed))


def test_2b_side_partitions_cleanly(result):
    claimed = {m.b2_id for m in result.match_result.matched}
    unclaimed = {r.source_id for r in result.match_result.unmatched_2b}
    assert claimed.isdisjoint(unclaimed)
    assert len(claimed) + len(unclaimed) == \
        len(result.normalised[SOURCE_GSTR2B])


# --- §2.6 the gate used the frozen threshold ------------------------------

def test_pipeline_uses_the_frozen_calibrated_threshold(result):
    assert result.threshold == C.load_threshold()


def test_an_explicit_threshold_overrides_the_artifact(tmp_path):
    strict = P.run(db_path=str(tmp_path / "a.sqlite"), threshold=101.0,
                   now=FIXED_NOW)
    assert strict.outcome_counts()[G.AUTO_RECONCILE] == 0


def test_a_lower_threshold_moves_records_into_auto_reconcile(tmp_path, result):
    loose = P.run(db_path=str(tmp_path / "b.sqlite"), threshold=0.0, now=FIXED_NOW)
    assert loose.outcome_counts()[G.AUTO_RECONCILE] >= \
        result.outcome_counts()[G.AUTO_RECONCILE]


# --- reproducibility -------------------------------------------------------

def test_two_runs_produce_identical_decisions(tmp_path):
    a = P.run(db_path=str(tmp_path / "a.sqlite"), now=FIXED_NOW)
    b = P.run(db_path=str(tmp_path / "b.sqlite"), now=FIXED_NOW)
    assert P.fingerprint(a) == P.fingerprint(b)


def test_reproducible_across_separate_databases(tmp_path, result):
    again = P.run(db_path=str(tmp_path / "c.sqlite"), now=FIXED_NOW)
    assert P.fingerprint(again) == P.fingerprint(result)


def test_fingerprint_ignores_timestamps(tmp_path):
    a = P.run(db_path=str(tmp_path / "a.sqlite"), now="2020-01-01T00:00:00+00:00")
    b = P.run(db_path=str(tmp_path / "b.sqlite"), now="2030-12-31T23:59:59+00:00")
    assert P.fingerprint(a) == P.fingerprint(b)


def test_fingerprint_changes_when_a_decision_changes(tmp_path, result):
    """A digest that never moves would prove nothing."""
    different = P.run(db_path=str(tmp_path / "d.sqlite"), threshold=101.0,
                      now=FIXED_NOW)
    assert P.fingerprint(different) != P.fingerprint(result)


def test_rerunning_replaces_rather_than_accumulates(tmp_path):
    db = str(tmp_path / "same.sqlite")
    P.run(db_path=db, now=FIXED_NOW)
    P.run(db_path=db, now=FIXED_NOW)
    with AuditLog(db) as alog:
        assert alog.count() == 480
    with QuarantineLog(db) as qlog:
        assert qlog.count() == 20


# --- the pipeline is label-blind ------------------------------------------

def test_pipeline_result_carries_no_label_or_split(result):
    import dataclasses
    names = {f.name for f in dataclasses.fields(P.PipelineResult)}
    assert not (names & {"split", "labels", "ground_truth", "expected_outcome",
                         "case_type", "accuracy"})


def test_pipeline_needs_no_arguments_beyond_infrastructure(tmp_path):
    """If it ran without being handed labels, it cannot have used them."""
    assert P.run(db_path=str(tmp_path / "x.sqlite"), now=FIXED_NOW).scored == 480


# --- audit rows are complete ----------------------------------------------

def test_every_audit_row_has_the_full_column_set(result):
    for entry in result.audit_entries:
        assert entry.record_id and entry.invoice_id
        assert entry.evidence_snapshot
        assert entry.action in G.OUTCOMES
        assert entry.confidence_score is not None
        assert entry.timestamp == FIXED_NOW
        assert entry.reviewer_decision is None


def test_audit_confidence_matches_the_gate_decision(result):
    by_id = {e.record_id: e for e in result.audit_entries}
    for decision in result.decisions:
        assert by_id[decision.record_id].confidence_score == \
            decision.confidence.value
        assert by_id[decision.record_id].action == decision.outcome


# ==========================================================================
# §2.2 AI-half outcomes must be visible in the CLI
# ==========================================================================
# Regression guard. The AI half falls back to the deterministic value on any
# failure, which keeps the pipeline correct but means a run where every call
# failed produces byte-identical decisions to one where nothing needed
# repairing. If the CLI does not say which happened, an operator can read
# exit 0 as "the batch was AI-normalised" when it was not.

class _AlwaysFailsClient:
    """Stands in for an Anthropic client with no usable credentials."""

    messages = property(lambda self: self)

    def create(self, **kwargs):
        raise TypeError("Could not resolve authentication method.")


class _AlwaysRepairsClient:
    """Every call succeeds and returns a cleaned string."""

    messages = property(lambda self: self)

    def create(self, **kwargs):
        return type("R", (), {"content": [
            type("B", (), {"type": "text",
                           "text": '{"cleaned_text": "Repaired Name"}'})()]})()


def test_stats_are_empty_when_the_ai_half_is_off(result):
    assert result.ai_assisted is False
    assert result.ai_attempted == 0
    assert result.ai_failed == 0
    assert result.ai_fell_back_entirely is False


def test_a_fully_failed_ai_run_is_counted(tmp_path):
    run = P.run(db_path=str(tmp_path / "fail.sqlite"), now=FIXED_NOW,
                ai_client=_AlwaysFailsClient())
    assert run.ai_assisted is True
    assert run.ai_attempted > 0
    assert run.ai_applied == 0
    assert run.ai_failed == run.ai_attempted
    assert run.ai_fell_back_entirely is True


def test_a_fully_failed_ai_run_is_visible_in_the_cli(tmp_path, monkeypatch, capsys):
    """The bug this guards: exit 0 with no indication the AI half was dead."""
    from src import normalization as Nz
    monkeypatch.setattr(Nz, "build_client", lambda: _AlwaysFailsClient())

    exit_code = P._main(["--ai", "--db", str(tmp_path / "cli.sqlite"),
                         "--now", FIXED_NOW])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "EVERY AI CALL FAILED" in out
    assert "NOT AI-normalised" in out
    assert "field(s) sent" in out
    assert "0 repaired" in out


def test_a_successful_ai_run_does_not_warn(tmp_path, monkeypatch, capsys):
    """The warning must be specific to total failure, not fire on every run."""
    from src import normalization as Nz
    monkeypatch.setattr(Nz, "build_client", lambda: _AlwaysRepairsClient())

    P._main(["--ai", "--db", str(tmp_path / "ok.sqlite"), "--now", FIXED_NOW])
    out = capsys.readouterr().out

    assert "EVERY AI CALL FAILED" not in out
    assert "repaired" in out


def test_the_offline_run_says_the_ai_half_was_not_requested(tmp_path, capsys):
    P._main(["--db", str(tmp_path / "off.sqlite"), "--now", FIXED_NOW])
    out = capsys.readouterr().out
    assert "not requested" in out
    assert "EVERY AI CALL FAILED" not in out


def test_a_failed_ai_half_changes_no_decision(tmp_path, result):
    """The fallback must be exactly the deterministic result — the reporting
    change must not have altered what the pipeline decides."""
    failed = P.run(db_path=str(tmp_path / "f.sqlite"), now=FIXED_NOW,
                   ai_client=_AlwaysFailsClient())
    assert P.fingerprint(failed) == P.fingerprint(result)


def test_ai_stats_stay_out_of_the_fingerprint(tmp_path, result):
    """Diagnostics are not decisions."""
    failed = P.run(db_path=str(tmp_path / "g.sqlite"), now=FIXED_NOW,
                   ai_client=_AlwaysFailsClient())
    assert failed.ai_stats != result.ai_stats
    assert P.fingerprint(failed) == P.fingerprint(result)
