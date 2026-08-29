"""§2.1 Input validation — build-order step 2.

Checks run in the order the architecture specifies, and the FIRST failure wins:

  1. Required fields present (invoice_id, vendor_gstin, invoice_date,
     taxable_value, and at least one of cgst/sgst/igst)
  2. GSTIN format valid (15 characters, correct checksum, valid state prefix)
  3. Date parseable and within a plausible range
  4. Numeric fields actually numeric and non-negative

On failure the record goes to the quarantine log and **no further** — it is not
counted in the match rate, the exception count, or the indeterminate count
(§2.1). This module decides validity and nothing else: it assigns no category,
no confidence and no reconciliation outcome.

Date handling note: §2.1 requires a date that is *parseable*, not one already in
a particular layout. Several input layouts are accepted here and
src/normalization.py standardises the survivors to ISO (§2.2). A date that no
accepted layout can parse, or that lands outside the plausible range, is a
quarantine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Sequence

from .source_records import SourceRecord

# --- error taxonomy (the four §2.1 checks, in order) ------------------------

MISSING_REQUIRED_FIELD = "missing_required_field"
INVALID_GSTIN_FORMAT = "invalid_gstin_format"
UNPARSEABLE_OR_IMPLAUSIBLE_DATE = "unparseable_or_implausible_date"
NON_NUMERIC_OR_NEGATIVE_AMOUNT = "non_numeric_or_negative_amount"

ERROR_TYPES = (
    MISSING_REQUIRED_FIELD,
    INVALID_GSTIN_FORMAT,
    UNPARSEABLE_OR_IMPLAUSIBLE_DATE,
    NON_NUMERIC_OR_NEGATIVE_AMOUNT,
)

REQUIRED_FIELDS = ("invoice_id", "vendor_gstin", "invoice_date", "taxable_value")
TAX_HEAD_FIELDS = ("cgst", "sgst", "igst")
NUMERIC_FIELDS = ("taxable_value", "cgst", "sgst", "igst", "total_tax", "invoice_value")

# GST went live 2017-07-01; nothing in this system legitimately post-dates 2030.
MIN_PLAUSIBLE_DATE = date(2017, 7, 1)
MAX_PLAUSIBLE_DATE = date(2030, 12, 31)

ACCEPTED_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y")

# --- GSTIN ------------------------------------------------------------------

GSTIN_CODE = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

VALID_STATE_CODES = frozenset(
    [f"{n:02d}" for n in range(1, 39)] + ["97", "99"]
)


def gstin_check_digit(first14: str) -> str:
    """Standard GSTIN base-36 alternating-factor checksum."""
    total = 0
    for i, ch in enumerate(first14):
        value = GSTIN_CODE.index(ch)
        product = value * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return GSTIN_CODE[(36 - total % 36) % 36]


def gstin_is_valid(gstin: str) -> bool:
    """15 chars, valid state prefix, PAN-shaped body, correct check digit."""
    if not isinstance(gstin, str) or len(gstin) != 15:
        return False
    if gstin != gstin.upper():
        return False
    if any(ch not in GSTIN_CODE for ch in gstin):
        return False
    if gstin[:2] not in VALID_STATE_CODES:
        return False
    pan = gstin[2:12]
    if not (pan[0:5].isalpha() and pan[5:9].isdigit() and pan[9].isalpha()):
        return False
    if not gstin[12].isalnum() or gstin[13] != "Z":
        return False
    return gstin[14] == gstin_check_digit(gstin[:14])


def parse_date(raw: str) -> Optional[date]:
    """Parse a date in any accepted layout. None if no layout applies."""
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ACCEPTED_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(raw: str) -> Optional[float]:
    """Parse a rupee amount, tolerating separators and whitespace.

    Returns None if the text is not a number. Being *negative* is a separate
    failure from being *non-numeric*, so this returns the parsed value and lets
    the caller decide.
    """
    text = (raw or "").strip().replace(",", "").replace("₹", "").replace(" ", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# --- result types -----------------------------------------------------------


@dataclass(frozen=True)
class ValidationError:
    error_type: str
    message: str
    field: str


@dataclass(frozen=True)
class ValidationResult:
    record: SourceRecord
    error: Optional[ValidationError] = None

    @property
    def is_valid(self) -> bool:
        return self.error is None


# --- the four checks, in §2.1 order -----------------------------------------


def _check_required_fields(record: SourceRecord) -> Optional[ValidationError]:
    for name in REQUIRED_FIELDS:
        if not record.get(name).strip():
            return ValidationError(
                MISSING_REQUIRED_FIELD,
                f"required field {name!r} is empty or absent",
                name,
            )
    if not any(record.get(h).strip() for h in TAX_HEAD_FIELDS):
        return ValidationError(
            MISSING_REQUIRED_FIELD,
            "at least one of cgst/sgst/igst must be present; all three are empty",
            "cgst|sgst|igst",
        )
    return None


def _check_gstin(record: SourceRecord) -> Optional[ValidationError]:
    gstin = record.get("vendor_gstin").strip()
    if gstin_is_valid(gstin):
        return None
    if len(gstin) != 15:
        detail = f"expected 15 characters, got {len(gstin)}"
    elif gstin[:2] not in VALID_STATE_CODES:
        detail = f"state-code prefix {gstin[:2]!r} is not a valid GST state code"
    elif not (gstin[2:7].isalpha() and gstin[7:11].isdigit() and gstin[11].isalpha()):
        detail = "characters 3-12 are not PAN-shaped (AAAAA9999A)"
    elif gstin[13] != "Z":
        detail = f"character 14 is {gstin[13]!r}, expected 'Z'"
    else:
        detail = (
            f"check digit {gstin[14]!r} is wrong; "
            f"expected {gstin_check_digit(gstin[:14])!r}"
        )
    return ValidationError(
        INVALID_GSTIN_FORMAT, f"GSTIN {gstin!r} is invalid: {detail}", "vendor_gstin"
    )


def _check_date(record: SourceRecord) -> Optional[ValidationError]:
    raw = record.get("invoice_date").strip()
    parsed = parse_date(raw)
    if parsed is None:
        return ValidationError(
            UNPARSEABLE_OR_IMPLAUSIBLE_DATE,
            f"invoice_date {raw!r} is not a real date in any accepted layout "
            f"({', '.join(ACCEPTED_DATE_FORMATS)})",
            "invoice_date",
        )
    if not (MIN_PLAUSIBLE_DATE <= parsed <= MAX_PLAUSIBLE_DATE):
        return ValidationError(
            UNPARSEABLE_OR_IMPLAUSIBLE_DATE,
            f"invoice_date {parsed.isoformat()} is outside the plausible range "
            f"{MIN_PLAUSIBLE_DATE.isoformat()}..{MAX_PLAUSIBLE_DATE.isoformat()}",
            "invoice_date",
        )
    return None


def _check_numerics(record: SourceRecord) -> Optional[ValidationError]:
    for name in NUMERIC_FIELDS:
        raw = record.get(name).strip()
        if not raw:
            continue          # absence is check 1's business, not this one
        value = parse_amount(raw)
        if value is None:
            return ValidationError(
                NON_NUMERIC_OR_NEGATIVE_AMOUNT,
                f"{name} {raw!r} is not numeric",
                name,
            )
        if value < 0:
            return ValidationError(
                NON_NUMERIC_OR_NEGATIVE_AMOUNT,
                f"{name} {raw!r} is negative",
                name,
            )
    return None


CHECKS = (_check_required_fields, _check_gstin, _check_date, _check_numerics)


def validate_record(record: SourceRecord) -> ValidationResult:
    """Run the §2.1 checks in order; the first failure is the reported one."""
    for check in CHECKS:
        error = check(record)
        if error is not None:
            return ValidationResult(record=record, error=error)
    return ValidationResult(record=record)


def partition(records: Sequence[SourceRecord]):
    """Split records into (valid, invalid). Invalid ones go to quarantine."""
    valid, invalid = [], []
    for record in records:
        result = validate_record(record)
        (valid if result.is_valid else invalid).append(result)
    return valid, invalid
