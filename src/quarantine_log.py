"""§3.3 Quarantine log — build-order step 2.

A SQLite table kept deliberately separate from the audit log. §2.1: a record
that fails validation "goes no further" and "is not counted in the match rate,
the exception count, or the indeterminate count — it gets its own reported
number". Merging these two logs would be the first step towards merging those
two numbers, so they are separate tables in the same database file.

Columns are §3.3's four (record_id, validation_error, raw_record_snapshot,
timestamp) plus the provenance needed to actually find the offending row again:
the source file, its row number, and a human-readable message alongside the
machine-readable error type.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from .source_records import REPO_ROOT
from .validation import ValidationResult

DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "out", "exception_ledger.sqlite")

TABLE = "quarantine_log"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id           TEXT    NOT NULL,   -- independent source id, <source>:<row key>
    source              TEXT    NOT NULL,
    source_record_id    TEXT    NOT NULL,
    source_row_number   INTEGER NOT NULL,
    validation_error    TEXT    NOT NULL,   -- error type (§2.1 taxonomy)
    validation_message  TEXT    NOT NULL,   -- human-readable detail
    error_field         TEXT    NOT NULL,
    raw_record_snapshot TEXT    NOT NULL,   -- the source row, verbatim, as JSON
    timestamp           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_error ON {TABLE}(validation_error);
CREATE UNIQUE INDEX IF NOT EXISTS idx_{TABLE}_record ON {TABLE}(record_id);
"""


@dataclass(frozen=True)
class QuarantineEntry:
    record_id: str
    source: str
    source_record_id: str
    source_row_number: int
    validation_error: str
    validation_message: str
    error_field: str
    raw_record_snapshot: str
    timestamp: str


class QuarantineLog:
    """Writer for the quarantine table.

    `now` is injectable so tests get deterministic timestamps; production leaves
    it unset and gets UTC wall-clock.
    """

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

    def __enter__(self) -> "QuarantineLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def clear(self) -> None:
        """Drop all rows. A pipeline run reports one batch, not an accumulation."""
        self._conn.execute(f"DELETE FROM {TABLE}")
        self._conn.commit()

    # -- writing ------------------------------------------------------------

    def _timestamp(self) -> str:
        if self._now is not None:
            return self._now
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def quarantine(self, result: ValidationResult) -> QuarantineEntry:
        """Record one failed validation. Raises if the result is actually valid."""
        if result.is_valid:
            raise ValueError(
                f"{result.record.source_id} passed validation; "
                "only failed records may be quarantined"
            )
        record, error = result.record, result.error
        entry = QuarantineEntry(
            record_id=record.source_id,
            source=record.source,
            source_record_id=record.record_id,
            source_row_number=record.row_number,
            validation_error=error.error_type,
            validation_message=error.message,
            error_field=error.field,
            raw_record_snapshot=json.dumps(record.raw, sort_keys=True),
            timestamp=self._timestamp(),
        )
        self._conn.execute(
            f"""INSERT OR REPLACE INTO {TABLE}
                (record_id, source, source_record_id, source_row_number,
                 validation_error, validation_message, error_field,
                 raw_record_snapshot, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.record_id, entry.source, entry.source_record_id,
                entry.source_row_number, entry.validation_error,
                entry.validation_message, entry.error_field,
                entry.raw_record_snapshot, entry.timestamp,
            ),
        )
        self._conn.commit()
        return entry

    def quarantine_all(self, results: Iterable[ValidationResult]) -> List[QuarantineEntry]:
        return [self.quarantine(r) for r in results]

    # -- reading ------------------------------------------------------------

    def count(self) -> int:
        return self._conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]

    def counts_by_error(self) -> dict:
        rows = self._conn.execute(
            f"SELECT validation_error, COUNT(*) AS n FROM {TABLE} "
            f"GROUP BY validation_error ORDER BY validation_error"
        ).fetchall()
        return {r["validation_error"]: r["n"] for r in rows}

    def entries(self) -> List[QuarantineEntry]:
        rows = self._conn.execute(
            f"SELECT * FROM {TABLE} ORDER BY source, source_row_number"
        ).fetchall()
        return [
            QuarantineEntry(
                record_id=r["record_id"], source=r["source"],
                source_record_id=r["source_record_id"],
                source_row_number=r["source_row_number"],
                validation_error=r["validation_error"],
                validation_message=r["validation_message"],
                error_field=r["error_field"],
                raw_record_snapshot=r["raw_record_snapshot"],
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    def snapshot(self, record_id: str) -> Optional[dict]:
        """The quarantined record's original source row, as a dict."""
        row = self._conn.execute(
            f"SELECT raw_record_snapshot FROM {TABLE} WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        return json.loads(row["raw_record_snapshot"]) if row else None

    def tables(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]
