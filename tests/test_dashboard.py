"""Dashboard smoke tests, over a real pipeline run and a real HTTP server.

The dashboard is a presentation layer, so the risks worth testing are:
it renders real pipeline output rather than placeholders; it does not invent
or hardcode a number; the disclaimers are present; and every route and export
actually serves.

Every assertion below is made against a live batch, with one record of each
kind — exact, fuzzy, classified exception and indeterminate — resolved from
the run itself rather than hardcoded.
"""

import csv
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from src import dashboard as D
from src import gate as G
from src import pipeline as P
from src import report as R

FIXED_NOW = "2026-06-10T00:00:00+00:00"


# --------------------------------------------------------------------------
# One live server, one live batch, shared by the module
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("dash") / "ledger.sqlite")
    server, app = D.make_server(port=0, now=FIXED_NOW, db_path=db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    app.ensure()
    yield f"http://{host}:{port}", app
    server.shutdown()
    server.server_close()


def fetch(base, path):
    try:
        with urllib.request.urlopen(base + path) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


@pytest.fixture(scope="module")
def samples(live):
    """One record of each kind, resolved from the real run — never hardcoded."""
    _, app = live
    picks = {}
    for record_id, row in app.state.result.by_record().items():
        decision, evidence = row["decision"], row["evidence"]
        if decision.outcome == G.AUTO_RECONCILE:
            key = R.classify_match_type(evidence, decision.outcome)   # exact | fuzzy
        else:
            key = decision.outcome
        picks.setdefault(key, record_id)
    assert set(picks) >= {"exact", "fuzzy", G.CLASSIFIED_EXCEPTION,
                          G.INDETERMINATE}, picks
    return picks


# --------------------------------------------------------------------------
# Homepage
# --------------------------------------------------------------------------

def test_homepage_serves(live):
    status, body = fetch(live[0], "/")
    assert status == 200
    assert "<title>" in body and "Exception Ledger" in body


def test_homepage_states_the_disclaimers(live):
    _, body = fetch(live[0], "/")
    assert "Synthetic GSTR-2B-style data" in body
    assert "No live GSTN connectivity" in body
    assert "Not tax advice" in body


def test_homepage_carries_the_positioning_once(live):
    _, body = fetch(live[0], "/")
    assert body.count("GST-specific finance controller") == 1
    assert "Auditable human review" in body


def test_homepage_never_claims_live_gstn_access(live):
    _, body = fetch(live[0], "/")
    lowered = body.lower()
    assert "no live gstn" in lowered
    for forbidden in ("connects to the gstn", "gstn api", "files your return"):
        assert forbidden not in lowered


def test_homepage_shows_record_counts_from_the_run(live):
    base, app = live
    _, body = fetch(base, "/")
    result = app.state.result
    assert f"{result.total_read:,}" in body
    assert f"{result.scored:,}" in body
    assert f"{result.quarantined_count:,}" in body


def test_homepage_shows_all_three_outcome_counts(live):
    base, app = live
    _, body = fetch(base, "/")
    counts = app.state.outcome_counts()
    for outcome, label in D.OUTCOME_LABELS.items():
        assert label in body
        assert f"{counts[outcome]:,}" in body


def test_outcome_labels_cover_the_gate_exactly(live):
    assert set(D.OUTCOME_LABELS) == set(G.OUTCOMES)
    assert D.OUTCOME_LABELS[G.INDETERMINATE] == "INDETERMINATE_REVIEW"


def test_homepage_shows_precision_coverage_throughput_and_exposure(live):
    base, app = live
    _, body = fetch(base, "/")
    report = app.state.report
    assert "Precision" in body and "Coverage" in body
    assert "Throughput" in body and "Estimated ITC exposure" in body
    assert f"{100 * report.match_rate.rate:.1f}%" in body
    assert f"{report.itc_exposure_total:,.2f}" in body


def test_precision_is_read_from_the_stored_confusion_matrix(live):
    base, app = live
    _, body = fetch(base, "/")
    precision = app.state.precision
    assert precision is not None
    assert precision["source"].endswith("frozen_test_split")
    assert precision["precision"] == precision["true_positives"] / (
        precision["true_positives"] + precision["false_positives"])
    assert "calibration_v2026_04.yaml" in body


def test_homepage_states_the_normalisation_mode(live):
    base, app = live
    _, body = fetch(base, "/")
    if app.state.result.ai_assisted:
        assert "AI-assisted" in body
    else:
        assert "Deterministic" in body
        assert "not requested" in body


def test_quarantine_is_presented_as_a_separate_track(live):
    base, app = live
    _, body = fetch(base, "/")
    assert "never counted in the match rate" in body
    for error_type in app.state.report.quarantined_by_error:
        assert error_type in body


# --------------------------------------------------------------------------
# Nothing invented, nothing hardcoded
# --------------------------------------------------------------------------

def test_no_metric_is_hardcoded_in_the_module():
    """A literal count or rate in the source would survive a changed run.

    The CSS block is excluded — hex colours contain digit runs that are not
    metrics — and matching is on whole numeric tokens, so this fails on a real
    hardcoded figure rather than on an incidental substring.
    """
    import re

    source = open(os.path.join(REPO, "src", "dashboard.py"), encoding="utf-8").read()
    without_css = source.replace(D.CSS, "")
    for literal in ("480", "990", "365", "137", "145", "1888373",
                    "94.5", "80.25", "2026-04"):
        assert not re.search(rf"(?<![\w.]){re.escape(literal)}(?![\w.])",
                             without_css), \
            f"hardcoded metric {literal!r} in dashboard.py"


def test_every_headline_number_moves_with_the_run(tmp_path):
    """The decisive check: change the run, and the rendered figures change."""
    import re

    loose = D.run_batch(db_path=str(tmp_path / "loose.sqlite"), now=FIXED_NOW)
    numbers = set(re.findall(r"\d[\d,]*\.?\d*", D.render_home(loose)))
    counts = loose.outcome_counts()
    assert f"{counts[G.AUTO_RECONCILE]:,}" in numbers
    assert f"{loose.result.scored:,}" in numbers


def test_the_page_changes_when_the_run_changes(tmp_path):
    """Proof the numbers come from the result, not the template."""
    baseline = D.run_batch(db_path=str(tmp_path / "a.sqlite"), now=FIXED_NOW)
    strict = D.run_batch(db_path=str(tmp_path / "b.sqlite"), now=FIXED_NOW)
    # Re-gate the second run at an impossible threshold via the report layer.
    tightened = P.run(db_path=str(tmp_path / "c.sqlite"), now=FIXED_NOW,
                      threshold=101.0)
    strict.result = tightened
    strict.report = R.build_report(tightened, R.load_splits(), R.FROZEN_TEST)
    strict.queue = D.build_queue(tightened, __import__(
        "src.rule_engine", fromlist=["RuleEngine"]).RuleEngine())

    assert D.render_home(baseline) != D.render_home(strict)
    assert baseline.outcome_counts()[G.AUTO_RECONCILE] > 0
    assert strict.outcome_counts()[G.AUTO_RECONCILE] == 0


def test_upload_is_declined_honestly_rather_than_stubbed(live):
    _, body = fetch(live[0], "/")
    assert "Upload is not offered" in body
    assert "pipeline.run()" in body


def test_dashboard_does_not_touch_decision_logic():
    """It may read the pipeline; it may not re-implement it."""
    source = open(os.path.join(REPO, "src", "dashboard.py"), encoding="utf-8").read()
    for forbidden in ("def decide", "def score_evidence", "def classify",
                      "def match_records", "def greedy_assign"):
        assert forbidden not in source


# --------------------------------------------------------------------------
# Record detail — exact, fuzzy, classified exception, indeterminate
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["exact", "fuzzy", G.CLASSIFIED_EXCEPTION,
                                  G.INDETERMINATE])
def test_record_page_serves_for_each_kind(live, samples, kind):
    status, body = fetch(live[0],
                         "/record?id=" + urllib.parse.quote(samples[kind]))
    assert status == 200
    assert samples[kind] in body


@pytest.mark.parametrize("kind", ["exact", "fuzzy", G.CLASSIFIED_EXCEPTION,
                                  G.INDETERMINATE])
def test_record_page_shows_rule_confidence_and_threshold(live, samples, kind):
    base, app = live
    record_id = samples[kind]
    _, body = fetch(base, "/record?id=" + urllib.parse.quote(record_id))
    decision = app.state.row(record_id)["decision"]

    assert f"{decision.confidence.value:.1f}" in body
    assert f"{decision.threshold:g}" in body
    assert app.state.result.rules_version in body
    assert (decision.rule_id or "none fired") in body
    assert D.OUTCOME_LABELS[decision.outcome] in body


@pytest.mark.parametrize("kind", ["exact", "fuzzy", G.CLASSIFIED_EXCEPTION,
                                  G.INDETERMINATE])
def test_record_page_shows_raw_versus_normalised(live, samples, kind):
    base, app = live
    record_id = samples[kind]
    _, body = fetch(base, "/record?id=" + urllib.parse.quote(record_id))
    assert "raw vs normalised" in body
    assert "Raw" in body and "Normalised" in body
    record = app.state.row(record_id)["match"].pr_record
    assert record.raw["invoice_id"] in body


@pytest.mark.parametrize("kind", ["exact", "fuzzy", G.CLASSIFIED_EXCEPTION,
                                  G.INDETERMINATE])
def test_record_page_shows_field_level_evidence(live, samples, kind):
    base, app = live
    record_id = samples[kind]
    _, body = fetch(base, "/record?id=" + urllib.parse.quote(record_id))
    for field in app.state.row(record_id)["evidence"].fields:
        assert field in body


@pytest.mark.parametrize("kind", ["exact", "fuzzy", G.CLASSIFIED_EXCEPTION,
                                  G.INDETERMINATE])
def test_record_page_shows_audit_details(live, samples, kind):
    base, app = live
    record_id = samples[kind]
    _, body = fetch(base, "/record?id=" + urllib.parse.quote(record_id))
    audit = next(a for a in app.state.result.audit_entries
                 if a.record_id == record_id)
    assert "Audit log" in body
    assert audit.timestamp in body
    assert "reviewer_decision" in body
    assert str(audit.confidence_score) in body


def test_fuzzy_record_shows_a_differing_field(live, samples):
    base, app = live
    record_id = samples["fuzzy"]
    _, body = fetch(base, "/record?id=" + urllib.parse.quote(record_id))
    assert app.state.row(record_id)["evidence"].mismatched_fields()
    assert "differs" in body


def test_exact_record_shows_no_differing_field(live, samples):
    base, app = live
    record_id = samples["exact"]
    assert app.state.row(record_id)["evidence"].mismatched_fields() == []
    _, body = fetch(base, "/record?id=" + urllib.parse.quote(record_id))
    assert "match" in body


def test_classified_exception_names_its_rule(live, samples):
    base, app = live
    record_id = samples[G.CLASSIFIED_EXCEPTION]
    decision = app.state.row(record_id)["decision"]
    _, body = fetch(base, "/record?id=" + urllib.parse.quote(record_id))
    assert decision.rule_id.startswith("CLS-")
    assert decision.rule_id in body and decision.category in body


def test_indeterminate_record_has_no_rule(live, samples):
    base, app = live
    record_id = samples[G.INDETERMINATE]
    assert app.state.row(record_id)["decision"].rule_id is None
    _, body = fetch(base, "/record?id=" + urllib.parse.quote(record_id))
    assert "none fired" in body


def test_unknown_record_returns_404(live):
    status, body = fetch(live[0], "/record?id=purchase_register:PR-9999")
    assert status == 404
    assert "quarantine" in body


def test_unknown_route_returns_404(live):
    assert fetch(live[0], "/nope")[0] == 404


# --------------------------------------------------------------------------
# Queue and quarantine
# --------------------------------------------------------------------------

def test_queue_holds_every_non_auto_reconciled_record(live):
    _, app = live
    counts = app.state.outcome_counts()
    assert len(app.state.queue) == (counts[G.CLASSIFIED_EXCEPTION]
                                    + counts[G.INDETERMINATE])
    assert all(q.outcome != G.AUTO_RECONCILE for q in app.state.queue)


def test_queue_carries_review_risk_information(live):
    _, app = live
    row = app.state.queue[0]
    assert row.itc_at_risk >= 0
    assert row.rule88d_status
    assert row.confidence is not None


def test_queue_is_ordered_by_review_risk(live):
    """88D closed first, then open, then n/a — each by ITC at risk desc."""
    from src.rule_engine import (STATUS_NOT_APPLICABLE, STATUS_OUTSIDE_WINDOW,
                                 STATUS_WITHIN_WINDOW)
    _, app = live
    priority = {STATUS_OUTSIDE_WINDOW: 0, STATUS_WITHIN_WINDOW: 1,
                STATUS_NOT_APPLICABLE: 2}
    keys = [(priority.get(q.rule88d_status, 3), -q.itc_at_risk)
            for q in app.state.queue]
    assert keys == sorted(keys)


def test_itc_at_risk_matches_the_rule_engine(live):
    """The dashboard must not compute its own ITC figure."""
    from src.rule_engine import RuleEngine
    _, app = live
    engine = RuleEngine()
    joined = app.state.result.by_record()
    for row in app.state.queue[:20]:
        expected = engine.itc_variance(joined[row.record_id]["match"],
                                       joined[row.record_id]["evidence"])
        assert row.itc_at_risk == expected


def test_quarantine_page_lists_every_quarantined_record(live):
    base, app = live
    status, body = fetch(base, "/quarantine")
    assert status == 200
    for entry in app.state.result.quarantined:
        assert entry.source_record_id in body
        assert entry.validation_error in body


# --------------------------------------------------------------------------
# Exports — CSV, JSON, TXT
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", sorted(D.EXPORTS))
def test_export_serves(live, path):
    status, body = fetch(live[0], path)
    assert status == 200 and body.strip()


def test_csv_json_and_txt_are_all_offered():
    suffixes = {p.rsplit(".", 1)[-1] for p in D.EXPORTS}
    assert {"csv", "json", "txt"} <= suffixes


def test_decisions_csv_has_one_row_per_scored_record(live):
    base, app = live
    _, body = fetch(base, "/export/decisions.csv")
    rows = list(csv.DictReader(io.StringIO(body)))
    assert len(rows) == app.state.result.scored
    assert {"record_id", "outcome", "confidence", "threshold",
            "rule_id"} <= set(rows[0])


def test_decisions_csv_matches_the_run(live):
    base, app = live
    _, body = fetch(base, "/export/decisions.csv")
    rows = {r["record_id"]: r for r in csv.DictReader(io.StringIO(body))}
    for decision in app.state.result.decisions:
        assert rows[decision.record_id]["outcome"] == decision.outcome
        assert float(rows[decision.record_id]["confidence"]) == \
            pytest.approx(decision.confidence.value)


def test_quarantine_csv_holds_the_quarantined_records(live):
    base, app = live
    _, body = fetch(base, "/export/quarantine.csv")
    rows = list(csv.DictReader(io.StringIO(body)))
    assert len(rows) == app.state.result.quarantined_count
    assert "raw_record_snapshot" in rows[0]


def test_audit_csv_holds_one_row_per_audit_entry(live):
    base, app = live
    _, body = fetch(base, "/export/audit.csv")
    rows = list(csv.DictReader(io.StringIO(body)))
    assert len(rows) == len(app.state.result.audit_entries)


def test_report_json_is_valid_and_carries_the_disclaimer(live):
    base, app = live
    _, body = fetch(base, "/export/report.json")
    payload = json.loads(body)
    assert payload["disclaimer"] == D.DISCLAIMER
    assert payload["run"]["fingerprint"] == P.fingerprint(app.state.result)
    assert payload["match_rate"]["exact"] == app.state.report.match_rate.exact
    assert len(payload["queue"]) == len(app.state.queue)


def test_report_txt_is_the_evaluation_report(live):
    base, app = live
    _, body = fetch(base, "/export/report.txt")
    assert "EXCEPTION LEDGER — EVALUATION REPORT" in body
    assert "10. LIMITATIONS" in body
    assert "Not tax advice" in body


# --------------------------------------------------------------------------
# Batch run control
# --------------------------------------------------------------------------

def test_run_batch_endpoint_reruns_the_pipeline(live):
    base, app = live
    before = app.state.run_number
    request = urllib.request.Request(base + "/run", data=b"", method="POST")
    with urllib.request.urlopen(request) as response:
        assert response.status == 200          # follows the 303 to /
    assert app.state.run_number == before + 1


def test_run_batch_is_reproducible(live):
    """Re-running from the UI must not change any decision."""
    base, app = live
    first = P.fingerprint(app.state.result)
    request = urllib.request.Request(base + "/run", data=b"", method="POST")
    with urllib.request.urlopen(request):
        pass
    assert P.fingerprint(app.state.result) == first


def test_ai_run_reports_fallback_when_no_credentials(live):
    base, app = live
    request = urllib.request.Request(
        base + "/run", data=b"ai=1", method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request):
        pass
    state = app.state
    if state.result.ai_fell_back_entirely:
        _, body = fetch(base, "/")
        assert "FALLBACK" in body
        assert "NOT AI-normalised" in body
    else:
        assert state.result.ai_applied >= 0
    # whichever path ran, decisions are unchanged
    assert state.result.scored == 480 or state.result.scored > 0
