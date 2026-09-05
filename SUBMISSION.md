# Exception Ledger — Razorpay Track 4 Submission

**A GST-specific finance controller that reconciles evidence, quantifies
estimated ITC exposure and safely escalates uncertainty.**

AI-assisted normalisation. Deterministic decisions. Auditable human review.

> Runs on **synthetic GSTR-2B-style data**. No live GSTN connectivity.
> Not tax advice. Every figure below is reproducible — see *Verify it yourself*.

---

## The problem

An Indian business claims input tax credit from its purchase register. The
government's GSTR-2B says what suppliers actually filed. The two disagree, and
the disagreements are not all the same kind of problem:

- A supplier filed under a different state registration, so CGST/SGST appears
  as IGST. The money is right; the header is wrong.
- A credit note was netted off in 2B but not yet absorbed in the register.
- An invoice is simply missing — but *why* matters. A supplier who has not yet
  filed is a chase; an invoice withdrawn after credit was claimed is a
  liability.
- The vendor name is OCR-damaged, or the date is a day out, and nothing is
  actually wrong.

Treating all of these as "mismatch" produces a list nobody can act on. The
finance controller's real question is: **what happened, how much is at risk,
and which ones does a human actually need to look at?**

## What this does

| Stage | What it decides |
|---|---|
| Input validation | Is this record usable at all? If not it is **quarantined** and never scored |
| Normalisation | Clean messy text — deterministically, with an optional AI assist that **cannot make decisions** |
| One-to-one matching | Pair each register record with at most one 2B record; unmatched is a valid answer |
| Evidence comparison | A plain field-by-field diff, with no verdict attached |
| GST rules | Name what happened, from a versioned rule file |
| Confidence gate | Auto-reconcile, classified exception, or escalate to a human |
| Audit log | An append-only record of every decision and every reviewer event |

Three outcomes, not two. The third — **indeterminate** — is the point: the
system says "I don't know" rather than guessing, and routes those records to a
person with the evidence attached.

## Results — frozen test set (145 records, scored once)

| | |
|---|---|
| **Match rate** | **94.5%** (137/145) — 89 exact, 22 fuzzy, 26 rule-classified |
| Escalated to human review | 8 (5.5%) |
| Quarantined (counted separately) | 20 |
| Outcome accuracy vs ground truth | **96.6%** (140/145) |
| Auto-reconcile precision | **100%** — zero false auto-reconciles |
| Estimated ITC exposure | ₹18.9 lakh across 40 suppliers (whole batch) |
| Throughput | 144 records/second (median of 3 runs) |

Classification rules fired: GSTIN header mismatch 9, credit-note netting 7,
late-filed supplier 6, invoice removed post-claim 4.

**Zero false auto-reconciles** is the number that matters. Wrongly
auto-reconciling silently accepts a mismatch; wrongly flagging one only costs a
review. The system is built to fail in the cheap direction.

## What makes it trustworthy

**No LLM makes a GST decision.** Matching, evidence, rules, confidence and the
gate are deterministic Python. The one AI touchpoint cleans vendor-name text
and is held to a schema permitting exactly one key — a response carrying a
confidence, a match verdict or a category is rejected and the deterministic
value stands. Tests assert the deciding modules cannot even reach the SDK.

**One-to-one matching is structural, not hoped for.** A record is claimed the
moment it is assigned, so no path can pair it twice. Proven on the real batch,
on identical records competing for identical counterparts, and as a property
test over 200 random score matrices.

**The threshold was calibrated honestly.** 70/30 split assigned before any
tuning; the sweep sees the calibration half only and takes data as an argument
so it cannot reach the frozen half; the frozen 30% was scored once and
reported.

**The pipeline never sees an answer key.** A test parses every pipeline
module's syntax tree and fails if any could open `ground_truth.csv`. Only the
evaluation script and the report may.

**Reproducible.** Same inputs, same decisions, every time — verified by a
SHA-256 fingerprint over every pairing, evidence diff, rule, flag and decision:
`a0208ded0b4fa21d…`

**Append-only audit.** Runs accumulate rather than overwrite; reviewer
decisions are events, never edits.

## Verify it yourself

```bash
pip install -r requirements.txt

python3 data/generate_dataset.py                # regenerate data (31 checks)
python3 -m src.pipeline --verify-reproducible   # run twice, compare fingerprints
python3 -m src.report                           # the frozen-set evaluation report
python3 -m src.dashboard                        # dashboard at 127.0.0.1:8000
python3 -m pytest tests/ -q                     # 613 tests
```

Everything is offline and deterministic. `data/generate_dataset.py` rebuilds
the dataset byte-for-byte from seed `20260401`.

## Architecture compliance

Built against [`ARCHITECTURE.pdf`](ARCHITECTURE.pdf) (in this repository),
following its build order exactly. All 8 steps complete. Four documented deviations, each
because the spec required something no listed file could hold:

| Addition | Why |
|---|---|
| `data/gstr2b_prior_period.csv` | §2.5 needs a prior-period snapshot |
| `src/source_records.py` | The record shape is needed from step 2; loading is step 8's job |
| `src/rules/calibration_v2026_04.yaml` | §2.6 needs a frozen threshold with provenance |
| `calibrate.py` | §2.6 needs a label-seeing script separate from the label-blind pipeline |

## Limitations — read before quoting any number

1. **Synthetic data.** The injected defects damage high-weight evidence fields
   while the fuzzy cases damage only low-weight ones, so the two confidence
   populations cannot overlap. Real filings do overlap. These figures show the
   protocol is wired correctly; they are **not a production forecast**.
2. **The live AI path is unverified.** `normalize_ai_assisted()` has never made
   a real API call — no credentials were available in any build environment.
   Its contract, gating and fallback are fully tested offline and the request
   shape is checked against the installed SDK, but the call itself is unproven.
3. **Single batch, single period.** One return period against one prior
   snapshot. No carry-forward, amendments or incremental runs.
4. **Synthetic DRC-01C trigger.** ₹75,000 cumulative variance, not the
   statutory ₹25 lakh, which no invoice in this batch approaches.
5. **Human review is a queue, not a workflow.** The dashboard is local and
   read-only: no upload, no reviewer write-back, no assignment, no escalation.
6. **Matching is O(n²)** — it scores the full cross product. Fine at this batch
   size, the first thing to revisit at ten times it.
7. **Not a filing tool and not tax advice.** Nothing here files or amends a
   return or talks to the GSTN.

## Repository map

```
ARCHITECTURE.pdf            the locked specification
data/generate_dataset.py    seeded synthetic dataset + ground truth
src/validation.py           §2.1 input validation
src/quarantine_log.py       §3.3 quarantine (append-only)
src/normalization.py        §2.2 deterministic + AI-assisted
src/matcher.py              §2.3 one-to-one matching
src/evidence.py             §2.4 field-by-field diff
src/rule_engine.py          §2.5 rules + operational checks
src/rules/*.yaml            versioned rules and calibration
src/confidence.py           §2.6 evidence-derived confidence
src/gate.py                 §2.6 three-way gate
src/audit_log.py            §2.7 audit (append-only)
src/pipeline.py             end-to-end orchestration (label-blind)
src/report.py               §2.7 evaluation report
src/dashboard.py            local read-only dashboard
calibrate.py                §2.6 calibration (evaluation-side)
tests/                      613 tests
```
