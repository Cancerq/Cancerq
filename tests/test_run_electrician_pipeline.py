#!/usr/bin/env python3
"""Tests for run_electrician_pipeline.py (one pass: age x year x electrician).

Four filters, all read straight from the columns:
    age band      Age in 18-30 / 31-40 / 41-50 / 51-60 / 61-70
    year          IncidentYear in 2018-2024
    electrician   Census2018_Occupation CONTAINS "electrician"
    construction  Census2018_Industry   EQUALS   "construction"

What would quietly ruin the analysis:
  - a band boundary off by one (30/31, 70/71)
  - a year boundary off by one (2017, 2025)
  - the funnel not reconciling, so dropped rows go unexplained
  - All_year not equalling the sum of its years, or of its bands
  - DeathYear or AgeGroup standing in for the real column

Run with:  python tests/test_run_electrician_pipeline.py
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

import run_electrician_pipeline as rep  # noqa: E402

failures: list[str] = []
BANDS = ["18-30", "31-40", "41-50", "51-60", "61-70"]
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


def band_of_age(age: int) -> str | None:
    for lo, hi in ((18, 30), (31, 40), (41, 50), (51, 60), (61, 70)):
        if lo <= age <= hi:
            return f"{lo}-{hi}"
    return None


# (occupation, industry, group) for the electrician mix used at every age/year
MIX = [
    ("Electricians", "Construction", "Construction_electrician"),
    ("Electrician, apprentice", "construction", "Construction_electrician"),
    ("Electricians", "Construction and extraction", "Non_construction_electrician"),
    ("Electricians", "Manufacturing", "Non_construction_electrician"),
    ("Electricians", "", "Unknown_industry_electrician"),
    ("Carpenters", "Construction", None),
    ("Electrical power-line installers and repairers", "Utilities", None),
]
N_ELEC_PER_CELL = sum(1 for m in MIX if m[2])
N_BY_GROUP = {
    g: sum(1 for m in MIX if m[2] == g)
    for g in ("Construction_electrician", "Non_construction_electrician",
              "Unknown_industry_electrician")
}

# Ages chosen to sit on every band boundary, plus just outside 18-70
AGES = [17, 18, 30, 31, 40, 41, 50, 51, 60, 61, 70, 71]
IN_AGES = [a for a in AGES if band_of_age(a)]
# Years on both boundaries plus just outside
ALL_YEARS = [2017] + YEARS + [2025]


def build(path: Path) -> dict:
    rows = []
    n = 0
    for age in AGES:
        for year in ALL_YEARS:
            for i, (occ, ind, group) in enumerate(MIX):
                rows.append({
                    "IncidentID": f"r{n}",
                    "Age": age,
                    "AgeGroup": "adult",          # categorical look-alike
                    "IncidentYear": year,
                    "DeathYear": 2016,            # contradicts IncidentYear
                    "Census2018_Occupation": occ,
                    "Census2018_Industry": ind,
                    "expected_band": band_of_age(age) or "",
                    "expected_group": group or "",
                })
                n += 1
    pd.DataFrame(rows).to_csv(path, index=False)
    return {"total": len(rows)}


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pipe-"))
    try:
        src = workdir / "Liu_1191_nvdrs_2024.csv"
        meta = build(src)
        out = workdir / "Electricians"
        total = meta["total"]
        n_cells = len(IN_AGES) * len(YEARS)
        n_elec = N_ELEC_PER_CELL * n_cells

        print("\n-- self-contained --")
        source = Path(rep.__file__).read_text(encoding="utf-8")
        import re as _re
        locals_ = _re.findall(
            r"^(?:from|import)\s+(census_2018\w*|nvdrs_split|filter_\w+|split_by_\w+"
            r"|run_\w+)", source, _re.M)
        check(not locals_, f"no imports of sibling repo modules (found {locals_})")
        alone = workdir / "alone"
        alone.mkdir()
        shutil.copy(rep.__file__, alone / "run_electrician_pipeline.py")
        proc = subprocess.run(
            [sys.executable, "run_electrician_pipeline.py", "--input", str(src),
             "--output-dir", str(workdir / "alone_out")],
            cwd=alone, capture_output=True, text=True)
        check(proc.returncode == 0,
              f"runs from a folder containing only itself (exit {proc.returncode})"
              + ("" if proc.returncode == 0 else f"\n{proc.stderr[-600:]}"))

        print("\n-- the run --")
        result, output = quiet(rep.run, [str(src)], "", str(out), chunk_size=7)
        check(result["funnel"]["read"] == total, f"read {total} rows")
        check(result["bands"] == BANDS, f"bands {result['bands']}")
        check(result["years"] == YEARS, f"years {result['years']}")

        print("\n-- the funnel reconciles --")
        f = result["funnel"]
        derived = (f["read"] - f["age_bad"] - f["age_out"] - f["year_bad"]
                   - f["year_out"] - f["not_electrician"])
        check(derived == f["electricians"],
              f"read - drops = electricians ({derived} == {f['electricians']})")
        check(f["electricians"] == n_elec,
              f"electricians = {f['electricians']} (expected {n_elec})")
        check("逐级相减 = 电工数" in output, "the run states the reconciliation")
        n_out_age = (len(AGES) - len(IN_AGES)) * len(ALL_YEARS) * len(MIX)
        check(f["age_out"] == n_out_age,
              f"ages 17 and 71 dropped: {f['age_out']} (expected {n_out_age})")
        n_out_year = len(IN_AGES) * (len(ALL_YEARS) - len(YEARS)) * len(MIX)
        check(f["year_out"] == n_out_year,
              f"years 2017 and 2025 dropped: {f['year_out']} (expected {n_out_year})")

        print("\n-- age band boundaries are exact --")
        allf = pd.read_csv(out / "All_year" / "All_industry_electrician_All_year.csv",
                           dtype=str)
        check(len(allf) == n_elec, f"All_year holds {len(allf)} electricians")
        bad = allf.loc[allf["age_band"] != allf["expected_band"]]
        check(bad.empty, "every row's age_band matches the expected band"
              + ("" if bad.empty else
                 "\n" + bad[["Age", "age_band", "expected_band"]]
                 .drop_duplicates().to_string(index=False)))
        check(set(allf["Age"].astype(int)) == set(IN_AGES),
              f"ages present: {sorted(set(allf['Age'].astype(int)))}")
        check(17 not in set(allf["Age"].astype(int))
              and 71 not in set(allf["Age"].astype(int)), "17 and 71 excluded")
        for lo, hi in ((18, 30), (31, 40), (41, 50), (51, 60), (61, 70)):
            label = f"{lo}-{hi}"
            part = allf.loc[allf["age_band"] == label, "Age"].astype(int)
            check(part.min() >= lo and part.max() <= hi,
                  f"band {label}: ages {part.min()}..{part.max()}")

        print("\n-- year boundaries are exact --")
        check(set(allf["incident_year"].astype(int)) == set(YEARS),
              f"years present: {sorted(set(allf['incident_year'].astype(int)))}")

        print("\n-- group assignment --")
        for group, per_cell in N_BY_GROUP.items():
            want = per_cell * n_cells
            got = result["all_year_rows"][group]
            check(got == want, f"{group} = {got} (expected {want})")
            df = pd.read_csv(out / "All_year" / f"{group}_All_year.csv", dtype=str)
            wrong = df.loc[df["expected_group"] != group]
            check(wrong.empty, f"{group}: no unexpected rows"
                  + ("" if wrong.empty else "\n" + wrong[
                      ["Census2018_Occupation", "Census2018_Industry", "expected_group"]
                  ].drop_duplicates().to_string(index=False)))
        constr = pd.read_csv(out / "All_year" / "Construction_electrician_All_year.csv",
                             dtype=str)
        check("Construction and extraction" not in set(constr["Census2018_Industry"]),
              "'Construction and extraction' is NOT construction (equals, not contains)")
        check("Carpenters" not in set(allf["Census2018_Occupation"]),
              "a non-electrician in construction is excluded entirely")

        print("\n-- All = C + N + U --")
        parts = sum(result["all_year_rows"][g] for g in rep.GROUPS
                    if g != "All_industry_electrician")
        check(result["all_year_rows"]["All_industry_electrician"] == parts,
              f"{result['all_year_rows']['All_industry_electrician']} == {parts}")

        print("\n-- All_year == sum of its years, and of its bands --")
        for group in rep.GROUPS:
            merged = pd.read_csv(out / "All_year" / f"{group}_All_year.csv", dtype=str)
            by_year = pd.concat(
                [pd.read_csv(out / "by_year" / str(y) / f"{group}_{y}.csv", dtype=str)
                 for y in YEARS], ignore_index=True)
            check(len(merged) == len(by_year),
                  f"{group}: All_year {len(merged)} == sum of 7 years {len(by_year)}")
            check(set(merged["IncidentID"]) == set(by_year["IncidentID"]),
                  f"{group}: same rows across the two views")
            by_band = pd.concat(
                [pd.read_csv(out / "All_year" / f"{group}_All_year_age_{b}.csv",
                             dtype=str) for b in BANDS], ignore_index=True)
            check(set(merged["IncidentID"]) == set(by_band["IncidentID"]),
                  f"{group}: All_year == its 5 age-band files combined")

        print("\n-- header written once --")
        text = (out / "All_year" / "All_industry_electrician_All_year.csv").read_text()
        check(text.count("IncidentID") == 1,
              f"one header despite many chunks (found {text.count('IncidentID')})")

        print("\n-- summary covers every year x band x group cell --")
        s = result["summary"]
        check(int(s.loc[s["group"] == "All_industry_electrician", "rows"].sum()) == n_elec,
              "summary All_industry total matches")
        check(len(s.loc[s["group"] == "All_industry_electrician"]) == len(BANDS) * len(YEARS),
              f"one row per year x band ({len(s[s['group'] == 'All_industry_electrician'])})")
        for label in BANDS:
            n = int(s.loc[(s["age_band"] == label)
                          & (s["group"] == "All_industry_electrician"), "rows"].sum())
            expect = N_ELEC_PER_CELL * len(YEARS) * sum(
                1 for a in IN_AGES if band_of_age(a) == label)
            check(n == expect, f"band {label}: summary {n} == expected {expect}")

        print("\n-- file_map --")
        fm = result["file_map"]
        check(fm["output_file"].map(lambda p: Path(p).is_file()).all(),
              "every output_file exists")
        check(set(fm["scope"]) == {"All_year", "by_year"}, "both scopes present")
        check(len(fm) == 4 * (1 + len(BANDS) + len(YEARS)),
              f"a row per output file ({len(fm)})")
        check(str(src) in fm["input_files"].iloc[0], "input file recorded")

        print("\n-- funnel.csv --")
        fdf = pd.read_csv(out / "funnel.csv")
        check(int(fdf["rows"].sum()) == 2 * f["electricians"],
              "funnel rows sum to read - drops + electricians")

        print("\n-- per-year-per-band files (opt-in) --")
        check(not list((out / "by_year" / "2024").glob("*age_18-30*")),
              "off by default")
        out2 = workdir / "Electricians_cells"
        r2, _ = quiet(rep.run, [str(src)], "", str(out2),
                      write_per_year_per_band=True, chunk_size=200)
        cell = out2 / "by_year" / "2024" / "All_industry_electrician_2024_age_18-30.csv"
        check(cell.is_file(), "per-cell file written when enabled")
        got = len(pd.read_csv(cell, dtype=str))
        want = N_ELEC_PER_CELL * sum(1 for a in IN_AGES if band_of_age(a) == "18-30")
        check(got == want, f"2024 x 18-30 has {got} rows (expected {want})")

        print("\n-- DeathYear / AgeGroup are not substituted --")
        check("IncidentYear" in output and "'Age'" in output,
              "the run prints which columns it chose")
        noyear = workdir / "noyear.csv"
        df = pd.read_csv(src, dtype=str).drop(columns=["IncidentYear"])
        df.to_csv(noyear, index=False)
        msg = expect_exit(rep.run, [str(noyear)], "", str(workdir / "o1"))
        check("缺少必需的列" in msg and "IncidentYear" in msg,
              "a missing IncidentYear is named")
        check("DeathYear" in msg and "不是 incident year" in msg,
              "and DeathYear is explicitly declined as a substitute")
        noage = workdir / "noage.csv"
        pd.read_csv(src, dtype=str).drop(columns=["Age"]).to_csv(noage, index=False)
        msg = expect_exit(rep.run, [str(noage)], "", str(workdir / "o2"))
        check("AgeGroup" in msg and "不是数值年龄" in msg,
              "AgeGroup is declined as a numeric age")

        print("\n-- custom bands / years --")
        out3 = workdir / "Electricians_alt"
        r3, _ = quiet(rep.run, [str(src)], "", str(out3),
                      age_bands="18-27 28-37", years=[2020, 2021], chunk_size=200)
        check(r3["bands"] == ["18-27", "28-37"], f"bands overridden {r3['bands']}")
        check(r3["years"] == [2020, 2021], f"years overridden {r3['years']}")
        alt = pd.read_csv(out3 / "All_year" / "All_industry_electrician_All_year.csv",
                          dtype=str)
        check(set(alt["incident_year"].astype(int)) == {2020, 2021}, "only those years")
        check(set(alt["age_band"]) <= {"18-27", "28-37"}, "only those bands")
        msg = expect_exit(rep.run, [str(src)], "", str(workdir / "o3"),
                          age_bands="18-30 25-40")
        check("重叠" in msg, "overlapping bands are refused")

        print("\n-- clear errors --")
        msg = expect_exit(rep.run, [""], "", str(workdir / "o4"))
        check("输入路径还没填" in msg, "empty input names the blank")
        msg = expect_exit(rep.run, [str(src)], "", "")
        check("输出路径还没填" in msg and "OUTPUT_DIR" in msg, "empty output names it")

        print("\n-- chunk size does not change the output --")
        out4 = workdir / "Electricians_chunk"
        quiet(rep.run, [str(src)], "", str(out4), chunk_size=3)
        a = pd.read_csv(out / "All_year" / "Construction_electrician_All_year.csv",
                        dtype=str)
        b = pd.read_csv(out4 / "All_year" / "Construction_electrician_All_year.csv",
                        dtype=str)
        check(a.equals(b), "chunk_size 3 vs 7 gives identical output")

        print("\n-- review files --")
        iv = pd.read_csv(out / "electrician_industry_values.csv", dtype=str)
        check(int(iv["n_electricians"].astype(int).sum()) == n_elec,
              "industry values cover every electrician")
        for label, group in (("Construction", "Construction_electrician"),
                             ("Non_construction", "Non_construction_electrician"),
                             ("Unknown", "Unknown_industry_electrician")):
            n = int(iv.loc[iv["counted_as"] == label, "n_electricians"].astype(int).sum())
            check(n == result["all_year_rows"][group],
                  f"counted_as {label}: {n} == {result['all_year_rows'][group]}")
        ov = pd.read_csv(out / "matched_occupation_values.csv", dtype=str)
        check("Carpenters" not in set(ov["Census2018_Occupation"]),
              "non-electricians absent from the matched-occupation file")

        print("\n-- CLI --")
        code, _ = quiet(rep.main, ["--input", str(src),
                                   "--output-dir", str(workdir / "cli_out")])
        check(code == 0, "CLI exit 0")
        check((workdir / "cli_out" / "All_year"
               / "Construction_electrician_All_year.csv").is_file(),
              "CLI produced the All_year files")
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
