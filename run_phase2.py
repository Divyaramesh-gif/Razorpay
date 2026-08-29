#!/usr/bin/env python3
"""Phase 2 end-to-end runner: validate -> quarantine -> normalise.

Temporary scaffolding, NOT part of the locked §4 layout. src/pipeline.py
(build-order step 8) is the real orchestrator; this exists only so Phase 2 can
be exercised and inspected before steps 4-7 exist. It will be absorbed into
pipeline.py and deleted.

Reads only the two source CSVs and writes only into out/. The input files are
never modified, and ground_truth.csv is never opened.

    python3 run_phase2.py              # deterministic normalisation only
    python3 run_phase2.py --ai         # also run the §2.2 AI-assisted half
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import normalization as N
from src import validation as V
from src.quarantine_log import QuarantineLog
from src.source_records import (
    REPO_ROOT,
    SOURCE_GSTR2B,
    SOURCE_PURCHASE_REGISTER,
    load_source,
)

OUT_DIR = os.path.join(REPO_ROOT, "out")
DB_PATH = os.path.join(OUT_DIR, "exception_ledger.sqlite")


def write_normalized_csv(path, results):
    """Both views, side by side: <field>_raw and <field>_normalized (§2.2)."""
    if not results:
        return 0
    fields = list(results[0].raw.keys())
    header = (["source_id", "source", "record_id", "row_number"]
              + [f"{f}_raw" for f in fields]
              + [f"{f}_normalized" for f in fields]
              + ["normalized_fields", "ai_assisted_fields"])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        for r in results:
            ai = [c.field for c in r.changes if c.method == N.AI_ASSISTED]
            writer.writerow(
                [r.source_id, r.source, r.record_id, r.row_number]
                + [r.raw.get(f, "") for f in fields]
                + [r.normalized.get(f, "") for f in fields]
                + ["|".join(r.changed_fields()), "|".join(ai)]
            )
    return len(results)


def write_quarantine_csv(path, entries):
    header = ["record_id", "source", "source_record_id", "source_row_number",
              "validation_error", "validation_message", "error_field",
              "raw_record_snapshot", "timestamp"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        for e in entries:
            writer.writerow([getattr(e, c) for c in header])
    return len(entries)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Exception Ledger — Phase 2 run")
    ap.add_argument("--ai", action="store_true",
                    help="also run the §2.2 AI-assisted half (needs credentials)")
    args = ap.parse_args(argv)

    os.makedirs(OUT_DIR, exist_ok=True)

    client = None
    if args.ai:
        client = N.build_client()
        if client is None:
            print("  !! --ai requested but no Anthropic client is available; "
                  "running deterministic-only\n")

    print("Exception Ledger — Phase 2 (validation -> quarantine -> normalisation)\n")

    summary = {"ai_assisted": client is not None, "sources": {}}
    all_entries = []
    totals = Counter()
    ai_stats = Counter()

    with QuarantineLog(DB_PATH) as log:
        log.clear()          # one run reports one batch, not an accumulation

        for source in (SOURCE_PURCHASE_REGISTER, SOURCE_GSTR2B):
            records = load_source(source)
            valid, invalid = V.partition(records)
            log.quarantine_all(invalid)

            normalised = [N.normalize(r.record, client, ai_stats) for r in valid]
            out_csv = os.path.join(OUT_DIR, f"normalized_{source}.csv")
            write_normalized_csv(out_csv, normalised)

            changed = sum(1 for r in normalised if r.changes)
            ai_changed = sum(
                1 for r in normalised
                if any(c.method == N.AI_ASSISTED for c in r.changes)
            )
            errors = Counter(r.error.error_type for r in invalid)

            summary["sources"][source] = {
                "read": len(records),
                "valid": len(valid),
                "quarantined": len(invalid),
                "quarantine_rate_pct": round(100 * len(invalid) / len(records), 2),
                "normalised_with_changes": changed,
                "ai_assisted_records": ai_changed,
                "quarantine_errors": dict(sorted(errors.items())),
                "output": os.path.relpath(out_csv, REPO_ROOT),
            }
            totals["read"] += len(records)
            totals["valid"] += len(valid)
            totals["quarantined"] += len(invalid)

            print(f"  {source}")
            print(f"    read              {len(records):>4}")
            print(f"    valid             {len(valid):>4}  -> normalised "
                  f"({changed} record(s) changed, {ai_changed} via AI)")
            print(f"    quarantined       {len(invalid):>4}  "
                  f"({100 * len(invalid) / len(records):.1f}% of source)")
            for error_type, n in sorted(errors.items()):
                print(f"        {error_type:<34} {n:>3}")
            print()

        all_entries = log.entries()
        q_csv = os.path.join(OUT_DIR, "quarantine_log.csv")
        write_quarantine_csv(q_csv, all_entries)
        summary["quarantine_log"] = {
            "db": os.path.relpath(DB_PATH, REPO_ROOT),
            "table": "quarantine_log",
            "csv": os.path.relpath(q_csv, REPO_ROOT),
            "rows": log.count(),
            "by_error": log.counts_by_error(),
        }
        summary["totals"] = dict(totals)
        summary["ai"] = dict(ai_stats)

    with open(os.path.join(OUT_DIR, "phase2_summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"  BATCH   {totals['read']} records read, {totals['valid']} valid, "
          f"{totals['quarantined']} quarantined "
          f"({100 * totals['quarantined'] / totals['read']:.1f}% of batch)")

    if client is None:
        print("\n  AI-assisted normalisation (§2.2): NOT RUN "
              "(deterministic half only).")
    else:
        attempted = ai_stats[N.AI_ATTEMPTED]
        print(f"\n  AI-assisted normalisation (§2.2): {attempted} field(s) sent, "
              f"{ai_stats[N.AI_APPLIED]} repaired, "
              f"{ai_stats[N.AI_UNCHANGED]} returned unchanged, "
              f"{ai_stats[N.AI_CONTRACT_VIOLATION]} rejected by the output "
              f"contract, {ai_stats[N.AI_CALL_FAILED]} call(s) failed")
        dead = ai_stats[N.AI_CONTRACT_VIOLATION] + ai_stats[N.AI_CALL_FAILED]
        if attempted and dead == attempted:
            print("  !! every AI call failed — the deterministic result stands "
                  "for all of them. Check credentials before trusting this run's")
            print("     normalisation as AI-assisted.")
    print("\n  Quarantined records exit here (§2.1). They are not counted in the")
    print("  match rate, the exception count or the indeterminate count.\n")

    if all_entries:
        e = all_entries[0]
        print("  Sample quarantine row:")
        print(f"    record_id          {e.record_id}")
        print(f"    source row         {e.source} line {e.source_row_number + 1}")
        print(f"    error type         {e.validation_error}")
        print(f"    error message      {e.validation_message}")
        snap = json.loads(e.raw_record_snapshot)
        print(f"    raw snapshot       {len(snap)} fields captured verbatim")

    print("\n  Files written:")
    for name in sorted(os.listdir(OUT_DIR)):
        path = os.path.join(OUT_DIR, name)
        print(f"    out/{name:<34} {os.path.getsize(path):>8,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
