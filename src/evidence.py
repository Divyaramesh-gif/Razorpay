"""§2.4 Field-by-field evidence comparison — build-order step 5.

Takes a matched pair (or a `no_candidate_found` singleton) and produces a plain
diff object — **no interpretation, no verdict**:

    Evidence(
        invoice_id="INV-1042",
        fields={
            "amount": {"pr_value": 11800.00, "2b_value": 10800.00,
                       "delta": 1000.00, "match": False},
            "gstin":  {"pr_value": "27ABCDE...", "2b_value": "07ABCDE...",
                       "match": False},
            "date":   {"pr_value": "2026-03-14", "2b_value": "2026-03-14",
                       "match": True},
            "invoice_number": {"match": True},
        },
        candidate_found=True,
    )

This object is logged verbatim in the audit log's matched/mismatched fields.
The rule engine consumes it but **does not modify it** — hence the frozen
dataclass and the defensive copy in `field_map()`.

Everything here is an observation. There is no category, no confidence, no
outcome, and no notion of whether a difference is acceptable — a `match: False`
on `amount` says the numbers differ, not that anything is wrong. Deciding what
a difference *means* is §2.5's job, and how much it matters is §2.6's.

Two fields beyond the four in the §2.4 example are recorded: `vendor_name`
(the field normalisation touches, so its before/after belongs in the audit
trail) and `tax_heads` (the CGST+SGST vs IGST split, which is the direct
evidence for the §2.5 GSTIN-header rule). Both follow the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .matcher import Match
from .normalization import NormalizedRecord
from .validation import parse_amount, parse_date

PR_VALUE = "pr_value"
B2_VALUE = "2b_value"
DELTA = "delta"
MATCH = "match"

# Amounts within this many rupees are recorded as matching (§2.3's "within Rs.1").
AMOUNT_MATCH_TOLERANCE = 1.00


@dataclass(frozen=True)
class Evidence:
    """A plain diff between a purchase-register record and its 2B counterpart.

    `candidate_found=False` means the matcher found no counterpart; the
    per-field entries then carry the register value with `2b_value: None` and
    `match: False`, so the absence is visible field by field rather than
    signalled only by a flag.
    """

    invoice_id: str
    fields: Dict[str, Dict[str, Any]]
    candidate_found: bool
    pr_record_id: str = ""
    b2_record_id: Optional[str] = None

    # -- read-only accessors ------------------------------------------------

    def field_map(self) -> Dict[str, Dict[str, Any]]:
        """A copy, so a consumer cannot mutate the logged evidence."""
        return {name: dict(entry) for name, entry in self.fields.items()}

    def matched_fields(self) -> List[str]:
        return sorted(n for n, e in self.fields.items() if e.get(MATCH) is True)

    def mismatched_fields(self) -> List[str]:
        return sorted(n for n, e in self.fields.items() if e.get(MATCH) is False)

    def delta(self, field_name: str) -> Optional[float]:
        return self.fields.get(field_name, {}).get(DELTA)

    def pr_value(self, field_name: str) -> Any:
        return self.fields.get(field_name, {}).get(PR_VALUE)

    def b2_value(self, field_name: str) -> Any:
        return self.fields.get(field_name, {}).get(B2_VALUE)

    def is_match(self, field_name: str) -> Optional[bool]:
        return self.fields.get(field_name, {}).get(MATCH)


# ---------------------------------------------------------------------------
# Field comparators — each returns one entry of `fields`
# ---------------------------------------------------------------------------


def _compare_text(pr: str, b2: Optional[str], *, case_insensitive: bool = False):
    if b2 is None:
        return {PR_VALUE: pr, B2_VALUE: None, MATCH: False}
    a, b = (pr.upper(), b2.upper()) if case_insensitive else (pr, b2)
    return {PR_VALUE: pr, B2_VALUE: b2, MATCH: a == b}


def _compare_amount(pr_raw: str, b2_raw: Optional[str]):
    a = parse_amount(pr_raw)
    if b2_raw is None:
        return {PR_VALUE: a, B2_VALUE: None, DELTA: None, MATCH: False}
    b = parse_amount(b2_raw)
    if a is None or b is None:
        return {PR_VALUE: a, B2_VALUE: b, DELTA: None, MATCH: False}
    delta = round(a - b, 2)
    return {PR_VALUE: a, B2_VALUE: b, DELTA: delta,
            MATCH: abs(delta) <= AMOUNT_MATCH_TOLERANCE}


def _compare_date(pr_raw: str, b2_raw: Optional[str]):
    a = parse_date(pr_raw)
    if b2_raw is None:
        return {PR_VALUE: pr_raw or None, B2_VALUE: None, DELTA: None, MATCH: False}
    b = parse_date(b2_raw)
    if a is None or b is None:
        return {PR_VALUE: pr_raw or None, B2_VALUE: b2_raw or None,
                DELTA: None, MATCH: False}
    return {PR_VALUE: a.isoformat(), B2_VALUE: b.isoformat(),
            DELTA: (a - b).days, MATCH: a == b}


def _tax_head_profile(record: NormalizedRecord) -> str:
    """Which GST heads this record carries: the §2.5 header-mismatch signal."""
    cgst = parse_amount(record.value("cgst")) or 0.0
    sgst = parse_amount(record.value("sgst")) or 0.0
    igst = parse_amount(record.value("igst")) or 0.0
    if igst > 0 and cgst == 0 and sgst == 0:
        return "igst"
    if igst == 0 and (cgst > 0 or sgst > 0):
        return "cgst_sgst"
    if igst > 0:
        return "mixed"
    return "none"


def _compare_tax_heads(pr: NormalizedRecord, b2: Optional[NormalizedRecord]):
    a = _tax_head_profile(pr)
    pr_total = parse_amount(pr.value("total_tax"))
    if b2 is None:
        return {PR_VALUE: a, B2_VALUE: None, DELTA: None, MATCH: False,
                "pr_total_tax": pr_total, "2b_total_tax": None}
    b = _tax_head_profile(b2)
    b2_total = parse_amount(b2.value("total_tax"))
    delta = (round(pr_total - b2_total, 2)
             if pr_total is not None and b2_total is not None else None)
    return {PR_VALUE: a, B2_VALUE: b, DELTA: delta, MATCH: a == b,
            "pr_total_tax": pr_total, "2b_total_tax": b2_total}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def compare(match: Match) -> Evidence:
    """Build the §2.4 diff for one match (or one no_candidate_found singleton)."""
    pr = match.pr_record
    b2 = match.b2_record

    def b2v(name: str) -> Optional[str]:
        return b2.value(name) if b2 is not None else None

    fields: Dict[str, Dict[str, Any]] = {
        "invoice_number": _compare_text(pr.value("invoice_id"), b2v("invoice_id")),
        "gstin": _compare_text(pr.value("vendor_gstin"), b2v("vendor_gstin")),
        "amount": _compare_amount(pr.value("taxable_value"), b2v("taxable_value")),
        "date": _compare_date(pr.value("invoice_date"), b2v("invoice_date")),
        "vendor_name": _compare_text(pr.value("vendor_name"), b2v("vendor_name"),
                                     case_insensitive=True),
        "tax_heads": _compare_tax_heads(pr, b2),
    }

    return Evidence(
        invoice_id=pr.value("invoice_id"),
        fields=fields,
        candidate_found=match.candidate_found,
        pr_record_id=pr.source_id,
        b2_record_id=match.b2_id,
    )


def compare_all(matches) -> List[Evidence]:
    return [compare(m) for m in matches]
