#!/usr/bin/env python3
"""Tests for run_native_american.py (Race_c -> American Indian / Alaska Native).

What would quietly ruin the analysis:
  - "Native Hawaiian" or "Two or more races" being swept in as Native American
  - a Native American row missed because of case or spacing
  - the age group taken from the wrong place, or 27/28 landing in the wrong band
  - rows lost across chunk boundaries, so the tables don't add up
  - blanks silently dropped from a distribution instead of shown
  - IncidentYear 2017 / 2025 let in, or 2018 / 2024 cut off
  - DeathYear standing in for a missing IncidentYear

Run with:  python tests/test_run_native_american.py
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

import run_native_american as rna  # noqa: E402

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


# (Race_c, Sex, EducationLevel, SiteState, Age, is Native American)
ROWS = [
    ("American Indian/Alaska Native", "Male", "High school graduate", "AZ", "27", True),
    ("american indian", "Female", "Bachelor's degree", "NM", "28", True),
    ("  Alaska Native ", "Male", "", "AK", "67", True),
    ("Native American", "", "Some college", "", "45", True),
    ("American Indian/Alaska Native", "Male", "High school graduate", "AZ", "18", True),
    ("White", "Male", "High school graduate", "AZ", "30", False),
    ("Native Hawaiian or Other Pacific Islander", "Female", "", "HI", "40", False),
    ("Two or more races", "Male", "", "OK", "50", False),
    ("Black or African American", "Female", "", "GA", "60", False),
    ("", "Male", "", "CO", "33", False),
]


def write_rows(path: Path, rows, encoding="utf-8", repeat=1, year="2020") -> None:
    frame = pd.DataFrame([r[:5] for r in rows] * repeat,
                         columns=["Race_c", "Sex", "EducationLevel", "SiteState", "Age"])
    frame["IncidentYear"] = year
    frame["IncidentID"] = [f"{i:06d}" for i in range(len(frame))]
    frame.to_csv(path, index=False, encoding=encoding)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="na_test_"))
    try:
        print("-- matching --")
        race = pd.Series([r[0] for r in ROWS])
        got = rna.is_native_american(race, rna.RACE_KEYWORDS, []).tolist()
        for (value, *_, expected), actual in zip(ROWS, got):
            check(actual == expected, f"{value!r} -> {expected}")
        codes = rna.is_native_american(pd.Series(["3", "3.0", "1", "33"]), [], ["3"])
        check(codes.tolist() == [True, True, False, False], "RACE_CODES exact match")

        print("\n-- full run (numeric Age, chunk size 3, repeated x4) --")
        src = workdir / "nvdrs.csv"
        write_rows(src, ROWS, repeat=4)
        out = workdir / "out"
        result, _ = quiet(rna.run, [str(src)], "", str(out), chunk_size=3)
        check(result["rows_read"] == 40, "read 40 rows")
        check(result["matched"] == 20, "matched 5 x 4 = 20")

        g = result["tables"]["gender"].set_index("category")["n"]
        check(g["Male"] == 12 and g["Female"] == 4 and g[rna.BLANK_LABEL] == 4,
              "gender counts incl. blank")
        check(g["Total"] == 20, "gender total = matched")
        e = result["tables"]["education"].set_index("category")["n"]
        check(e["High school graduate"] == 8, "education counts")
        check(e[rna.BLANK_LABEL] == 4, "blank education kept")
        s = result["tables"]["state"].set_index("category")["n"]
        check(s["AZ"] == 8 and s["NM"] == 4 and s["AK"] == 4, "state counts")
        check("HI" not in s and "OK" not in s, "non-matching states absent")

        a = result["tables"]["age_group"]
        check(list(a["category"]) == ["18-27", "28-37", "38-47", "58-67", "Total"],
              "age groups in band order")
        an = a.set_index("category")["n"]
        check(an["18-27"] == 8, "27 and 18 -> 18-27")
        check(an["28-37"] == 4, "28 -> 28-37")
        check(an["58-67"] == 4, "67 -> 58-67")
        for role in ("gender", "education", "state", "age_group"):
            t = result["tables"][role]
            body = t[t["category"] != "Total"]
            check(int(body["n"].sum()) == 20, f"{role} sums to 20")
            check(abs(body["percent"].sum() - 100) < 0.05, f"{role} percent ~ 100")

        cases = pd.read_csv(out / "native_american_cases.csv", dtype=str,
                            keep_default_na=False)
        check(len(cases) == 20, "cases file has 20 rows")
        check(cases["IncidentID"].str.len().eq(6).all(), "leading zeros kept")
        check(set(cases["Race_c"].str.strip().str.lower()) <= {
            "american indian/alaska native", "american indian", "alaska native",
            "native american"}, "cases file holds only Native American rows")
        rv = result["race_values"].set_index("Race_c")
        check(rv.loc["Two or more races", "counted_as_native_american"] == 0,
              "race_values flags multi-race as not counted")
        for name in ("dist_gender.csv", "dist_education.csv", "dist_state.csv",
                     "dist_age_group.csv", "dist_year.csv", "funnel.csv",
                     "distributions_all.csv", "race_values.csv",
                     "native_american_distributions.xlsx"):
            check((out / name).is_file(), f"wrote {name}")

        print("\n-- age group from an existing column beats numeric Age --")
        src2 = workdir / "with_group.csv"
        frame = pd.read_csv(src, dtype=str)
        frame["AgeGroup"] = "X"
        frame.to_csv(src2, index=False)
        result, _ = quiet(rna.run, [str(src2)], "", str(workdir / "out2"))
        check(set(result["tables"]["age_group"]["category"]) == {"X", "Total"},
              "AgeGroup column used")

        print("\n-- age group from file name when there is no age column --")
        chunks = workdir / "age_chunks"
        chunks.mkdir()
        for band in ("18_27", "28_37"):
            p = chunks / f"nvdrs_age_{band}.csv"
            write_rows(p, ROWS)
            pd.read_csv(p, dtype=str).drop(columns="Age").to_csv(p, index=False)
        (chunks / "age_distribution.csv").write_text("x\n1\n")
        result, log = quiet(rna.run, [], str(chunks), str(workdir / "out3"))
        an = result["tables"]["age_group"].set_index("category")["n"]
        check(an["18-27"] == 5 and an["28-37"] == 5, "bands from file names")
        check("age_distribution.csv" in log, "summary file skipped")
        check(result["matched"] == 10, "both files read")

        print("\n-- GBK input and a missing column --")
        gbk = workdir / "gbk.csv"
        rows = [r for r in ROWS]
        write_rows(gbk, rows, encoding="gbk")
        pd.read_csv(gbk, dtype=str, encoding="gbk").drop(columns="EducationLevel") \
            .assign(备注="中文").to_csv(gbk, index=False, encoding="gbk")
        result, log = quiet(rna.run, [str(gbk)], "", str(workdir / "out4"))
        check(result["matched"] == 5, "GBK file reads")
        check("Education Level" in log and "找不到" in log, "missing column reported")

        print("\n-- IncidentYear limited to 2018-2024 --")
        yr = workdir / "years.csv"
        na = ("American Indian/Alaska Native", "Male", "", "AZ", "30")
        years = ["2017", "2018", "2019.0", "2024", "2025", "", "2021-06-01", "abc"]
        frame = pd.DataFrame([na] * len(years),
                             columns=["Race_c", "Sex", "EducationLevel", "SiteState", "Age"])
        frame["IncidentYear"] = years
        frame["DeathYear"] = "2020"            # must not rescue the bad rows
        frame.to_csv(yr, index=False)
        result, _ = quiet(rna.run, [str(yr)], "", str(workdir / "out_yr"), chunk_size=3)
        check(result["matched"] == 4, "2018 / 2019.0 / 2024 / 2021-06-01 kept")
        y = result["tables"]["year"].set_index("category")["n"]
        check(list(result["tables"]["year"]["category"]) ==
              ["2018", "2019", "2021", "2024", "Total"], "year table in year order")
        check("2017" not in y and "2025" not in y, "2017 and 2025 excluded")
        funnel = pd.read_csv(workdir / "out_yr" / "funnel.csv").set_index("step")["rows"]
        check(funnel.iloc[0] == 8, "funnel: 8 read")
        check(funnel.iloc[1] == 2, "funnel: blank + unreadable = 2")
        check(funnel.iloc[2] == 2, "funnel: outside range = 2")
        check(funnel.iloc[3] == 4 and funnel.iloc[4] == 4, "funnel: 4 in range, 4 matched")
        result, _ = quiet(rna.run, [str(yr)], "", str(workdir / "out_yr2"),
                          year_min=2019, year_max=2021)
        check(result["matched"] == 2, "YEAR_MIN / YEAR_MAX respected")

        noyear = workdir / "noyear.csv"
        frame.drop(columns="IncidentYear").to_csv(noyear, index=False)
        msg = expect_exit(rna.run, [str(noyear)], "", str(workdir / "o"))
        check("IncidentYear" in msg and "DeathYear" in msg,
              "missing IncidentYear refused, DeathYear named but not used")

        print("\n-- errors --")
        check("input_location" in expect_exit(rna.run, [], "", str(workdir / "o")),
              "blank input explained")
        check("output_location" in expect_exit(rna.run, [str(src)], "", ""),
              "blank output explained")
        nocol = workdir / "norace.csv"
        pd.DataFrame({"Sex": ["Male"], "IncidentYear": ["2020"]}).to_csv(nocol, index=False)
        check("Race_c" in expect_exit(rna.run, [str(nocol)], "", str(workdir / "o")),
              "missing Race_c explained")

        print("\n-- CLI --")
        code, _ = quiet(rna.main, ["--input", str(src), "--output-dir",
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
