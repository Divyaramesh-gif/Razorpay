# Exception Ledger

GST purchase-register vs GSTR-2B reconciliation with a calibrated confidence gate.

The system architecture is **locked** at `Architecture_v2_FINAL.pdf`. That document
is the source of truth; this README only restates it and records build progress.
Section references below (§) point into it.

## Status

| Build step (Architecture §5) | Component | Status |
|---|---|---|
| 1 | `data/generate_dataset.py` | **Done** — Part 1 |
| 2 | `src/validation.py` + `src/quarantine_log.py` | **Done** — Phase 2 |
| 3 | `src/normalization.py` | **Done** — Phase 2 |
| 4 | `src/matcher.py` + `tests/test_matcher.py` | **Done** — Phase 3 |
| 5 | `src/evidence.py` | **Done** — Phase 3 |
| 6 | `src/rule_engine.py` + `src/rules/rules_v2026_04.yaml` | **Done** — Phase 3 |
| 7 | `src/confidence.py` + `src/gate.py` | Not started |
| 8 | `src/audit_log.py` + `src/report.py` + `src/pipeline.py` | Not started |

## Pipeline (§1)

```
purchase_register.csv + gstr2b.csv
        |
        v
  input validation ----[FAILS]---> quarantine log   (exits; never scored)
        |
     [PASSES]
        v
  deterministic + AI-assisted normalisation
        v
  exact + fuzzy ONE-TO-ONE matching
        v
  field-by-field evidence comparison
        v
  versioned GST rules + operational checks
        v
  calibrated confidence gate
        |
   +----+--------------+------------------+
   v                   v                  v
auto-reconcile   classified exception   indeterminate -> human review
   +----+--------------+------------------+
        v
  audit log + evaluation report
```

Two exit paths, kept separate: **quarantine** (a data-quality problem, never
scored) and the **three-way gate outcome** (valid input, scored). A quarantined
record is never folded into the exception or indeterminate counts.

## Part 1 — synthetic dataset generator

```bash
python3 data/generate_dataset.py            # generate + run all sanity checks
python3 data/generate_dataset.py --verify   # re-check the files on disk
```

Stdlib only, Python 3.9+. Seed `20260401`. Regeneration is byte-identical
(asserted by check R1).

### Files produced

| File | Rows | Purpose |
|---|---|---|
| `data/purchase_register.csv` | 500 | Buyer-side register — pipeline input |
| `data/gstr2b.csv` | 490 | Synthetic GSTR-2B, period 2026-04 — pipeline input |
| `data/gstr2b_prior_period.csv` | 75 | Prior-period snapshot, period 2026-03 (§2.5) |
| `data/ground_truth.csv` | 500 | Labels — **the pipeline never reads this** (§2.6) |

`ground_truth.csv` is one row per purchase-register record. It is consumed only
by the evaluation script (§2.6 threshold sweep, §2.7 report). No label column
appears in either source CSV — check C6 asserts this.

### Defect plan

| Case type | n | Expected outcome | Exercises |
|---|---|---|---|
| `clean_exact_match` | 275 | auto-reconcile | baseline |
| `fuzzy_vendor_name_variant` | 40 | auto-reconcile | §2.2 `normalize_ai_assisted` |
| `fuzzy_ocr_artifact` | 20 | auto-reconcile | §2.2 OCR-style artifacts |
| `fuzzy_date_tolerance` | 15 | auto-reconcile | §2.3 date tolerance window |
| `fuzzy_amount_rounding` | 15 | auto-reconcile | §2.3 amount within ₹1 |
| `gstin_header_mismatch` | 30 | classified exception | §2.5 CGST/SGST vs IGST filing error |
| `credit_note_netting` | 25 | classified exception | §2.5 credit-note pattern |
| `late_filed_supplier` | 20 | classified exception | §2.3 `no_candidate_found` → §2.5 |
| `invoice_removed_post_claim` | 15 | classified exception | §2.3 `no_candidate_found` → §2.5 |
| `indeterminate_ambiguous` | 25 | indeterminate | §2.6 third gate outcome |
| `quarantine_missing_field` | 6 | quarantine | §2.1 required fields |
| `quarantine_bad_gstin` | 6 | quarantine | §2.1 GSTIN format/checksum/state code |
| `quarantine_bad_date` | 4 | quarantine | §2.1 date parseable and plausible |
| `quarantine_bad_amount` | 4 | quarantine | §2.1 numeric and non-negative |

Batch composition: 73.0% auto-reconcile, 18.0% classified exception,
5.0% indeterminate, 4.0% quarantined.

`gstr2b.csv` also carries **25 rows with no register counterpart**: 10 near-duplicate
decoys (same supplier and date as a real row, invoice number one transposition
away, amount ₹2 off) and 15 invoices the buyer never booked. The §2.3 greedy
one-to-one assignment must pair each register record with the true row and leave
the decoys unassigned; `tests/test_matcher.py` (build step 4) will assert this.

### Key columns

- `simulated_current_date` — `2026-06-10`, identical on all three source CSVs (§3.1).
  Nothing in the system reads a wall clock.
- `rule88d_intimation_date` — on register rows that carry a mismatch. Days elapsed
  to `simulated_current_date` drives the §2.5 7-day window check. 65 records land
  inside the window, 50 outside.
- `supplier_filing_date`, `itc_claimed_date` — supporting dates for the §2.5 checks.
- `split` (ground truth) — `calibration` (349) / `frozen_test` (151), stratified so
  every case type appears in both. Assigned at generation time, i.e. **before any
  tuning**, exactly as §2.6 requires. `src/confidence.py` reads this column; it
  must never re-derive the split.

## Phase 2 — validation, quarantine, normalisation

```bash
python3 run_phase2.py            # deterministic normalisation only (offline)
python3 run_phase2.py --ai       # also run the §2.2 AI-assisted half
python3 -m pytest tests/ -q      # 130 tests
```

`run_phase2.py` writes into `out/` (gitignored — regenerate any time). It reads
only the two source CSVs; `data/` is never written to.

### Modules

| Module | Section | Responsibility |
|---|---|---|
| `src/source_records.py` | §3.2-style shared shape | Loads the two source CSVs into `SourceRecord`s. Refuses to load ground truth. |
| `src/validation.py` | §2.1 | The four checks, in order; first failure wins. Decides validity and nothing else. |
| `src/quarantine_log.py` | §3.3 | SQLite `quarantine_log` table, separate from the audit log. |
| `src/normalization.py` | §2.2 | `normalize_deterministic()` (pure code) + `normalize_ai_assisted()` (Claude). |

### Record identity

A record is addressed by `source_id` = `<source>:<record_id>` —
`purchase_register:PR-0033`, `gstr2b:2B-0001`. The two files number their rows
independently, so a bare `record_id` is ambiguous across sources and a
`source_id` never is. Row numbers are carried too, so a quarantined record can
be found by eye in the CSV.

### What a quarantine row carries

§3.3's four columns (`record_id`, `validation_error`, `raw_record_snapshot`,
`timestamp`) plus the provenance needed to act on it: `source`,
`source_record_id`, `source_row_number`, a human-readable `validation_message`,
and `error_field`. The snapshot is the complete source row as JSON, verbatim —
including the bad value. Quarantined records go no further: `run_phase2.py`
normalises only the valid partition, and a test asserts the two sets never
intersect.

### Normalisation cleans text; it does not decide

Both views are kept: `NormalizedRecord.raw` is the untouched source row,
`.normalized` is the cleaned mapping, `.changes` records every field that moved
with before, after, and which half of the stage did it. The result type carries
no confidence, score, category or outcome field, and the module imports nothing
from build steps 4-7.

The AI half is held to the same line by an **enforced output contract**. The
request declares a JSON schema with `additionalProperties: false` and a single
permitted key, `cleaned_text`; the response is then re-checked client-side.
Anything else — a `confidence`, an `is_match`, a `score`, a `category`, an
`explanation`, non-JSON, or output too long to be a repair of the input — raises
`AIContractViolation`, and the deterministic value stands. A model cannot
smuggle a decision into this stage even if it tries.

The AI half is also *gated*: only text that `looks_messy()` (digits welded into
words, punctuation mid-word, characters outside the expected set) is sent, and
only `vendor_name` is ever in scope.

**On "GSTIN checksum correction" (§2.2):** by the time a record reaches
normalisation it has already passed the §2.1 checksum check, so there is no
broken checksum left to correct — a wrong check digit is a quarantine. Silently
rewriting one would also destroy the evidence the §2.5 GSTIN-header rule depends
on. `standardize_gstin()` therefore standardises presentation (casing,
separators) and leaves the digits alone.

## Phase 3 — matching, evidence, rules

```bash
python3 run_phase3.py            # matching -> evidence -> rules
python3 -m pytest tests/ -q      # 262 tests
```

Phase 3 stops at the rule engine. There is deliberately **no confidence score
and no auto-reconcile / exception / indeterminate outcome** — that is the §2.6
gate, build step 7.

| Module | Section | Responsibility |
|---|---|---|
| `src/matcher.py` | §2.3 | Score matrix + greedy one-to-one assignment |
| `src/evidence.py` | §2.4 | Plain field-by-field diff, no verdict |
| `src/rule_engine.py` | §2.5 | Classification rules + operational checks |
| `src/rules/rules_v2026_04.yaml` | §2.5 | The versioned parameters |

### The one-to-one guarantee

Enforced structurally, not checked afterwards: a record enters `claimed_pr` or
`claimed_2b` the moment it is assigned, and every later pair touching it is
skipped. There is no path through `greedy_assign()` that assigns either side
twice. Ties break on `(pr_id, b2_id)`, so the assignment is reproducible
regardless of input order.

`tests/test_matcher.py` proves it four ways: on the real batch, on 5 identical
records competing for 5 identical counterparts, on 10 records competing for 2,
and as a property test over 200 random score matrices.

### Scoring

Exact fields carry 100 of the 116 available points, fuzzy fields 16 — §2.3's
"exact highest, fuzzy lower but non-zero".

| Signal | Points | Kind |
|---|---|---|
| invoice number exact | 40 | exact |
| GSTIN exact | 30 | exact |
| GSTIN same PAN, different state | 20 | exact-ish |
| amount within ₹1 | 30 | exact |
| amount within 2% / 10% | 12 / 6 | partial |
| vendor name (rapidfuzz) | ≤ 10 | fuzzy |
| invoice number garbled | ≤ 8 | fuzzy |
| date same / ≤2d / ≤7d / ≤30d | 6 / 4 / 2 / 1 | fuzzy |

`MIN_CANDIDATE_SCORE = 45` sits just above GSTIN(30) + a perfect name(10) + a
weak date(2) = 42 — the profile of two *unrelated* invoices from the same
supplier, which is the dominant false-pair shape. It was swept 20..60 on the
**calibration split only**; the frozen 30% is not a tuning surface.

### Rules: two categories, never merged

`rules_v2026_04.yaml` holds the version-sensitive **parameters**; the logic is
deterministic Python. **No LLM participates in any GST classification** — a test
asserts `rule_engine.py` cannot even reach the Anthropic SDK.

| Category | Rules |
|---|---|
| Classification (what happened) | CLS-001 GSTIN header mismatch · CLS-002 credit-note netting · CLS-003 invoice removed post-claim · CLS-004 late-filed supplier |
| Operational (what to do, by when) | OPS-88D 7-day response window · OPS-DRC01C cumulative ITC variance |

CLS-003 and CLS-004 are separated solely by whether the invoice appears in
`gstr2b_prior_period.csv`, which joins the pipeline as a source in this phase.

### Measured results

| Stage | Result |
|---|---|
| Matching accuracy | 97.3% overall (calibration 97.9%, frozen 95.9%) |
| Matching, 7 well-defined case types | 100% |
| One-to-one property | Holds — 443 pairs, 443 distinct 2B records |
| Classification accuracy | 97.5% |
| CLS-001 / CLS-002 | 100%, zero false positives |
| Rule 88D | 65 within window, 50 outside, 365 n/a — matches the dataset |
| DRC-01C | 12 of 40 suppliers breach |

Every classification error traces to a matcher miss on a deliberately ambiguous
or absent record, not to a rule misfiring — asserted by a test.

## Assumptions

The locked architecture references "the earlier defect-distribution spec" (§5
step 1), which was not supplied. Everything above is derived from defect types
the architecture names explicitly, but four parameters had to be chosen. All four
are single named constants at the top of `data/generate_dataset.py`:

1. **Batch size 500 / 40 vendors.** §2.3 justifies greedy over Hungarian
   assignment "at this batch size", implying a modest batch.
2. **Case-mix percentages** (`DEFECT_PLAN`) — a majority-clean batch with every
   named defect present in a testable quantity.
3. **`DRC01C_THRESHOLD_RUPEES = 75000.00`** — a synthetic trigger, *not* the
   statutory ₹25 lakh test, which no invoice in this batch would approach. At
   ₹75,000, 11 of the 40 supplier GSTINs breach, making the check observable.
   The authoritative copy belongs in `rules_v2026_04.yaml` at build step 6.
4. **Prior-period snapshot direction.** §2.5 disambiguates `no_candidate_found`
   "by whether the invoice appears in a prior-period synthetic snapshot" without
   saying which way. Implemented as: **present** in the snapshot → the supplier
   withdrew it → `invoice_removed_post_claim`; **absent** → the supplier never
   filed it → `late_filed_supplier`.

### Deviations from §4

`data/gstr2b_prior_period.csv` is not in the §4 file listing, but §2.5 requires a
prior-period snapshot and no listed file can hold it. It is added as the minimal
way to satisfy §2.5.

`src/source_records.py` is not in the §4 listing either. Loading is naturally
`pipeline.py`'s job, but that is build step 8; the record shape is needed from
step 2 onward, so it lives in its own module in the spirit of §3.2 ("define it
once as a dataclass; every downstream stage consumes the same shape").

`run_phase2.py` and `run_phase3.py` (repo root, not `src/`) are temporary
scaffolding so each phase can be exercised before step 8 exists. They will be
absorbed into `src/pipeline.py` and deleted.

The repo root maps to the architecture's `exception-ledger/` root, so `data/`,
`src/` and `tests/` sit at the top level rather than nested one directory deeper.

## Sanity checks

`python3 data/generate_dataset.py` runs 31 checks, each keyed to an architecture
section. Groups: **C** structure and label isolation, **M** one-to-one pairing,
**V** validation and quarantine, **A** tax arithmetic, **D** per-defect
invariants, **O** operational checks, **S** calibration split, **R**
reproducibility. All 31 pass; `--verify` runs 30 (R1 needs a live regeneration).

`reference_validate()` in the generator is a reference implementation of the §2.1
rules, present so Part 1 can prove the 20 quarantine records genuinely fail and
the other 480 genuinely pass. `src/validation.py` (build step 2) is the
production implementation and must agree with it on every record.

## Dependencies

Part 1 needs nothing installed. Later steps need `requirements.txt`
(`rapidfuzz` §2.3, `PyYAML` §2.5, `anthropic` §2.2, `pytest` §5), plus an
`ANTHROPIC_API_KEY` for `normalize_ai_assisted()` only — every other stage,
including the whole deterministic path, runs offline.
