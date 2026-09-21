#!/usr/bin/env python3
"""Tests for split_by_age.py.

The cases that matter for a 1.9 GB run: band boundaries are exact, every input
row is accounted for (written or explicitly excluded), no row lands in two
files, and chunk size changes nothing about the output.

Run with:  python tests/test_split_by_age.py
"""

from __future__ import annotations

import gzip
import io
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import split_by_age  # noqa: E402
from split_by_age import parse_age, parse_bands, split_by_age as run_split  # noqa: E402

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


BANDS = [(18, 27), (28, 37), (38, 47), (48, 57), (58, 67)]


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="split-age-"))
    try:
        print("\n-- parse_age --")
        for raw, want in [
            ("18", 18), (45, 45), ("45.0", 45), ("0", 0),
            ("less than 1 year", 0), ("<1", 0),
            ("", "MISSING"), (None, "MISSING"),
            ("unknown", "UNPARSEABLE"), ("18-27", "UNPARSEABLE"),
            ("999", 999),
        ]:
            got = parse_age(raw)
            check(got == want, f"parse_age({raw!r}) -> {got!r}")

        print("\n-- parse_bands --")
        check(parse_bands("18-27,28-37") == [(18, 27), (28, 37)], "comma separated")
        check(parse_bands(["18-27 28-37"]) == [(18, 27), (28, 37)], "space separated")
        try:
            parse_bands("18-27,25-37")
            check(False, "should reject overlapping bands")
        except SystemExit as exc:
            check("overlap" in str(exc), "rejects overlapping bands (would double-count)")
        try:
            parse_bands("37-28")
            check(False, "should reject a backwards band")
        except SystemExit as exc:
            check("backwards" in str(exc), "rejects a backwards band")

        print("\n-- boundaries and completeness --")
        rows = []
        # 1 row per age 0..100, so every boundary is exercised exactly once.
        for age in range(0, 101):
            rows.append({"IncidentID": f"a{age}", "Age": age, "State": "NY"})
        rows.append({"IncidentID": "blank", "Age": "", "State": "NY"})
        rows.append({"IncidentID": "bad", "Age": "unknown", "State": "NY"})
        rows.append({"IncidentID": "sent", "Age": 999, "State": "NY"})
        big = workdir / "nvdrs_big.csv"
        pd.DataFrame(rows).to_csv(big, index=False)

        out = workdir / "chunks"
        result, output = quiet(run_split, [big], output_dir=out, chunk_size=7)

        check(result["rows_read"] == len(rows), f"read all {len(rows)} rows")
        check(result["rows_written"] == 50, f"18-67 inclusive = 50 rows (got {result['rows_written']})")
        for low, high in BANDS:
            n = result["band_counts"][f"{low}-{high}"]
            check(n == 10, f"band {low}-{high} has 10 rows (got {n})")

        b1 = pd.read_csv(out / "nvdrs_age_18_27.csv")
        check(b1["Age"].min() == 18 and b1["Age"].max() == 27,
              f"18-27 file spans exactly 18..27 ({b1['Age'].min()}..{b1['Age'].max()})")
        b5 = pd.read_csv(out / "nvdrs_age_58_67.csv")
        check(b5["Age"].min() == 58 and b5["Age"].max() == 67,
              f"58-67 file spans exactly 58..67 ({b5['Age'].min()}..{b5['Age'].max()})")

        all_ids, all_ages = [], []
        for low, high in BANDS:
            part = pd.read_csv(out / f"nvdrs_age_{low}_{high}.csv")
            all_ids.extend(part["IncidentID"])
            all_ages.extend(part["Age"])
        check(len(all_ids) == len(set(all_ids)), "no row appears in two band files")
        check(sorted(all_ages) == list(range(18, 68)), "bands together cover 18..67 exactly")
        check(17 not in all_ages and 68 not in all_ages, "17 and 68 excluded")

        print("\n-- excluded rows are counted, not lost --")
        check(result["rows_missing_age"] == 1, "1 blank age")
        check(result["rows_unparseable_age"] == 1, "1 unreadable age")
        check(result["rows_out_of_range"] == 52,
              f"0-17 (18) + 68-100 (33) + 999 (1) = 52 out of range (got {result['rows_out_of_range']})")
        check(
            result["rows_written"] + result["rows_out_of_range"]
            + result["rows_missing_age"] + result["rows_unparseable_age"]
            == result["rows_read"],
            "written + excluded == read (every row accounted for)",
        )
        check("999+" in output or "sentinel" in output.lower(),
              "warns that age 999 looks like an unknown-age sentinel")

        print("\n-- chunk size does not change the output --")
        out2 = workdir / "chunks2"
        r2, _ = quiet(run_split, [big], output_dir=out2, chunk_size=100_000)
        for low, high in BANDS:
            a = pd.read_csv(out / f"nvdrs_age_{low}_{high}.csv")
            b = pd.read_csv(out2 / f"nvdrs_age_{low}_{high}.csv")
            check(a.equals(b), f"band {low}-{high} identical at chunk_size 7 vs 100000")
        check(r2["rows_read"] == result["rows_read"], "same total either way")

        print("\n-- header written exactly once per band file --")
        text = (out / "nvdrs_age_18_27.csv").read_text()
        check(text.count("IncidentID") == 1,
              f"header appears once despite chunked appends (found {text.count('IncidentID')})")

        print("\n-- custom bands --")
        out3 = workdir / "chunks3"
        r3, _ = quiet(run_split, [big], output_dir=out3, bands="18-30,31-40", chunk_size=50)
        check(r3["band_counts"]["18-30"] == 13, "custom band 18-30 -> 13 rows")
        check(r3["band_counts"]["31-40"] == 10, "custom band 31-40 -> 10 rows")

        print("\n-- --keep-out-of-range --")
        out4 = workdir / "chunks4"
        r4, _ = quiet(run_split, [big], output_dir=out4, keep_out_of_range=True, chunk_size=13)
        excluded = pd.read_csv(out4 / "nvdrs_age_excluded.csv")
        check(len(excluded) == 54, f"all 54 excluded rows written (got {len(excluded)})")
        check(
            set(excluded["exclusion_reason"]) == {"OUT_OF_RANGE", "MISSING", "UNPARSEABLE"},
            f"exclusion_reason filled in: {sorted(set(excluded['exclusion_reason']))}",
        )
        check(
            list(excluded.loc[excluded["IncidentID"] == "blank", "exclusion_reason"]) == ["MISSING"],
            "the blank-age row is labelled MISSING",
        )

        print("\n-- gzip output --")
        out5 = workdir / "chunks5"
        quiet(run_split, [big], output_dir=out5, use_gzip=True, chunk_size=11)
        gz = out5 / "nvdrs_age_18_27.csv.gz"
        check(gz.exists(), "writes .csv.gz")
        with gzip.open(gz, "rt") as fh:
            gz_df = pd.read_csv(fh)
        check(gz_df.equals(b1), "gzipped content matches the plain CSV")

        print("\n-- empty band still gets a header --")
        out6 = workdir / "chunks6"
        quiet(run_split, [big], output_dir=out6, bands="200-210", chunk_size=50)
        empty = pd.read_csv(out6 / "nvdrs_age_200_210.csv")
        check(len(empty) == 0 and list(empty.columns) == ["IncidentID", "Age", "State"],
              "empty band file has the right header and no rows")

        print("\n-- refuses a categorical age column --")
        cat = workdir / "cat.csv"
        pd.DataFrame({"IncidentID": ["1"], "AgeGroup": ["18-24"]}).to_csv(cat, index=False)
        try:
            quiet(run_split, [cat], output_dir=workdir / "c7")
            check(False, "should refuse an AgeGroup-only file")
        except SystemExit as exc:
            check("could not find a numeric age column" in str(exc), "refuses to guess")
            check("AgeGroup" in str(exc), "names the categorical column it declined")

        print("\n-- refuses to concatenate mismatched headers --")
        other = workdir / "other.csv"
        pd.DataFrame({"IncidentID": ["z"], "Age": [30], "Extra": ["x"]}).to_csv(other, index=False)
        try:
            quiet(run_split, [big, other], output_dir=workdir / "c8")
            check(False, "should refuse mismatched headers")
        except SystemExit as exc:
            check("different header" in str(exc), "refuses to misalign columns")
            check("Extra" in str(exc), "names the offending column")

        print("\n-- multiple files with matching headers combine --")
        same = workdir / "same.csv"
        pd.DataFrame({"IncidentID": ["z1", "z2"], "Age": [25, 60], "State": ["CA", "CA"]}).to_csv(
            same, index=False
        )
        out9 = workdir / "chunks9"
        r9, _ = quiet(run_split, [big, same], output_dir=out9, chunk_size=9)
        check(r9["rows_written"] == 52, f"50 + 2 rows written (got {r9['rows_written']})")
        check(r9["band_counts"]["18-27"] == 11, "the extra 25-year-old joined band 18-27")

        print("\n-- CLI and --inspect --")
        code, cli = quiet(
            split_by_age.main,
            ["--input", str(big), "--output-dir", str(workdir / "c10")],
        )
        check(code == 0, "CLI exit 0 when rows were written")
        code, insp = quiet(split_by_age.main, ["--input", str(big), "--inspect"])
        check(code == 0 and "numeric age column: Age" in insp, "--inspect finds the column")
        check("18-27: 10" in insp, "--inspect previews band sizes")
        check(not (workdir / "c11").exists(), "--inspect wrote nothing")

        print("\n-- age_distribution.csv --")
        dist = pd.read_csv(out / "age_distribution.csv")
        check(int(dist["n"].sum()) == len(rows), f"distribution covers every row ({int(dist['n'].sum())})")
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
