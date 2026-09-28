#!/usr/bin/env python3
"""Tests for run_construction_concat.py.

What matters here: construction is judged by the Census2018_Industry text,
rows are pulled from the concat file by IncidentID + PersonID (not IncidentID
alone, which would drag in other victims of the same incident), each pulled
row carries its age band, and unmatched indexes are reported.

Run with:  python tests/test_run_construction_concat.py
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_construction_concat as rcc  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def quiet(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        result = fn(*a, **kw)
    return result, out.getvalue() + err.getvalue()


def expect_exit(fn, *a, **kw) -> str:
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            fn(*a, **kw)
    except SystemExit:
        return out.getvalue() + err.getvalue()
    return ""


# (IncidentID, PersonID, age, industry, age file)
ROWS = [
    ("100", "1", "20", "Construction", "18_27"),
    ("100", "2", "25", "Retail trade", "18_27"),        # same incident, not construction
    ("101", "1", "30", " construction ", "28_37"),      # case / whitespace
    ("102", "1", "40", "", "38_47"),                    # blank
    ("103", "1", "50", "Construction", "48_57"),
    ("104", "1", "60", "Manufacturing", "58_67"),
    ("105", "1", "62", "Construction", "58_67"),        # missing from concat
]


def build(tmp: Path) -> Path:
    age_dir = tmp / "age_chunks"
    age_dir.mkdir()
    frame = pd.DataFrame(ROWS, columns=["IncidentID", "PersonID", "Age",
                                        "Census2018_Industry", "file"])
    for band in ["18_27", "28_37", "38_47", "48_57", "58_67"]:
        part = frame[frame["file"] == band].drop(columns="file")
        part.to_csv(age_dir / f"nvdrs_age_{band}.csv", index=False)
    pd.DataFrame({"age_band": ["x"], "n": [1]}).to_csv(age_dir / "age_distribution.csv", index=False)

    concat = frame.drop(columns="file")
    concat = concat[concat["IncidentID"] != "105"].copy()
    concat["IncidentID"] = concat["IncidentID"] + ".0"      # float-style export
    concat["Narrative"] = "text " + concat["IncidentID"]
    concat.to_csv(tmp / "NVDRS_concat.csv", index=False)
    return age_dir


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        age_dir = build(tmp)
        out = tmp / "out"

        print("default run")
        result, log = quiet(rcc.run, str(age_dir), [], str(tmp / "NVDRS_concat.csv"), str(out))
        check(result["key_cols"] == [("IncidentID", "IncidentID"), ("PersonID", "PersonID")],
              "auto-detects IncidentID + PersonID")
        check(result["n_construction_keys"] == 4, "4 construction cases (text match, case-insensitive)")
        pulled = pd.read_csv(out / "NVDRS_concat_construction.csv", dtype=str)
        check(len(pulled) == 3, "3 rows pulled from concat")
        check("Retail trade" not in set(pulled["Census2018_Industry"]),
              "other victim of the same incident is not pulled")
        check("Narrative" in pulled.columns, "concat-only columns are kept")
        check(dict(zip(pulled["IncidentID"], pulled["age_band"]))
              == {"100.0": "18-27", "101.0": "28-37", "103.0": "48-57"},
              "age_band attached from the age file")
        missing = pd.read_csv(out / "index_not_found_in_concat.csv", dtype=str)
        check(list(missing["IncidentID"]) == ["105"], "unmatched index reported")
        merged = pd.read_csv(out / "construction_all_ages.csv", dtype=str)
        check(len(merged) == 4, "all-ages construction file has 4 rows")
        check((out / "construction_age_58_67.csv").is_file(), "per-band file written")
        summary = pd.read_csv(out / "summary.csv")
        check(len(summary) == 5, "age_distribution.csv is not read as data")

        print("unfilled paths")
        msg = expect_exit(rcc.run, "", [""], str(tmp / "NVDRS_concat.csv"), str(out))
        check("AGE_CHUNK_DIR" in msg, "blank input -> instruction")
        msg = expect_exit(rcc.run, str(age_dir), [], "", str(out))
        check("NVDRS_CONCAT_FILE" in msg, "blank concat -> instruction")
        msg = expect_exit(rcc.run, str(age_dir), [], str(tmp / "NVDRS_concat.csv"), "")
        check("OUTPUT_DIR" in msg, "blank output -> instruction")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
