# Exception Ledger

**A GST-specific finance controller that reconciles evidence, quantifies
estimated ITC exposure and safely escalates uncertainty.**

AI-assisted normalisation. Deterministic decisions. Auditable human review.

> Reconciles a purchase register against **synthetic GSTR-2B-style data** using a
> calibrated confidence gate. **No live GSTN connectivity. Not tax advice.**
> Read [Limitations](#limitations) before quoting any figure.

| | |
|---|---|
| Submission write-up | [SUBMISSION.md](SUBMISSION.md) |
| Locked specification | [ARCHITECTURE.pdf](ARCHITECTURE.pdf) |
| Tests | 613 passing |

---

## Project overview

Exception Ledger takes two files — a company's purchase register and a
GSTR-2B-style supplier filing extract — and answers three questions about every
invoice: **was it matched, what explains the difference, and does a human need
to look at it?**

It is a complete, reproducible pipeline: validation, normalisation, one-to-one
matching, evidence comparison, versioned GST rules, a calibrated confidence
gate, an append-only audit log, an evaluation report and a local dashboard.

`ARCHITECTURE.pdf` in this repository is the locked specification. It was fixed
before implementation began and was not revised to fit the code; this README
records what was built against it.

### Provenance

| | |
|---|---|
| **Dataset seed** | `20260401` (`data/generate_dataset.py`) — regeneration is byte-identical |
| **Rules version** | `2026-04` (`src/rules/rules_v2026_04.yaml`) |
| **Calibration** | `src/rules/calibration_v2026_04.yaml`, threshold **80.25** |
| **Return period** | 2026-04, prior-period snapshot 2026-03 |
| **Simulated current date** | `2026-06-10` — nothing reads a wall clock |
| **Batch** | 500 register records, 490 GSTR-2B rows, 75 prior-period rows |

---

## Problem and solution

An Indian business claims input tax credit from its purchase register. The
government's GSTR-2B says what suppliers actually filed. The two disagree — and
the disagreements are not the same kind of problem:

- A supplier filed under a different state registration, so CGST/SGST appears as
  IGST. The money is right; the header is wrong.
- A credit note was netted off in 2B but not yet absorbed in the register.
- An invoice is missing — but *why* matters. A supplier who has not yet filed is
  a chase; an invoice withdrawn after credit was claimed is a liability.
- The vendor name is OCR-damaged or the date is a day out, and nothing is
  actually wrong.

Treating all of these as "mismatch" produces a list nobody can act on.

**The solution is to separate three things that are usually collapsed into one.**
Whether records pair up is a *matching* question. What explains a difference is a
*rules* question. Whether the system is sure enough to act alone is a
*confidence* question. Exception Ledger answers them in that order, keeps the
evidence for each, and produces three outcomes rather than two:

| Outcome | Meaning |
|---|---|
| **Auto-reconcile** | Evidence agrees above the calibrated threshold; no human needed |
| **Classified exception** | A named, versioned rule explains the difference |
| **Indeterminate** | The system does not know, and says so — routed to a human with the evidence attached |

The third outcome is the point. A system that always produces an answer hides
its failures inside the confident ones.

---

## Product positioning

**For** finance controllers reconciling input tax credit, **Exception Ledger is**
a reconciliation control rather than a matching script.

| It does | It does not |
|---|---|
| Explain *why* two records differ, with the field-level evidence retained | File, amend, or transmit anything |
| Quantify estimated ITC exposure per supplier | Give tax advice |
| Escalate what it cannot decide, instead of guessing | Connect to the GSTN or any live source |
| Keep an append-only audit trail of every decision | Let an AI model decide an outcome |

Three claims define the boundary, and each is enforced in code rather than
asserted in prose:

1. **AI is used only for normalisation.** It cleans messy vendor-name text.
2. **Matching, rules, confidence and final decisions are deterministic.** No
   model influences an outcome.
3. **Uncertainty is escalated, not absorbed.** Indeterminate is a first-class
   result with its own queue.

All figures come from **synthetic GSTR-2B-style data**. There is **no live GSTN
connectivity**, and nothing here is **tax advice**.

---

## Pipeline

```
purchase_register.csv + gstr2b.csv
        |
        v
   input validation ----[FAILS]---> quarantine log   (exits; never scored)
        |
     [PASSES]
        v
   normalisation  (deterministic, with optional AI assist)
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
   append-only audit log + evaluation report
```

**Two exit paths, never merged.** Quarantine means bad input and is never
scored. The three-way gate outcome means valid input that was scored. A
quarantined record is a data-quality problem, not a reconciliation problem, and
folding the two together would flatter every metric on this page.

### Stages

| Module | Responsibility |
|---|---|
| `data/generate_dataset.py` | Seeded synthetic dataset and ground truth |
| `src/source_records.py` | Record shape and the three-file loader |
| `src/validation.py` | Four checks in order; first failure wins |
| `src/quarantine_log.py` | Append-only quarantine table |
| `src/normalization.py` | Deterministic cleaning, with an optional AI assist |
| `src/matcher.py` | Score matrix and greedy one-to-one assignment |
| `src/evidence.py` | Plain field-by-field diff, with no verdict attached |
| `src/rule_engine.py` | Classification rules and operational checks |
| `src/confidence.py` | Evidence-derived score and threshold sweep |
| `src/gate.py` | Three-way outcome |
| `src/audit_log.py` | Append-only audit table |
| `src/pipeline.py` | End-to-end orchestration (**label-blind**) |
| `src/report.py` | Evaluation report (**evaluation-side**) |
| `src/dashboard.py` | Local read-only dashboard |
| `calibrate.py` | Threshold calibration (**evaluation-side**) |

---

## Key capabilities

**One-to-one matching is structural, not a post-hoc check.** A record is claimed
the moment it is assigned, and every later pair touching it is skipped — so no
code path can pair a record twice. Ties break deterministically, so the
assignment is identical regardless of input order. Proven on the real batch, on
5 identical records competing for 5 identical counterparts, on 10 competing for
2, and as a property test over 200 random score matrices.

**Evidence is kept, not summarised away.** Every decision retains the
field-by-field comparison that produced it: invoice number, GSTIN, amount, date,
vendor name and tax heads. The evidence object carries no verdict, so the same
diff can be re-read by a rule, by the confidence score, or by a human.

**Rules are versioned and data-driven.** Parameters live in
`src/rules/rules_v2026_04.yaml`; only the logic is in Python. Every decision
records the rule ID and rules version that produced it, so a decision made today
remains explainable after the rules change.

**Confidence is derived from evidence, never self-reported.** The score is a
weighted function of which fields agree. No model is asked how confident it is.

**Operational GST checks are reported separately from classification.** Rule 88D
timing and DRC-01C variance are flags on a record, not verdicts about it, and
they are never folded into the match-rate figures.

**The audit log is append-only.** Runs accumulate rather than overwrite.
Reviewer decisions are recorded as events, never as edits to a prior row, so the
history of a record cannot be rewritten.

**Quarantine cannot leak into the scored path.** The gate raises if a
quarantined record is passed to it, and the audit log refuses to write one,
checking the quarantine table in the same database. "One row per record that
passed validation" holds by construction rather than by discipline.

---

## Evaluation results

### Evaluation protocol

The codebase is split in two, and the boundary is enforced by tests rather than
convention.

**The pipeline never sees a label.** `src/pipeline.py` reads the two source CSVs,
the prior-period snapshot, and a calibrated threshold as a plain number. It does
not import `report.py`. A test parses every pipeline module's syntax tree and
fails if any string literal or identifier could address `ground_truth.csv` —
docstrings explaining the rule are allowed; code that could open the file is not.
Only `calibrate.py` and `src/report.py` may read labels.

The calibration protocol, and how each step is held:

1. **The 70/30 split was assigned at dataset-generation time, before any
   tuning**, stratified so every case type appears in both halves. It lives in
   `ground_truth.csv` and is read, never re-derived.
2. **The sweep sees the calibration split only.** `sweep_thresholds()` takes
   `(score, label)` pairs *as arguments* and reads no file — so calibrating on
   the frozen split is not expressible, not merely discouraged.
3. **The frozen 30% was scored once** and reported. It is never fed back.

Chosen threshold **80.25** — the midpoint of a tied plateau spanning 70.5–90. An
endpoint would sit flush against a score cluster and generalise worse.

> ⚠️ Re-running `calibrate.py` overwrites the frozen artifact. Re-running it
> *after* seeing frozen-test results is exactly the re-tuning the protocol
> forbids.

**Split arithmetic:** 500 register records = 349 calibration + 151 frozen by
label; 20 were quarantined (14 / 6), leaving **335 + 145 = 480 scored**.

### Frozen test set (145 records, scored once)

**Overall match rate 94.5%** (137/145 resolved):

| Bucket | n | % | Meaning |
|---|---|---|---|
| exact | 89 | 61.4% | every compared field agreed |
| fuzzy | 22 | 15.2% | auto-reconciled with tolerated differences |
| rule-classified | 26 | 17.9% | a named rule explained it |
| unresolved | 8 | 5.5% | indeterminate → human review |

**Classification-rule exceptions** (26): `gstin_header_mismatch` 9,
`credit_note_netting` 7, `late_filed_supplier` 6, `invoice_removed_post_claim` 4.

**Operational-check flags**, listed separately: Rule 88D — 17 within window, 17
outside, 111 not applicable. DRC-01C — 39 records breaching, across 12 of 40
suppliers.

**Indeterminate**: 8. **Quarantined**: 20 (whole batch, on its own line, not
folded into anything above).

**Accuracy vs ground truth**: 140/145 = **96.6%** on the frozen split, scored
once. All 5 disagreements are indeterminate ↔ classified-exception swaps on
deliberately ambiguous or absent records, every one traceable to a matcher miss
rather than a rule misfiring.

### Whole batch

480 scored — 365 auto-reconcile, 92 classified exception, 23 indeterminate.
**97.5% outcome accuracy with zero false auto-reconciles.**

Zero false auto-reconciles is the number that matters. Wrongly auto-reconciling
silently accepts a mismatch; wrongly flagging one costs a review. The system is
built to fail in the cheap direction.

Estimated ITC exposure: **₹18.9 lakh across 40 suppliers**, computed over the
whole batch — not the frozen split. Exposure figures are labelled with their
scope wherever they appear.

### Throughput

```bash
python3 -m src.pipeline --benchmark 3     # median of N runs, with spread
```

**Method:** wall clock around each stage, single-threaded, one process, no cache,
on one container. A single timing is noise, so the figure below is the **median
of 3 runs** with min/max showing how much to trust it. Decisions are identical
across every run — only the clock varies.

| | |
|---|---|
| Mode | `deterministic` |
| Batch size | 500 register rows × 490 2B rows (**235,200** match-matrix cells) |
| Valid records | 480 of 990 source rows read |

| Metric | min | **median** | max |
|---|---|---|---|
| Elapsed (s) | 3.220 | **3.336** | 3.407 |
| Records/second | 140.9 | **143.9** | 149.1 |

| Stage (median) | Seconds | Share |
|---|---|---|
| validate + quarantine + normalise | 0.054 | 1.6% |
| **match** | **2.867** | **85.9%** |
| evidence | 0.007 | 0.2% |
| rules | 0.011 | 0.3% |
| confidence gate | 0.005 | 0.1% |
| audit log | 0.396 | 11.9% |

Matching dominates because it scores the **full cross product** — 480 × 490 =
235,200 pairs. That is O(n²): time per record grows with the square of batch
size, not linearly. At 10× the batch, expect roughly 100× the
matching time. The specification accepts this deliberately, choosing greedy
assignment over Hungarian for explainability at this batch size; it is the first
thing to revisit if batches grow.

Throughput is **not comparable across modes** — the AI path adds a network round
trip per messy field — nor across batch sizes, for the reason above.

### Deterministic vs AI-assisted

`python3 -m src.pipeline --compare-ai` runs both and diffs them. With no
credentials available in this environment:

| | deterministic | AI-assisted |
|---|---|---|
| auto_reconcile | 365 | 365 |
| classified_exception | 92 | 92 |
| indeterminate | 23 | 23 |
| normalised fields changed | — | 0 |
| decisions changed | — | 0 |
| AI outcomes | — | 45 attempted, **45 failed** |

Identical, because every AI call failed and the deterministic value stood. That
is the fallback working — **not** evidence that the AI path functions. See
[AI safety boundary](#ai-safety-boundary).

---

## Dashboard

```bash
python3 -m src.dashboard          # then open http://127.0.0.1:8000
```

One command, no extra dependencies — `http.server` from the standard library. It
runs the pipeline on start and renders what comes back.

| Page | Shows |
|---|---|
| `/` | Record counts · the three gate outcomes · precision, coverage, throughput, estimated ITC exposure · normalisation mode · exception queue · quarantine summary · batch run controls · exports |
| `/record?id=…` | Raw vs normalised values, field-level evidence, rule ID and version, confidence and threshold, operational checks, audit row |
| `/quarantine` | Every quarantined record with its validation error and source row |
| `/export/…` | `decisions.csv` · `queue.csv` · `quarantine.csv` · `audit.csv` · `report.json` · `report.txt` |

**It is a presentation layer and nothing else.** Every figure is read from a
`PipelineResult`, an `EvaluationReport`, the audit log or the frozen calibration
artifact. It computes no reconciliation logic, and a test asserts that no metric
is hardcoded — change the run and the page changes.

**The dashboard is local and read-only.** It serves on localhost with no
authentication. There is **no upload, no reviewer write-back, no assignment and
no escalation**. Two consequences are stated in the interface rather than
stubbed:

- **No upload.** `pipeline.run()` reads fixed source files; accepting an
  arbitrary batch would require a backend change. The batch panel names the files
  it will read and says so.
- **Precision is read, not recomputed.** It comes from the stored frozen-split
  confusion matrix in `calibration_v2026_04.yaml`, because the protocol fixes
  those numbers at calibration time. The panel cites its source.

Colour encodes state only, and every badge carries a glyph and a text label, so
hue is never the sole channel of meaning.

---

## Quick start

```bash
pip install -r requirements.txt

python3 data/generate_dataset.py                  # regenerate the dataset (+31 checks)
python3 -m src.pipeline                           # run the pipeline (label-blind)
python3 -m src.pipeline --verify-reproducible     # run twice, compare fingerprints
python3 -m src.pipeline --benchmark 3             # stable throughput (median of N)
python3 -m src.pipeline --compare-ai              # deterministic vs AI-assisted
python3 -m src.report                             # evaluation report on the frozen 30%
python3 -m src.dashboard                          # dashboard at http://127.0.0.1:8000
python3 -m pytest tests/ -q                       # 613 tests
```

`python3 calibrate.py --report-frozen` re-derives the confidence threshold.
**Do not run it casually** — see [Evaluation protocol](#evaluation-protocol).

---

## Reproducibility

```bash
python3 -m src.pipeline --verify-reproducible
```

This runs the pipeline twice and compares a SHA-256 fingerprint taken over every
pairing, evidence diff, rule, flag and gate decision. Timestamps are excluded, so
a real change cannot be masked by the clock. Current fingerprint:

```
a0208ded0b4fa21d93c3b9872c1881c4e6d9406cc807f8a143b843497beb9a7a
```

The dataset regenerates byte-for-byte from seed `20260401`, and nothing in the
pipeline reads a wall clock — the simulated current date is a fixed constant.

Tests also assert that the fingerprint is stable across separate databases and
different pinned timestamps, and that it **changes** when a decision changes. A
digest that never moves would prove nothing.

---

## AI safety boundary

**AI is used only for normalisation.** Its single job is cleaning messy
vendor-name text. **Matching, rules, confidence and final decisions are
deterministic** Python and cannot be influenced by a model.

How that boundary is held:

| Guard | Mechanism |
|---|---|
| Deciding modules cannot reach the model | Tests assert `rule_engine.py`, `confidence.py` and `gate.py` cannot import or call the Anthropic SDK |
| The model cannot return a decision | Responses are held to a schema permitting exactly one key, `cleaned_text`; a reply carrying a confidence, match verdict or category raises `AIContractViolation` |
| A failure cannot corrupt a run | On violation, error, or timeout the deterministic value stands and the run continues |
| Only one field is reachable | A test asserts `vendor_name` is the only field the AI path can change |
| Failures are visible, never silent | The CLI reports attempts, applications and failures, and warns loudly if AI was requested and every call failed |

**The live AI API path is unverified without credentials.**
`normalize_ai_assisted()` has never made a real API call — no credentials were
available in any build environment. Its contract, gating and fallback are fully
tested offline, and the request shape is checked against the installed SDK's
signature, but the call itself is unproven. Running `python3 -m src.pipeline --ai`
on a machine with credentials closes that gap.

---

## Repository structure

```
ARCHITECTURE.pdf            the locked specification
README.md                   this file
SUBMISSION.md               submission write-up, with detailed spec references
requirements.txt            dependencies
calibrate.py                threshold calibration (evaluation-side)

data/
  generate_dataset.py       seeded synthetic dataset + ground truth
  purchase_register.csv     500 register records
  gstr2b.csv                490 GSTR-2B-style rows
  gstr2b_prior_period.csv   75 prior-period snapshot rows
  ground_truth.csv          labels + the 70/30 split (evaluation-side only)

src/
  source_records.py         record shape and loader
  validation.py             input validation
  quarantine_log.py         quarantine (append-only)
  normalization.py          deterministic + AI-assisted cleaning
  matcher.py                one-to-one matching
  evidence.py               field-by-field diff
  rule_engine.py            rules + operational checks
  rules/*.yaml              versioned rules and frozen calibration
  confidence.py             evidence-derived confidence
  gate.py                   three-way gate
  audit_log.py              audit (append-only)
  pipeline.py               end-to-end orchestration (label-blind)
  report.py                 evaluation report (evaluation-side)
  dashboard.py              local read-only dashboard

tests/                      613 tests
```

Four files were added beyond the specification's file list, each because the
specification required something no listed file could hold: the prior-period
snapshot, the record-shape module, the frozen calibration artifact, and the
label-seeing calibration script. Each is justified in
[SUBMISSION.md](SUBMISSION.md).

---

## Limitations

Read these before quoting any number above. They are also printed in the final
section of every evaluation report, because a figure travels further than the
document that qualifies it.

| Limitation | Detail |
|---|---|
| **Synthetic data** | Every figure comes from `generate_dataset.py` (seed 20260401). Injected defects damage high-weight evidence fields while fuzzy cases damage only low-weight ones, so the two confidence populations *cannot* overlap. Real filings do. |
| **Single batch, single period** | One return period (2026-04) against one prior snapshot. No carry-forward, amendments, or incremental runs. |
| **Throughput is indicative** | One machine, single-threaded, no cache. Matching is O(register × 2B). |
| **Synthetic DRC-01C trigger** | ₹75,000, not the statutory ₹25 lakh — which no invoice here approaches. |
| **Not a filing tool, not tax advice** | Nothing files or amends a return. Rule IDs are a triage aid covering this dataset's defect patterns, not the statutory surface. |
| **Live AI path unverified** | `normalize_ai_assisted()` has never made a real API call — no credentials in any build environment. Contract, gating and fallback are tested offline; the call itself is not. |
| **Dashboard is local and read-only** | Serves on localhost with no auth. No upload, no reviewer write-back, no assignment, no escalation. It reviews a run; it cannot write a reviewer's decision back. |
| **Human review is a queue** | `pending_review()` lists what waits; decisions are appended via `audit_log.record_reviewer_decision()` outside the interface. |
| **Matcher is not perfect** | Deliberately ambiguous records are the weak spot; every outcome error traces to a matcher miss there, not a rule misfiring. |

### Documented assumptions

The specification references an earlier defect-distribution spec that was not
supplied. Four parameters had to be chosen, and all are named constants rather
than inline values:

1. **Batch size 500 across 40 vendors** — the specification justifies greedy
   assignment over Hungarian at this batch size.
2. **Case-mix percentages** (`DEFECT_PLAN`) — majority-clean, with every named
   defect present in testable quantity.
3. **`DRC01C_THRESHOLD_RUPEES = 75000.00`** — a synthetic trigger, *not* the
   statutory ₹25 lakh test, which no invoice in this batch approaches. At
   ₹75,000, 12 of 40 suppliers breach.
4. **Prior-period snapshot direction** — present in the snapshot means
   `invoice_removed_post_claim`; absent means `late_filed_supplier`.

Two further judgement calls: `MIN_CANDIDATE_SCORE = 45` in the matcher, swept on
the calibration split only, and same-PAN-different-state GSTINs scoring 20 rather
than 0 — without which the header-mismatch case could never survive matching to
be classified at all.

---

## Scope boundaries

What this is **not**:

- **Synthetic data only.** Every number here comes from `generate_dataset.py`.
  The clean confidence separation (true matches 90–100, everything else 0–70) is
  a property of a dataset whose injected defects damage high-weight fields while
  its fuzzy cases damage only low-weight ones. **Real filings overlap.** Treat
  these figures as evidence that the protocol is wired correctly, not as a
  production forecast.
- **No live GSTN connectivity.** Nothing here talks to the GSTN or any external
  filing system. It reconciles two CSVs and explains the differences.
- **Not a filing tool.** Nothing files a return or amends one.
- **Not tax advice.** Rule IDs and categories are a triage aid. DRC-01C uses a
  synthetic trigger, and the classification rules recognise the specific defect
  patterns this dataset contains — not the full statutory surface.
- **The AI path is unexercised against a live API.** The live AI API path is
  unverified without credentials. Its contract, gating and fallback are fully
  tested offline, but the call itself is unproven.
- **Human review is a queue, not a workflow.** The dashboard is local and
  read-only: no upload, no reviewer write-back, no assignment, no escalation.
- **Single batch, single period.** No multi-period carry-forward, no amendment
  tracking, no incremental runs.
