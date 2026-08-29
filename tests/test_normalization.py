"""§2.2 normalisation: cleans text, decides nothing, preserves both values."""

import dataclasses
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import normalization as N
from src import validation as V
from src.source_records import SOURCE_PURCHASE_REGISTER, SourceRecord, load_source


def make(**overrides) -> SourceRecord:
    raw = {
        "record_id": "PR-0001",
        "invoice_id": "INV-2604-00001",
        "vendor_gstin": "27AAPFU0939F1ZV",
        "vendor_name": "Acme Industries Private Limited",
        "invoice_date": "2026-04-15",
        "taxable_value": "100000.00",
        "cgst": "9000.00", "sgst": "9000.00", "igst": "0.00",
        "total_tax": "18000.00", "invoice_value": "118000.00",
    }
    raw.update(overrides)
    return SourceRecord(SOURCE_PURCHASE_REGISTER, raw["record_id"], 1, raw)


# =========================================================================
# Deterministic half
# =========================================================================

@pytest.mark.parametrize("raw,expected", [
    ("27aapfu0939f1zv", "27AAPFU0939F1ZV"),
    ("27 AAPFU 0939 F1ZV", "27AAPFU0939F1ZV"),
    ("27-AAPFU-0939F1ZV", "27AAPFU0939F1ZV"),
    ("27AAPFU0939F1ZV", "27AAPFU0939F1ZV"),
])
def test_gstin_casing_and_separators(raw, expected):
    assert N.standardize_gstin(raw) == expected


def test_gstin_check_digit_is_never_rewritten():
    """§2.1 already rejected bad checksums; silently 'correcting' one here would
    destroy the evidence the §2.5 GSTIN-header rule depends on."""
    assert N.standardize_gstin("27AAPFU0939F1ZX") == "27AAPFU0939F1ZX"


@pytest.mark.parametrize("raw,expected", [
    ("15/04/2026", "2026-04-15"),
    ("15-04-2026", "2026-04-15"),
    ("2026/04/15", "2026-04-15"),
    ("2026-04-15", "2026-04-15"),
    ("", ""),
])
def test_dates_standardise_to_iso(raw, expected):
    assert N.standardize_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1,00,000.00", "100000.00"),
    ("1234.5", "1234.50"),
    ("  4200 ", "4200.00"),
    ("₹9,000.00", "9000.00"),
    ("0", "0.00"),
    ("", ""),
])
def test_amounts_round_to_two_places(raw, expected):
    assert N.standardize_amount(raw) == expected


def test_amount_normalisation_preserves_value():
    for raw in ("1,00,000.00", "1234.5", "0", "99999.994"):
        assert float(N.standardize_amount(raw)) == pytest.approx(
            float(raw.replace(",", "")), abs=0.005
        )


@pytest.mark.parametrize("raw,expected", [
    ("  Acme   Industries  ", "Acme Industries"),
    ("M/s Acme Industries", "Acme Industries"),
    ("Acme Industries Pvt Ltd.", "Acme Industries Pvt Ltd"),
    ("Acme  Inds. ;", "Acme Inds"),
])
def test_text_whitespace_and_affix_cleanup(raw, expected):
    assert N.standardize_text(raw) == expected


def test_deterministic_makes_no_api_call(monkeypatch):
    """The deterministic half must work with the SDK absent entirely."""
    monkeypatch.setitem(sys.modules, "anthropic", None)
    result = N.normalize_deterministic(make(vendor_gstin="27aapfu0939f1zv"))
    assert result.normalized["vendor_gstin"] == "27AAPFU0939F1ZV"


# =========================================================================
# Raw and normalised are both preserved
# =========================================================================

def test_raw_is_preserved_untouched():
    record = make(vendor_gstin="27 aapfu 0939 f1zv", invoice_date="15/04/2026")
    result = N.normalize_deterministic(record)
    assert result.raw == record.raw
    assert result.raw["vendor_gstin"] == "27 aapfu 0939 f1zv"
    assert result.normalized["vendor_gstin"] == "27AAPFU0939F1ZV"


def test_source_record_object_is_not_mutated():
    record = make(invoice_date="15/04/2026")
    before = dict(record.raw)
    N.normalize_deterministic(record)
    assert record.raw == before


def test_changes_record_before_after_and_method():
    result = N.normalize_deterministic(make(invoice_date="15/04/2026"))
    change = next(c for c in result.changes if c.field == "invoice_date")
    assert change.raw == "15/04/2026"
    assert change.normalized == "2026-04-15"
    assert change.method == N.DETERMINISTIC
    assert change.rule


def test_unchanged_fields_are_not_reported_as_changes():
    result = N.normalize_deterministic(make())
    assert "vendor_gstin" not in result.changed_fields()


def test_identity_fields_survive_normalisation():
    result = N.normalize_deterministic(make())
    assert result.source_id == "purchase_register:PR-0001"
    assert result.record_id == "PR-0001"
    assert result.source == SOURCE_PURCHASE_REGISTER


# =========================================================================
# Normalisation must not decide anything
# =========================================================================

FORBIDDEN_FIELDS = {
    "confidence", "score", "match", "is_match", "matched", "decision",
    "verdict", "category", "classification", "rule_id", "exception",
    "outcome", "action", "reconciled",
}


def test_result_type_exposes_no_decision_field():
    names = {f.name for f in dataclasses.fields(N.NormalizedRecord)}
    assert not (names & FORBIDDEN_FIELDS), names & FORBIDDEN_FIELDS
    names = {f.name for f in dataclasses.fields(N.FieldNormalization)}
    assert not (names & FORBIDDEN_FIELDS)


def test_normalisation_does_not_import_downstream_stages():
    """Steps 4-7 own the decisions. This module must not reach into them."""
    source = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "normalization.py")).read()
    for module in ("matcher", "rule_engine", "confidence", "gate", "evidence"):
        assert f"import {module}" not in source
        assert f"from .{module}" not in source


def test_normalisation_never_drops_or_adds_fields():
    result = N.normalize_deterministic(make())
    assert set(result.normalized) == set(result.raw)


# =========================================================================
# AI-assisted half — output contract
# =========================================================================

class FakeResponse:
    def __init__(self, text):
        self.content = [type("Block", (), {"type": "text", "text": text})()]


class FakeClient:
    """Stands in for anthropic.Anthropic. Records what it was asked."""

    def __init__(self, body):
        self._body = body
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self._body)


def test_valid_response_yields_the_cleaned_string():
    assert N.parse_ai_response('{"cleaned_text": "Acme Industries"}',
                               "Acme  Ind0stries") == "Acme Industries"


@pytest.mark.parametrize("body,why", [
    ('{"cleaned_text": "Acme", "confidence": 0.92}', "confidence"),
    ('{"cleaned_text": "Acme", "is_match": true}', "match decision"),
    ('{"cleaned_text": "Acme", "score": 88}', "score"),
    ('{"cleaned_text": "Acme", "category": "gstin_header_mismatch"}', "category"),
    ('{"cleaned_text": "Acme", "explanation": "looks like vendor 12"}', "prose"),
])
def test_any_extra_key_is_a_contract_violation(body, why):
    with pytest.raises(N.AIContractViolation, match="disallowed key"):
        N.parse_ai_response(body, "Acme")


@pytest.mark.parametrize("body", [
    "not json at all",
    '["Acme"]',
    '"Acme"',
    '{"text": "Acme"}',
    '{"cleaned_text": 42}',
    '{"cleaned_text": null}',
    '{"cleaned_text": ""}',
    '{}',
])
def test_malformed_responses_are_rejected(body):
    with pytest.raises(N.AIContractViolation):
        N.parse_ai_response(body, "Acme")


def test_runaway_output_is_rejected():
    body = '{"cleaned_text": "%s"}' % ("Acme Industries " * 20)
    with pytest.raises(N.AIContractViolation, match="repair, not a composition"):
        N.parse_ai_response(body, "Acme")


def test_request_declares_a_schema_that_forbids_extra_keys():
    assert N.OUTPUT_SCHEMA["additionalProperties"] is False
    assert N.OUTPUT_SCHEMA["required"] == ["cleaned_text"]
    assert set(N.OUTPUT_SCHEMA["properties"]) == {"cleaned_text"}


def test_system_prompt_forbids_decisions():
    prompt = N.SYSTEM_PROMPT.lower()
    assert "json only" in prompt
    assert "confidence" in prompt
    assert "do not judge whether two records match" in prompt


# =========================================================================
# AI-assisted half — behaviour
# =========================================================================

def test_ai_repairs_a_messy_name():
    client = FakeClient('{"cleaned_text": "Konark Fabricators and Company"}')
    record = make(vendor_name="Konark Fabricat0,rs and C0mpany")
    result = N.normalize_ai_assisted(record, client)

    assert result.normalized["vendor_name"] == "Konark Fabricators and Company"
    assert result.raw["vendor_name"] == "Konark Fabricat0,rs and C0mpany"
    change = next(c for c in result.changes if c.method == N.AI_ASSISTED)
    assert change.field == "vendor_name"


def test_clean_names_are_not_sent_to_the_api():
    client = FakeClient('{"cleaned_text": "Something Else Entirely"}')
    result = N.normalize_ai_assisted(make(), client)
    assert client.calls == []
    assert result.normalized["vendor_name"] == "Acme Industries Private Limited"


@pytest.mark.parametrize("name,messy", [
    ("Acme Industries Private Limited", False),
    ("Acme Inds. Pvt Ltd", False),
    ("Konark Fabricat0,rs and C0mpany", True),
    ("De,ccan Soluti0ns 1ndia Pvt. Ltd.", True),
    ("Acme Industries — Ltd", True),
])
def test_messiness_gate(name, messy):
    assert N.looks_messy(name) is messy


def test_contract_violation_falls_back_to_deterministic():
    """A model that tries to return a decision must not affect the pipeline."""
    client = FakeClient('{"cleaned_text": "Acme", "confidence": 0.99}')
    record = make(vendor_name="Ac0me  Ind,ustries")
    result = N.normalize_ai_assisted(record, client)

    deterministic = N.normalize_deterministic(record)
    assert result.normalized["vendor_name"] == deterministic.normalized["vendor_name"]
    assert not [c for c in result.changes if c.method == N.AI_ASSISTED]


def test_api_failure_falls_back_to_deterministic():
    class Boom:
        messages = property(lambda self: self)

        def create(self, **kwargs):
            raise RuntimeError("network down")

    record = make(vendor_name="Ac0me  Ind,ustries")
    result = N.normalize_ai_assisted(record, Boom())
    assert result.normalized["vendor_name"] == \
        N.normalize_deterministic(record).normalized["vendor_name"]


def test_no_client_means_deterministic_only():
    record = make(vendor_name="Ac0me  Ind,ustries")
    assert N.normalize(record, None).changes == \
        N.normalize_deterministic(record).changes


def test_ai_half_only_touches_free_text_fields():
    client = FakeClient('{"cleaned_text": "Repaired Name"}')
    record = make(vendor_name="Ac0me  Ind,ustries")
    result = N.normalize_ai_assisted(record, client)
    ai_fields = {c.field for c in result.changes if c.method == N.AI_ASSISTED}
    assert ai_fields <= set(N.TEXT_FIELDS)
    for field in ("vendor_gstin", "invoice_date", "taxable_value", "invoice_id"):
        assert result.normalized[field] == result.raw[field]


def test_request_uses_the_configured_model_and_json_schema():
    client = FakeClient('{"cleaned_text": "Repaired"}')
    N.normalize_ai_assisted(make(vendor_name="Ac0me  Ind,ustries"), client)
    call = client.calls[0]
    assert call["model"] == N.MODEL
    assert call["output_config"]["format"]["schema"] == N.OUTPUT_SCHEMA
    assert call["system"] == N.SYSTEM_PROMPT


# =========================================================================
# Over the real Phase 1 batch
# =========================================================================

def test_every_valid_record_normalises_without_error():
    valid, _ = V.partition(load_source(SOURCE_PURCHASE_REGISTER))
    results = [N.normalize_deterministic(r.record) for r in valid]
    assert len(results) == 480
    assert all(set(r.normalized) == set(r.raw) for r in results)


def test_normalised_records_still_pass_validation():
    """Cleanup must never turn a valid record invalid."""
    valid, _ = V.partition(load_source(SOURCE_PURCHASE_REGISTER))
    for result in valid:
        normalised = N.normalize_deterministic(result.record)
        rebuilt = SourceRecord(
            normalised.source, normalised.record_id,
            normalised.row_number, normalised.normalized,
        )
        assert V.validate_record(rebuilt).is_valid, normalised.source_id


# =========================================================================
# AI outcomes are observable, not silent
# =========================================================================

def test_stats_count_a_successful_repair():
    from collections import Counter
    stats = Counter()
    client = FakeClient('{"cleaned_text": "Acme Industries"}')
    N.normalize_ai_assisted(make(vendor_name="Ac0me  Ind,ustries"), client,
                            stats=stats)
    assert stats[N.AI_ATTEMPTED] == 1
    assert stats[N.AI_APPLIED] == 1
    assert stats[N.AI_CONTRACT_VIOLATION] == 0
    assert stats[N.AI_CALL_FAILED] == 0


def test_stats_distinguish_a_contract_violation_from_a_quiet_run():
    """'0 repaired because nothing was messy' and '0 repaired because every
    response was rejected' must not look the same to an operator."""
    from collections import Counter

    quiet = Counter()
    N.normalize_ai_assisted(make(), FakeClient('{"cleaned_text": "x"}'),
                            stats=quiet)
    assert quiet[N.AI_ATTEMPTED] == 0

    rejected = Counter()
    N.normalize_ai_assisted(
        make(vendor_name="Ac0me  Ind,ustries"),
        FakeClient('{"cleaned_text": "Acme", "confidence": 0.9}'),
        stats=rejected,
    )
    assert rejected[N.AI_ATTEMPTED] == 1
    assert rejected[N.AI_CONTRACT_VIOLATION] == 1
    assert rejected[N.AI_APPLIED] == 0


def test_stats_count_a_call_failure():
    from collections import Counter

    class Boom:
        messages = property(lambda self: self)

        def create(self, **kwargs):
            raise RuntimeError("no credentials")

    stats = Counter()
    N.normalize_ai_assisted(make(vendor_name="Ac0me  Ind,ustries"), Boom(),
                            stats=stats)
    assert stats[N.AI_ATTEMPTED] == 1
    assert stats[N.AI_CALL_FAILED] == 1
    assert stats[N.AI_APPLIED] == 0


def test_response_identical_to_input_counts_as_unchanged():
    from collections import Counter
    stats = Counter()
    messy = "Ac0me Ind,ustries"
    N.normalize_ai_assisted(make(vendor_name=messy),
                            FakeClient(json.dumps({"cleaned_text": messy})),
                            stats=stats)
    assert stats[N.AI_UNCHANGED] == 1
    assert stats[N.AI_APPLIED] == 0


# =========================================================================
# The request is one the real SDK accepts
# =========================================================================

def test_request_kwargs_are_accepted_by_the_installed_sdk():
    """Guards against a parameter name that only the fake client tolerates.
    Checks the signature only — no network, no credentials, no API call."""
    anthropic = pytest.importorskip("anthropic")
    import inspect

    sig = inspect.signature(anthropic.Anthropic(api_key="sk-ant-dummy").messages.create)
    client = FakeClient('{"cleaned_text": "Repaired"}')
    N.normalize_ai_assisted(make(vendor_name="Ac0me  Ind,ustries"), client)
    for name in client.calls[0]:
        assert name in sig.parameters, f"{name} is not a messages.create parameter"
