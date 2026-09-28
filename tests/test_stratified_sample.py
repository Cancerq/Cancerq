#!/usr/bin/env python3
"""Tests for stratified_sample.py.

What matters here: proportional allocation sums exactly to the requested n,
every stratum is represented in proportion, equal_n caps small strata, the
same seed reproduces the same sample, sample + not_sampled = population, and
multi-column strata work.

Run with:  python tests/test_stratified_sample.py
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

import stratified_sample as ss  # noqa: E402

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


SIZES = {"18-27": 400, "28-37": 300, "38-47": 200, "48-57": 70, "58-67": 30}


def build(tmp: Path) -> Path:
    rows = []
    pid = 1
    for band, n in SIZES.items():
        for i in range(n):
            rows.append({"IncidentID": str(pid), "PersonID": str(pid),
                         "Narrative": "" if i % 10 == 0 else f"text {pid}",
                         "Sex": "Male" if i % 3 else "Female", "age_band": band})
            pid += 1
    path = tmp / "construction_all_ages_narrative.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        src = build(tmp)
        out = tmp / "out"

        print("proportional_n")
        r, _ = quiet(ss.run, str(src), str(out), sampling="proportional_n", sample_n=101)
        alloc = r["allocation"].set_index("age_band")["sampled"].to_dict()
        check(sum(alloc.values()) == 101, "allocation sums exactly to n")
        check(all(abs(alloc[b] - SIZES[b] * 101 / 1000) < 1 for b in SIZES),
              "each stratum within 1 of its exact proportional share")
        sample = pd.read_csv(out / "construction_all_ages_narrative_sample.csv", dtype=str)
        rest = pd.read_csv(out / "construction_all_ages_narrative_not_sampled.csv", dtype=str)
        check(len(sample) + len(rest) == 1000, "sample + not_sampled = population")
        check(not set(sample["PersonID"]) & set(rest["PersonID"]), "no overlap")
        check("sample_weight" in sample.columns, "sample_weight column present")
        w = sample.groupby("age_band")["sample_weight"].first().astype(float)
        check(abs(w["18-27"] - 400 / alloc["18-27"]) < 1e-6, "weight = population / sampled")

        r2, _ = quiet(ss.run, str(src), str(tmp / "out2"), sampling="proportional_n", sample_n=101)
        check(sorted(r["sample"]["PersonID"]) == sorted(r2["sample"]["PersonID"]),
              "same seed -> same sample")
        r3, _ = quiet(ss.run, str(src), str(tmp / "out3"), sampling="proportional_n",
                      sample_n=101, random_seed=7)
        check(sorted(r["sample"]["PersonID"]) != sorted(r3["sample"]["PersonID"]),
              "different seed -> different sample")

        print("proportional_frac / equal_n")
        r, _ = quiet(ss.run, str(src), str(out), sampling="proportional_frac", sample_frac=0.1)
        check(len(r["sample"]) == 100, "10% of each stratum")
        r, _ = quiet(ss.run, str(src), str(out), sampling="equal_n", sample_n_per_stratum=50)
        got = r["sample"]["age_band"].value_counts().to_dict()
        check(got == {"18-27": 50, "28-37": 50, "38-47": 50, "48-57": 50, "58-67": 30},
              "equal_n caps the small stratum at its size")

        print("multi-column strata + nonempty filter")
        r, _ = quiet(ss.run, str(src), str(out), strata_cols=["age_band", "Sex"],
                     sampling="equal_n", sample_n_per_stratum=5,
                     require_nonempty_cols=["Narrative"])
        check(len(r["allocation"]) == 10, "5 bands x 2 sexes = 10 strata")
        check(len(r["sample"]) == 50, "5 per stratum")
        check(r["population"] == 900, "blank-Narrative rows excluded before sampling")
        check(r["sample"]["Narrative"].notna().all(), "no blank Narrative in sample")

        print("errors")
        check("INPUT_FILE" in expect_exit(ss.run, "", str(out)), "blank input -> instruction")
        check("OUTPUT_DIR" in expect_exit(ss.run, str(src), ""), "blank output -> instruction")
        check("Sexx" in expect_exit(ss.run, str(src), str(out), strata_cols=["Sexx"]),
              "unknown strata column -> named in error")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
