"""§2.7 Audit log.

    "one row per record that passed validation — record_id, evidence_snapshot,
     rule_id_fired, confidence_score, action, reviewer_decision (nullable),
     timestamp"

A SQLite table alongside — but deliberately separate from — the §3.3 quarantine
log. The two describe different things and are counted separately: a
quarantined record is a data-quality failure that never reached the gate, and a
row here is a scored reconciliation decision. Keeping one table would make the
two easy to accidentally add together, which §2.1 and §2.7 both forbid.

The invariant this module enforces, rather than merely documents: a record that
was quarantined CANNOT be written here. `record()` checks the quarantine table
in the same database and refuses. "One row per record that passed validation"
is therefore true by construction, not by convention.

`evidence_snapshot` is the §2.4 diff serialised verbatim. Together with
`confidence_score` it lets an auditor re-derive the decision by hand: the score
is a weighted count of the matching fields in that very snapshot.

`reviewer_decision` is nullable and starts null. It is the human's column: the
pipeline writes a row, and a reviewer later resolves the indeterminate ones
through `record_reviewer_decision()`. Nothing in the pipeline ever writes it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .evidence import Evidence
from .gate import GateDecision
from .quarantine_log import TABLE as QUARANTINE_TABLE
from .rule_engine import RuleEvaluation
from .source_records import REPO_ROOT

DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "out", "exception_ledger.sqlite")

TABLE = "audit_log"
REVIEW_TABLE = "reviewer_events"

# APPEND-ONLY. Two things follow from that and are enforced below:
#
#   * A run never deletes or overwrites an earlier run's rows. Each run gets a
#     run_id and a run_sequence, and uniqueness is (run_id, record_id) rather
#     than record_id alone — so re-running the pipeline ADDS a generation of
#     audit rows beside the ones already there.
#   * A reviewer's decision is an EVENT, not an edit. It is inserted into
#     reviewer_events; the audit row itself is never updated. The
#     `reviewer_decision` column on audit_log is therefore the value AS AT
#     DECISION TIME, which the pipeline always writes NULL (§2.7 requires the
#     column and forbids the pipeline from deciding for a human). The effective
#     current value is resolved on read from the latest reviewer event.
#
# An audit trail that can be rewritten is not an audit trail. Nothing in this
# module issues UPDATE or DELETE against either table except `purge()`, which
# exists only for tests and says so.

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT    NOT NULL,         -- which pipeline run wrote this
    run_sequence       INTEGER NOT NULL,         -- 1, 2, 3... within this database
    record_id          TEXT    NOT NULL,         -- independent source id
    invoice_id         TEXT    NOT NULL,
    evidence_snapshot  TEXT    NOT NULL,         -- the §2.4 diff, verbatim JSON
    rule_id_fired      TEXT,                     -- NULL when no rule fired
    category           TEXT,
    confidence_score   REAL    NOT NULL,
    action             TEXT    NOT NULL,         -- the §2.6 gate outcome
    reviewer_decision  TEXT,                     -- as at decision time; always NULL
    reason             TEXT    NOT NULL,
    timestamp          TEXT    NOT NULL,
    UNIQUE(run_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_action ON {TABLE}(action);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_rule ON {TABLE}(rule_id_fired);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_run ON {TABLE}(run_id);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_record ON {TABLE}(record_id);

CREATE TABLE IF NOT EXISTS {REVIEW_TABLE} (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT    NOT NULL,
    record_id          TEXT    NOT NULL,
    reviewer_decision  TEXT    NOT NULL,
    reviewer           TEXT,
    note               TEXT,
    timestamp          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{REVIEW_TABLE}_record
    ON {REVIEW_TABLE}(run_id, record_id);
"""

# Legacy databases predate run scoping. Migration copies every existing row
# into the new shape under a reserved run id rather than dropping it — the
# whole point of this change is that history survives.
LEGACY_RUN_ID = "run-legacy"

# Sentinel for "every run in this database", distinct from None ("this run").
ALL_RUNS = object()


class QuarantinedRecordError(ValueError):
    """Raised when something tries to audit a record that failed §2.1."""


class AuditImmutableError(ValueError):
    """Raised when something tries to rewrite an existing audit row."""


@dataclass(frozen=True)
class ReviewerEvent:
    """One append-only reviewer decision. Never edited, never deleted."""

    run_id: str
    record_id: str
    reviewer_decision: str
    reviewer: Optional[str]
    note: Optional[str]
    timestamp: str


@dataclass(frozen=True)
class AuditEntry:
    run_id: str
    run_sequence: int
    record_id: str
    invoice_id: str
    evidence_snapshot: str
    rule_id_fired: Optional[str]
    category: Optional[str]
    confidence_score: float
    action: str
    reviewer_decision: Optional[str]
    reason: str
    timestamp: str

    def evidence(self) -> dict:
        return json.loads(self.evidence_snapshot)


class AuditLog:
    """Writer for the audit table. `now` is injectable for deterministic tests."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, now: Optional[str] = None,
                 run_id: Optional[str] = None):
        self.db_path = db_path
        self._now = now
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._migrate_legacy_schema()
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # A run is allocated on the FIRST WRITE, not at construction: opening
        # the log to read history must not invent an empty run. Until then,
        # reads default to the most recent run the database already holds.
        self._requested_run_id = run_id
        self._active_run: Optional[Tuple[str, int]] = None

    @property
    def run_id(self) -> str:
        return self._current_run()[0]

    @property
    def run_sequence(self) -> int:
        return self._current_run()[1]

    def _current_run(self) -> Tuple[str, int]:
        """The run this instance reads from / writes to."""
        if self._active_run is not None:
            return self._active_run
        latest = self._conn.execute(
            f"SELECT run_id, run_sequence FROM {TABLE} "
            f"ORDER BY run_sequence DESC, id DESC LIMIT 1").fetchone()
        if latest is not None and self._requested_run_id is None:
            return latest["run_id"], int(latest["run_sequence"])
        return self._allocate_run()

    def _allocate_run(self) -> Tuple[str, int]:
        if self._active_run is None:
            sequence = self._next_run_sequence()
            self._active_run = (
                self._requested_run_id or f"run-{sequence:04d}", sequence)
        return self._active_run

    # -- schema migration ---------------------------------------------------

    def _columns(self, table: str) -> List[str]:
        return [r["name"] for r in
                self._conn.execute(f"PRAGMA table_info({table})").fetchall()]

    def _migrate_legacy_schema(self) -> None:
        """Bring a pre-run-scoping database forward WITHOUT losing rows."""
        existing = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,)).fetchone()
        if not existing or "run_id" in self._columns(TABLE):
            return
        self._conn.executescript(f"""
            ALTER TABLE {TABLE} RENAME TO {TABLE}_legacy;
        """)
        self._conn.executescript(SCHEMA)
        self._conn.execute(f"""
            INSERT INTO {TABLE}
                (run_id, run_sequence, record_id, invoice_id, evidence_snapshot,
                 rule_id_fired, category, confidence_score, action,
                 reviewer_decision, reason, timestamp)
            SELECT ?, 0, record_id, invoice_id, evidence_snapshot,
                   rule_id_fired, category, confidence_score, action,
                   NULL, reason, timestamp
            FROM {TABLE}_legacy
        """, (LEGACY_RUN_ID,))
        # A legacy reviewer_decision was an in-place edit; preserve it as the
        # event it should always have been.
        self._conn.execute(f"""
            INSERT INTO {REVIEW_TABLE}
                (run_id, record_id, reviewer_decision, reviewer, note, timestamp)
            SELECT ?, record_id, reviewer_decision, NULL,
                   'migrated from legacy audit_log.reviewer_decision', timestamp
            FROM {TABLE}_legacy WHERE reviewer_decision IS NOT NULL
        """, (LEGACY_RUN_ID,))
        self._conn.execute(f"DROP TABLE {TABLE}_legacy")
        self._conn.commit()

    def _next_run_sequence(self) -> int:
        row = self._conn.execute(
            f"SELECT COALESCE(MAX(run_sequence), 0) AS m FROM {TABLE}").fetchone()
        return int(row["m"]) + 1

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def purge(self) -> None:
        """Delete EVERYTHING. Test fixtures only.

        The audit log is append-only in normal operation: the pipeline never
        calls this, and a test asserts src/pipeline.py does not reference it.
        It exists so a test can start from an empty database, not so a run can
        erase its predecessors.
        """
        self._conn.execute(f"DELETE FROM {TABLE}")
        self._conn.execute(f"DELETE FROM {REVIEW_TABLE}")
        self._conn.commit()

    def tables(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def _timestamp(self) -> str:
        if self._now is not None:
            return self._now
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- the §2.1 boundary, enforced ---------------------------------------

    def _quarantined_ids(self) -> set:
        """Record ids sitting in the quarantine table of this same database."""
        exists = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (QUARANTINE_TABLE,),
        ).fetchone()
        if not exists:
            return set()
        rows = self._conn.execute(
            f"SELECT record_id FROM {QUARANTINE_TABLE}"
        ).fetchall()
        return {r["record_id"] for r in rows}

    # -- writing ------------------------------------------------------------

    def record(self, decision: GateDecision, evidence: Evidence,
               evaluation: Optional[RuleEvaluation] = None) -> AuditEntry:
        """Write one row. Refuses any record that was quarantined (§2.1)."""
        if decision.record_id in self._quarantined_ids():
            raise QuarantinedRecordError(
                f"{decision.record_id} was quarantined at validation and must "
                "not appear in the audit log; §2.1 keeps it off the scored path "
                "entirely"
            )
        if evidence.pr_record_id and evidence.pr_record_id != decision.record_id:
            raise ValueError(
                f"evidence is for {evidence.pr_record_id} but the decision is "
                f"for {decision.record_id}"
            )

        run_id, run_sequence = self._allocate_run()
        entry = AuditEntry(
            run_id=run_id,
            run_sequence=run_sequence,
            record_id=decision.record_id,
            invoice_id=decision.invoice_id,
            evidence_snapshot=json.dumps(
                {
                    "invoice_id": evidence.invoice_id,
                    "candidate_found": evidence.candidate_found,
                    "b2_record_id": evidence.b2_record_id,
                    "fields": evidence.field_map(),
                },
                sort_keys=True,
            ),
            rule_id_fired=decision.rule_id,
            category=decision.category,
            confidence_score=decision.confidence.value,
            action=decision.outcome,
            reviewer_decision=None,
            reason=decision.reason,
            timestamp=self._timestamp(),
        )
        # INSERT, never INSERT OR REPLACE: an existing row for this
        # (run_id, record_id) means something tried to rewrite history.
        try:
            self._conn.execute(
                f"""INSERT INTO {TABLE}
                    (run_id, run_sequence, record_id, invoice_id,
                     evidence_snapshot, rule_id_fired, category,
                     confidence_score, action, reviewer_decision, reason,
                     timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (entry.run_id, entry.run_sequence, entry.record_id,
                 entry.invoice_id, entry.evidence_snapshot, entry.rule_id_fired,
                 entry.category, entry.confidence_score, entry.action,
                 None, entry.reason, entry.timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise AuditImmutableError(
                f"{entry.record_id} already has an audit row for run "
                f"{entry.run_id}; audit rows are append-only and are never "
                "rewritten"
            ) from exc
        self._conn.commit()
        return entry

    def record_all(self, decisions: Sequence[GateDecision],
                   evidences: Sequence[Evidence]) -> List[AuditEntry]:
        if len(decisions) != len(evidences):
            raise ValueError("decisions and evidences must correspond one to one")
        return [self.record(d, e) for d, e in zip(decisions, evidences)]

    def record_reviewer_decision(self, record_id: str, decision: str,
                                 reviewer: Optional[str] = None,
                                 note: Optional[str] = None,
                                 run_id: Optional[str] = None) -> ReviewerEvent:
        """Append a reviewer decision. Nothing in the pipeline calls this.

        This INSERTS an event; it never updates the audit row. A record can
        therefore carry several decisions over time and each one survives —
        the effective value is the latest, resolved on read.
        """
        target_run = run_id or self._latest_run_for(record_id)
        if target_run is None:
            raise KeyError(f"{record_id} is not in the audit log")
        event = ReviewerEvent(
            run_id=target_run, record_id=record_id, reviewer_decision=decision,
            reviewer=reviewer, note=note, timestamp=self._timestamp(),
        )
        self._conn.execute(
            f"""INSERT INTO {REVIEW_TABLE}
                (run_id, record_id, reviewer_decision, reviewer, note, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)""",
            (event.run_id, event.record_id, event.reviewer_decision,
             event.reviewer, event.note, event.timestamp),
        )
        self._conn.commit()
        return event

    def _latest_run_for(self, record_id: str) -> Optional[str]:
        row = self._conn.execute(
            f"SELECT run_id FROM {TABLE} WHERE record_id = ? "
            f"ORDER BY run_sequence DESC, id DESC LIMIT 1", (record_id,)
        ).fetchone()
        return row["run_id"] if row else None

    def reviewer_events(self, record_id: Optional[str] = None) -> List[ReviewerEvent]:
        """Every reviewer event, oldest first. Nothing is ever removed."""
        if record_id is None:
            rows = self._conn.execute(
                f"SELECT * FROM {REVIEW_TABLE} ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT * FROM {REVIEW_TABLE} WHERE record_id = ? ORDER BY id",
                (record_id,)).fetchall()
        return [ReviewerEvent(
            run_id=r["run_id"], record_id=r["record_id"],
            reviewer_decision=r["reviewer_decision"], reviewer=r["reviewer"],
            note=r["note"], timestamp=r["timestamp"]) for r in rows]

    def _effective_reviewer_decisions(self, run_id: Optional[str] = None) -> Dict[str, str]:
        """Latest reviewer decision per record, resolved from the event log."""
        if run_id is None:
            rows = self._conn.execute(
                f"SELECT record_id, reviewer_decision FROM {REVIEW_TABLE} "
                f"ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT record_id, reviewer_decision FROM {REVIEW_TABLE} "
                f"WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return {r["record_id"]: r["reviewer_decision"] for r in rows}

    # -- reading ------------------------------------------------------------

    # Reads default to THIS run. Pass run_id=ALL_RUNS to see the whole
    # history — the point of an append-only log is that it is still there.

    def count(self, run_id: Optional[str] = None) -> int:
        """Rows in one run (default: this one), or ALL_RUNS for the history."""
        if run_id is ALL_RUNS:
            return self._conn.execute(
                f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        return self._conn.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE run_id = ?",
            (run_id or self.run_id,)).fetchone()[0]

    def runs(self) -> List[Dict[str, object]]:
        """Every run this database holds, oldest first."""
        rows = self._conn.execute(
            f"SELECT run_id, run_sequence, COUNT(*) AS rows, MIN(timestamp) AS "
            f"first_seen FROM {TABLE} GROUP BY run_id, run_sequence "
            f"ORDER BY run_sequence").fetchall()
        return [{"run_id": r["run_id"], "run_sequence": r["run_sequence"],
                 "rows": r["rows"], "first_seen": r["first_seen"]} for r in rows]

    def _scope(self, run_id: Optional[str]):
        if run_id is ALL_RUNS:
            return "", ()
        return " WHERE run_id = ?", (run_id or self.run_id,)

    def counts_by_action(self, run_id: Optional[str] = None) -> Dict[str, int]:
        where, params = self._scope(run_id)
        rows = self._conn.execute(
            f"SELECT action, COUNT(*) AS n FROM {TABLE}{where} GROUP BY action "
            f"ORDER BY action", params).fetchall()
        return {r["action"]: r["n"] for r in rows}

    def counts_by_rule(self, run_id: Optional[str] = None) -> Dict[str, int]:
        where, params = self._scope(run_id)
        rows = self._conn.execute(
            f"SELECT rule_id_fired, COUNT(*) AS n FROM {TABLE}{where} "
            f"GROUP BY rule_id_fired ORDER BY rule_id_fired", params).fetchall()
        return {(r["rule_id_fired"] or "(none)"): r["n"] for r in rows}

    def _row_to_entry(self, r, resolved: Optional[Dict[str, str]] = None) -> AuditEntry:
        """`reviewer_decision` is resolved from the event log, never from the
        stored column — the column is the decision-time snapshot and is always
        NULL."""
        decision = (resolved or {}).get(r["record_id"])
        return AuditEntry(
            run_id=r["run_id"], run_sequence=r["run_sequence"],
            record_id=r["record_id"], invoice_id=r["invoice_id"],
            evidence_snapshot=r["evidence_snapshot"],
            rule_id_fired=r["rule_id_fired"], category=r["category"],
            confidence_score=r["confidence_score"], action=r["action"],
            reviewer_decision=decision, reason=r["reason"],
            timestamp=r["timestamp"],
        )

    def entries(self, action: Optional[str] = None,
                run_id: Optional[str] = None) -> List[AuditEntry]:
        where, params = self._scope(run_id)
        if action is not None:
            joiner = " AND" if where else " WHERE"
            where = f"{where}{joiner} action = ?"
            params = params + (action,)
        rows = self._conn.execute(
            f"SELECT * FROM {TABLE}{where} ORDER BY run_sequence, record_id",
            params).fetchall()
        resolved = self._effective_reviewer_decisions(
            None if run_id is ALL_RUNS else (run_id or self.run_id))
        return [self._row_to_entry(r, resolved) for r in rows]

    def entry(self, record_id: str,
              run_id: Optional[str] = None) -> Optional[AuditEntry]:
        """The record's row in one run (default: the most recent run holding it)."""
        if run_id is ALL_RUNS:
            run_id = None
        target = run_id or self._latest_run_for(record_id)
        if target is None:
            return None
        row = self._conn.execute(
            f"SELECT * FROM {TABLE} WHERE record_id = ? AND run_id = ?",
            (record_id, target)).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row, self._effective_reviewer_decisions(target))

    def history(self, record_id: str) -> List[AuditEntry]:
        """Every generation of this record's audit row, oldest run first."""
        rows = self._conn.execute(
            f"SELECT * FROM {TABLE} WHERE record_id = ? ORDER BY run_sequence, id",
            (record_id,)).fetchall()
        resolved = self._effective_reviewer_decisions(None)
        return [self._row_to_entry(r, resolved) for r in rows]

    def pending_review(self, run_id: Optional[str] = None) -> List[AuditEntry]:
        """Indeterminate records with no reviewer event yet."""
        return [e for e in self.entries("indeterminate", run_id)
                if e.reviewer_decision is None]
