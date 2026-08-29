# Exception Ledger

GST purchase-register vs GSTR-2B reconciliation with a calibrated confidence gate.

The system architecture is **locked** at `Architecture_v2_FINAL.pdf`. That document
is the source of truth; this README restates it and records what was built.
Section references (§) point into it.

## Status — all 8 build-order steps complete

| Step (§5) | Component | Phase |
|---|---|---|
| 1 | `data/generate_dataset.py` | 1 |
| 2 | `src/validation.py` + `src/quarantine_log.py` | 2 |
| 3 | `src/normalization.py` | 2 |
| 4 | `src/matcher.py` + `tests/test_matcher.py` | 3 |
| 5 | `src/evidence.py` | 3 |
| 6 | `src/rule_engine.py` + `src/rules/rules_v2026_04.yaml` | 3 |
| 7 | `src/confidence.py` + `src/gate.py` | 4 |
| 8 | `src/audit_log.py` + `src/report.py` + `src/pipeline.py` | 4–5 |

## Quick start

```bash
pip install -r requirements.txt

python3 data/generate_dataset.py                  # regenerate the dataset (+31 checks)
python3 -m src.pipeline                           # run the pipeline (label-blind)
python3 -m src.pipeline --verify-reproducible     # run twice, compare fingerprints
python3 -m src.report                             # §2.7 report on the frozen 30%
python3 -m pytest tests/ -q                       # 408 tests
```

`python3 calibrate.py --report-frozen` re-derives the confidence threshold.
**Do not run it casually** — see *Evaluation protocol* below.

## Provenance

| | |
|---|---|
| **Dataset seed** | `20260401` (`data/generate_dataset.py`) — regeneration is byte-identical |
| **Rules version** | `2026-04` (`src/rules/rules_v2026_04.yaml`) |
| **Calibration** | `src/rules/calibration_v2026_04.yaml`, threshold **80.25** |
| **Return period** | 2026-04, prior-period snapshot 2026-03 |
| **`simulated_current_date`** | `2026-06-10` — nothing reads a wall clock |
| **Batch** | 500 register records, 490 GSTR-2B rows, 75 prior-period rows |

## Pipeline (§1)

```
purchase_register.csv + gstr2b.csv
        |
        v
  §2.1 validation ----[FAILS]---> §3.3 quarantine log   (exits; never scored)
        |
     [PASSES]
        v
  §2.2 deterministic + AI-assisted normalisation
        v
  §2.3 exact + fuzzy ONE-TO-ONE matching
        v
  §2.4 field-by-field evidence comparison
        v
  §2.5 versioned GST rules + operational checks
        v
  §2.6 calibrated confidence gate
        |
   +----+--------------+------------------+
   v                   v                  v
auto-reconcile   classified exception   indeterminate -> human review
   +----+--------------+------------------+
        v
  §2.7 audit log + evaluation report
```

**Two exit paths, never merged.** Quarantine (bad input, never scored) and the
three-way gate outcome (valid input, scored). A quarantined record is a
data-quality problem, not a reconciliation problem.

## Modules

| Module | § | Responsibility |
|---|---|---|
| `data/generate_dataset.py` | 3 | Seeded synthetic dataset + ground truth |
| `src/source_records.py` | 3.2 | `SourceRecord`, the three-file loader |
| `src/validation.py` | 2.1 | Four checks in order, first failure wins |
| `src/quarantine_log.py` | 3.3 | SQLite `quarantine_log` table |
| `src/normalization.py` | 2.2 | `normalize_deterministic()` + `normalize_ai_assisted()` |
| `src/matcher.py` | 2.3 | Score matrix + greedy one-to-one assignment |
| `src/evidence.py` | 2.4 | Plain field-by-field diff, no verdict |
| `src/rule_engine.py` | 2.5 | Classification rules + operational checks |
| `src/confidence.py` | 2.6 | Evidence-derived score + threshold sweep |
| `src/gate.py` | 2.6 | Three-way outcome |
| `src/audit_log.py` | 2.7 | SQLite `audit_log` table |
| `src/pipeline.py` | — | End-to-end orchestration (**label-blind**) |
| `src/report.py` | 2.7 | Evaluation report (**evaluation-side**) |
| `calibrate.py` | 2.6 | Threshold calibration (**evaluation-side**) |

## Evaluation protocol (§2.6)

The codebase is split in two, and the boundary is enforced by tests rather than
convention.

**The pipeline never sees a label.** `src/pipeline.py` reads the two source CSVs
and the prior-period snapshot, and a calibrated threshold as a plain number. It
does not import `report.py`. A test parses every pipeline module's AST and fails
if any string literal or identifier can address `ground_truth.csv` — docstrings
explaining the rule are allowed, code that could open the file is not.

**Only `calibrate.py` and `src/report.py` may read labels.** Both are named in
the architecture as evaluation-side.

The calibration protocol, and how each step is held:

1. **The 70/30 split was assigned at dataset-generation time, before any
   tuning**, stratified so every case type appears in both halves. It lives in
   `ground_truth.csv` and is read, never re-derived.
2. **The sweep sees the calibration split only.** `sweep_thresholds()` takes
   `(score, label)` pairs *as arguments* and reads no file — so calibrating on
   the frozen split is not expressible, not merely discouraged.
3. **The frozen 30% was scored once** and reported. It is never fed back.

Chosen threshold **80.25** — the midpoint of a tied plateau spanning 70.5–90.
An endpoint would sit flush against a score cluster and generalise worse.

> ⚠️ Re-running `calibrate.py` overwrites the frozen artifact. Re-running it
> *after* seeing frozen-test results is the re-tuning §2.6 forbids.

Split arithmetic: 500 register records = 349 calibration + 151 frozen by label;
20 were quarantined (14 / 6), leaving **335 + 145 = 480 scored**.

## Final metrics — frozen test set (145 records)

**Overall match rate 94.5%** (137/145 resolved), broken out as §2.7 requires:

| Bucket | n | % | Meaning |
|---|---|---|---|
| exact | 89 | 61.4% | every compared field agreed |
| fuzzy | 22 | 15.2% | auto-reconciled with tolerated differences |
| rule-classified | 26 | 17.9% | a named rule explained it |
| unresolved | 8 | 5.5% | indeterminate → human review |

**Classification-rule exceptions** (26): `gstin_header_mismatch` 9,
`credit_note_netting` 7, `late_filed_supplier` 6, `invoice_removed_post_claim` 4.

**Operational-check flags**, listed separately: Rule 88D — 17 within window,
17 outside, 111 n/a. DRC-01C — 39 records breaching, across 12 of 40 suppliers.

**Indeterminate**: 8. **Quarantined**: 20 (whole batch, its own line, not folded
into anything above).

**Accuracy vs ground truth**: 140/145 = **96.6%** on the frozen split, scored
once. All 5 disagreements are indeterminate ↔ classified-exception swaps on
deliberately ambiguous or absent records, traceable to Phase 3 matcher misses.

Whole-batch figures: 480 scored — 365 auto-reconcile, 92 classified exception,
23 indeterminate; 97.5% outcome accuracy with **zero false auto-reconciles**.

### Reproducibility

`python3 -m src.pipeline --verify-reproducible` runs the pipeline twice and
compares a SHA-256 fingerprint over every pairing, evidence diff, rule, flag and
gate decision (timestamps excluded, so a real change cannot be masked by the
clock). Current fingerprint:

```
a0208ded0b4fa21d93c3b9872c1881c4e6d9406cc807f8a143b843497beb9a7a
```

Tests also assert the fingerprint is stable across separate databases and
different pinned timestamps, and that it *changes* when a decision changes — a
digest that never moves would prove nothing.

## Guarantees worth knowing

**One-to-one matching (§2.3)** is structural, not a post-hoc check: a record
enters `claimed_pr`/`claimed_2b` the moment it is assigned and every later pair
touching it is skipped. Ties break on `(pr_id, b2_id)`, so the assignment is
reproducible regardless of input order. Proven on the real batch, on 5 identical
records competing for 5 identical counterparts, on 10 competing for 2, and as a
property test over 200 random matrices.

**No LLM makes a GST decision.** Matching, evidence, rules, confidence and the
gate are deterministic Python. Tests assert `rule_engine.py`, `confidence.py`
and `gate.py` cannot reach the Anthropic SDK. The one AI touchpoint is
`normalize_ai_assisted()`, which cleans vendor-name text and is held to a schema
permitting exactly one key (`cleaned_text`); a response carrying a confidence, a
match verdict or a category raises `AIContractViolation` and the deterministic
value stands.

**Quarantine cannot leak into the scored path.** `decide_batch()` raises if a
quarantined record is passed in; `AuditLog.record()` refuses to write one,
checking the quarantine table in the same database. §2.7's "one row per record
that passed validation" holds by construction.

## Assumptions

The architecture references "the earlier defect-distribution spec" (§5 step 1),
which was not supplied. Four parameters had to be chosen; all are named
constants:

1. **Batch size 500 / 40 vendors** — §2.3 justifies greedy over Hungarian "at
   this batch size".
2. **Case-mix percentages** (`DEFECT_PLAN`) — majority-clean, every named defect
   present in testable quantity.
3. **`DRC01C_THRESHOLD_RUPEES = 75000.00`** — a synthetic trigger, *not* the
   statutory ₹25 lakh test, which no invoice in this batch approaches. At
   ₹75,000, 12 of 40 suppliers breach.
4. **Prior-period snapshot direction** — §2.5 does not say which way round.
   Implemented as: present in the snapshot → `invoice_removed_post_claim`;
   absent → `late_filed_supplier`.

Two further judgement calls: `MIN_CANDIDATE_SCORE = 45` in the matcher (swept on
the calibration split only), and same-PAN-different-state GSTINs scoring 20
rather than 0 — without that, the §2.5 header-mismatch case could never survive
matching to be classified.

## Deviations from §4

| File | Why |
|---|---|
| `data/gstr2b_prior_period.csv` | §2.5 needs a prior-period snapshot; no listed file can hold it |
| `src/source_records.py` | The record shape is needed from step 2, but loading is `pipeline.py`'s job at step 8 — split out in the spirit of §3.2 |
| `src/rules/calibration_v2026_04.yaml` | §2.6 needs a frozen threshold with provenance, kept out of the hand-written rules file |
| `calibrate.py` | §2.6 needs a label-seeing evaluation script separate from the label-blind pipeline |

The repo root maps to the architecture's `exception-ledger/` root, so `data/`,
`src/` and `tests/` sit at the top level. The Phase 2–4 scaffolding runners
(`run_phase2.py`, `run_phase3.py`, `run_phase4.py`) were absorbed into
`src/pipeline.py` at step 8 and removed, as planned.

## Scope boundaries — what this is NOT

- **Synthetic data only.** Every number here comes from `generate_dataset.py`.
  The clean confidence separation (true matches 90–100, everything else 0–70) is
  a property of a dataset whose injected defects damage high-weight fields while
  its fuzzy cases damage only low-weight ones. **Real filings overlap.** Treat
  these figures as evidence the protocol is wired correctly, not as a production
  forecast.
- **Not a filing tool.** Nothing here files a return, amends one, or talks to the
  GSTN. It reconciles two CSVs and explains the differences.
- **Not tax advice.** Rule IDs and categories are a triage aid. `DRC01C` uses a
  synthetic trigger, and the classification rules recognise the specific defect
  patterns this dataset contains — not the full statutory surface.
- **The AI half is unexercised against a live API.** `normalize_ai_assisted()`
  has never made a real call: no credentials were available in the build
  environment. Its contract, gating and fallback are fully tested offline, and
  the request shape is checked against the installed SDK's signature, but the
  call itself is unproven. `python3 -m src.pipeline --ai` on a machine with
  credentials closes that gap.
- **Human review is a queue, not a workflow.** `reviewer_decision` is nullable
  and `pending_review()` lists what is waiting. There is no UI, assignment,
  or escalation.
- **Single batch, single period.** No multi-period carry-forward, no
  amendment tracking, no incremental runs.
