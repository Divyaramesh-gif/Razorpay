"""§2.2 Deterministic + AI-assisted normalisation — build-order step 3.

Split explicitly into two functions, not one:

    normalize_deterministic(record)  pure code. GSTIN casing/separators, date
                                     format standardisation, numeric rounding.
                                     No API call, no network, always available.

    normalize_ai_assisted(record)    Claude API call, JSON-only, used ONLY for
                                     messy free text (vendor-name variants,
                                     OCR-style artifacts). Returns a cleaned
                                     string and nothing else.

**This module cleans text. It does not decide anything.** It never scores a
match, never assigns a GST rule, never produces a confidence number and never
reaches a reconciliation outcome — those belong to matcher.py, rule_engine.py,
confidence.py and gate.py (build-order steps 4-7). The AI half is held to that
by an enforced output contract (see `AIContractViolation` below): the model may
return exactly one key, `cleaned_text`, holding a string. A response carrying
anything else — a confidence, a verdict, an is_match flag, an explanation — is
rejected and the deterministic value is kept instead. A model cannot smuggle a
decision into this stage even if it tries.

Raw and normalised values are both preserved. `NormalizedRecord.raw` is the
untouched source row; `.normalized` is the cleaned mapping; `.changes` records
every field that moved, with the before value, the after value, and which half
of the stage did it.

Normalisation runs only on records that PASSED §2.1 validation. Quarantined
records never reach this module.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from .source_records import SourceRecord
from .validation import (
    NUMERIC_FIELDS,
    parse_amount,
    parse_date,
)

DETERMINISTIC = "deterministic"
AI_ASSISTED = "ai_assisted"

TEXT_FIELDS = ("vendor_name",)
GSTIN_FIELDS = ("vendor_gstin",)
DATE_FIELDS = ("invoice_date", "itc_claimed_date", "rule88d_intimation_date",
               "supplier_filing_date", "simulated_current_date")


# ---------------------------------------------------------------------------
# Result types — raw and normalised side by side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldNormalization:
    field: str
    raw: str
    normalized: str
    method: str      # DETERMINISTIC | AI_ASSISTED
    rule: str        # which cleanup applied, for the audit log


@dataclass(frozen=True)
class NormalizedRecord:
    """A record's raw and cleaned views. Carries no verdict of any kind."""

    source_id: str
    source: str
    record_id: str
    row_number: int
    raw: Dict[str, str]
    normalized: Dict[str, str]
    changes: List[FieldNormalization] = field(default_factory=list)

    def changed_fields(self) -> List[str]:
        return [c.field for c in self.changes]

    def value(self, field_name: str) -> str:
        return self.normalized.get(field_name, "")


# ---------------------------------------------------------------------------
# Deterministic half — pure code, no API call
# ---------------------------------------------------------------------------


def standardize_gstin(raw: str) -> str:
    """Uppercase, drop separators and whitespace.

    Note on §2.2's "GSTIN checksum correction": by the time a record reaches
    normalisation it has already passed the §2.1 checksum check, so there is no
    broken checksum left to correct — a wrong check digit is a quarantine, not
    something to silently repair. Silently rewriting a check digit would also
    destroy the very evidence the §2.5 GSTIN-header rule depends on. So this
    function standardises presentation only and leaves the digits alone.
    """
    return re.sub(r"[\s\-]", "", (raw or "")).upper()


def standardize_date(raw: str) -> str:
    """Any accepted input layout -> ISO YYYY-MM-DD. Unparseable text is left
    untouched (it cannot occur on a validated record; optional date fields that
    are simply blank stay blank)."""
    text = (raw or "").strip()
    if not text:
        return ""
    parsed = parse_date(text)
    return parsed.isoformat() if isinstance(parsed, date) else text


def standardize_amount(raw: str) -> str:
    """Strip separators, round to 2 decimal places, render with both of them."""
    text = (raw or "").strip()
    if not text:
        return ""
    value = parse_amount(text)
    if value is None:
        return text
    return f"{value:.2f}"


def standardize_text(raw: str) -> str:
    """Conservative free-text cleanup: collapse whitespace, drop a courtesy
    prefix, strip trailing punctuation. Nothing here changes the identity of
    the name — that is what the AI half is for."""
    text = (raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^(?:M/s\.?|M/S\.?|Messrs\.?)\s+", "", text)
    text = text.strip(" .,;:-")
    return re.sub(r"\s+", " ", text).strip()


def normalize_deterministic(record: SourceRecord) -> NormalizedRecord:
    """Pure-code normalisation. No LLM call, no network, no decisions."""
    raw = dict(record.raw)
    normalized = dict(raw)
    changes: List[FieldNormalization] = []

    def apply(field_name: str, cleaner, rule: str) -> None:
        if field_name not in raw:
            return
        before = raw[field_name]
        after = cleaner(before)
        normalized[field_name] = after
        if after != before:
            changes.append(
                FieldNormalization(field_name, before, after, DETERMINISTIC, rule)
            )

    for name in GSTIN_FIELDS:
        apply(name, standardize_gstin, "gstin_casing_and_separators")
    for name in DATE_FIELDS:
        apply(name, standardize_date, "date_iso_8601")
    for name in NUMERIC_FIELDS:
        apply(name, standardize_amount, "amount_2dp")
    for name in TEXT_FIELDS:
        apply(name, standardize_text, "text_whitespace_and_affixes")

    return NormalizedRecord(
        source_id=record.source_id,
        source=record.source,
        record_id=record.record_id,
        row_number=record.row_number,
        raw=raw,
        normalized=normalized,
        changes=changes,
    )


# ---------------------------------------------------------------------------
# AI-assisted half — Claude, JSON-only, cleaned string and nothing else
# ---------------------------------------------------------------------------

MODEL = "claude-opus-5"

# The ONLY key the model is permitted to return.
CLEANED_TEXT_KEY = "cleaned_text"

# Structural guarantee: additionalProperties=false means the API itself refuses
# to emit a confidence, a verdict or an is_match flag. The client-side contract
# check below is the second lock on the same door — it holds even if the request
# is served without structured-output support.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {CLEANED_TEXT_KEY: {"type": "string"}},
    "required": [CLEANED_TEXT_KEY],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You clean up messy supplier names taken from Indian GST filings. The text may \
carry OCR damage (0 for O, 1 for l or I, 5 for S, stray commas or semicolons, \
doubled spaces) or abbreviation variants (Pvt Ltd, Ltd., & Co.).

Return the supplier name with that damage repaired. Preserve the name's \
identity: do not expand or contract the legal suffix, do not translate, do not \
reorder words, and do not invent words that are not there. If the text is \
already clean, return it unchanged.

You are a text-cleaning step in a larger pipeline. You are NOT deciding \
anything. Do not judge whether two records match. Do not report a confidence, \
a probability, a score, a category or a recommendation. Do not explain \
yourself.

Reply with JSON only, in exactly this shape and with no other keys:
{"cleaned_text": "<the cleaned name>"}
"""

# A cleaned name is a repair of the input, not a new composition. Anything much
# longer is prose, an explanation or a hallucination — reject it.
MAX_GROWTH_FACTOR = 2.0
MAX_GROWTH_SLACK = 24

# Characters a legitimate Indian supplier name uses. Anything else is a sign of
# OCR damage worth sending to the AI half.
_CLEAN_NAME_CHARS = re.compile(r"^[A-Za-z0-9 &.,'()\-/]+$")
# A digit welded to letters inside one token ("Soluti0ns", "1ndia").
_DIGIT_IN_WORD = re.compile(r"[A-Za-z]\d|\d[A-Za-z]")
# Punctuation sitting inside a word ("Fabricat0,rs").
_PUNCT_IN_WORD = re.compile(r"[A-Za-z][,;][A-Za-z]")


class AIContractViolation(Exception):
    """The model returned something other than a single cleaned string.

    Raised on: non-JSON output, a non-object payload, a missing cleaned_text,
    a non-string cleaned_text, ANY additional key (a confidence, a score, a
    match verdict, an explanation), or output too long to be a repair of the
    input. Callers treat this as "AI unavailable" and keep the deterministic
    value — a violating response is never allowed to influence the pipeline.
    """


def looks_messy(text: str) -> bool:
    """Is this free text damaged enough to be worth an API call?

    §2.2 scopes the AI half to messy free text only. Sending clean names would
    burn tokens and risk the model 'improving' a name that was already right.
    """
    if not text or not text.strip():
        return False
    if not _CLEAN_NAME_CHARS.match(text):
        return True
    if _DIGIT_IN_WORD.search(text):
        return True
    if _PUNCT_IN_WORD.search(text):
        return True
    return False


def _enforce_contract(payload: Any, original: str) -> str:
    """Reduce a model response to a cleaned string, or refuse it."""
    if not isinstance(payload, dict):
        raise AIContractViolation(
            f"expected a JSON object, got {type(payload).__name__}"
        )
    extra = set(payload) - {CLEANED_TEXT_KEY}
    if extra:
        raise AIContractViolation(
            f"response carries disallowed key(s) {sorted(extra)}; normalisation "
            f"may return {CLEANED_TEXT_KEY!r} only and must not report a "
            "decision, score or confidence"
        )
    if CLEANED_TEXT_KEY not in payload:
        raise AIContractViolation(f"response is missing {CLEANED_TEXT_KEY!r}")
    cleaned = payload[CLEANED_TEXT_KEY]
    if not isinstance(cleaned, str):
        raise AIContractViolation(
            f"{CLEANED_TEXT_KEY} must be a string, got {type(cleaned).__name__}"
        )
    cleaned = cleaned.strip()
    if not cleaned:
        raise AIContractViolation(f"{CLEANED_TEXT_KEY} is empty")
    limit = len(original) * MAX_GROWTH_FACTOR + MAX_GROWTH_SLACK
    if len(cleaned) > limit:
        raise AIContractViolation(
            f"{CLEANED_TEXT_KEY} is {len(cleaned)} chars for a {len(original)}-char "
            "input; a cleaned name is a repair, not a composition"
        )
    return cleaned


def parse_ai_response(text: str, original: str) -> str:
    """Parse and contract-check a raw model response body."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise AIContractViolation(f"response is not valid JSON: {exc}") from exc
    return _enforce_contract(payload, original)


def build_client():
    """Construct an Anthropic client, or None if the SDK/credentials are absent.

    Returning None rather than raising keeps the whole pipeline runnable
    offline: every stage but this one is pure code, and this one falls back to
    its deterministic result.
    """
    try:
        import anthropic
    except ImportError:
        return None
    try:
        return anthropic.Anthropic()
    except Exception:
        return None


def clean_text_with_claude(text: str, client: Any) -> str:
    """One API call. Returns a cleaned string; raises AIContractViolation
    if the model returns anything else."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=256,
        system=SYSTEM_PROMPT,
        # A name repair is a simple task — low effort keeps it cheap and fast.
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        messages=[{"role": "user", "content": text}],
    )
    body = next((b.text for b in response.content if b.type == "text"), "")
    return parse_ai_response(body, text)


# Outcome counters for the AI half. Falling back silently keeps the pipeline
# correct, but an operator still needs to know whether the stage actually ran —
# "0 records repaired" because nothing was messy and "0 repaired" because every
# call was rejected look identical otherwise.
AI_ATTEMPTED = "ai_attempted"
AI_APPLIED = "ai_applied"
AI_UNCHANGED = "ai_unchanged"
AI_CONTRACT_VIOLATION = "ai_contract_violation"
AI_CALL_FAILED = "ai_call_failed"


def normalize_ai_assisted(
    record: SourceRecord,
    client: Any = None,
    *,
    base: Optional[NormalizedRecord] = None,
    stats: Optional[Counter] = None,
) -> NormalizedRecord:
    """§2.2 AI-assisted normalisation, layered on the deterministic result.

    Only messy free-text fields are sent. If the client is absent, the field is
    already clean, the call fails, or the response breaks the output contract,
    the deterministic value stands. This function can therefore never make the
    pipeline worse than the deterministic half alone, and never returns a match
    decision or a confidence score.

    Pass a `stats` Counter to see what actually happened — how many fields were
    sent, how many came back usable, and how many were rejected and why.
    """
    result = base if base is not None else normalize_deterministic(record)
    if client is None:
        return result
    if stats is None:
        stats = Counter()

    normalized = dict(result.normalized)
    changes = list(result.changes)

    for field_name in TEXT_FIELDS:
        current = normalized.get(field_name, "")
        if not looks_messy(current):
            continue
        stats[AI_ATTEMPTED] += 1
        try:
            cleaned = clean_text_with_claude(current, client)
        except AIContractViolation:
            stats[AI_CONTRACT_VIOLATION] += 1   # keep deterministic
            continue
        except Exception:
            stats[AI_CALL_FAILED] += 1          # keep deterministic
            continue
        if cleaned != current:
            stats[AI_APPLIED] += 1
            normalized[field_name] = cleaned
            changes.append(
                FieldNormalization(
                    field_name, current, cleaned, AI_ASSISTED, "ai_text_repair"
                )
            )
        else:
            stats[AI_UNCHANGED] += 1

    return NormalizedRecord(
        source_id=result.source_id,
        source=result.source,
        record_id=result.record_id,
        row_number=result.row_number,
        raw=result.raw,
        normalized=normalized,
        changes=changes,
    )


def normalize(record: SourceRecord, client: Any = None,
              stats: Optional[Counter] = None) -> NormalizedRecord:
    """Deterministic first, then the AI half if a client was supplied (§2.2)."""
    base = normalize_deterministic(record)
    if client is None:
        return base
    return normalize_ai_assisted(record, client, base=base, stats=stats)
