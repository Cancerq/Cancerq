#!/usr/bin/env python3
"""Tests for run_construction_concat.py.

What matters here: construction is judged by the Census2018_Industry text,
Narrative is looked up in the concat file by PersonID and merged back onto
the construction rows, the Narrative column sits right after IncidentID, no
construction row is lost or duplicated, and misses/conflicts are reported.

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


# (SiteID, IncidentID, PersonID, Age, industry, age file)
ROWS = [
    ("MA", "100", "5001", "20", "Construction", "18_27"),
    ("MA", "100", "5002", "25", "Retail trade", "18_27"),     # same incident, not construction
    ("NY", "101", "5003", "30", " construction ", "28_37"),   # case / whitespace
    ("NY", "102", "5004", "40", "", "38_47"),                 # blank
    ("OH", "103", "5005", "50", "Construction", "48_57"),
    ("OH", "104", "5006", "60", "Manufacturing", "58_67"),
    ("CA", "105", "5007", "62", "Construction", "58_67"),     # missing from concat
]
AGE_COLS = ["SiteID", "IncidentID", "PersonID", "Age", "Census2018_Industry"]


def build(tmp: Path) -> Path:
    age_dir = tmp / "age_chunks"
    age_dir.mkdir()
    frame = pd.DataFrame(ROWS, columns=AGE_COLS + ["file"])
    for band in ["18_27", "28_37", "38_47", "48_57", "58_67"]:
        part = frame[frame["file"] == band].drop(columns="file")
        part.to_csv(age_dir / f"nvdrs_age_{band}.csv", index=False)
    pd.DataFrame({"age_band": ["x"], "n": [1]}).to_csv(age_dir / "age_distribution.csv", index=False)

    concat = pd.DataFrame({
        "PersonID": ["5001.0", "5002", "5003", "5003", "5004", "5005", "5005", "5006"],
        "Narrative": ["fell from roof", "store", "trench collapse", "trench collapse",
                      "x", None, "struck by beam", "y"],
        "OtherVar": list("abcdefgh"),
    })
    concat.to_csv(tmp / "NVDRS_concat.csv", index=False)
    return age_dir


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        age_dir = build(tmp)
        out = tmp / "out"

        print("default run")
        result, log = quiet(rcc.run, str(age_dir), [], str(tmp / "NVDRS_concat.csv"), str(out))
        merged = pd.read_csv(out / "construction_all_ages_narrative.csv", dtype=str)
        check(result["n_construction"] == 4, "4 construction cases (text match, case-insensitive)")
        check(len(merged) == 4, "merged table keeps exactly the 4 construction rows")
        cols = list(merged.columns)
        check(cols[cols.index("IncidentID") + 1] == "Narrative", "Narrative sits right after IncidentID")
        check("OtherVar" not in cols, "only Narrative is taken from concat")
        got = dict(zip(merged["PersonID"], merged["Narrative"].fillna("")))
        check(got == {"5001": "fell from roof", "5003": "trench collapse",
                      "5005": "struck by beam", "5007": ""},
              "Narrative matched by PersonID (5001.0 == 5001, identical dup collapsed, non-empty preferred)")
        check(dict(zip(merged["PersonID"], merged["age_band"]))["5005"] == "48-57", "age_band kept")
        missing = pd.read_csv(out / "PersonID_not_found_in_concat.csv", dtype=str)
        check(list(missing["PersonID"]) == ["5007"], "unmatched PersonID reported")
        conflicts = pd.read_csv(out / "PersonID_multiple_narratives.csv", dtype=str)
        check(set(conflicts["PersonID"]) == {"5005"}, "conflicting narratives reported")
        band = pd.read_csv(out / "construction_age_58_67_narrative.csv", dtype=str)
        check(len(band) == 1 and "Narrative" in band.columns, "per-band file has Narrative")
        check(len(pd.read_csv(out / "summary.csv")) == 5, "age_distribution.csv is not read as data")

        print("rerun on same dir does not pick up its own outputs")
        _, _ = quiet(rcc.run, str(age_dir), [], str(tmp / "NVDRS_concat.csv"), str(age_dir))
        check(len(pd.read_csv(age_dir / "summary.csv")) == 5, "own *_narrative.csv skipped")

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
