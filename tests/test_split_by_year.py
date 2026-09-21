#!/usr/bin/env python3
"""Tests for split_by_year.py.

The rule under test is one column: IncidentYear.

What would quietly ruin the analysis:
  - filtering on DeathYear when IncidentYear is what was asked for
  - year boundaries off by one (2017 or 2025 slipping in)
  - rows vanishing instead of being reported as excluded
  - a row landing in two year folders
  - the per-year folders not being readable by the next step

Run with:  python tests/test_split_by_year.py
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import split_by_year as sby  # noqa: E402

failures: list[str] = []
BANDS = ["18_27", "28_37", "38_47", "48_57", "58_67"]
YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]


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


# (IncidentYear value, expected destination)
ROWS = [
    ("2017", "OUT_OF_RANGE"),      # 下界外
    ("2018", "2018"),              # 下边界
    ("2019", "2019"),
    ("2020", "2020"),
    ("2021", "2021"),
    ("2022", "2022"),
    ("2023", "2023"),
    ("2024", "2024"),              # 上边界
    ("2025", "OUT_OF_RANGE"),      # 上界外
    ("2024-05-13", "2024"),        # 日期里取年
    ("2020.0", "2020"),            # 浮点写法
    ("", "BLANK"),
    ("24", "UNPARSEABLE"),         # 两位年份不猜
    ("unknown", "UNPARSEABLE"),
]

N_PER_YEAR = {y: sum(1 for r in ROWS if r[1] == str(y)) for y in YEARS}
N_OUT = sum(1 for r in ROWS if r[1] == "OUT_OF_RANGE")
N_BLANK = sum(1 for r in ROWS if r[1] == "BLANK")
N_BAD = sum(1 for r in ROWS if r[1] == "UNPARSEABLE")


def build(directory: Path, reps: int = 2) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for band in BANDS:
        rows = []
        for rep in range(reps):
            for i, (year, expected) in enumerate(ROWS):
                rows.append({
                    "IncidentID": f"{band}-{rep}-{i}",
                    "Age": int(band.split("_")[0]) + 1,
                    "IncidentYear": year,
                    # DeathYear deliberately disagrees with IncidentYear
                    "DeathYear": "2016",
                    "Census2018_Occupation": "Electricians",
                    "Census2018_Industry": "Construction",
                    "expected_dest": expected,
                })
        pd.DataFrame(rows).to_csv(directory / f"nvdrs_age_{band}.csv", index=False)
    pd.DataFrame({"age_value": [18], "n": [1]}).to_csv(
        directory / "age_distribution.csv", index=False
    )


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="year-"))
    try:
        data = workdir / "age_chunks"
        build(data)
        out = workdir / "out"
        reps = 2
        total = len(ROWS) * reps * len(BANDS)

        print("\n-- parse_year --")
        for raw, want in [
            ("2024", 2024), (2024, 2024), ("2024.0", 2024),
            ("2024-05-13", 2024), ("5/13/2020", 2020),
            ("", "BLANK"), (None, "BLANK"),
            ("24", "UNPARSEABLE"), ("unknown", "UNPARSEABLE"),
            ("2019 to 2020", "UNPARSEABLE"),
        ]:
            got = sby.parse_year(raw)
            check(got == want, f"parse_year({raw!r}) -> {got!r}")

        print("\n-- IncidentYear is used, DeathYear is never substituted --")
        cols = ["IncidentID", "DeathYear", "IncidentYear"]
        check(sby.find_year_column(cols) == "IncidentYear",
              "picks IncidentYear when both are present")
        msg = expect_exit(sby.find_year_column, ["IncidentID", "DeathYear"])
        check("找不到 IncidentYear 列" in msg, "refuses a DeathYear-only file")
        check("DeathYear" in msg and "不是】incident year" in msg,
              "names the look-alike column and says it is not a substitute")

        print("\n-- self-contained --")
        source = Path(sby.__file__).read_text(encoding="utf-8")
        import re as _re
        locals_ = _re.findall(
            r"^(?:from|import)\s+(census_2018\w*|nvdrs_split|filter_\w+|split_by_age"
            r"|run_\w+)", source, _re.M,
        )
        check(not locals_, f"no imports of sibling repo modules (found {locals_})")
        alone = workdir / "alone"
        alone.mkdir()
        shutil.copy(sby.__file__, alone / "split_by_year.py")
        proc = subprocess.run(
            [sys.executable, "split_by_year.py",
             "--input-dir", str(data), "--output-dir", str(workdir / "alone_out")],
            cwd=alone, capture_output=True, text=True,
        )
        check(proc.returncode == 0,
              f"runs from a folder containing only itself (exit {proc.returncode})"
              + ("" if proc.returncode == 0 else f"\n{proc.stderr[-600:]}"))

        print("\n-- the split --")
        result, output = quiet(sby.run, str(data), [""] * 5, str(out), chunk_size=5)
        check(result["rows_read"] == total, f"read {total} rows")
        check("age_distribution.csv" in output, "skipped the summary file")

        print("\n-- year boundaries are exact --")
        for year in YEARS:
            want = N_PER_YEAR[year] * reps * len(BANDS)
            got = result["year_rows"][year]
            check(got == want, f"{year}: {got} rows (expected {want})")
        all_kept_years = set()
        for year in YEARS:
            df = pd.read_csv(out / "_all_ages" / f"nvdrs_{year}_all_ages.csv", dtype=str)
            all_kept_years |= {sby.parse_year(v) for v in df["IncidentYear"]}
            check(
                {sby.parse_year(v) for v in df["IncidentYear"]} == {year},
                f"{year} file holds only {year}",
            )
        check(2017 not in all_kept_years and 2025 not in all_kept_years,
              "2017 and 2025 excluded")

        print("\n-- every row accounted for --")
        check(result["rows_kept"] + result["rows_excluded"] == result["rows_read"],
              f"kept {result['rows_kept']} + excluded {result['rows_excluded']} "
              f"== read {result['rows_read']}")
        check("没有行丢失或重复" in output, "the run states the reconciliation")
        for name, want in (("out_of_range.csv", N_OUT), ("blank_year.csv", N_BLANK),
                           ("unparseable_year.csv", N_BAD)):
            df = pd.read_csv(out / "_excluded" / name, dtype=str)
            check(len(df) == want * reps * len(BANDS),
                  f"_excluded/{name}: {len(df)} rows (expected {want * reps * len(BANDS)})")
        bad = pd.read_csv(out / "_excluded" / "unparseable_year.csv", dtype=str)
        check(set(bad["IncidentYear"].dropna()) == {"24", "unknown"},
              f"two-digit years are reported, not guessed ({sorted(set(bad['IncidentYear'].dropna()))})")

        print("\n-- no row lands in two year folders --")
        ids = []
        for year in YEARS:
            for band in BANDS:
                part = pd.read_csv(out / str(year) / f"nvdrs_age_{band}.csv", dtype=str)
                ids.extend(part["IncidentID"].tolist())
        check(len(ids) == len(set(ids)), f"{len(ids)} rows, all distinct")
        check(len(ids) == result["rows_kept"], "per-year folders hold exactly the kept rows")

        print("\n-- every row went where expected --")
        for year in YEARS:
            df = pd.read_csv(out / "_all_ages" / f"nvdrs_{year}_all_ages.csv", dtype=str)
            bad_rows = df.loc[df["expected_dest"] != str(year)]
            check(bad_rows.empty, f"{year}: no unexpected rows"
                  + ("" if bad_rows.empty else
                     "\n" + bad_rows[["IncidentYear", "expected_dest"]].to_string(index=False)))

        print("\n-- per-year folders keep the input filenames --")
        for year in YEARS:
            names = sorted(p.name for p in (out / str(year)).glob("*.csv"))
            check(names == sorted(f"nvdrs_age_{b}.csv" for b in BANDS),
                  f"{year}/ contains exactly the 5 band files")
        check(not list((out / "2024").glob("all_ages*")),
              "the merged file is NOT inside the year folder (would double-count)")
        check((out / "_all_ages").is_dir() and (out / "_excluded").is_dir(),
              "merged and excluded live in their own folders")

        print("\n-- a year folder feeds the next step --")
        merged_2024 = pd.read_csv(out / "_all_ages" / "nvdrs_2024_all_ages.csv", dtype=str)
        parts = pd.concat(
            [pd.read_csv(out / "2024" / f"nvdrs_age_{b}.csv", dtype=str) for b in BANDS],
            ignore_index=True,
        )
        check(merged_2024.equals(parts),
              "the 2024 merged file == its 5 band files concatenated")
        elec = Path(sby.__file__).parent / "run_electrician_split.py"
        if elec.is_file():
            proc = subprocess.run(
                [sys.executable, str(elec), "--input-dir", str(out / "2024"),
                 "--output-dir", str(workdir / "elec_2024")],
                capture_output=True, text=True,
            )
            check(proc.returncode == 0,
                  f"run_electrician_split.py accepts the 2024 folder (exit {proc.returncode})"
                  + ("" if proc.returncode == 0 else f"\n{proc.stderr[-500:]}"))
            if proc.returncode == 0:
                got = pd.read_csv(
                    workdir / "elec_2024" / "All_industry_electrician_all_ages.csv",
                    dtype=str,
                )
                check(len(got) == len(parts),
                      f"and sees exactly the 2024 rows ({len(got)} vs {len(parts)})")

        print("\n-- header written once per file --")
        text = (out / "_all_ages" / "nvdrs_2024_all_ages.csv").read_text()
        check(text.count("IncidentID") == 1,
              f"merged file has one header despite 5 inputs (found {text.count('IncidentID')})")

        print("\n-- summary and distribution --")
        pivot = pd.read_csv(out / "summary_by_year_and_age.csv", index_col=0)
        check(int(pivot["TOTAL"].sum()) == result["rows_kept"],
              f"pivot totals equal the kept rows ({int(pivot['TOTAL'].sum())})")
        check(list(pivot.index) == YEARS, f"one row per year ({list(pivot.index)})")
        dist = pd.read_csv(out / "year_distribution.csv", dtype=str)
        check(int(dist["n"].astype(int).sum()) == total,
              "year_distribution accounts for every input row")
        fmap = pd.read_csv(out / "file_map.csv")
        check(len(fmap) == len(YEARS) * len(BANDS) + len(YEARS),
              f"file_map has a row per output file ({len(fmap)})")
        check(fmap["output_file"].map(lambda p: Path(p).is_file()).all(),
              "every output_file exists")
        check(fmap.loc[fmap["age_band"] != "ALL", "input_file"]
              .map(lambda p: Path(p).is_file()).all(), "every input_file exists")
        check("输出:" in output and "输入:" in output, "console prints both paths")

        print("\n-- custom year list --")
        out2 = workdir / "out2"
        r2, _ = quiet(sby.run, str(data), [""] * 5, str(out2),
                      years=[2020, 2021], chunk_size=100)
        check(sorted(r2["year_rows"]) == [2020, 2021], "only the requested years")
        check(not (out2 / "2018").exists(), "no folder for a year that was not requested")
        check(r2["rows_excluded"] == total - r2["rows_kept"], "excluded count follows")

        print("\n-- chunk size does not change the output --")
        out3 = workdir / "out3"
        quiet(sby.run, str(data), [""] * 5, str(out3), chunk_size=2)
        a = pd.read_csv(out / "_all_ages" / "nvdrs_2024_all_ages.csv", dtype=str)
        b = pd.read_csv(out3 / "_all_ages" / "nvdrs_2024_all_ages.csv", dtype=str)
        check(a.equals(b), "chunk_size 2 vs 5 gives identical output")

        print("\n-- clear errors, not tracebacks --")
        msg = expect_exit(sby.run, "", [""] * 5, str(workdir / "o5"))
        check("输入路径还没填" in msg, "empty input names the blank")
        msg = expect_exit(sby.run, str(data), [""] * 5, "")
        check("输出路径还没填" in msg and "OUTPUT_DIR" in msg, "empty output names the blank")

        print("\n-- warns when no year matches --")
        _, none_out = quiet(sby.run, str(data), [""] * 5, str(workdir / "o6"),
                            years=[1999], chunk_size=100)
        check("没有任何行落在指定年份里" in none_out, "warns loudly")
        check("year_distribution.csv" in none_out, "points at the distribution file")

        print("\n-- CLI --")
        code, _ = quiet(sby.main, ["--input-dir", str(data),
                                   "--output-dir", str(workdir / "out8")])
        check(code == 0, "CLI exit 0")
        check((workdir / "out8" / "2024" / "nvdrs_age_18_27.csv").is_file(),
              "CLI produced the per-year files")
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
