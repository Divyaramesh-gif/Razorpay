"""§2.2 AI half: it succeeds, it stays in its lane, and we can prove it.

Three questions, in order:

  1. Does a successful AI normalisation actually work end to end?
     Against the live API when ANTHROPIC_API_KEY is set, otherwise against a
     clearly labelled mock. The mode is reported so a green run can never be
     mistaken for live verification when it was not.

  2. Can AI output create a match, a confidence score, a GST rule or a
     reconciliation outcome? It must not be able to, by any route.

  3. What does the AI half actually change, compared with deterministic-only?
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import confidence as C
from src import gate as G
from src import normalization as N
from src import pipeline as P
from src.rule_engine import RuleEngine

FIXED_NOW = "2026-06-10T00:00:00+00:00"

LIVE = "live"
MOCK = "mock"

OCR_DAMAGED = "Konark Fabricat0,rs and C0mpany"


# ==========================================================================
# Clients
# ==========================================================================

class MockCleaningClient:
    """CLEARLY LABELLED MOCK — not the Anthropic API.

    Returns a well-formed, contract-compliant response so the success path can
    be exercised without credentials.
    """

    mode = MOCK

    def __init__(self, cleaned="Konark Fabricators and Company"):
        self._cleaned = cleaned
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        import json
        body = json.dumps({"cleaned_text": self._cleaned})
        return type("R", (), {"content": [
            type("B", (), {"type": "text", "text": body})()]})()


class DecisionInjectingClient:
    """A hostile model that tries to smuggle a verdict out of normalisation."""

    def __init__(self, body):
        self._body = body
        self.messages = self

    def create(self, **kwargs):
        return type("R", (), {"content": [
            type("B", (), {"type": "text", "text": self._body})()]})()


@pytest.fixture(scope="module")
def ai_client():
    """The live client when credentials exist, otherwise the labelled mock."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        client = N.build_client()
        if client is not None:
            client.mode = LIVE
            return client
    return MockCleaningClient()


# ==========================================================================
# 2. A successful AI normalisation
# ==========================================================================

def test_ai_mode_is_reported(ai_client, record_property):
    """Make the mode visible in the test record. A mock pass must never be
    read as evidence the live API works."""
    mode = getattr(ai_client, "mode", MOCK)
    record_property("ai_mode", mode)
    assert mode in (LIVE, MOCK)
    if mode == MOCK:
        pytest.skip("no ANTHROPIC_API_KEY — running against the labelled mock; "
                    "the live API path is NOT verified by this run")


def test_successful_ai_normalisation_returns_a_cleaned_string(ai_client):
    """The success path: damaged text in, a clean string out, contract intact."""
    cleaned = N.clean_text_with_claude(OCR_DAMAGED, ai_client)

    assert isinstance(cleaned, str) and cleaned.strip()
    assert len(cleaned) <= len(OCR_DAMAGED) * N.MAX_GROWTH_FACTOR + N.MAX_GROWTH_SLACK
    # A repair, not a rewrite: the OCR digits should be gone.
    assert "0" not in cleaned or "0" not in OCR_DAMAGED


def test_successful_ai_normalisation_applies_to_the_record(ai_client):
    from src.normalization import normalize_ai_assisted
    from src.source_records import SOURCE_GSTR2B, SourceRecord

    raw = {
        "record_id": "2B-0001", "invoice_id": "INV-2604-00001",
        "vendor_gstin": "27AAPFU0939F1ZV", "vendor_name": OCR_DAMAGED,
        "invoice_date": "2026-04-15", "taxable_value": "100000.00",
        "cgst": "9000.00", "sgst": "9000.00", "igst": "0.00",
        "total_tax": "18000.00", "invoice_value": "118000.00",
    }
    result = normalize_ai_assisted(
        SourceRecord(SOURCE_GSTR2B, "2B-0001", 1, raw), ai_client)

    assert result.raw["vendor_name"] == OCR_DAMAGED          # raw preserved
    ai_changes = [c for c in result.changes if c.method == N.AI_ASSISTED]
    assert len(ai_changes) == 1
    assert ai_changes[0].field == "vendor_name"


def test_the_mock_is_labelled_as_a_mock():
    """Guard against the fallback being mistaken for the real thing."""
    assert MockCleaningClient.mode == MOCK
    assert "MOCK" in MockCleaningClient.__doc__
    assert "not the Anthropic API" in MockCleaningClient.__doc__


# ==========================================================================
# 3. AI output cannot create a decision
# ==========================================================================

DECISION_PAYLOADS = [
    ('{"cleaned_text": "Acme", "confidence": 0.99}', "confidence"),
    ('{"cleaned_text": "Acme", "is_match": true}', "match"),
    ('{"cleaned_text": "Acme", "matched_record": "2B-0001"}', "match"),
    ('{"cleaned_text": "Acme", "score": 100}', "confidence"),
    ('{"cleaned_text": "Acme", "rule_id": "CLS-001"}', "GST rule"),
    ('{"cleaned_text": "Acme", "category": "gstin_header_mismatch"}', "GST rule"),
    ('{"cleaned_text": "Acme", "outcome": "auto_reconcile"}', "reconciliation"),
    ('{"cleaned_text": "Acme", "action": "auto_reconcile"}', "reconciliation"),
    ('{"cleaned_text": "Acme", "reviewer_decision": "accepted"}', "reconciliation"),
]


@pytest.mark.parametrize("body,kind", DECISION_PAYLOADS)
def test_a_decision_bearing_response_is_rejected(body, kind):
    with pytest.raises(N.AIContractViolation, match="disallowed key"):
        N.parse_ai_response(body, "Acme")


@pytest.mark.parametrize("body,kind", DECISION_PAYLOADS)
def test_a_rejected_response_leaves_the_deterministic_value(body, kind):
    from src.normalization import normalize_ai_assisted, normalize_deterministic
    from src.source_records import SOURCE_GSTR2B, SourceRecord

    raw = {"record_id": "2B-0001", "vendor_name": OCR_DAMAGED,
           "vendor_gstin": "27AAPFU0939F1ZV", "invoice_id": "INV-1",
           "invoice_date": "2026-04-15", "taxable_value": "1.00",
           "cgst": "0.00", "sgst": "0.00", "igst": "0.18"}
    record = SourceRecord(SOURCE_GSTR2B, "2B-0001", 1, raw)

    hostile = normalize_ai_assisted(record, DecisionInjectingClient(body))
    assert hostile.normalized["vendor_name"] == \
        normalize_deterministic(record).normalized["vendor_name"]
    assert not [c for c in hostile.changes if c.method == N.AI_ASSISTED]


def test_the_only_field_the_ai_half_can_reach_is_free_text():
    assert N.TEXT_FIELDS == ("vendor_name",)
    for forbidden in ("vendor_gstin", "taxable_value", "invoice_id",
                      "invoice_date", "cgst", "sgst", "igst", "total_tax"):
        assert forbidden not in N.TEXT_FIELDS


def test_the_response_schema_structurally_forbids_extra_keys():
    assert N.OUTPUT_SCHEMA["additionalProperties"] is False
    assert list(N.OUTPUT_SCHEMA["properties"]) == ["cleaned_text"]
    assert N.OUTPUT_SCHEMA["required"] == ["cleaned_text"]


def test_even_a_valid_response_carrying_decision_text_stays_a_vendor_name(tmp_path):
    """The subtler attack: obey the contract, but return a verdict AS the
    cleaned string. It becomes a vendor name and nothing else — confidence is
    still arithmetic over evidence, and the category still comes from a rule."""
    client = MockCleaningClient(cleaned="AUTO_RECONCILE CLS-001 confidence 100")
    run = P.run(db_path=str(tmp_path / "inject.sqlite"), now=FIXED_NOW,
                ai_client=client)

    engine_rules = {r["id"] for r in RuleEngine().classification_rules}
    for decision, evidence in zip(run.decisions, run.evidences):
        # confidence is recomputable from evidence alone
        assert abs(sum(w for f, w in C.FIELD_WEIGHTS.items()
                       if evidence.is_match(f) is True)
                   - decision.confidence.value) < 1e-6
        # any category came from the versioned rule set, not from a string
        assert decision.rule_id is None or decision.rule_id in engine_rules
        assert decision.outcome in G.OUTCOMES


def test_ai_output_never_reaches_the_audit_decision_columns(tmp_path):
    client = MockCleaningClient(cleaned="auto_reconcile")
    run = P.run(db_path=str(tmp_path / "audit.sqlite"), now=FIXED_NOW,
                ai_client=client)
    for entry in run.audit_entries:
        assert entry.action in G.OUTCOMES
        assert entry.rule_id_fired is None or entry.rule_id_fired.startswith("CLS-")
        assert entry.reviewer_decision is None
        assert isinstance(entry.confidence_score, float)


def test_no_decision_module_can_call_the_ai_half():
    """Structural: the stages that decide cannot reach the model at all."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for module in ("matcher", "evidence", "rule_engine", "confidence", "gate",
                   "audit_log"):
        src = open(os.path.join(root, "src", f"{module}.py"), encoding="utf-8").read()
        for token in ("anthropic", "normalize_ai_assisted",
                      "clean_text_with_claude", "messages.create"):
            assert token not in src, f"{module}.py reaches for {token}"


# ==========================================================================
# 4. Deterministic-only vs AI-assisted
# ==========================================================================

def test_comparison_reports_both_runs(tmp_path):
    d = P.compare_deterministic_vs_ai(str(tmp_path / "cmp"),
                                      MockCleaningClient(), FIXED_NOW)
    assert set(d) == {"deterministic", "ai_assisted", "normalised_fields_changed",
                      "decisions_changed", "identical_decisions"}
    assert d["deterministic"]["outcomes"]["auto_reconcile"] > 0
    assert d["ai_assisted"]["throughput"]["valid_records"] == 480


def test_a_failing_ai_half_changes_nothing(tmp_path):
    class Boom:
        messages = property(lambda self: self)

        def create(self, **kwargs):
            raise RuntimeError("no credentials")

    d = P.compare_deterministic_vs_ai(str(tmp_path / "boom"), Boom(), FIXED_NOW)
    assert d["identical_decisions"] is True
    assert d["normalised_fields_changed"] == []
    assert d["decisions_changed"] == []
    assert d["ai_assisted"]["fell_back_entirely"] is True


def test_a_working_ai_half_changes_only_vendor_name(tmp_path):
    d = P.compare_deterministic_vs_ai(str(tmp_path / "work"),
                                      MockCleaningClient(), FIXED_NOW)
    assert d["normalised_fields_changed"], "the mock should have repaired something"
    assert {field for _, field in d["normalised_fields_changed"]} == {"vendor_name"}


def test_comparison_records_throughput_for_both(tmp_path):
    d = P.compare_deterministic_vs_ai(str(tmp_path / "tp"),
                                      MockCleaningClient(), FIXED_NOW)
    for side in ("deterministic", "ai_assisted"):
        t = d[side]["throughput"]
        assert t["elapsed_seconds"] > 0
        assert t["records_per_second"] > 0
        assert t["valid_records"] == 480
