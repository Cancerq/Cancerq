#!/usr/bin/env python3
"""Tests for run_suicide_rate.py (NVDRS deaths / (ACS PUMS population / 1000)).

What would quietly ruin the analysis:
  - table A letting a state that joined NVDRS after 2018 into later years
    (numerator or denominator), so the trend reflects coverage growth
  - table B dropping states that joined after 2018
  - a missing ACS cell being summed as 0, understating the denominator
  - FIPS / abbreviation / full-name spellings of one state not matching
  - the ACS population not being divided by 1000 first
  - the coverage workbook's title rows or its Full-state column being read
    instead of the Coverage-weighted population
  - a 2018 state that drops out one year (New York 2019 in the workbook)
    keeping its denominator, or its deaths, in that year

Run with:  python tests/test_run_suicide_rate.py
"""

from __future__ import annotations

import io
import math
import shutil
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_suicide_rate as rsr  # noqa: E402

failures: list[str] = []
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
    failures.append("expected SystemExit")
    return ""


def make_nvdrs(path: Path) -> dict:
    """AK and CO are in NVDRS from 2018; TX joins in 2022.

    Deaths per state-year: AK = 2, CO = 3, TX = 5 (TX only 2022+).
    State spellings deliberately mixed. Plus junk rows that must drop out.
    """
    rows = []
    for year in YEARS:
        rows += [(year, "AK")] * 2
        rows += [(year, "Colorado" if year % 2 else "8")] * 3
        if year >= 2022:
            rows += [(year, "48")] * 5
    rows += [(2017, "AK"), (2025, "CO"), ("", "AK"), (2019, ""), (2019, "Narnia")]
    frame = pd.DataFrame(rows, columns=["IncidentYear", "SiteState"])
    frame["DeathYear"] = 1999            # must not be taken as the year column
    frame["Age"] = 40
    frame.to_csv(path, index=False)
    return {"junk": 5}


def make_acs(path: Path, drop=None, tx_every_year=False) -> None:
    """A coverage file: lists only the states covered each year (TX from 2022)."""
    rows = []
    for year in YEARS:
        rows.append((year, "02", 1000 + year - 2018))     # AK as FIPS
        rows.append((year, "CO", 2000))
        if year >= 2022 or tx_every_year:
            rows.append((year, "Texas", 10_000))
    frame = pd.DataFrame(rows, columns=["year", "state", "population"])
    if drop:
        frame = frame[~((frame["year"] == drop[0]) & (frame["state"] == drop[1]))]
    frame.to_csv(path, index=False)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="rate_test_"))
    try:
        src = workdir / "nvdrs.csv"
        make_nvdrs(src)
        acs = workdir / "acs.csv"
        make_acs(acs)

        print("-- state parsing --")
        for raw in ("CA", "ca", "6", "06", "6.0", "California", " california "):
            check(rsr.parse_state(raw) == "CA", f"{raw!r} -> CA")
        check(rsr.parse_state("Washington, DC") == "DC", "Washington, DC -> DC")
        check(rsr.parse_state("Narnia") is None, "unknown name -> None")
        check(rsr.parse_state("99") is None, "unknown FIPS -> None")

        print("\n-- missing denominator writes a template --")
        out0 = workdir / "out0"
        msg = expect_exit(rsr.run, [str(src)], str(out0))
        check("acs_denominator_template.csv" in msg, "error names the template")
        tpl = pd.read_csv(out0 / "acs_denominator_template.csv", dtype=str)
        pairs = set(zip(tpl["year"].astype(int), tpl["state"]))
        expected = {(y, s) for y in YEARS for s in ("AK", "CO")} \
            | {(2024, s) for s in ("AK", "CO", "TX")}
        check(pairs == expected, f"template covers exactly {len(expected)} cells")

        print("\n-- full run --")
        out = workdir / "out"
        result, _ = quiet(rsr.run, [str(src)], str(out), acs_file=str(acs))
        check(result["base_states"] == ["AK", "CO"], "2018 set = AK, CO")
        check(result["single_states"] == ["AK", "CO", "TX"], "2024 set = AK, CO, TX")

        a = result["table_a"].set_index("year")
        check(list(a.index) == YEARS, "table A has 2018-2024")
        for year in YEARS:
            deaths, pop = 5, 1000 + year - 2018 + 2000
            check(a.loc[year, "nvdrs_deaths"] == deaths,
                  f"A {year}: deaths = 5 (TX excluded)")
            check(a.loc[year, "acs_population"] == pop,
                  f"A {year}: population = {pop} (TX excluded)")
            check(math.isclose(a.loc[year, "rate_per_1000"], deaths / (pop / 1000)),
                  f"A {year}: rate = deaths / (pop/1000)")

        b = result["table_b"].iloc[0]
        check(b["nvdrs_deaths"] == 10, "B: 2024 deaths = 2 + 3 + 5")
        check(b["acs_population"] == 1006 + 2000 + 10_000, "B: 2024 population")
        check(math.isclose(b["rate_per_1000"], 10 / 13.006), "B: rate")

        bs = result["by_state"].set_index("state")
        check(math.isclose(bs.loc["TX", "rate_per_1000"], 5 / 10), "B by state: TX")
        check(int(bs["nvdrs_deaths"].sum()) == b["nvdrs_deaths"],
              "B by state sums to B")

        f = pd.read_csv(out / "funnel.csv").set_index("step")["rows"]
        check(f.iloc[0] - f.iloc[1] - f.iloc[2] - f.iloc[3] == f.iloc[4],
              "funnel reconciles")
        check(f.iloc[4] == 7 * 5 + 3 * 5, "rows used = 50")

        for name in ("A_rate_2018_2024_2018_states.csv",
                     "B_rate_2024_all_states.csv", "B_rate_2024_by_state.csv",
                     "detail_by_year_state.csv", "state_coverage.csv"):
            check((out / name).is_file(), f"wrote {name}")

        print("\n-- coverage from ACS: a 2018 state drops out one year --")
        acs_gap = workdir / "acs_gap.csv"
        make_acs(acs_gap, drop=(2020, "CO"))
        result, _ = quiet(rsr.run, [str(src)], str(workdir / "out_gap"),
                          acs_file=str(acs_gap))
        a = result["table_a"].set_index("year")
        check(a.loc[2020, "n_states"] == 1, "2020 uses AK only")
        check(a.loc[2020, "dropped_from_base"] == "CO", "2020 names CO as dropped")
        check(a.loc[2020, "nvdrs_deaths"] == 2, "2020 numerator drops CO deaths")
        check(a.loc[2020, "acs_population"] == 1002, "2020 denominator drops CO")
        check(a.loc[2020, "excluded_nvdrs_deaths"] == 3, "CO's 3 deaths reported")
        check(a.loc[2021, "n_states"] == 2, "2021 back to AK, CO")

        print("\n-- coverage from NVDRS: a missing ACS cell blanks the rate --")
        acs_all = workdir / "acs_all.csv"
        make_acs(acs_all, drop=(2020, "CO"), tx_every_year=True)
        result, _ = quiet(rsr.run, [str(src)], str(workdir / "out_nv"),
                          acs_file=str(acs_all), coverage_from="nvdrs")
        a = result["table_a"].set_index("year")
        check(result["base_states"] == ["AK", "CO"], "2018 set from NVDRS = AK, CO")
        check(a.loc[2019, "acs_population"] == 3001, "TX listed in ACS but not used")
        check(math.isnan(a.loc[2020, "rate_per_1000"]), "2020 rate is blank")
        check(a.loc[2020, "missing_acs_states"] == "CO", "2020 names CO as missing")
        check(not math.isnan(a.loc[2021, "rate_per_1000"]), "2021 still computed")

        print("\n-- the NVDRS RAD coverage workbook format --")
        book = workdir / "acs_pums_coverage_weighted.xlsx"
        detail = []
        for y in YEARS:
            detail.append((y, "Alaska", 1.0, "statewide", f"{y} ACS 1-year PUMS",
                           1000 + y - 2018, 1000 + y - 2018, 10))
            detail.append((y, "Colorado", 0.5, "Partial coverage", f"{y} ACS 1-year PUMS",
                           4000, 2000, 10))
            if y >= 2022:
                detail.append((y, "Texas", 0.5, "Partial coverage",
                               f"{y} ACS 1-year PUMS", 20_000, 10_000, 10))
        cols = ["Year", "Jurisdiction", "Coverage weight", "Coverage definition",
                "ACS PUMS source", "Full-state employed population",
                "Coverage-weighted employed population",
                "Unweighted PUMS person records"]
        with pd.ExcelWriter(book) as xl:
            pd.DataFrame([["title"], ["Year", "ACS PUMS source"]]).to_excel(
                xl, sheet_name="Annual summary", index=False, header=False)
            pd.DataFrame([[None], ["NVDRS RAD coverage weights ..."], ["note"], [None]]
                         ).to_excel(xl, sheet_name="State coverage detail",
                                    index=False, header=False)
            pd.DataFrame(detail, columns=cols).to_excel(
                xl, sheet_name="State coverage detail", index=False, startrow=4)
        result, log = quiet(rsr.run, [str(src)], str(workdir / "out_book"),
                            acs_file=str(book))
        check("State coverage detail" in log and "第 5 行" in log,
              "finds the sheet and the header on row 5")
        b = result["table_b"].iloc[0]
        check(b["acs_population"] == 13_006, "uses Coverage-weighted, not Full-state")
        check(math.isclose(b["rate_per_1000"], 10 / 13.006), "workbook B rate")
        check(result["table_a"]["acs_source"].iloc[0] == "2018 ACS 1-year PUMS",
              "acs_source carried into table A")

        result, log = quiet(rsr.run, [str(src)], str(workdir / "out_ovr"),
                            acs_file=str(book), acs_weight_overrides={(2024, "TX"): 1.0})
        by = result["by_state"].set_index("state")
        check(by.loc["TX", "acs_population"] == 20_000,
              "override: TX 2024 = full-state x 1.0")
        check(result["table_a"].set_index("year").loc[2023, "acs_population"] == 3005,
              "override touches only the named year x state")
        msg = expect_exit(rsr.run, [str(src)], str(workdir / "out_bad"),
                          acs_file=str(book), acs_weight_overrides={(2019, "TX"): 1.0})
        check("找不到" in msg, "override for an uncovered cell is refused")

        print("\n-- covered state with zero NVDRS deaths is flagged --")
        acs_extra = workdir / "acs_extra.csv"
        make_acs(acs_extra)
        with open(acs_extra, "a", encoding="utf-8") as handle:
            handle.write("2024,Utah,500\n")
        result, _ = quiet(rsr.run, [str(src)], str(workdir / "out_zero"),
                          acs_file=str(acs_extra))
        check(result["table_b"].iloc[0]["zero_death_states"] == "UT", "UT flagged")

        print("\n-- scale --")
        result, _ = quiet(rsr.run, [str(src)], str(workdir / "out_100k"),
                          acs_file=str(acs), scale=100_000)
        check(math.isclose(result["table_b"].iloc[0]["rate_per_100000"],
                           10 / (13_006 / 100_000)), "per 100,000 when asked")

        print("\n-- in-script ACS_TABLE --")
        table = {y: {"AK": 1000 + y - 2018, "CO": 2000,
                     **({"TX": 10_000} if y >= 2022 else {})} for y in YEARS}
        result, _ = quiet(rsr.run, [str(src)], str(workdir / "out_tbl"),
                          acs_table=table)
        check(math.isclose(result["table_b"].iloc[0]["rate_per_1000"], 10 / 13.006),
              "ACS_TABLE gives the same answer as ACS_FILE")

        print("\n-- ACS file saved by Excel on Chinese Windows (GBK, Chinese header) --")
        gbk = workdir / "acs_gbk.csv"
        frame = pd.read_csv(acs, dtype=str)
        frame.columns = ["年份", "州", "人口"]
        frame["州"] = frame["州"].replace({"CO": "Colorado"})
        frame.to_csv(gbk, index=False, encoding="gbk")
        check(open(gbk, "rb").read(1) == "年".encode("gbk")[:1], "file really is GBK")
        result, _ = quiet(rsr.run, [str(src)], str(workdir / "out_gbk"),
                          acs_file=str(gbk))
        check(math.isclose(result["table_b"].iloc[0]["rate_per_1000"], 10 / 13.006),
              "GBK ACS file gives the same answer")

        print("\n-- ACS as .xlsx --")
        xlsx = workdir / "acs.xlsx"
        pd.read_csv(acs, dtype=str).to_excel(xlsx, index=False)
        result, _ = quiet(rsr.run, [str(src)], str(workdir / "out_xlsx"),
                          acs_file=str(xlsx))
        check(math.isclose(result["table_b"].iloc[0]["rate_per_1000"], 10 / 13.006),
              "xlsx ACS file gives the same answer")

        print("\n-- NVDRS numerator in GBK --")
        src_gbk = workdir / "nvdrs_gbk.csv"
        pd.read_csv(src, dtype=str).assign(备注="中文").to_csv(
            src_gbk, index=False, encoding="gbk")
        result, _ = quiet(rsr.run, [str(src_gbk)], str(workdir / "out_ngbk"),
                          acs_file=str(acs))
        check(result["table_b"].iloc[0]["nvdrs_deaths"] == 10, "GBK NVDRS file reads")

        print("\n-- CLI --")
        code, _ = quiet(rsr.main, ["--input", str(src), "--output-dir",
                                   str(workdir / "cli"), "--acs-file", str(acs)])
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
