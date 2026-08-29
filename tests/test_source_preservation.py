"""Phase 2 must not touch its inputs, and must not read ground truth."""

import ast
import glob
import hashlib
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from src import normalization as N
from src import validation as V
from src.quarantine_log import QuarantineLog
from src.source_records import (
    GSTR2B_CSV,
    PURCHASE_REGISTER_CSV,
    SOURCE_GSTR2B,
    SOURCE_PURCHASE_REGISTER,
    load_source,
)

DATA_DIR = os.path.join(REPO, "data")
GROUND_TRUTH_CSV = os.path.join(DATA_DIR, "ground_truth.csv")

# Every module the PIPELINE runs. calibrate.py is deliberately absent: §2.6
# names it the evaluation script, and it is the one program permitted to open
# ground_truth.csv.
PIPELINE_FILES = sorted(glob.glob(os.path.join(REPO, "src", "*.py"))) + [
    os.path.join(REPO, "run_phase2.py"),
    os.path.join(REPO, "run_phase3.py"),
    os.path.join(REPO, "run_phase4.py"),
]


def digests():
    return {
        os.path.basename(p): hashlib.sha256(open(p, "rb").read()).hexdigest()
        for p in sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))
    }


# --- source files are read-only --------------------------------------------

def test_phase2_run_leaves_every_data_csv_byte_identical(tmp_path):
    before = digests()

    with QuarantineLog(str(tmp_path / "q.sqlite")) as log:
        for source in (SOURCE_PURCHASE_REGISTER, SOURCE_GSTR2B):
            valid, invalid = V.partition(load_source(source))
            log.quarantine_all(invalid)
            for result in valid:
                N.normalize_deterministic(result.record)

    assert digests() == before


def test_all_four_phase1_csvs_are_present_and_accounted_for():
    assert set(digests()) == {
        "ground_truth.csv", "gstr2b.csv",
        "gstr2b_prior_period.csv", "purchase_register.csv",
    }


def test_pipeline_output_goes_outside_the_data_directory(tmp_path):
    from src.quarantine_log import DEFAULT_DB_PATH
    assert not os.path.abspath(DEFAULT_DB_PATH).startswith(DATA_DIR + os.sep)


# --- ground truth is not a pipeline input ----------------------------------

def _code_strings_and_names(path):
    """Every string literal and identifier in a module, EXCLUDING docstrings.

    Prose explaining that ground truth is off limits is fine and wanted; a
    string literal or attribute that could actually address the file is not.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                found.append(node.value)
        elif isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
    return found


def test_no_pipeline_module_can_address_ground_truth():
    """Prose about the rule is allowed; a literal or name that reaches the
    file is not."""
    offenders = []
    for path in PIPELINE_FILES:
        if not os.path.exists(path):
            continue
        for text in _code_strings_and_names(path):
            if "ground_truth" in text.lower():
                offenders.append((os.path.relpath(path, REPO), text))
    assert not offenders, f"pipeline code must not address ground truth: {offenders}"


def test_that_guard_would_catch_a_real_violation(tmp_path):
    """The guard above is only worth having if it fails on an actual read."""
    sneaky = tmp_path / "sneaky.py"
    sneaky.write_text('rows = open("data/ground_truth.csv").read()\n')
    assert any("ground_truth" in t.lower()
               for t in _code_strings_and_names(str(sneaky)))

    innocent = tmp_path / "innocent.py"
    innocent.write_text('"""Never reads data/ground_truth.csv."""\nx = 1\n')
    assert not any("ground_truth" in t.lower()
                   for t in _code_strings_and_names(str(innocent)))


def test_loading_ground_truth_through_the_loader_is_refused():
    with pytest.raises(ValueError, match="not a pipeline source"):
        load_source("ground_truth")


def test_loader_knows_only_the_declared_pipeline_files():
    """The prior-period snapshot joined in Phase 3 (§2.5). Ground truth still
    must not be reachable."""
    from src.source_records import (GSTR2B_PRIOR_PERIOD_CSV, MATCHING_SOURCES,
                                    PIPELINE_SOURCES, SOURCE_PRIOR_PERIOD)
    assert set(PIPELINE_SOURCES) == {SOURCE_PURCHASE_REGISTER, SOURCE_GSTR2B,
                                     SOURCE_PRIOR_PERIOD}
    assert set(PIPELINE_SOURCES.values()) == {
        PURCHASE_REGISTER_CSV, GSTR2B_CSV, GSTR2B_PRIOR_PERIOD_CSV}
    assert GROUND_TRUTH_CSV not in PIPELINE_SOURCES.values()
    assert MATCHING_SOURCES == (SOURCE_PURCHASE_REGISTER, SOURCE_GSTR2B)


def test_the_evaluation_script_is_the_only_one_allowed_near_ground_truth():
    """§2.6: the labels exist for the evaluation script alone. calibrate.py may
    read them; nothing the pipeline runs may."""
    calibrate = os.path.join(REPO, "calibrate.py")
    assert os.path.exists(calibrate)
    assert calibrate not in PIPELINE_FILES
    assert any("ground_truth" in t.lower()
               for t in _code_strings_and_names(calibrate)), \
        "calibrate.py is expected to read ground truth — that is its job"


def test_prior_period_snapshot_carries_no_labels():
    """It is consulted as evidence, so it must look like a source file, not
    like an answer key."""
    from src.source_records import SOURCE_PRIOR_PERIOD
    columns = set(load_source(SOURCE_PRIOR_PERIOD)[0].raw)
    assert not (columns & {"case_type", "expected_outcome", "split",
                           "expected_classification", "match_type"})


# --- independent source ids -------------------------------------------------

def test_source_ids_are_unique_across_both_files():
    ids = [r.source_id for r in load_source(SOURCE_PURCHASE_REGISTER)]
    ids += [r.source_id for r in load_source(SOURCE_GSTR2B)]
    assert len(ids) == len(set(ids)) == 990


def test_source_id_namespaces_the_per_file_key():
    pr = load_source(SOURCE_PURCHASE_REGISTER)[0]
    b2 = load_source(SOURCE_GSTR2B)[0]
    assert pr.source_id == "purchase_register:PR-0001"
    assert b2.source_id == "gstr2b:2B-0001"
    assert pr.record_id != b2.record_id


def test_row_numbers_locate_the_record_in_its_csv():
    records = load_source(SOURCE_PURCHASE_REGISTER)
    assert [r.row_number for r in records[:3]] == [1, 2, 3]
    lines = open(PURCHASE_REGISTER_CSV, encoding="utf-8").read().splitlines()
    for record in records[:3]:
        assert lines[record.row_number].startswith(record.record_id)
