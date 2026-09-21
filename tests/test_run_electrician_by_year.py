#!/usr/bin/env python3
"""Tests for run_electrician_by_year.py.

Rules under test:
    electrician   = Census2018_Occupation CONTAINS "electrician"
    construction  = Census2018_Industry   EQUALS   "construction"  (or CONTAINS)

What would quietly ruin the analysis:
  - the year taken from the filename disagreeing with IncidentYear, silently
  - exact vs contains changing the construction arm without anyone noticing
  - All_industry not equalling its parts, per year and overall
  - two input files resolving to the same year and overwriting each other
  - reading Census2018_Industry as if it were the occupation column

Run with:  python tests/test_run_electrician_by_year.py
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

import run_electrician_by_year as rby  # noqa: E402

failures: list[str] = []
YEARS = ["2018", "2019", "2020", "2021", "2022", "2023", "2024"]


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


# (occupation, industry, group under exact, group under contains)
ROWS = [
    ("Electricians", "Construction", "C", "C"),
    ("Electrician", "construction", "C", "C"),            # 大小写
    ("electricians", "  Construction  ", "C", "C"),       # 首尾空格
    ("Electrician, apprentice", "Construction", "C", "C"),
    # 含 construction 但不等于 —— exact 算非建筑，contains 算建筑
    ("Electricians", "Construction and extraction", "N", "C"),
    ("Electricians", "Heavy construction contractors", "N", "C"),
    ("Electricians", "Educational services", "N", "N"),
    ("Electricians", "Manufacturing", "N", "N"),
    ("Electricians", "", "U", "U"),
    ("Electricians", "Unknown", "U", "U"),
    ("Carpenters", "Construction", "", ""),
    ("Electrical power-line installers and repairers", "Utilities", "", ""),
    ("Registered nurses", "Health care and social assistance", "", ""),
    ("", "Construction", "", ""),
]

GROUP_OF = {
    "C": "Construction_electrician",
    "N": "Non_construction_electrician",
    "U": "Unknown_industry_electrician",
}
N_ELEC = sum(1 for r in ROWS if r[2])
N_EXACT = {k: sum(1 for r in ROWS if r[2] == k) for k in "CNU"}
N_CONTAINS = {k: sum(1 for r in ROWS if r[3] == k) for k in "CNU"}


def build(directory: Path, reps: int = 2, year_col: bool = True) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for year in YEARS:
        rows = []
        for rep in range(reps):
            for i, (occ, ind, exact, contains) in enumerate(ROWS):
                row = {
                    "IncidentID": f"{year}-{rep}-{i}",
                    "Age": 40,
                    "Census2018_Industry": ind,
                    "Census2018_Occupation": occ,
                    "expected_exact": GROUP_OF.get(exact, ""),
                    "expected_contains": GROUP_OF.get(contains, ""),
                }
                if year_col:
                    row["IncidentYear"] = year
                rows.append(row)
        pd.DataFrame(rows).to_csv(directory / f"nvdrs_{year}_all_ages.csv", index=False)
    pd.DataFrame({"year": [2018], "n": [1]}).to_csv(
        directory / "year_distribution.csv", index=False
    )


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="elecyr-"))
    try:
        data = workdir / "filter_by_year"
        build(data)
        out = workdir / "Electricians"
        reps = 2
        per_year = len(ROWS) * reps
        total = per_year * len(YEARS)

        print("\n-- year parsed from the filename --")
        check(rby.year_of(Path("nvdrs_2018_all_ages.csv")) == "2018", "simple name")
        check(
            rby.year_of(Path("2003-2024 NVDRS_nvdrs_2021_all_ages.csv")) == "2021",
            "a folder-style prefix with other years does not win (last match used)",
        )

        print("\n-- self-contained --")
        source = Path(rby.__file__).read_text(encoding="utf-8")
        import re as _re
        locals_ = _re.findall(
            r"^(?:from|import)\s+(census_2018\w*|nvdrs_split|filter_\w+|split_by_\w+"
            r"|run_\w+)", source, _re.M,
        )
        check(not locals_, f"no imports of sibling repo modules (found {locals_})")
        alone = workdir / "alone"
        alone.mkdir()
        shutil.copy(rby.__file__, alone / "run_electrician_by_year.py")
        proc = subprocess.run(
            [sys.executable, "run_electrician_by_year.py",
             "--input-dir", str(data), "--output-dir", str(workdir / "alone_out")],
            cwd=alone, capture_output=True, text=True,
        )
        check(proc.returncode == 0,
              f"runs from a folder containing only itself (exit {proc.returncode})"
              + ("" if proc.returncode == 0 else f"\n{proc.stderr[-600:]}"))

        print("\n-- column resolution --")
        cols = ["IncidentID", "Census2018_Industry", "Census2018_Occupation"]
        i_col = rby.find_industry_column(cols)
        check(i_col == "Census2018_Industry", f"industry -> {i_col}")
        check(rby.find_occupation_column(cols, exclude=[i_col])
              == "Census2018_Occupation", "occupation resolved separately")
        check(rby.find_occupation_column(["IncidentID", "Census2018_Industry"]) is None,
              "an industry-only file yields NO occupation column")

        print("\n-- the split (exact, the default) --")
        result, output = quiet(rby.run, str(data), [""] * 7, str(out), chunk_size=5)
        check(result["rows_read"] == total, f"read {total} rows")
        check("year_distribution.csv" in output, "skipped the summary file")
        merged = result["merged_rows"]
        check(merged["All_industry_electrician"] == N_ELEC * reps * len(YEARS),
              f"All = {merged['All_industry_electrician']}")
        for key, group in GROUP_OF.items():
            want = N_EXACT[key] * reps * len(YEARS)
            check(merged[group] == want, f"{group} = {merged[group]} (expected {want})")

        print("\n-- every row landed in its expected group (exact) --")
        for key, group in GROUP_OF.items():
            df = pd.read_csv(out / "_all_years" / f"{group}_all_years.csv", dtype=str)
            bad = df.loc[df["expected_exact"] != group]
            check(bad.empty, f"{group}: no unexpected rows"
                  + ("" if bad.empty else "\n" + bad[
                      ["Census2018_Occupation", "Census2018_Industry", "expected_exact"]
                  ].drop_duplicates().to_string(index=False)))

        print("\n-- All = C + N + U, overall and per year --")
        parts = sum(merged[g] for g in rby.GROUPS if g != "All_industry_electrician")
        check(merged["All_industry_electrician"] == parts, f"overall: {parts}")
        summary = result["summary"]
        per_year_ok = (
            summary["electricians_all_industry"]
            == summary["construction"] + summary["non_construction"]
            + summary["unknown_industry"]
        ).all()
        check(bool(per_year_ok), "every single year reconciles")
        check("每一年单独核对：全部相符" in output, "and the run says so")
        check("并集" in output, "the report says All overlaps the parts by design")

        print("\n-- per-year folders --")
        for year in YEARS:
            for group in rby.GROUPS:
                check((out / year / f"{group}_{year}.csv").exists(),
                      f"{year}/{group}_{year}.csv written")
            df = pd.read_csv(out / year / f"All_industry_electrician_{year}.csv", dtype=str)
            check(set(df["incident_year"]) == {year}, f"{year}: incident_year stamped")
            check(len(df) == N_ELEC * reps, f"{year}: {len(df)} electricians")
        check(not list((out / "2024").glob("*all_years*")),
              "merged files are NOT inside a year folder")

        print("\n-- merged == concatenation of the years --")
        for group in rby.GROUPS:
            m = pd.read_csv(out / "_all_years" / f"{group}_all_years.csv", dtype=str)
            parts_df = pd.concat(
                [pd.read_csv(out / y / f"{group}_{y}.csv", dtype=str) for y in YEARS],
                ignore_index=True,
            )
            check(m.equals(parts_df), f"{group}: merged == 7 years concatenated")
        text = (out / "_all_years" / "All_industry_electrician_all_years.csv").read_text()
        check(text.count("IncidentID") == 1, "header written once in the merged file")

        print("\n-- exact vs contains is reported without having to re-run --")
        extra = (N_CONTAINS["C"] - N_EXACT["C"]) * reps * len(YEARS)
        check("建筑业判定口径对比" in output, "the comparison section is printed")
        check(f"{N_EXACT['C'] * reps * len(YEARS):,} 名电工" in output,
              "exact count shown")
        check(f"{N_CONTAINS['C'] * reps * len(YEARS):,} 名电工" in output,
              "contains count shown")
        check("Construction and extraction" in output
              and "Heavy construction contractors" in output,
              "the values only contains would add are listed by name")
        check(f"这 {extra:,} 行只有 contains 会算作建筑业" in output,
              f"and the size of the difference is stated ({extra} rows)")

        print("\n-- switching to contains actually changes the split --")
        out_c = workdir / "Electricians_contains"
        r_c, o_c = quiet(rby.run, str(data), [""] * 7, str(out_c),
                         construction_match="contains", chunk_size=100)
        for key, group in GROUP_OF.items():
            want = N_CONTAINS[key] * reps * len(YEARS)
            check(r_c["merged_rows"][group] == want,
                  f"contains: {group} = {r_c['merged_rows'][group]} (expected {want})")
        check(r_c["merged_rows"]["Construction_electrician"]
              == merged["Construction_electrician"] + extra,
              f"contains adds exactly the {extra} predicted rows")
        df_c = pd.read_csv(out_c / "_all_years" / "Construction_electrician_all_years.csv",
                           dtype=str)
        check(df_c.loc[df_c["expected_contains"] != "Construction_electrician"].empty,
              "contains mode matches the contains expectations")
        check(r_c["merged_rows"]["All_industry_electrician"]
              == merged["All_industry_electrician"],
              "All_industry is unchanged by the industry rule")

        print("\n-- filename year vs IncidentYear --")
        check(not result["year_warnings"], "clean data raises no year warning")
        mixed = workdir / "mixed"
        build(mixed, reps=1)
        bad_file = mixed / "nvdrs_2020_all_ages.csv"
        df = pd.read_csv(bad_file, dtype=str)
        df.loc[: len(df) // 2, "IncidentYear"] = "2019"     # 混入别的年份
        df.to_csv(bad_file, index=False)
        r_mix, o_mix = quiet(rby.run, str(mixed), [""] * 7, str(workdir / "o_mix"),
                             chunk_size=100)
        check(r_mix["year_warnings"], "a year mismatch is detected")
        check("文件名说是 2020" in o_mix and "2019" in o_mix,
              "the warning names the file, the expected year and what was found")
        check("仍然按【文件名】的年份归档" in o_mix,
              "and says how those rows were filed")

        print("\n-- two files resolving to the same year are refused --")
        clash = workdir / "clash"
        clash.mkdir()
        for name in ("nvdrs_2021_all_ages.csv", "copy_2021_extra.csv"):
            pd.DataFrame({"IncidentID": ["1"], "Census2018_Industry": ["Construction"],
                          "Census2018_Occupation": ["Electricians"]}).to_csv(
                clash / name, index=False)
        msg = expect_exit(rby.run, str(clash), [""] * 7, str(workdir / "o_clash"))
        check("同一个年份" in msg and "互相覆盖" in msg,
              "refuses rather than silently overwriting one year's output")
        check("nvdrs_2021_all_ages.csv" in msg and "copy_2021_extra.csv" in msg,
              "and names both files")

        print("\n-- review files --")
        iv = pd.read_csv(out / "electrician_industry_values.csv", dtype=str)
        check(int(iv["n_electricians"].astype(int).sum()) == N_ELEC * reps * len(YEARS),
              "industry-values file covers every electrician")
        for label, group in (("Construction", "Construction_electrician"),
                             ("Non_construction", "Non_construction_electrician"),
                             ("Unknown", "Unknown_industry_electrician")):
            n = int(iv.loc[iv["counted_as"] == label, "n_electricians"].astype(int).sum())
            check(n == merged[group], f"counted_as {label} totals {n} == {merged[group]}")
        blank = iv.loc[iv["Census2018_Industry"] == rby.BLANK_LABEL, "counted_as"]
        check(list(blank) == ["Unknown"], f"blank industry labelled Unknown ({list(blank)})")
        ov = pd.read_csv(out / "matched_occupation_values.csv", dtype=str)
        check("Registered nurses" not in set(ov["Census2018_Occupation"]),
              "non-electricians absent from the matched-occupation file")

        print("\n-- summary and file_map --")
        s = pd.read_csv(out / "summary_by_year.csv", dtype=str)
        check(list(s["year"]) == YEARS, f"one row per year ({list(s['year'])})")
        fmap = pd.read_csv(out / "file_map.csv")
        check(len(fmap) == len(YEARS) * 4 + 4, f"a row per output file ({len(fmap)})")
        check(fmap["output_file"].map(lambda p: Path(p).is_file()).all(),
              "every output_file exists")
        check(fmap.loc[fmap["year"] != "ALL", "input_file"]
              .map(lambda p: Path(p).is_file()).all(), "every input_file exists")
        check("输出:" in output and "输入:" in output, "console prints both paths")

        print("\n-- clear errors, not tracebacks --")
        msg = expect_exit(rby.run, "", [""] * 7, str(workdir / "o5"))
        check("输入路径还没填" in msg, "empty input names the blank")
        msg = expect_exit(rby.run, str(data), [""] * 7, "")
        check("输出路径还没填" in msg and "OUTPUT_DIR" in msg, "empty output names it")
        bad = workdir / "bad"
        bad.mkdir()
        pd.DataFrame({"IncidentID": ["1"], "Census2018_Industry": ["Construction"]}).to_csv(
            bad / "nvdrs_2018_all_ages.csv", index=False)
        msg = expect_exit(rby.run, str(bad), [""] * 7, str(workdir / "o6"))
        check("缺少必需的列" in msg and "Census2018_Occupation" in msg,
              "a missing occupation column is named, not substituted")

        print("\n-- no electricians: loud warning --")
        _, none_out = quiet(rby.run, str(data), [""] * 7, str(workdir / "o7"),
                            electrician_keyword="zzz", chunk_size=100)
        check("一个电工都没匹配到" in none_out, "warns")
        check("数字码" in none_out, "and suggests the column may hold codes")

        print("\n-- chunk size does not change the output --")
        out8 = workdir / "out8"
        quiet(rby.run, str(data), [""] * 7, str(out8), chunk_size=2)
        a = pd.read_csv(out / "_all_years" / "Construction_electrician_all_years.csv",
                        dtype=str)
        b = pd.read_csv(out8 / "_all_years" / "Construction_electrician_all_years.csv",
                        dtype=str)
        check(a.equals(b), "chunk_size 2 vs 5 gives identical output")

        print("\n-- works without an IncidentYear column --")
        noyear = workdir / "noyear"
        build(noyear, reps=1, year_col=False)
        r_ny, o_ny = quiet(rby.run, str(noyear), [""] * 7, str(workdir / "o_ny"),
                           chunk_size=100)
        check(r_ny["merged_rows"]["All_industry_electrician"] == N_ELEC * len(YEARS),
              "still splits correctly using the filename year")
        check(not r_ny["year_warnings"], "no spurious warning when the column is absent")

        print("\n-- CLI --")
        code, _ = quiet(rby.main, ["--input-dir", str(data),
                                   "--output-dir", str(workdir / "out9")])
        check(code == 0, "CLI exit 0")
        check((workdir / "out9" / "2024" /
               "Construction_electrician_2024.csv").is_file(),
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
