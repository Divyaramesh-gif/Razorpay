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
from typing import Dict, Iterable, List, Optional, Sequence

from .evidence import Evidence
from .gate import GateDecision
from .quarantine_log import TABLE as QUARANTINE_TABLE
from .rule_engine import RuleEvaluation
from .source_records import REPO_ROOT

DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "out", "exception_ledger.sqlite")

TABLE = "audit_log"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id          TEXT    NOT NULL UNIQUE,  -- independent source id
    invoice_id         TEXT    NOT NULL,
    evidence_snapshot  TEXT    NOT NULL,         -- the §2.4 diff, verbatim JSON
    rule_id_fired      TEXT,                     -- NULL when no rule fired
    category           TEXT,
    confidence_score   REAL    NOT NULL,
    action             TEXT    NOT NULL,         -- the §2.6 gate outcome
    reviewer_decision  TEXT,                     -- NULL until a human resolves it
    reason             TEXT    NOT NULL,
    timestamp          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_action ON {TABLE}(action);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_rule ON {TABLE}(rule_id_fired);
"""


class QuarantinedRecordError(ValueError):
    """Raised when something tries to audit a record that failed §2.1."""


@dataclass(frozen=True)
class AuditEntry:
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

    def __init__(self, db_path: str = DEFAULT_DB_PATH, now: Optional[str] = None):
        self.db_path = db_path
        self._now = now
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def clear(self) -> None:
        self._conn.execute(f"DELETE FROM {TABLE}")
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

        entry = AuditEntry(
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
        self._conn.execute(
            f"""INSERT OR REPLACE INTO {TABLE}
                (record_id, invoice_id, evidence_snapshot, rule_id_fired,
                 category, confidence_score, action, reviewer_decision,
                 reason, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry.record_id, entry.invoice_id, entry.evidence_snapshot,
             entry.rule_id_fired, entry.category, entry.confidence_score,
             entry.action, entry.reviewer_decision, entry.reason,
             entry.timestamp),
        )
        self._conn.commit()
        return entry

    def record_all(self, decisions: Sequence[GateDecision],
                   evidences: Sequence[Evidence]) -> List[AuditEntry]:
        if len(decisions) != len(evidences):
            raise ValueError("decisions and evidences must correspond one to one")
        return [self.record(d, e) for d, e in zip(decisions, evidences)]

    def record_reviewer_decision(self, record_id: str, decision: str) -> None:
        """The human's column. Nothing in the pipeline calls this."""
        updated = self._conn.execute(
            f"UPDATE {TABLE} SET reviewer_decision = ? WHERE record_id = ?",
            (decision, record_id),
        ).rowcount
        if not updated:
            raise KeyError(f"{record_id} is not in the audit log")
        self._conn.commit()

    # -- reading ------------------------------------------------------------

    def count(self) -> int:
        return self._conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]

    def counts_by_action(self) -> Dict[str, int]:
        rows = self._conn.execute(
            f"SELECT action, COUNT(*) AS n FROM {TABLE} GROUP BY action "
            f"ORDER BY action"
        ).fetchall()
        return {r["action"]: r["n"] for r in rows}

    def counts_by_rule(self) -> Dict[str, int]:
        rows = self._conn.execute(
            f"SELECT rule_id_fired, COUNT(*) AS n FROM {TABLE} "
            f"GROUP BY rule_id_fired ORDER BY rule_id_fired"
        ).fetchall()
        return {(r["rule_id_fired"] or "(none)"): r["n"] for r in rows}

    def _row_to_entry(self, r) -> AuditEntry:
        return AuditEntry(
            record_id=r["record_id"], invoice_id=r["invoice_id"],
            evidence_snapshot=r["evidence_snapshot"],
            rule_id_fired=r["rule_id_fired"], category=r["category"],
            confidence_score=r["confidence_score"], action=r["action"],
            reviewer_decision=r["reviewer_decision"], reason=r["reason"],
            timestamp=r["timestamp"],
        )

    def entries(self, action: Optional[str] = None) -> List[AuditEntry]:
        if action is None:
            rows = self._conn.execute(
                f"SELECT * FROM {TABLE} ORDER BY record_id").fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT * FROM {TABLE} WHERE action = ? ORDER BY record_id",
                (action,)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def entry(self, record_id: str) -> Optional[AuditEntry]:
        row = self._conn.execute(
            f"SELECT * FROM {TABLE} WHERE record_id = ?", (record_id,)).fetchone()
        return self._row_to_entry(row) if row else None

    def pending_review(self) -> List[AuditEntry]:
        rows = self._conn.execute(
            f"SELECT * FROM {TABLE} WHERE reviewer_decision IS NULL "
            f"AND action = 'indeterminate' ORDER BY record_id"
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]
