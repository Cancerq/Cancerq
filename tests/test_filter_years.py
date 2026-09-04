#!/usr/bin/env python3
"""Tests for filter_years.py.

The important cases: the year boundaries are exact, a death-year column is
never used as a stand-in for the incident year, and rows whose year cannot be
read are reported rather than silently dropped.

Run with:  python tests/test_filter_years.py
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import filter_years  # noqa: E402
from filter_years import filter_year_range, parse_year, parse_years_arg  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def quiet(fn, *a, **kw):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = fn(*a, **kw)
    return result, buffer.getvalue()


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="filter-years-"))
    try:
        print("\n-- parse_year --")
        cases = [
            ("2020", 2020), (2021, 2021), ("2022.0", 2022),
            ("2020-05-13", 2020), ("5/13/2021", 2021), ("13JUL2022", 2022),
            ("", "MISSING"), (None, "MISSING"),
            ("20", "UNPARSEABLE"), ("unknown", "UNPARSEABLE"),
            ("2020 to 2021", "UNPARSEABLE"),  # ambiguous: two different years
        ]
        for raw, want in cases:
            got = parse_year(raw)
            check(got == want, f"parse_year({raw!r}) -> {got!r}")

        print("\n-- parse_years_arg --")
        check(parse_years_arg(["2020-2023"]) == {2020, 2021, 2022, 2023}, "'2020-2023'")
        check(parse_years_arg(["2020", "2023"]) == {2020, 2023}, "'2020 2023' (two years, not a range)")
        check(parse_years_arg(["2020:2023"]) == {2020, 2021, 2022, 2023}, "'2020:2023'")

        print("\n-- boundaries are exact --")
        data_dir = workdir / "raw"
        data_dir.mkdir()
        years = [2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]
        rows = []
        for i, y in enumerate(years):
            for rep in range(10):
                rows.append({"IncidentID": f"{y}-{rep}", "incident_year": y, "Age": 40})
        rows.append({"IncidentID": "blank", "incident_year": "", "Age": 40})
        rows.append({"IncidentID": "bad", "incident_year": "unknown", "Age": 40})
        pd.DataFrame(rows).to_csv(data_dir / "nvdrs_all.csv", index=False)

        out = workdir / "filtered"
        result, _ = quiet(
            filter_year_range, [data_dir / "nvdrs_all.csv"], years=(2020, 2023),
            output_dir=out,
        )
        check(result["rows_read"] == len(rows), f"read all {len(rows)} rows")
        check(result["rows_kept"] == 40, f"kept exactly 4 years x 10 = 40 (got {result['rows_kept']})")

        kept = pd.read_csv(out / "nvdrs_all_2020_2023.csv")
        check(
            sorted(kept["incident_year"].unique()) == [2020, 2021, 2022, 2023],
            f"only 2020-2023 survive: {sorted(kept['incident_year'].unique())}",
        )
        check(2019 not in set(kept["incident_year"]), "2019 excluded (lower boundary)")
        check(2024 not in set(kept["incident_year"]), "2024 excluded (upper boundary)")
        check(result["rows_missing_year"] == 1, "1 blank year reported")
        check(result["rows_unparseable_year"] == 1, "1 unreadable year reported")

        print("\n-- refuses to use death year as the incident year --")
        death_only = workdir / "death_only.csv"
        pd.DataFrame(
            {"IncidentID": ["1"], "death_year": [2021], "DeathDate": ["2021-03-01"]}
        ).to_csv(death_only, index=False)
        try:
            quiet(filter_year_range, [death_only], years=(2020, 2023),
                  output_dir=workdir / "o2")
            check(False, "should have refused a death-year-only file")
        except SystemExit as exc:
            text = str(exc)
            check("could not find an incident-year column" in text, "refuses to guess")
            check("death_year" in text and "death year" in text,
                  "names the look-alike column it declined to use")
            check("--year-col" in text, "tells you how to override")

        print("\n-- --year-col override works --")
        result, _ = quiet(
            filter_year_range, [death_only], years=(2020, 2023),
            output_dir=workdir / "o3", year_col="death_year",
        )
        check(result["rows_kept"] == 1, "explicit --year-col is honoured")

        print("\n-- incident year wins when both columns exist --")
        both = workdir / "both.csv"
        pd.DataFrame({
            "IncidentID": ["a", "b"],
            "death_year": [2024, 2020],      # would give the opposite answer
            "incident_year": [2023, 2019],
        }).to_csv(both, index=False)
        result, output = quiet(
            filter_year_range, [both], years=(2020, 2023), output_dir=workdir / "o4"
        )
        check("year column: incident_year" in output, "picked incident_year, not death_year")
        got = pd.read_csv(workdir / "o4" / "both_2020_2023.csv")
        check(
            list(got["IncidentID"]) == ["a"],
            f"kept the row whose INCIDENT year is in range: {list(got['IncidentID'])}",
        )

        print("\n-- accepts a list of paths, a directory, and a .txt list --")
        second = data_dir / "nvdrs_more.csv"
        pd.DataFrame({"IncidentID": ["x"], "incident_year": [2022]}).to_csv(second, index=False)

        r_dir, _ = quiet(filter_year_range, [data_dir], years=(2020, 2023),
                         output_dir=workdir / "o5")
        check(len(r_dir["files"]) == 2, f"directory expanded to 2 files ({len(r_dir['files'])})")

        input_list = [str(data_dir / "nvdrs_all.csv"), str(second)]
        r_list, _ = quiet(filter_year_range, input_list, years=(2020, 2023),
                          output_dir=workdir / "o6")
        check(r_list["rows_kept"] == 41, f"input_list of paths: kept 41 (got {r_list['rows_kept']})")

        list_file = workdir / "input_list.txt"
        list_file.write_text("\n".join(["# my files"] + input_list) + "\n")
        r_txt, _ = quiet(filter_year_range, [list_file], years=(2020, 2023),
                         output_dir=workdir / "o7")
        check(r_txt["rows_kept"] == 41, "a .txt path-list gives the same result")

        print("\n-- a repeated path is not counted twice --")
        r_dup, _ = quiet(
            filter_year_range,
            [str(data_dir / "nvdrs_all.csv"), str(data_dir / "nvdrs_all.csv")],
            years=(2020, 2023), output_dir=workdir / "o8",
        )
        check(r_dup["rows_kept"] == 40, f"deduplicated to 40 rows (got {r_dup['rows_kept']})")

        print("\n-- chunked reading matches a single-pass read --")
        r_small, _ = quiet(
            filter_year_range, [data_dir / "nvdrs_all.csv"], years=(2020, 2023),
            output_dir=workdir / "o9", batch_size=7,
        )
        check(
            r_small["rows_kept"] == 40 and r_small["rows_read"] == len(rows),
            f"batch_size=7 gives the same counts ({r_small['rows_kept']}/{r_small['rows_read']})",
        )
        chunked = pd.read_csv(workdir / "o9" / "nvdrs_all_2020_2023.csv")
        check(
            chunked.equals(kept),
            "chunked output is byte-identical in content to the single-pass output",
        )
        check(
            len(pd.read_csv(workdir / "o9" / "nvdrs_all_2020_2023.csv")) == 40,
            "header written exactly once when appending chunks",
        )

        print("\n-- year_distribution.csv --")
        dist = pd.read_csv(out / "year_distribution.csv")
        check(int(dist["n"].sum()) == len(rows), f"distribution accounts for every row ({int(dist['n'].sum())})")
        check(
            set(dist.loc[dist["kept"] == True, "year_value"].astype(str))  # noqa: E712
            == {"2020", "2021", "2022", "2023"},
            "distribution marks the right years as kept",
        )

        print("\n-- date-formatted year column --")
        dated = workdir / "dated.csv"
        pd.DataFrame({
            "IncidentID": ["d1", "d2", "d3"],
            "IncidentDate": ["2019-12-31", "2020-01-01", "2023-12-31"],
        }).to_csv(dated, index=False)
        r_dated, _ = quiet(filter_year_range, [dated], years=(2020, 2023),
                           output_dir=workdir / "o10")
        got = pd.read_csv(workdir / "o10" / "dated_2020_2023.csv")
        check(list(got["IncidentID"]) == ["d2", "d3"],
              f"year pulled out of dates, boundaries exact: {list(got['IncidentID'])}")

        print("\n-- CLI --")
        code, cli_out = quiet(
            filter_years.main,
            ["--input", str(data_dir), "--years", "2020-2023",
             "--output-dir", str(workdir / "o11")],
        )
        check(code == 0, "CLI exit code 0 when rows were kept")
        code, _ = quiet(
            filter_years.main,
            ["--input", str(dated), "--years", "1999",
             "--output-dir", str(workdir / "o12")],
        )
        check(code == 1, "CLI exit code 1 when nothing matched")
        code, insp = quiet(filter_years.main, ["--input", str(data_dir), "--inspect"])
        check(code == 0 and "detected incident-year column" in insp,
              "--inspect reports the column without writing")
        check(
            not (workdir / "o12" / "dated_1999_1999.csv").stat().st_size == 0,
            "an empty result still gets a header-only file",
        )
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
