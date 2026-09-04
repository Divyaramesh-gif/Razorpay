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
from typing import Iterable, List, Optional, Tuple

from .source_records import REPO_ROOT
from .validation import ValidationResult

DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "out", "exception_ledger.sqlite")

TABLE = "quarantine_log"

# APPEND-ONLY, on the same terms as the audit log. A quarantine row is the
# evidence that a record was rejected and why; a later run must not be able to
# erase that. Rows are scoped by run_id/run_sequence and uniqueness is
# (run_id, record_id), so re-running ADDS a generation rather than replacing one.
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT    NOT NULL,   -- which pipeline run wrote this
    run_sequence        INTEGER NOT NULL,   -- 1, 2, 3... within this database
    record_id           TEXT    NOT NULL,   -- independent source id, <source>:<row key>
    source              TEXT    NOT NULL,
    source_record_id    TEXT    NOT NULL,
    source_row_number   INTEGER NOT NULL,
    validation_error    TEXT    NOT NULL,   -- error type (§2.1 taxonomy)
    validation_message  TEXT    NOT NULL,   -- human-readable detail
    error_field         TEXT    NOT NULL,
    raw_record_snapshot TEXT    NOT NULL,   -- the source row, verbatim, as JSON
    timestamp           TEXT    NOT NULL,
    UNIQUE(run_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_error ON {TABLE}(validation_error);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_run ON {TABLE}(run_id);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_record ON {TABLE}(record_id);
"""

LEGACY_RUN_ID = "run-legacy"

# Sentinel for "every run in this database", distinct from None ("this run").
ALL_RUNS = object()


class QuarantineImmutableError(ValueError):
    """Raised when something tries to rewrite an existing quarantine row."""


@dataclass(frozen=True)
class QuarantineEntry:
    run_id: str
    run_sequence: int
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
        # Allocated on first WRITE, so opening the log to read history does not
        # invent an empty run.
        self._requested_run_id = run_id
        self._active_run: Optional[Tuple[str, int]] = None

    # -- run identity -------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._current_run()[0]

    @property
    def run_sequence(self) -> int:
        return self._current_run()[1]

    def _current_run(self) -> Tuple[str, int]:
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
            row = self._conn.execute(
                f"SELECT COALESCE(MAX(run_sequence), 0) AS m FROM {TABLE}").fetchone()
            sequence = int(row["m"]) + 1
            self._active_run = (
                self._requested_run_id or f"run-{sequence:04d}", sequence)
        return self._active_run

    # -- schema migration ---------------------------------------------------

    def _migrate_legacy_schema(self) -> None:
        """Bring a pre-run-scoping database forward WITHOUT losing rows."""
        existing = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE,)).fetchone()
        if not existing:
            return
        columns = [r["name"] for r in
                   self._conn.execute(f"PRAGMA table_info({TABLE})").fetchall()]
        if "run_id" in columns:
            return
        self._conn.executescript(f"ALTER TABLE {TABLE} RENAME TO {TABLE}_legacy;")
        self._conn.executescript(SCHEMA)
        self._conn.execute(f"""
            INSERT INTO {TABLE}
                (run_id, run_sequence, record_id, source, source_record_id,
                 source_row_number, validation_error, validation_message,
                 error_field, raw_record_snapshot, timestamp)
            SELECT ?, 0, record_id, source, source_record_id, source_row_number,
                   validation_error, validation_message, error_field,
                   raw_record_snapshot, timestamp
            FROM {TABLE}_legacy
        """, (LEGACY_RUN_ID,))
        self._conn.execute(f"DROP TABLE {TABLE}_legacy")
        self._conn.commit()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "QuarantineLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def purge(self) -> None:
        """Delete EVERYTHING. Test fixtures only.

        The quarantine log is append-only in normal operation: the pipeline
        never calls this, and a test asserts src/pipeline.py does not reference
        it. A rejected record's evidence is not a later run's to erase.
        """
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
        run_id, run_sequence = self._allocate_run()
        entry = QuarantineEntry(
            run_id=run_id,
            run_sequence=run_sequence,
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
        # INSERT, never INSERT OR REPLACE: an existing row for this
        # (run_id, record_id) means something tried to rewrite history.
        try:
            self._conn.execute(
                f"""INSERT INTO {TABLE}
                    (run_id, run_sequence, record_id, source, source_record_id,
                     source_row_number, validation_error, validation_message,
                     error_field, raw_record_snapshot, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.run_id, entry.run_sequence, entry.record_id,
                    entry.source, entry.source_record_id,
                    entry.source_row_number, entry.validation_error,
                    entry.validation_message, entry.error_field,
                    entry.raw_record_snapshot, entry.timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise QuarantineImmutableError(
                f"{entry.record_id} already has a quarantine row for run "
                f"{entry.run_id}; quarantine rows are append-only and are "
                "never rewritten"
            ) from exc
        self._conn.commit()
        return entry

    def quarantine_all(self, results: Iterable[ValidationResult]) -> List[QuarantineEntry]:
        return [self.quarantine(r) for r in results]

    # -- reading ------------------------------------------------------------

    # Reads default to THIS run. Pass run_id=ALL_RUNS for the whole history.

    def _scope(self, run_id):
        if run_id is ALL_RUNS:
            return "", ()
        return " WHERE run_id = ?", (run_id or self.run_id,)

    def count(self, run_id=None) -> int:
        where, params = self._scope(run_id)
        return self._conn.execute(
            f"SELECT COUNT(*) FROM {TABLE}{where}", params).fetchone()[0]

    def runs(self) -> List[dict]:
        """Every run this database holds, oldest first."""
        rows = self._conn.execute(
            f"SELECT run_id, run_sequence, COUNT(*) AS rows, MIN(timestamp) AS "
            f"first_seen FROM {TABLE} GROUP BY run_id, run_sequence "
            f"ORDER BY run_sequence").fetchall()
        return [{"run_id": r["run_id"], "run_sequence": r["run_sequence"],
                 "rows": r["rows"], "first_seen": r["first_seen"]} for r in rows]

    def counts_by_error(self, run_id=None) -> dict:
        where, params = self._scope(run_id)
        rows = self._conn.execute(
            f"SELECT validation_error, COUNT(*) AS n FROM {TABLE}{where} "
            f"GROUP BY validation_error ORDER BY validation_error", params
        ).fetchall()
        return {r["validation_error"]: r["n"] for r in rows}

    def _row_to_entry(self, r) -> QuarantineEntry:
        return QuarantineEntry(
            run_id=r["run_id"], run_sequence=r["run_sequence"],
            record_id=r["record_id"], source=r["source"],
            source_record_id=r["source_record_id"],
            source_row_number=r["source_row_number"],
            validation_error=r["validation_error"],
            validation_message=r["validation_message"],
            error_field=r["error_field"],
            raw_record_snapshot=r["raw_record_snapshot"],
            timestamp=r["timestamp"],
        )

    def entries(self, run_id=None) -> List[QuarantineEntry]:
        where, params = self._scope(run_id)
        rows = self._conn.execute(
            f"SELECT * FROM {TABLE}{where} "
            f"ORDER BY run_sequence, source, source_row_number", params
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def history(self, record_id: str) -> List[QuarantineEntry]:
        """Every generation of this record's quarantine row, oldest run first."""
        rows = self._conn.execute(
            f"SELECT * FROM {TABLE} WHERE record_id = ? ORDER BY run_sequence, id",
            (record_id,)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def snapshot(self, record_id: str, run_id=None) -> Optional[dict]:
        """The quarantined record's original source row, as a dict."""
        row = self._conn.execute(
            f"SELECT raw_record_snapshot FROM {TABLE} WHERE record_id = ? "
            f"ORDER BY run_sequence DESC, id DESC LIMIT 1", (record_id,),
        ).fetchone()
        return json.loads(row["raw_record_snapshot"]) if row else None

    def tables(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]
