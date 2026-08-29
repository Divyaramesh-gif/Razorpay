"""Source record loading and identity.

Phase 2 support module. Holds the one record shape every stage downstream of
CSV loading consumes, so validation, quarantine and normalisation all agree on
what "a record" is — the same discipline §3.2 applies to the Evidence object.

Two rules this module exists to enforce:

  1. **Independent source IDs.** A record's identity is `<source>:<record_id>`,
     built from the file it came from and that file's own primary key. The two
     source files number their rows independently (PR-0001, 2B-0001), so a bare
     record_id is ambiguous across sources; `source_id` never is. Row numbers
     are carried too, so a quarantined record can be found by eye in the CSV.

  2. **Ground truth is not an input.** The only files this module will open are
     the two pipeline source CSVs. `data/ground_truth.csv` is for the evaluation
     script (§2.6, §2.7) and must never reach the pipeline; loading it here is
     refused outright. tests/test_source_preservation.py asserts no module under
     src/ so much as names it.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Dict, Iterator, List

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")

PURCHASE_REGISTER_CSV = os.path.join(DATA_DIR, "purchase_register.csv")
GSTR2B_CSV = os.path.join(DATA_DIR, "gstr2b.csv")

SOURCE_PURCHASE_REGISTER = "purchase_register"
SOURCE_GSTR2B = "gstr2b"

# Files the pipeline is allowed to read. Anything else is a programming error.
PIPELINE_SOURCES = {
    SOURCE_PURCHASE_REGISTER: PURCHASE_REGISTER_CSV,
    SOURCE_GSTR2B: GSTR2B_CSV,
}


@dataclass(frozen=True)
class SourceRecord:
    """One row of one source file, exactly as it was read.

    `raw` is never mutated by any stage. Normalisation produces a separate
    cleaned mapping and leaves this one alone (§2.2 keeps raw and normalised
    values side by side so the audit log can show both).
    """

    source: str
    record_id: str
    row_number: int
    raw: Dict[str, str] = field(default_factory=dict)

    @property
    def source_id(self) -> str:
        """Globally unique id across both source files."""
        return f"{self.source}:{self.record_id}"

    def get(self, field_name: str, default: str = "") -> str:
        return self.raw.get(field_name, default)


def load_source(source: str, path: str = "") -> List[SourceRecord]:
    """Read one pipeline source file into SourceRecords.

    Raises ValueError for any source that is not a pipeline input — in
    particular ground_truth.csv, which the pipeline must never see.
    """
    if source not in PIPELINE_SOURCES:
        raise ValueError(
            f"{source!r} is not a pipeline source. The pipeline reads only "
            f"{sorted(PIPELINE_SOURCES)}; labels are for the evaluation script."
        )
    resolved = path or PIPELINE_SOURCES[source]
    records: List[SourceRecord] = []
    with open(resolved, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row_number, row in enumerate(reader, start=1):
            records.append(
                SourceRecord(
                    source=source,
                    record_id=row.get("record_id", ""),
                    row_number=row_number,
                    raw=dict(row),
                )
            )
    return records


def load_all_sources() -> Iterator[SourceRecord]:
    for source in (SOURCE_PURCHASE_REGISTER, SOURCE_GSTR2B):
        yield from load_source(source)
