#!/usr/bin/env python3
"""
Exception Ledger — Part 1: synthetic dataset generator.

Implements build-order step 1 of Architecture v2 (LOCKED):
    "data/generate_dataset.py — the defect-plan generator. Nothing else can be
     verified without this existing first."

Produces, into the directory containing this file:

    purchase_register.csv     §3.1 buyer-side purchase register (500 records)
    gstr2b.csv                §3.1 synthetic GSTR-2B for return period 2026-04
    gstr2b_prior_period.csv   §2.5 prior-period snapshot used to disambiguate
                                   no_candidate_found into late-filed-supplier
                                   vs invoice-removed-post-claim
    ground_truth.csv          §3.1 labels — the PIPELINE NEVER READS THIS.
                                   Only the evaluation script (§2.6, §2.7) does.

Design rules honoured here:
  * Fully reproducible: one seed, stdlib `random.Random` only, no global state,
    no wall-clock, no environment lookups. Re-running overwrites with byte-
    identical files (asserted by check R1 in --verify).
  * The pipeline's own input files carry NO label columns. Every ground-truth
    signal lives in ground_truth.csv.
  * simulated_current_date is carried on all three source CSVs (§3.1) so the
    Rule 88D window and the DRC-01C threshold check are computable without a
    wall-clock read.

Usage:
    python3 data/generate_dataset.py            # generate + verify
    python3 data/generate_dataset.py --verify   # verify existing files only
    python3 data/generate_dataset.py --seed N   # regenerate under a new seed

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 0. Locked generation constants
# ---------------------------------------------------------------------------

SEED = 20260401

N_PURCHASE_RECORDS = 500
N_VENDORS = 40

BUYER_STATE_CODE = "27"          # Maharashtra — the filing entity
BUYER_STATE_NAME = "Maharashtra"

RETURN_PERIOD = "2026-04"        # the period under reconciliation
PRIOR_PERIOD = "2026-03"         # §2.5 prior-period snapshot
INVOICE_WINDOW = (date(2026, 4, 1), date(2026, 4, 30))
PRIOR_WINDOW = (date(2026, 3, 1), date(2026, 3, 31))

# §2.5 / §3.1 — the batch's frozen "today". Everything time-relative is
# computed from this, never from the system clock.
SIMULATED_CURRENT_DATE = date(2026, 6, 10)

RULE_88D_WINDOW_DAYS = 7         # §2.5 operational check

# §2.5 DRC-01C: "does the cumulative ITC variance for this vendor exceed the
# auto-notice trigger?"
#
# This is a SYNTHETIC trigger sized to this batch, not the statutory figure.
# The real DRC-01C test is an ITC difference above Rs.25 lakh; at this batch's
# invoice values no vendor would ever cross it and the check would be dead
# weight. Rs.75,000 puts 11 of the 40 supplier GSTINs over the line, which is
# what makes the operational check observable in the §2.7 report.
#
# The authoritative copy lives in src/rules/rules_v2026_04.yaml (build step 6);
# it is restated here only so ground_truth.csv can carry the expected label.
# If the YAML value changes, change it here and regenerate.
DRC01C_THRESHOLD_RUPEES = 75000.00

# §2.6 calibration protocol — the split is decided HERE, at generation time,
# "before any tuning", and is stratified so every case type appears in both
# halves.
CALIBRATION_FRACTION = 0.70

TAX_RATES = (5, 12, 18, 28)

# ---------------------------------------------------------------------------
# 1. The defect plan
# ---------------------------------------------------------------------------
# Every case type below is traceable to a named requirement in Architecture v2.
# Counts sum to N_PURCHASE_RECORDS and are asserted in check C1.

DEFECT_PLAN: Dict[str, int] = {
    # --- clean baseline: must auto-reconcile ---------------------------------
    "clean_exact_match":              275,   # §2.6 auto-reconcile
    # --- fuzzy: counterpart exists, tolerance required (§2.3) ----------------
    "fuzzy_vendor_name_variant":       40,   # §2.2 normalize_ai_assisted
    "fuzzy_ocr_artifact":              20,   # §2.2 "OCR-style artifacts"
    "fuzzy_date_tolerance":            15,   # §2.3 "date within a tolerance window"
    "fuzzy_amount_rounding":           15,   # §2.3 "amount within Rs.1"
    # --- classification rules (§2.5) ----------------------------------------
    "gstin_header_mismatch":           30,   # CGST/SGST vs IGST filing error
    "credit_note_netting":             25,   # amount delta = credit-note pattern
    "late_filed_supplier":             20,   # no_candidate_found, NOT in prior snapshot
    "invoice_removed_post_claim":      15,   # no_candidate_found, IS in prior snapshot
    # --- indeterminate (§2.6 third gate outcome) -----------------------------
    "indeterminate_ambiguous":         25,
    # --- quarantine (§2.1 / §3.3) — never scored, never reaches the gate -----
    "quarantine_missing_field":         6,
    "quarantine_bad_gstin":             6,
    "quarantine_bad_date":              4,
    "quarantine_bad_amount":            4,
}

QUARANTINE_CASES = {k for k in DEFECT_PLAN if k.startswith("quarantine_")}
NO_CANDIDATE_CASES = {"late_filed_supplier", "invoice_removed_post_claim"}

# GSTR-2B rows with no purchase-register counterpart. These exist to exercise
# the §2.3 one-to-one property from the 2B side: the greedy assignment must
# leave them unassigned rather than double-claiming.
N_DECOY_NEAR_DUPLICATES = 10     # near-clone of a real 2B row; must NOT win a pair
N_UNRELATED_2B_ONLY = 15         # supplier filed something we never booked

N_PRIOR_PERIOD_NOISE = 60        # prior-snapshot rows unrelated to this batch

DEFECT_FAMILY = {
    "clean_exact_match": "clean",
    "fuzzy_vendor_name_variant": "fuzzy",
    "fuzzy_ocr_artifact": "fuzzy",
    "fuzzy_date_tolerance": "fuzzy",
    "fuzzy_amount_rounding": "fuzzy",
    "gstin_header_mismatch": "classification_exception",
    "credit_note_netting": "classification_exception",
    "late_filed_supplier": "absence",
    "invoice_removed_post_claim": "absence",
    "indeterminate_ambiguous": "ambiguous",
    "quarantine_missing_field": "invalid_input",
    "quarantine_bad_gstin": "invalid_input",
    "quarantine_bad_date": "invalid_input",
    "quarantine_bad_amount": "invalid_input",
}

# §2.7 report buckets. match_type describes what the GROUND TRUTH says is
# required to resolve the record, not what the pipeline actually did.
MATCH_TYPE = {
    "clean_exact_match": "exact",
    "fuzzy_vendor_name_variant": "fuzzy",
    "fuzzy_ocr_artifact": "fuzzy",
    "fuzzy_date_tolerance": "fuzzy",
    "fuzzy_amount_rounding": "fuzzy",
    "gstin_header_mismatch": "rule_classified",
    "credit_note_netting": "rule_classified",
    "late_filed_supplier": "none",
    "invoice_removed_post_claim": "none",
    "indeterminate_ambiguous": "fuzzy",
    "quarantine_missing_field": "quarantined",
    "quarantine_bad_gstin": "quarantined",
    "quarantine_bad_date": "quarantined",
    "quarantine_bad_amount": "quarantined",
}

EXPECTED_CLASSIFICATION = {
    "clean_exact_match": "",
    "fuzzy_vendor_name_variant": "",
    "fuzzy_ocr_artifact": "",
    "fuzzy_date_tolerance": "",
    "fuzzy_amount_rounding": "",
    "gstin_header_mismatch": "gstin_header_mismatch",
    "credit_note_netting": "credit_note_netting",
    "late_filed_supplier": "late_filed_supplier",
    "invoice_removed_post_claim": "invoice_removed_post_claim",
    "indeterminate_ambiguous": "",
    "quarantine_missing_field": "",
    "quarantine_bad_gstin": "",
    "quarantine_bad_date": "",
    "quarantine_bad_amount": "",
}

# §2.6 three-way gate outcome, plus the separate §2.1 quarantine exit.
EXPECTED_OUTCOME = {
    "clean_exact_match": "auto_reconcile",
    "fuzzy_vendor_name_variant": "auto_reconcile",
    "fuzzy_ocr_artifact": "auto_reconcile",
    "fuzzy_date_tolerance": "auto_reconcile",
    "fuzzy_amount_rounding": "auto_reconcile",
    "gstin_header_mismatch": "classified_exception",
    "credit_note_netting": "classified_exception",
    "late_filed_supplier": "classified_exception",
    "invoice_removed_post_claim": "classified_exception",
    "indeterminate_ambiguous": "indeterminate",
    "quarantine_missing_field": "quarantine",
    "quarantine_bad_gstin": "quarantine",
    "quarantine_bad_date": "quarantine",
    "quarantine_bad_amount": "quarantine",
}

# §2.1 validation error the record must trip. Empty for records that must pass.
EXPECTED_VALIDATION_ERROR = {
    "quarantine_missing_field": "missing_required_field",
    "quarantine_bad_gstin": "invalid_gstin_format",
    "quarantine_bad_date": "unparseable_or_implausible_date",
    "quarantine_bad_amount": "non_numeric_or_negative_amount",
}


# ---------------------------------------------------------------------------
# 2. GSTIN helpers (§2.1 "15 characters, correct checksum pattern, valid
#    state-code prefix")
# ---------------------------------------------------------------------------

GSTIN_CODE = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Real GST state codes. 27 is the buyer's own state (intra-state supply).
VALID_STATE_CODES = [
    "01", "02", "03", "04", "05", "06", "07", "08", "09", "10",
    "19", "21", "22", "23", "24", "27", "29", "30", "32", "33",
    "36", "37",
]


def gstin_check_digit(first14: str) -> str:
    """Standard GSTIN base-36 alternating-factor checksum."""
    total = 0
    for i, ch in enumerate(first14):
        value = GSTIN_CODE.index(ch)
        product = value * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return GSTIN_CODE[(36 - total % 36) % 36]


def gstin_is_valid(gstin: str) -> bool:
    """Reference validator. src/validation.py (build step 2) must agree with it."""
    if not isinstance(gstin, str) or len(gstin) != 15:
        return False
    if gstin != gstin.upper():
        return False
    if gstin[:2] not in VALID_STATE_CODES:
        return False
    pan = gstin[2:12]
    if not (pan[0:5].isalpha() and pan[5:9].isdigit() and pan[9].isalpha()):
        return False
    if not gstin[12].isalnum() or gstin[13] != "Z":
        return False
    return gstin[14] == gstin_check_digit(gstin[:14])


def make_gstin(rng: random.Random, state_code: str, pan: Optional[str] = None) -> str:
    if pan is None:
        pan = (
            "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(5))
            + "".join(rng.choice("0123456789") for _ in range(4))
            + rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        )
    body = f"{state_code}{pan}1Z"
    return body + gstin_check_digit(body)


def restate_gstin(gstin: str, new_state_code: str) -> str:
    """Same PAN, different state prefix, checksum recomputed so it stays VALID.

    This is what a genuine CGST/SGST-vs-IGST header error looks like: the
    supplier is a real registered entity in another state, not a corrupt string.
    """
    body = new_state_code + gstin[2:14]
    return body + gstin_check_digit(body)


# ---------------------------------------------------------------------------
# 3. Money helpers — all arithmetic in integer paise, formatted at write time
# ---------------------------------------------------------------------------

def rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    return f"{sign}{p // 100}.{p % 100:02d}"


def tax_paise(taxable_paise: int, rate: int) -> int:
    """Round-half-up tax on a taxable value, in paise."""
    return (taxable_paise * rate + 50) // 100


def split_heads(total_tax: int, intra_state: bool) -> Tuple[int, int, int]:
    """Return (cgst, sgst, igst) in paise."""
    if intra_state:
        cgst = total_tax // 2
        return cgst, total_tax - cgst, 0
    return 0, 0, total_tax


# ---------------------------------------------------------------------------
# 4. Vendor and text generation
# ---------------------------------------------------------------------------

NAME_HEAD = [
    "Aditya", "Bharat", "Chandra", "Deccan", "Everest", "Ganga", "Himalaya",
    "Indus", "Jyoti", "Konark", "Lotus", "Meridian", "Narmada", "Orbit",
    "Pinnacle", "Quantum", "Rashtra", "Sagar", "Trident", "Udyog", "Vindhya",
    "Wavelength", "Yamuna", "Zenith", "Arcadia", "Bluestone", "Crestline",
    "Dynamo", "Elmwood", "Falcon",
]
NAME_MID = [
    "Industries", "Enterprises", "Technologies", "Traders", "Logistics",
    "Chemicals", "Textiles", "Engineering", "Polymers", "Packaging",
    "Components", "Solutions", "Fabricators", "Steel", "Agro",
]
NAME_TAIL = ["Private Limited", "Limited", "LLP", "and Company", "India Private Limited"]


@dataclass
class Vendor:
    vendor_id: str
    name: str
    state_code: str
    gstin: str

    @property
    def intra_state(self) -> bool:
        return self.state_code == BUYER_STATE_CODE


def build_vendors(rng: random.Random) -> List[Vendor]:
    """~40% of vendors are in the buyer's own state, so intra-state
    (CGST/SGST) invoices are plentiful enough to seed gstin_header_mismatch."""
    vendors: List[Vendor] = []
    used_names = set()
    for i in range(N_VENDORS):
        while True:
            name = f"{rng.choice(NAME_HEAD)} {rng.choice(NAME_MID)} {rng.choice(NAME_TAIL)}"
            if name not in used_names:
                used_names.add(name)
                break
        state = BUYER_STATE_CODE if i % 5 < 2 else rng.choice(
            [s for s in VALID_STATE_CODES if s != BUYER_STATE_CODE]
        )
        vendors.append(
            Vendor(
                vendor_id=f"V{i + 1:03d}",
                name=name,
                state_code=state,
                gstin=make_gstin(rng, state),
            )
        )
    return vendors


def vendor_name_variant(rng: random.Random, name: str) -> str:
    """A legitimate alternate rendering of the same legal name (§2.2)."""
    out = name
    swaps = [
        ("India Private Limited", "India Pvt. Ltd."),
        ("Private Limited", "Pvt Ltd"),
        ("Limited", "Ltd."),
        ("and Company", "& Co."),
        ("Industries", "Inds."),
        ("Enterprises", "Ent."),
        ("Technologies", "Tech"),
        ("Engineering", "Engg."),
    ]
    for src, dst in swaps:
        if src in out:
            out = out.replace(src, dst)
            break
    style = rng.randrange(4)
    if style == 0:
        out = out.upper()
    elif style == 1:
        out = "M/s " + out
    elif style == 2:
        out = out.replace(" ", "  ")
    else:
        out = out + "."
    return out


def ocr_artifact(rng: random.Random, name: str) -> str:
    """OCR-style corruption of the vendor name (§2.2)."""
    subs = {"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "B": "8", "G": "6"}
    chars = list(name)
    positions = [i for i, c in enumerate(chars) if c in subs]
    rng.shuffle(positions)
    for i in positions[: max(2, len(positions) // 2)]:
        chars[i] = subs[chars[i]]
    out = "".join(chars)
    style = rng.randrange(3)
    if style == 0:
        out = out.replace(" ", "  ", 1)
    elif style == 1:
        cut = rng.randrange(2, max(3, len(out) - 2))
        out = out[:cut] + "," + out[cut:]
    else:
        out = out.rstrip(".") + " ;"
    return out


def garble_invoice_id(rng: random.Random, invoice_id: str,
                      reserved: set) -> str:
    """Transpose two digits — enough to defeat an exact key, not a total loss.

    The result must not land on any invoice number that is already in use.
    A transposition can otherwise coincide with a *different* real invoice,
    which would silently give a no_candidate_found record a phantom candidate
    and corrupt the §2.5 absence cases. `reserved` is mutated with the result.
    """
    chars = list(invoice_id)
    digit_positions = [i for i, c in enumerate(chars) if c.isdigit()]
    tail = digit_positions[-4:]
    for attempt in range(64):
        candidate = list(chars)
        i, j = rng.sample(tail, 2)
        candidate[i], candidate[j] = candidate[j], candidate[i]
        if attempt >= 8 or "".join(candidate) == invoice_id:
            k = rng.choice(tail)
            candidate[k] = str((int(candidate[k]) + 1 + attempt) % 10)
        out = "".join(candidate)
        if out != invoice_id and out not in reserved:
            reserved.add(out)
            return out
    raise AssertionError(f"could not garble {invoice_id} without a collision")


def random_date(rng: random.Random, window: Tuple[date, date]) -> date:
    span = (window[1] - window[0]).days
    return window[0] + timedelta(days=rng.randrange(span + 1))


# ---------------------------------------------------------------------------
# 5. Record structures
# ---------------------------------------------------------------------------

PR_COLUMNS = [
    "record_id", "invoice_id", "vendor_id", "vendor_name", "vendor_gstin",
    "invoice_date", "place_of_supply", "tax_rate", "taxable_value",
    "cgst", "sgst", "igst", "total_tax", "invoice_value",
    "itc_claimed_date", "rule88d_intimation_date", "simulated_current_date",
]

B2_COLUMNS = [
    "record_id", "invoice_id", "vendor_name", "vendor_gstin", "invoice_date",
    "place_of_supply", "tax_rate", "taxable_value", "cgst", "sgst", "igst",
    "total_tax", "invoice_value", "supplier_filing_date", "gstr2b_period",
    "simulated_current_date",
]

GT_COLUMNS = [
    "pr_record_id", "gstr2b_record_id", "invoice_id", "vendor_id",
    "vendor_gstin", "case_type", "defect_family", "match_type",
    "expected_validation_error", "expected_classification", "expected_outcome",
    "amount_delta", "credit_note_amount", "in_prior_period_snapshot",
    "vendor_cumulative_itc_variance", "expected_drc01c_breach",
    "expected_rule88d_within_window", "split", "notes",
]


@dataclass
class PRRecord:
    record_id: str
    invoice_id: str
    vendor_id: str
    vendor_name: str
    vendor_gstin: str
    invoice_date: str
    place_of_supply: str
    tax_rate: str
    taxable_value: str
    cgst: str
    sgst: str
    igst: str
    total_tax: str
    invoice_value: str
    itc_claimed_date: str
    rule88d_intimation_date: str
    simulated_current_date: str

    def row(self) -> List[str]:
        return [getattr(self, c) for c in PR_COLUMNS]


@dataclass
class B2Record:
    record_id: str
    invoice_id: str
    vendor_name: str
    vendor_gstin: str
    invoice_date: str
    place_of_supply: str
    tax_rate: str
    taxable_value: str
    cgst: str
    sgst: str
    igst: str
    total_tax: str
    invoice_value: str
    supplier_filing_date: str
    gstr2b_period: str
    simulated_current_date: str

    def row(self) -> List[str]:
        return [getattr(self, c) for c in B2_COLUMNS]


@dataclass
class GTRow:
    pr_record_id: str
    gstr2b_record_id: str = ""
    invoice_id: str = ""
    vendor_id: str = ""
    vendor_gstin: str = ""
    case_type: str = ""
    defect_family: str = ""
    match_type: str = ""
    expected_validation_error: str = ""
    expected_classification: str = ""
    expected_outcome: str = ""
    amount_delta: str = "0.00"
    credit_note_amount: str = ""
    in_prior_period_snapshot: str = "false"
    vendor_cumulative_itc_variance: str = "0.00"
    expected_drc01c_breach: str = "false"
    expected_rule88d_within_window: str = ""
    split: str = ""
    notes: str = ""

    def row(self) -> List[str]:
        return [getattr(self, c) for c in GT_COLUMNS]


@dataclass
class Dataset:
    purchase_register: List[PRRecord] = field(default_factory=list)
    gstr2b: List[B2Record] = field(default_factory=list)
    prior_period: List[B2Record] = field(default_factory=list)
    ground_truth: List[GTRow] = field(default_factory=list)


@dataclass
class _Invoice:
    """Working representation before the case mutation is applied."""
    case_type: str
    vendor: Vendor
    invoice_id: str
    invoice_date: date
    tax_rate: int
    taxable_paise: int
    total_tax_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int


def _fresh_invoice(rng: random.Random, case_type: str, vendor: Vendor,
                   invoice_id: str) -> _Invoice:
    rate = rng.choice(TAX_RATES)
    taxable = rng.randrange(500_000, 50_000_000, 100)   # Rs.5,000 - Rs.5,00,000
    total_tax = tax_paise(taxable, rate)
    cgst, sgst, igst = split_heads(total_tax, vendor.intra_state)
    return _Invoice(
        case_type=case_type,
        vendor=vendor,
        invoice_id=invoice_id,
        invoice_date=random_date(rng, INVOICE_WINDOW),
        tax_rate=rate,
        taxable_paise=taxable,
        total_tax_paise=total_tax,
        cgst_paise=cgst,
        sgst_paise=sgst,
        igst_paise=igst,
    )


def _pr_from_invoice(inv: _Invoice, record_id: str, itc_date: date,
                     intimation: Optional[date]) -> PRRecord:
    return PRRecord(
        record_id=record_id,
        invoice_id=inv.invoice_id,
        vendor_id=inv.vendor.vendor_id,
        vendor_name=inv.vendor.name,
        vendor_gstin=inv.vendor.gstin,
        invoice_date=inv.invoice_date.isoformat(),
        place_of_supply=f"{BUYER_STATE_CODE}-{BUYER_STATE_NAME}",
        tax_rate=str(inv.tax_rate),
        taxable_value=rupees(inv.taxable_paise),
        cgst=rupees(inv.cgst_paise),
        sgst=rupees(inv.sgst_paise),
        igst=rupees(inv.igst_paise),
        total_tax=rupees(inv.total_tax_paise),
        invoice_value=rupees(inv.taxable_paise + inv.total_tax_paise),
        itc_claimed_date=itc_date.isoformat(),
        rule88d_intimation_date=intimation.isoformat() if intimation else "",
        simulated_current_date=SIMULATED_CURRENT_DATE.isoformat(),
    )


def _b2_from_invoice(inv: _Invoice, filing_date: date, period: str,
                     *, vendor_name: Optional[str] = None,
                     gstin: Optional[str] = None,
                     invoice_id: Optional[str] = None,
                     invoice_date: Optional[date] = None,
                     taxable_paise: Optional[int] = None,
                     heads: Optional[Tuple[int, int, int]] = None) -> B2Record:
    """Build the 2B twin, with per-case overrides. record_id is filled later,
    after the 2B file is shuffled, so row order leaks no pairing information."""
    taxable = inv.taxable_paise if taxable_paise is None else taxable_paise
    if heads is None:
        cgst, sgst, igst = inv.cgst_paise, inv.sgst_paise, inv.igst_paise
    else:
        cgst, sgst, igst = heads
    total = cgst + sgst + igst
    return B2Record(
        record_id="",
        invoice_id=inv.invoice_id if invoice_id is None else invoice_id,
        vendor_name=inv.vendor.name if vendor_name is None else vendor_name,
        vendor_gstin=inv.vendor.gstin if gstin is None else gstin,
        invoice_date=(inv.invoice_date if invoice_date is None else invoice_date).isoformat(),
        place_of_supply=f"{BUYER_STATE_CODE}-{BUYER_STATE_NAME}",
        tax_rate=str(inv.tax_rate),
        taxable_value=rupees(taxable),
        cgst=rupees(cgst),
        sgst=rupees(sgst),
        igst=rupees(igst),
        total_tax=rupees(total),
        invoice_value=rupees(taxable + total),
        supplier_filing_date=filing_date.isoformat(),
        gstr2b_period=period,
        simulated_current_date=SIMULATED_CURRENT_DATE.isoformat(),
    )


# ---------------------------------------------------------------------------
# 6. Dataset construction
# ---------------------------------------------------------------------------

# Outcomes for which a Rule 88D intimation would have been issued (§2.5).
# A clean auto-reconcile record has no mismatch, so no intimation and no window.
INTIMATION_OUTCOMES = {"classified_exception", "indeterminate"}


def _intimation_date(rng: random.Random) -> Tuple[date, bool]:
    """Rule 88D intimation date (§2.5). ~60% land inside the 7-day window."""
    if rng.random() < 0.60:
        elapsed = rng.randrange(0, RULE_88D_WINDOW_DAYS + 1)     # 0..7 -> inside
        return SIMULATED_CURRENT_DATE - timedelta(days=elapsed), True
    elapsed = rng.randrange(RULE_88D_WINDOW_DAYS + 2, 26)        # 9..25 -> outside
    return SIMULATED_CURRENT_DATE - timedelta(days=elapsed), False


def _filing_date(rng: random.Random) -> date:
    """Supplier's GSTR-1 filing date for the April 2026 period."""
    return date(2026, 5, 5) + timedelta(days=rng.randrange(9))


def _apply_quarantine_defect(rng: random.Random, pr: PRRecord, case_type: str) -> str:
    """Corrupt the PR record so §2.1 validation rejects it. Returns a note."""
    if case_type == "quarantine_missing_field":
        which = rng.choice(
            ["invoice_id", "vendor_gstin", "invoice_date", "taxable_value", "tax_heads"]
        )
        if which == "tax_heads":
            pr.cgst = pr.sgst = pr.igst = ""
            return "all of cgst/sgst/igst blank"
        setattr(pr, which, "")
        return f"{which} blank"

    if case_type == "quarantine_bad_gstin":
        style = rng.randrange(4)
        if style == 0:
            pr.vendor_gstin = pr.vendor_gstin[:14]
            return "gstin truncated to 14 characters"
        if style == 1:
            pr.vendor_gstin = pr.vendor_gstin[:14] + (
                "0" if pr.vendor_gstin[14] != "0" else "1"
            )
            return "gstin check digit incorrect"
        if style == 2:
            pr.vendor_gstin = "40" + pr.vendor_gstin[2:]
            return "gstin state-code prefix 40 does not exist"
        pr.vendor_gstin = pr.vendor_gstin[:2] + "12345" + pr.vendor_gstin[7:]
        return "gstin PAN block has digits where letters are required"

    if case_type == "quarantine_bad_date":
        pr.invoice_date = rng.choice(
            ["2026-02-31", "2026-13-05", "31/04/2026", "not-a-date",
             "1899-01-01", "2199-06-30"]
        )
        return f"invoice_date '{pr.invoice_date}' unparseable or implausible"

    if case_type == "quarantine_bad_amount":
        style = rng.randrange(4)
        if style == 0:
            pr.taxable_value = "-" + pr.taxable_value
            return "taxable_value negative"
        if style == 1:
            pr.taxable_value = pr.taxable_value.replace(".", ",") + "abc"
            return "taxable_value not numeric"
        if style == 2:
            pr.taxable_value = "N/A"
            return "taxable_value is 'N/A'"
        head = "cgst" if pr.cgst != "0.00" else "igst"
        setattr(pr, head, "-" + getattr(pr, head))
        return f"{head} negative"

    raise AssertionError(f"unknown quarantine case {case_type}")


def build_dataset(seed: int = SEED) -> Dataset:
    """Deterministic end-to-end construction. Same seed -> byte-identical files."""
    rng = random.Random(seed)
    vendors = build_vendors(rng)
    intra_vendors = [v for v in vendors if v.intra_state]
    inter_state_codes = [s for s in VALID_STATE_CODES if s != BUYER_STATE_CODE]
    assert intra_vendors, "need intra-state vendors to seed gstin_header_mismatch"

    ds = Dataset()

    # Every invoice number this batch will ever use. Garbled numbers (the
    # indeterminate case and the 2B decoys) must avoid all of them, so a
    # corruption can never coincide with a different real invoice.
    reserved_invoice_ids = {f"INV-2604-{i:05d}" for i in range(1, N_PURCHASE_RECORDS + 1)}
    reserved_invoice_ids |= {f"INV-2604-9{k:04d}" for k in range(N_UNRELATED_2B_ONLY)}
    reserved_invoice_ids |= {f"INV-2603-{k + 1:05d}" for k in range(N_PRIOR_PERIOD_NOISE)}

    # Case order is shuffled so record_ids are not grouped by defect type.
    plan: List[str] = []
    for case_type, count in DEFECT_PLAN.items():
        plan.extend([case_type] * count)
    rng.shuffle(plan)

    # 2B rows are collected with their pairing key, then shuffled and id-stamped
    # as a block, so 2B row order leaks no pairing information.
    pending_2b: List[Tuple[Optional[str], B2Record]] = []
    clean_twins: List[Tuple[_Invoice, B2Record]] = []      # decoy source material
    # ITC variance per supplier GSTIN, in paise, for the DRC-01C check (§2.5).
    variance_by_gstin: Dict[str, int] = defaultdict(int)

    for idx, case_type in enumerate(plan, start=1):
        pr_id = f"PR-{idx:04d}"
        invoice_id = f"INV-2604-{idx:05d}"
        vendor = (rng.choice(intra_vendors) if case_type == "gstin_header_mismatch"
                  else rng.choice(vendors))
        inv = _fresh_invoice(rng, case_type, vendor, invoice_id)

        itc_date = date(2026, 5, 18) + timedelta(days=rng.randrange(3))
        if EXPECTED_OUTCOME[case_type] in INTIMATION_OUTCOMES:
            intimation, within_window = _intimation_date(rng)
        else:
            intimation, within_window = None, None

        pr = _pr_from_invoice(inv, pr_id, itc_date, intimation)

        gt = GTRow(
            pr_record_id=pr_id,
            invoice_id=invoice_id,
            vendor_id=vendor.vendor_id,
            vendor_gstin=vendor.gstin,
            case_type=case_type,
            defect_family=DEFECT_FAMILY[case_type],
            match_type=MATCH_TYPE[case_type],
            expected_validation_error=EXPECTED_VALIDATION_ERROR.get(case_type, ""),
            expected_classification=EXPECTED_CLASSIFICATION[case_type],
            expected_outcome=EXPECTED_OUTCOME[case_type],
            expected_rule88d_within_window=(
                "" if within_window is None else str(within_window).lower()
            ),
        )

        twin: Optional[B2Record] = None
        delta_paise = 0

        # ---------------- clean -------------------------------------------
        if case_type == "clean_exact_match":
            twin = _b2_from_invoice(inv, _filing_date(rng), RETURN_PERIOD)
            gt.notes = "all comparison fields identical"
            clean_twins.append((inv, twin))

        # ---------------- fuzzy (§2.3) ------------------------------------
        elif case_type == "fuzzy_vendor_name_variant":
            alt = vendor_name_variant(rng, vendor.name)
            twin = _b2_from_invoice(inv, _filing_date(rng), RETURN_PERIOD,
                                    vendor_name=alt)
            gt.notes = f"2B vendor_name rendered as '{alt}'"

        elif case_type == "fuzzy_ocr_artifact":
            alt = ocr_artifact(rng, vendor.name)
            twin = _b2_from_invoice(inv, _filing_date(rng), RETURN_PERIOD,
                                    vendor_name=alt)
            gt.notes = f"2B vendor_name OCR-corrupted to '{alt}'"

        elif case_type == "fuzzy_date_tolerance":
            shift = rng.choice([-2, -1, 1, 2])
            twin = _b2_from_invoice(inv, _filing_date(rng), RETURN_PERIOD,
                                    invoice_date=inv.invoice_date + timedelta(days=shift))
            gt.notes = f"2B invoice_date shifted by {shift:+d} day(s)"

        elif case_type == "fuzzy_amount_rounding":
            # Sub-rupee difference on the taxable value only; tax heads are
            # copied unchanged so the whole invoice stays within Rs.1 (§2.3).
            delta_paise = rng.choice([-1, 1]) * rng.randrange(1, 100)
            twin = _b2_from_invoice(inv, _filing_date(rng), RETURN_PERIOD,
                                    taxable_paise=inv.taxable_paise + delta_paise)
            gt.notes = f"taxable_value differs by Rs.{rupees(abs(delta_paise))} (within Rs.1)"

        # ---------------- classification rules (§2.5) ----------------------
        elif case_type == "gstin_header_mismatch":
            # Supplier filed under a different state registration: PR booked
            # CGST+SGST (intra-state), 2B shows IGST. Total tax is identical.
            other_state = rng.choice(inter_state_codes)
            twin = _b2_from_invoice(
                inv, _filing_date(rng), RETURN_PERIOD,
                gstin=restate_gstin(vendor.gstin, other_state),
                heads=(0, 0, inv.total_tax_paise),
            )
            gt.notes = (f"PR CGST/SGST under state {BUYER_STATE_CODE}; "
                        f"2B IGST under state {other_state}; total tax equal")

        elif case_type == "credit_note_netting":
            # 2B is net of a credit note the register has not yet absorbed.
            credit = rng.choice([100_000, 250_000, 500_000, 1_000_000,
                                 inv.taxable_paise // 10 // 100 * 100])
            credit = max(100_000, min(credit, inv.taxable_paise - 100_000))
            new_taxable = inv.taxable_paise - credit
            new_total = tax_paise(new_taxable, inv.tax_rate)
            twin = _b2_from_invoice(
                inv, _filing_date(rng), RETURN_PERIOD,
                taxable_paise=new_taxable,
                heads=split_heads(new_total, vendor.intra_state),
            )
            delta_paise = -credit
            gt.credit_note_amount = rupees(credit)
            gt.notes = f"2B net of credit note Rs.{rupees(credit)}"

        # ---------------- absence (§2.3 no_candidate_found -> §2.5) --------
        elif case_type == "late_filed_supplier":
            # No 2B row now, and none in the prior period either: the supplier
            # simply has not filed. Disambiguation signal = absent from snapshot.
            gt.in_prior_period_snapshot = "false"
            gt.notes = "absent from current 2B and from the prior-period snapshot"

        elif case_type == "invoice_removed_post_claim":
            # Present in the prior-period snapshot, gone from the current 2B:
            # the supplier withdrew or amended it after ITC was claimed.
            snapshot = _b2_from_invoice(inv, date(2026, 4, 9) + timedelta(days=rng.randrange(5)),
                                        PRIOR_PERIOD)
            snapshot.record_id = f"P2B-X{idx:04d}"
            ds.prior_period.append(snapshot)
            gt.in_prior_period_snapshot = "true"
            gt.notes = "present in prior-period snapshot, absent from current 2B"

        # ---------------- ambiguous (§2.6 indeterminate) -------------------
        elif case_type == "indeterminate_ambiguous":
            # A weak candidate: same supplier GSTIN, but the invoice number is
            # garbled, the date is well outside tolerance, the name is degraded
            # and the amount delta matches no credit-note pattern. Enough signal
            # to score, not enough for any rule to fire confidently.
            odd_delta = -(rng.randrange(30_000, 900_000) // 7 * 7 + 13)
            new_taxable = inv.taxable_paise + odd_delta
            new_total = tax_paise(new_taxable, inv.tax_rate)
            twin = _b2_from_invoice(
                inv, _filing_date(rng), RETURN_PERIOD,
                invoice_id=garble_invoice_id(rng, invoice_id, reserved_invoice_ids),
                vendor_name=ocr_artifact(rng, vendor_name_variant(rng, vendor.name)),
                invoice_date=inv.invoice_date + timedelta(days=rng.randrange(5, 10)),
                taxable_paise=new_taxable,
                heads=split_heads(new_total, vendor.intra_state),
            )
            delta_paise = odd_delta
            gt.notes = ("invoice number garbled, date beyond tolerance, "
                        "non-round amount delta - no rule fires confidently")

        # ---------------- quarantine (§2.1 / §3.3) -------------------------
        elif case_type in QUARANTINE_CASES:
            # The invoice is genuine; the register entry is not. The supplier's
            # 2B row still exists and will be left unassigned by the matcher.
            twin = _b2_from_invoice(inv, _filing_date(rng), RETURN_PERIOD)
            gt.notes = _apply_quarantine_defect(rng, pr, case_type)

        else:
            raise AssertionError(f"unhandled case type {case_type}")

        ds.purchase_register.append(pr)
        ds.ground_truth.append(gt)
        gt.amount_delta = rupees(delta_paise)

        if twin is not None:
            pending_2b.append((pr_id, twin))

        # DRC-01C cumulative ITC variance (§2.5). Quarantined records are never
        # scored, so they contribute nothing. A missing invoice puts the whole
        # claimed ITC at risk.
        if case_type not in QUARANTINE_CASES:
            if case_type in NO_CANDIDATE_CASES:
                variance_by_gstin[vendor.gstin] += inv.total_tax_paise
            elif twin is not None:
                twin_total = int(round(float(twin.total_tax) * 100))
                variance_by_gstin[vendor.gstin] += abs(inv.total_tax_paise - twin_total)

    ds.ground_truth.sort(key=lambda g: g.pr_record_id)
    return _finalise(ds, rng, pending_2b, clean_twins, vendors, variance_by_gstin,
                     reserved_invoice_ids)


def _finalise(ds: Dataset, rng: random.Random,
              pending_2b: List[Tuple[Optional[str], B2Record]],
              clean_twins: List[Tuple[_Invoice, B2Record]],
              vendors: List[Vendor],
              variance_by_gstin: Dict[str, int],
              reserved_invoice_ids: set) -> Dataset:
    """Add unpaired 2B rows and prior-period noise, stamp ids, attach the
    DRC-01C aggregate and the §2.6 calibration/frozen split."""

    # --- 2B rows with no purchase-register counterpart ----------------------
    # (a) Near-duplicate decoys. Each is a close clone of a real 2B row: same
    #     supplier and date, invoice number one transposition away, amount Rs.2
    #     off. The §2.3 greedy one-to-one assignment must pair the PR record
    #     with the true row and leave the decoy unassigned. tests/test_matcher.py
    #     (build step 4) asserts exactly this.
    decoy_sources = rng.sample(clean_twins, N_DECOY_NEAR_DUPLICATES)
    for inv, real in decoy_sources:
        bumped = inv.taxable_paise + 200
        decoy = _b2_from_invoice(
            inv, date.fromisoformat(real.supplier_filing_date), RETURN_PERIOD,
            invoice_id=garble_invoice_id(rng, inv.invoice_id, reserved_invoice_ids),
            taxable_paise=bumped,
        )
        pending_2b.append((None, decoy))

    # (b) Supplier filed an invoice we never booked at all.
    for k in range(N_UNRELATED_2B_ONLY):
        vendor = rng.choice(vendors)
        inv = _fresh_invoice(rng, "gstr2b_only", vendor, f"INV-2604-9{k:04d}")
        pending_2b.append((None, _b2_from_invoice(inv, _filing_date(rng), RETURN_PERIOD)))

    # --- shuffle, then stamp ids so row order leaks no pairing --------------
    rng.shuffle(pending_2b)
    pr_to_2b: Dict[str, str] = {}
    for i, (pr_id, row) in enumerate(pending_2b, start=1):
        row.record_id = f"2B-{i:04d}"
        if pr_id is not None:
            pr_to_2b[pr_id] = row.record_id
        ds.gstr2b.append(row)

    # --- prior-period snapshot (§2.5): removed invoices + unrelated noise ---
    for k in range(N_PRIOR_PERIOD_NOISE):
        vendor = rng.choice(vendors)
        inv = _fresh_invoice(rng, "prior_noise", vendor, f"INV-2603-{k + 1:05d}")
        inv.invoice_date = random_date(rng, PRIOR_WINDOW)
        row = _b2_from_invoice(inv, date(2026, 4, 8) + timedelta(days=rng.randrange(6)),
                               PRIOR_PERIOD)
        ds.prior_period.append(row)
    rng.shuffle(ds.prior_period)
    for i, row in enumerate(ds.prior_period, start=1):
        row.record_id = f"P2B-{i:04d}"

    # --- attach pairing + DRC-01C aggregate to ground truth ----------------
    threshold_paise = int(round(DRC01C_THRESHOLD_RUPEES * 100))
    for gt in ds.ground_truth:
        gt.gstr2b_record_id = pr_to_2b.get(gt.pr_record_id, "")
        total = variance_by_gstin.get(gt.vendor_gstin, 0)
        gt.vendor_cumulative_itc_variance = rupees(total)
        gt.expected_drc01c_breach = str(total > threshold_paise).lower()

    # --- §2.6 calibration / frozen-test split, stratified by case type -----
    # Decided here, at generation time, "before any tuning". src/confidence.py
    # (build step 7) reads this column; it never re-derives the split.
    by_case: Dict[str, List[GTRow]] = defaultdict(list)
    for gt in ds.ground_truth:
        by_case[gt.case_type].append(gt)
    for case_type in sorted(by_case):
        rows = by_case[case_type]
        order = list(range(len(rows)))
        rng.shuffle(order)
        n_cal = int(round(len(rows) * CALIBRATION_FRACTION))
        for rank, i in enumerate(order):
            rows[i].split = "calibration" if rank < n_cal else "frozen_test"

    return ds


# ---------------------------------------------------------------------------
# 7. Rendering
# ---------------------------------------------------------------------------

DATA_DIR = os.path.dirname(os.path.abspath(__file__))

FILES = {
    "purchase_register.csv": "purchase_register",
    "gstr2b.csv": "gstr2b",
    "gstr2b_prior_period.csv": "prior_period",
    "ground_truth.csv": "ground_truth",
}
HEADERS = {
    "purchase_register": PR_COLUMNS,
    "gstr2b": B2_COLUMNS,
    "prior_period": B2_COLUMNS,
    "ground_truth": GT_COLUMNS,
}


def render_csv(rows, header: List[str]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for r in rows:
        writer.writerow(r.row())
    return buf.getvalue()


def render_all(ds: Dataset) -> Dict[str, str]:
    return {
        filename: render_csv(getattr(ds, attr), HEADERS[attr])
        for filename, attr in FILES.items()
    }


def write_all(rendered: Dict[str, str], out_dir: str = DATA_DIR) -> None:
    for filename, text in rendered.items():
        with open(os.path.join(out_dir, filename), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


# ---------------------------------------------------------------------------
# 8. Sanity checks
# ---------------------------------------------------------------------------

class CheckReport:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool, str]] = []

    def check(self, cid: str, ok: bool, detail: str = "") -> None:
        self.results.append((cid, bool(ok), detail))

    @property
    def failures(self) -> List[Tuple[str, bool, str]]:
        return [r for r in self.results if not r[1]]

    def render(self) -> str:
        lines = []
        for cid, ok, detail in self.results:
            lines.append(f"  [{'PASS' if ok else 'FAIL'}] {cid}" + (f" — {detail}" if detail else ""))
        lines.append("")
        lines.append(f"  {len(self.results) - len(self.failures)}/{len(self.results)} checks passed")
        return "\n".join(lines)


def _num(text: str) -> float:
    return float(text)


def _parse_iso(text: str) -> Optional[date]:
    try:
        return date.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def reference_validate(pr: Dict[str, str]) -> Optional[str]:
    """§2.1 reference implementation, in the order the architecture specifies.

    This exists so Part 1 can prove the quarantine cases are genuinely invalid
    and the other 480 records are genuinely valid. src/validation.py (build
    step 2) is the production implementation and must agree with this on every
    record in the dataset.
    """
    required = ["invoice_id", "vendor_gstin", "invoice_date", "taxable_value"]
    for fld in required:
        if not (pr.get(fld) or "").strip():
            return "missing_required_field"
    if not any((pr.get(h) or "").strip() for h in ("cgst", "sgst", "igst")):
        return "missing_required_field"

    if not gstin_is_valid(pr["vendor_gstin"]):
        return "invalid_gstin_format"

    parsed = _parse_iso(pr["invoice_date"])
    if parsed is None or not (date(2017, 7, 1) <= parsed <= date(2030, 12, 31)):
        return "unparseable_or_implausible_date"

    for fld in ("taxable_value", "cgst", "sgst", "igst", "total_tax", "invoice_value"):
        raw = (pr.get(fld) or "").strip()
        if raw == "":
            continue
        try:
            if float(raw) < 0:
                return "non_numeric_or_negative_amount"
        except ValueError:
            return "non_numeric_or_negative_amount"
    return None


def run_checks(pr_rows: List[Dict[str, str]], b2_rows: List[Dict[str, str]],
               prior_rows: List[Dict[str, str]], gt_rows: List[Dict[str, str]],
               *, seed: int, rendered: Optional[Dict[str, str]] = None) -> CheckReport:
    """Sanity checks over the generated dataset, keyed to Architecture v2."""
    rep = CheckReport()
    pr_by_id = {r["record_id"]: r for r in pr_rows}
    b2_by_id = {r["record_id"]: r for r in b2_rows}
    gt_by_pr = {r["pr_record_id"]: r for r in gt_rows}
    b2_invoice_ids = {r["invoice_id"] for r in b2_rows}
    prior_invoice_ids = {r["invoice_id"] for r in prior_rows}
    by_case: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for g in gt_rows:
        by_case[g["case_type"]].append(g)

    # --- C: structure -------------------------------------------------------
    rep.check("C1 record counts",
              len(pr_rows) == N_PURCHASE_RECORDS
              and len(gt_rows) == N_PURCHASE_RECORDS
              and len(b2_rows) == sum(
                  DEFECT_PLAN[c] for c in DEFECT_PLAN if c not in NO_CANDIDATE_CASES
              ) + N_DECOY_NEAR_DUPLICATES + N_UNRELATED_2B_ONLY
              and len(prior_rows) == DEFECT_PLAN["invoice_removed_post_claim"] + N_PRIOR_PERIOD_NOISE,
              f"PR={len(pr_rows)} 2B={len(b2_rows)} prior={len(prior_rows)} GT={len(gt_rows)}")

    rep.check("C2 record_ids unique",
              len(pr_by_id) == len(pr_rows) and len(b2_by_id) == len(b2_rows)
              and len({r['record_id'] for r in prior_rows}) == len(prior_rows))

    rep.check("C3 invoice_id unique in purchase register",
              len({r["invoice_id"] for r in pr_rows if r["invoice_id"]})
              == len([r for r in pr_rows if r["invoice_id"]]))

    rep.check("C4 ground truth is one row per PR record, no orphans",
              set(gt_by_pr) == set(pr_by_id) and len(gt_by_pr) == len(gt_rows))

    rep.check("C5 case-type counts match DEFECT_PLAN",
              {c: len(v) for c, v in by_case.items()} == dict(DEFECT_PLAN))

    rep.check("C6 no label columns leaked into pipeline inputs",
              not (set(PR_COLUMNS) & set(GT_COLUMNS) - {"invoice_id", "vendor_id",
                                                        "vendor_gstin"})
              and "case_type" not in PR_COLUMNS and "case_type" not in B2_COLUMNS)

    # --- M: one-to-one property (§2.3) --------------------------------------
    claimed = [g["gstr2b_record_id"] for g in gt_rows if g["gstr2b_record_id"]]
    rep.check("M1 ground truth pairing is one-to-one",
              len(claimed) == len(set(claimed))
              and all(x in b2_by_id for x in claimed),
              f"{len(claimed)} paired 2B rows, all distinct and resolvable")

    unclaimed = set(b2_by_id) - set(claimed)
    rep.check("M2 unpaired 2B rows exist (matcher must leave them unassigned)",
              len(unclaimed) == N_DECOY_NEAR_DUPLICATES + N_UNRELATED_2B_ONLY,
              f"{len(unclaimed)} unpaired 2B rows")

    # A decoy shares supplier GSTIN + invoice date with a real paired row.
    decoy_like = 0
    paired_keys = {(b2_by_id[c]["vendor_gstin"], b2_by_id[c]["invoice_date"])
                   for c in claimed}
    for rid in unclaimed:
        row = b2_by_id[rid]
        if (row["vendor_gstin"], row["invoice_date"]) in paired_keys:
            decoy_like += 1
    rep.check("M3 near-duplicate decoys present",
              decoy_like >= N_DECOY_NEAR_DUPLICATES,
              f"{decoy_like} unpaired 2B rows collide with a paired row on (gstin, date)")

    # --- V: validation / quarantine (§2.1, §3.3) ----------------------------
    should_pass = [r for r in pr_rows
                   if gt_by_pr[r["record_id"]]["expected_outcome"] != "quarantine"]
    bad_passers = [r["record_id"] for r in should_pass if reference_validate(r) is not None]
    rep.check("V1 every non-quarantine record passes §2.1 validation",
              not bad_passers, f"{len(should_pass)} records checked; offenders={bad_passers[:5]}")

    quarantined = [r for r in pr_rows
                   if gt_by_pr[r["record_id"]]["expected_outcome"] == "quarantine"]
    mislabelled = []
    for r in quarantined:
        err = reference_validate(r)
        expected = gt_by_pr[r["record_id"]]["expected_validation_error"]
        if err != expected:
            mislabelled.append((r["record_id"], expected, err))
    rep.check("V2 every quarantine record fails with its labelled error",
              not mislabelled,
              f"{len(quarantined)} records checked; mismatches={mislabelled[:5]}")

    rep.check("V3 quarantine share is a reportable minority (§2.1)",
              0 < len(quarantined) <= N_PURCHASE_RECORDS * 0.10,
              f"{len(quarantined)} records = {100 * len(quarantined) / len(pr_rows):.1f}% of batch")

    rep.check("V4 all valid GSTINs pass the checksum, in both source files",
              all(gstin_is_valid(r["vendor_gstin"]) for r in should_pass)
              and all(gstin_is_valid(r["vendor_gstin"]) for r in b2_rows)
              and all(gstin_is_valid(r["vendor_gstin"]) for r in prior_rows))

    # --- A: arithmetic ------------------------------------------------------
    arith_bad = []
    for r in should_pass + b2_rows + prior_rows:
        heads = _num(r["cgst"]) + _num(r["sgst"]) + _num(r["igst"])
        if abs(heads - _num(r["total_tax"])) > 0.005:
            arith_bad.append((r["record_id"], "heads != total_tax"))
        elif abs(_num(r["taxable_value"]) + _num(r["total_tax"])
                 - _num(r["invoice_value"])) > 0.005:
            arith_bad.append((r["record_id"], "taxable + tax != invoice_value"))
    rep.check("A1 tax arithmetic holds on every valid row",
              not arith_bad, f"offenders={arith_bad[:5]}")

    rep.check("A2 exactly one tax regime per row (CGST+SGST xor IGST)",
              all((_num(r["igst"]) > 0) != (_num(r["cgst"]) > 0)
                  for r in b2_rows + prior_rows))

    # --- D: defect invariants ----------------------------------------------
    def pair(g):
        return pr_by_id[g["pr_record_id"]], b2_by_id[g["gstr2b_record_id"]]

    ok = True
    for g in by_case["clean_exact_match"]:
        p, b = pair(g)
        if not all(p[f] == b[f] for f in ("invoice_id", "vendor_name", "vendor_gstin",
                                          "invoice_date", "taxable_value", "cgst",
                                          "sgst", "igst", "total_tax")):
            ok = False
            break
    rep.check("D1 clean_exact_match rows are identical on every compared field", ok,
              f"{len(by_case['clean_exact_match'])} rows")

    ok = all(pair(g)[0]["vendor_name"] != pair(g)[1]["vendor_name"]
             and all(pair(g)[0][f] == pair(g)[1][f]
                     for f in ("invoice_id", "vendor_gstin", "invoice_date",
                               "taxable_value", "total_tax"))
             for g in by_case["fuzzy_vendor_name_variant"] + by_case["fuzzy_ocr_artifact"])
    rep.check("D2 vendor-name fuzz differs only in the name", ok)

    ok = True
    for g in by_case["fuzzy_date_tolerance"]:
        p, b = pair(g)
        d = abs((date.fromisoformat(p["invoice_date"]) - date.fromisoformat(b["invoice_date"])).days)
        if not (1 <= d <= 2) or p["taxable_value"] != b["taxable_value"]:
            ok = False
            break
    rep.check("D3 fuzzy_date_tolerance is 1-2 days, amounts unchanged", ok)

    ok = True
    for g in by_case["fuzzy_amount_rounding"]:
        p, b = pair(g)
        d = abs(_num(p["invoice_value"]) - _num(b["invoice_value"]))
        if not (0 < d <= 1.0) or p["invoice_date"] != b["invoice_date"]:
            ok = False
            break
    rep.check("D4 fuzzy_amount_rounding stays within Rs.1 (§2.3)", ok)

    ok = True
    for g in by_case["gstin_header_mismatch"]:
        p, b = pair(g)
        if (p["vendor_gstin"][:2] == b["vendor_gstin"][:2]
                or p["vendor_gstin"][2:14] != b["vendor_gstin"][2:14]
                or not (_num(p["cgst"]) > 0 and _num(p["igst"]) == 0)
                or not (_num(b["igst"]) > 0 and _num(b["cgst"]) == 0)
                or abs(_num(p["total_tax"]) - _num(b["total_tax"])) > 0.005):
            ok = False
            break
    rep.check("D5 gstin_header_mismatch flips the tax heads, same PAN, same total tax", ok,
              f"{len(by_case['gstin_header_mismatch'])} rows")

    ok = True
    for g in by_case["credit_note_netting"]:
        p, b = pair(g)
        delta = _num(p["taxable_value"]) - _num(b["taxable_value"])
        if delta <= 1.0 or abs(delta - _num(g["credit_note_amount"])) > 0.005:
            ok = False
            break
    rep.check("D6 credit_note_netting: 2B is lower by the labelled credit note", ok)

    rep.check("D7 late_filed_supplier absent from current 2B AND prior snapshot (§2.5)",
              all(not g["gstr2b_record_id"]
                  and g["invoice_id"] not in b2_invoice_ids
                  and g["invoice_id"] not in prior_invoice_ids
                  and g["in_prior_period_snapshot"] == "false"
                  for g in by_case["late_filed_supplier"]),
              f"{len(by_case['late_filed_supplier'])} rows")

    rep.check("D8 invoice_removed_post_claim absent from 2B but PRESENT in snapshot (§2.5)",
              all(not g["gstr2b_record_id"]
                  and g["invoice_id"] not in b2_invoice_ids
                  and g["invoice_id"] in prior_invoice_ids
                  and g["in_prior_period_snapshot"] == "true"
                  for g in by_case["invoice_removed_post_claim"]),
              f"{len(by_case['invoice_removed_post_claim'])} rows")

    ok = True
    for g in by_case["indeterminate_ambiguous"]:
        p, b = pair(g)
        d = abs((date.fromisoformat(p["invoice_date"]) - date.fromisoformat(b["invoice_date"])).days)
        delta = abs(_num(p["taxable_value"]) - _num(b["taxable_value"]))
        if (p["invoice_id"] == b["invoice_id"] or d < 5 or delta <= 1.0
                or p["vendor_gstin"] != b["vendor_gstin"] or delta % 100 == 0):
            ok = False
            break
    rep.check("D9 indeterminate rows are weak candidates matching no rule pattern", ok,
              f"{len(by_case['indeterminate_ambiguous'])} rows")

    # --- O: operational checks (§2.5) ---------------------------------------
    same_scd = ({r["simulated_current_date"] for r in pr_rows}
                | {r["simulated_current_date"] for r in b2_rows}
                | {r["simulated_current_date"] for r in prior_rows})
    rep.check("O1 simulated_current_date present and identical across all source CSVs (§3.1)",
              same_scd == {SIMULATED_CURRENT_DATE.isoformat()}, str(sorted(same_scd)))

    inside = outside = 0
    w_bad = []
    for r in pr_rows:
        g = gt_by_pr[r["record_id"]]
        raw = r["rule88d_intimation_date"]
        if not raw:
            if g["expected_rule88d_within_window"]:
                w_bad.append(r["record_id"])
            continue
        elapsed = (SIMULATED_CURRENT_DATE - date.fromisoformat(raw)).days
        actual = elapsed <= RULE_88D_WINDOW_DAYS
        if str(actual).lower() != g["expected_rule88d_within_window"]:
            w_bad.append(r["record_id"])
        inside += actual
        outside += not actual
    rep.check("O2 Rule 88D window label agrees with the dates, both sides populated",
              not w_bad and inside > 0 and outside > 0,
              f"inside={inside} outside={outside} offenders={w_bad[:5]}")

    rep.check("O3 no Rule 88D intimation on records expected to auto-reconcile",
              all(not pr_by_id[g["pr_record_id"]]["rule88d_intimation_date"]
                  for g in gt_rows if g["expected_outcome"] in ("auto_reconcile", "quarantine")))

    breaching = {g["vendor_gstin"] for g in gt_rows if g["expected_drc01c_breach"] == "true"}
    all_vendors = {g["vendor_gstin"] for g in gt_rows}
    rep.check(f"O4 DRC-01C threshold (Rs.{DRC01C_THRESHOLD_RUPEES:,.2f}) separates vendors",
              3 <= len(breaching) <= len(all_vendors) - 5,
              f"{len(breaching)} of {len(all_vendors)} supplier GSTINs breach")

    # --- S: calibration split (§2.6) ---------------------------------------
    cal = [g for g in gt_rows if g["split"] == "calibration"]
    frozen = [g for g in gt_rows if g["split"] == "frozen_test"]
    rep.check("S1 split is 70/30 and covers every record",
              len(cal) + len(frozen) == len(gt_rows)
              and abs(len(cal) / len(gt_rows) - CALIBRATION_FRACTION) < 0.01,
              f"calibration={len(cal)} frozen_test={len(frozen)}")

    missing_strata = [c for c in DEFECT_PLAN
                      if not any(g["split"] == "calibration" for g in by_case[c])
                      or not any(g["split"] == "frozen_test" for g in by_case[c])]
    rep.check("S2 every case type appears in both splits (stratified)",
              not missing_strata, f"missing={missing_strata}")

    # --- R: reproducibility -------------------------------------------------
    if rendered is not None:
        again = render_all(build_dataset(seed))
        digests_a = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in rendered.items()}
        digests_b = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in again.items()}
        rep.check(f"R1 regeneration under seed {seed} is byte-identical",
                  digests_a == digests_b,
                  " ".join(f"{k}={v[:12]}" for k, v in sorted(digests_a.items())))
    return rep


# ---------------------------------------------------------------------------
# 9. CLI
# ---------------------------------------------------------------------------

def load_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def summarise(gt_rows: List[Dict[str, str]]) -> str:
    by_case = Counter(g["case_type"] for g in gt_rows)
    by_outcome = Counter(g["expected_outcome"] for g in gt_rows)
    by_family = Counter(g["defect_family"] for g in gt_rows)
    total = len(gt_rows)
    lines = ["  Expected gate outcome (§2.6) + quarantine exit (§2.1):"]
    for k in ("auto_reconcile", "classified_exception", "indeterminate", "quarantine"):
        lines.append(f"    {k:<22} {by_outcome[k]:>4}  ({100 * by_outcome[k] / total:5.1f}%)")
    lines.append("")
    lines.append("  Defect family:")
    for k, v in sorted(by_family.items()):
        lines.append(f"    {k:<26} {v:>4}")
    lines.append("")
    lines.append("  Case type:")
    for k in DEFECT_PLAN:
        lines.append(f"    {k:<30} {by_case[k]:>4}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Exception Ledger synthetic dataset generator")
    ap.add_argument("--seed", type=int, default=SEED, help=f"RNG seed (default {SEED})")
    ap.add_argument("--verify", action="store_true",
                    help="only re-run the sanity checks against the files on disk")
    ap.add_argument("--out", default=DATA_DIR, help="output directory")
    args = ap.parse_args(argv)

    if args.verify:
        pr = load_csv(os.path.join(args.out, "purchase_register.csv"))
        b2 = load_csv(os.path.join(args.out, "gstr2b.csv"))
        prior = load_csv(os.path.join(args.out, "gstr2b_prior_period.csv"))
        gt = load_csv(os.path.join(args.out, "ground_truth.csv"))
        rendered = None
        print(f"Verifying dataset in {args.out} (seed {args.seed})\n")
    else:
        ds = build_dataset(args.seed)
        rendered = render_all(ds)
        write_all(rendered, args.out)
        pr = list(csv.DictReader(io.StringIO(rendered["purchase_register.csv"])))
        b2 = list(csv.DictReader(io.StringIO(rendered["gstr2b.csv"])))
        prior = list(csv.DictReader(io.StringIO(rendered["gstr2b_prior_period.csv"])))
        gt = list(csv.DictReader(io.StringIO(rendered["ground_truth.csv"])))
        print(f"Exception Ledger — dataset generated (seed {args.seed})\n")
        for filename in FILES:
            path = os.path.join(args.out, filename)
            n = sum(1 for _ in open(path, encoding="utf-8")) - 1
            print(f"  wrote {filename:<28} {n:>4} rows   "
                  f"sha256={hashlib.sha256(rendered[filename].encode()).hexdigest()[:16]}")
        print()

    print(summarise(gt))
    print("\nSanity checks (Architecture v2 references in brackets):\n")
    rep = run_checks(pr, b2, prior, gt, seed=args.seed, rendered=rendered)
    print(rep.render())
    if rep.failures:
        print(f"\n{len(rep.failures)} CHECK(S) FAILED")
        return 1
    print("\nAll sanity checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
