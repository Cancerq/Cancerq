#!/usr/bin/env python3
"""Tests for run_construction_index.py (Census2018_Industry -> index -> concat).

What would quietly ruin the analysis:
  - "Construction and extraction" swept in by a substring match
  - " construction " or code 0770 missed
  - source_row_index not pointing back at the right row of the original file,
    especially across chunk boundaries
  - the concat not equalling the sum of the per-age files
  - files with different columns misaligning in the concat

Run with:  python tests/test_run_construction_index.py
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

import run_construction_index as rci  # noqa: E402

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
    failures.append("expected SystemExit")
    return ""


INDUSTRIES = ["Construction", "Retail trade", " construction ", "Construction and extraction",
              "", "0770", "Manufacturing", "CONSTRUCTION"]
EXPECTED = [True, False, True, False, False, True, False, True]


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="ci_test_"))
    try:
        chunks = workdir / "age_chunks"
        chunks.mkdir()
        for k, band in enumerate(("18_27", "28_37")):
            rows = INDUSTRIES * 3
            frame = pd.DataFrame({
                "IncidentID": [f"{band}-{i:04d}" for i in range(len(rows))],
                "Census2018_Industry": rows,
                "Age": ["20" if k == 0 else "30"] * len(rows),
            })
            if k == 1:
                frame["ExtraCol"] = "x"          # second file has an extra column
            frame.to_csv(chunks / f"nvdrs_age_{band}.csv", index=False)
        (chunks / "age_distribution.csv").write_text("a\n1\n")

        out = workdir / "out"
        result, log = quiet(rci.run, [], str(chunks), str(out), chunk_size=5)
        per_file = sum(EXPECTED) * 3
        index = result["index"]
        check(len(index) == 2 * per_file, f"index has {2 * per_file} cases")
        check("age_distribution.csv" in log, "summary file skipped")
        check("Construction and extraction" in log, "near-miss value reported")

        print("\n-- index points back at the right rows --")
        ok = True
        for name, part in index.groupby("source_file"):
            original = pd.read_csv(chunks / name, dtype=str, keep_default_na=False)
            back = original.loc[part["source_row_index"].astype(int)]
            ok &= (back["IncidentID"].to_numpy() == part["IncidentID"].to_numpy()).all()
        check(ok, "source_row_index -> same IncidentID in the original file")
        check(set(index["age_band"]) == {"18-27", "28-37"}, "age bands from file names")

        print("\n-- concat --")
        concat = pd.read_csv(result["concat_path"], dtype=str, keep_default_na=False)
        check(len(concat) == len(index), "concat rows = index rows")
        check(list(concat.columns[:3]) == rci.ADDED_COLS, "added columns first")
        check("ExtraCol" in concat.columns, "column union kept")
        check((concat.loc[concat["age_band"] == "18-27", "ExtraCol"] == "").all(),
              "missing column left blank for the other file")
        by_age = sum(len(pd.read_csv(p)) for p in (out / "by_age").glob("*.csv"))
        check(by_age == len(concat), "per-age files sum to concat")
        normed = concat["Census2018_Industry"].str.strip().str.lower()
        check(normed.isin({"construction", "0770"}).all(), "only construction rows")

        s = result["summary"].set_index("age_band")
        check(s.loc["All", "construction_cases"] == len(concat), "summary total")
        check(s.loc["18-27", "blank_industry"] == 3, "blank industry counted")

        print("\n-- errors --")
        check("输入路径" in expect_exit(rci.run, [], "", str(workdir / "o")),
              "blank input explained")
        bad = workdir / "noind.csv"
        pd.DataFrame({"Age": ["20"]}).to_csv(bad, index=False)
        check("Census2018_Industry" in expect_exit(rci.run, [str(bad)], "",
                                                   str(workdir / "o")),
              "missing industry column explained")

        print("\n-- CLI --")
        code, _ = quiet(rci.main, ["--input-dir", str(chunks), "--output-dir",
                                   str(workdir / "cli")])
        check(code == 0, "CLI exit 0")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
