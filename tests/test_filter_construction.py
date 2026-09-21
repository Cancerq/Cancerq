#!/usr/bin/env python3
"""Tests for filter_construction.py.

What matters: the Census 2018 range boundaries are exact, blank codes go to
their own file rather than being lumped in with non-construction, the three
buckets partition the input, and the manifests are usable as the next step's
input list.

Run with:  python tests/test_filter_construction.py
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

import filter_construction as fc  # noqa: E402

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


# (census_2018 value, what it should be with default settings)
ROWS = [
    ("6199", "other"),          # just below the construction range
    ("6200", "construction"),   # lower boundary: supervisors
    ("6230", "construction"),   # carpenters
    ("6260", "construction"),   # construction laborers
    ("6441", "construction"),   # plumbers
    ("6765", "construction"),   # upper boundary of trades
    ("6766", "other"),          # just above the trades range
    ("6800", "other"),          # extraction: excluded by default
    ("6950", "other"),          # extraction upper bound: excluded by default
    ("6951", "other"),          # above extraction
    ("0220", "other"),          # construction managers: excluded by default
    ("3130", "other"),          # registered nurse
    ("9820", "other"),          # military
    ("", "blank"),              # blank
    ("unknown", "other"),       # non-numeric -> not construction, not blank
]


def build(path: Path, reps: int = 3) -> None:
    rows = []
    for rep in range(reps):
        for i, (code, expected) in enumerate(ROWS):
            rows.append(
                {
                    "IncidentID": f"{rep}-{i}",
                    "Age": 40,
                    "census_2018": code,
                    "expected_bucket": expected,
                }
            )
    pd.DataFrame(rows).to_csv(path, index=False)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="constr-"))
    try:
        print("\n-- parse_code --")
        for raw, want in [
            ("6230", 6230), (6230, 6230), ("06230", 6230), ("6230.0", 6230),
            ("", "BLANK"), (None, "BLANK"),
            ("unknown", "UNPARSEABLE"), ("62-30", "UNPARSEABLE"),
        ]:
            got = fc.parse_code(raw)
            check(got == want, f"parse_code({raw!r}) -> {got!r}")

        print("\n-- Census 2018 range boundaries --")
        ranges = fc.build_construction_ranges(False, False)
        check(fc.in_ranges(6200, ranges), "6200 in (lower boundary of trades)")
        check(fc.in_ranges(6765, ranges), "6765 in (upper boundary of trades)")
        check(not fc.in_ranges(6199, ranges), "6199 out")
        check(not fc.in_ranges(6766, ranges), "6766 out")
        check(not fc.in_ranges(6800, ranges), "6800 out by default (extraction)")
        check(not fc.in_ranges(220, ranges), "0220 out by default (managers)")

        ext = fc.build_construction_ranges(True, False)
        check(fc.in_ranges(6800, ext) and fc.in_ranges(6950, ext),
              "--include-extraction covers 6800-6950")
        check(not fc.in_ranges(6951, ext), "6951 still out with extraction")
        check(not fc.in_ranges(6799, ext), "6799 still out (gap between ranges)")
        mgr = fc.build_construction_ranges(False, True)
        check(fc.in_ranges(220, mgr), "--include-managers covers 0220")

        print("\n-- default filtering matches the expected bucket --")
        data_dir = workdir / "age_chunks"
        data_dir.mkdir()
        for band in ("18_27", "28_37"):
            build(data_dir / f"nvdrs_age_{band}.csv")

        out = workdir / "construction"
        result, output = quiet(fc.filter_construction, [data_dir], output_dir=out)

        n_rows_per_file = len(ROWS) * 3
        check(result["totals"]["read"] == n_rows_per_file * 2,
              f"read both files ({result['totals']['read']} rows)")
        check(result["totals"]["construction"] == 5 * 3 * 2,
              f"5 construction codes x 3 reps x 2 files = 30 (got {result['totals']['construction']})")
        check(result["totals"]["blank"] == 1 * 3 * 2,
              f"blank rows kept separate: 6 (got {result['totals']['blank']})")

        constr = pd.read_csv(out / "nvdrs_age_18_27_construction.csv", dtype=str)
        check(
            set(constr["expected_bucket"]) == {"construction"},
            f"construction file holds only expected-construction rows: "
            f"{sorted(set(constr['expected_bucket']))}",
        )
        check(
            sorted(set(constr["census_2018"])) == ["6200", "6230", "6260", "6441", "6765"],
            f"exactly the in-range codes: {sorted(set(constr['census_2018']))}",
        )

        blank = pd.read_csv(out / "nvdrs_age_18_27_blank.csv", dtype=str)
        check(len(blank) == 3, f"blank file has the 3 blank-code rows (got {len(blank)})")
        check(
            blank["census_2018"].isna().all(),
            "every row in the blank file really has an empty census_2018",
        )
        check(
            set(blank["expected_bucket"]) == {"blank"},
            "blank file holds only the rows expected to be blank",
        )

        print("\n-- the three buckets partition the input --")
        out2 = workdir / "constr2"
        r2, _ = quiet(
            fc.filter_construction, [data_dir / "nvdrs_age_18_27.csv"],
            output_dir=out2, keep_nonconstruction=True,
        )
        ids = []
        for name in ("construction", "blank", "nonconstruction"):
            part = pd.read_csv(out2 / f"nvdrs_age_18_27_{name}.csv", dtype=str)
            ids.extend(part["IncidentID"].tolist())
        check(len(ids) == n_rows_per_file, f"all {n_rows_per_file} rows present (got {len(ids)})")
        check(len(ids) == len(set(ids)), "no row written to two buckets")
        nonc = pd.read_csv(out2 / "nvdrs_age_18_27_nonconstruction.csv", dtype=str)
        check(
            "unknown" in set(nonc["census_2018"]),
            "a non-numeric code lands in non-construction, not silently dropped",
        )
        check(r2["totals"]["unparseable"] == 3, "non-numeric codes are counted and reported")

        print("\n-- --include-extraction / --include-managers --")
        out3 = workdir / "constr3"
        r3, _ = quiet(fc.filter_construction, [data_dir / "nvdrs_age_18_27.csv"],
                      output_dir=out3, include_extraction=True)
        c3 = pd.read_csv(out3 / "nvdrs_age_18_27_construction.csv", dtype=str)
        check(set(c3["census_2018"]) >= {"6800", "6950"}, "extraction codes now included")
        check("6951" not in set(c3["census_2018"]), "6951 still excluded")
        check(r3["totals"]["construction"] == 7 * 3, f"5 + 2 codes x 3 reps (got {r3['totals']['construction']})")

        out4 = workdir / "constr4"
        r4, _ = quiet(fc.filter_construction, [data_dir / "nvdrs_age_18_27.csv"],
                      output_dir=out4, include_managers=True)
        c4 = pd.read_csv(out4 / "nvdrs_age_18_27_construction.csv", dtype=str)
        check("0220" in set(c4["census_2018"]), "0220 included, leading zero preserved")

        print("\n-- manifests --")
        input_list = (out / "input_list.txt").read_text().strip().splitlines()
        constr_list = (out / "construction_list.txt").read_text().strip().splitlines()
        blank_list = (out / "blank_list.txt").read_text().strip().splitlines()
        check(len(input_list) == 2, f"input_list.txt lists both inputs ({len(input_list)})")
        check(all(Path(p).is_file() for p in input_list), "input_list paths all exist")
        check(len(constr_list) == 2 and all(Path(p).is_file() for p in constr_list),
              "construction_list.txt lists existing output files")
        check(len(blank_list) == 2 and all(Path(p).is_file() for p in blank_list),
              "blank_list.txt lists existing output files")

        # The whole point of the manifest: feed it straight back in.
        out5 = workdir / "constr5"
        r5, _ = quiet(fc.filter_construction, [out / "construction_list.txt"],
                      output_dir=out5)
        check(r5["totals"]["read"] == 30,
              f"construction_list.txt is reusable as an input list (read {r5['totals']['read']})")
        check(r5["totals"]["construction"] == 30, "re-filtering construction keeps everything")

        print("\n-- code breakdown --")
        breakdown = pd.read_csv(out / "census_2018_breakdown.csv", dtype=str)
        check(int(breakdown["n"].astype(int).sum()) == n_rows_per_file * 2,
              "breakdown accounts for every row")
        kept = breakdown.loc[breakdown["kept_as_construction"] == "True"]
        check(len(kept) == 5, f"5 distinct codes kept (got {len(kept)})")
        check(
            kept.loc[kept["census_2018"] == "6230", "title"].iloc[0] == "Carpenters",
            "titles attached for review",
        )
        check("BLANK" in set(breakdown["census_2018"]), "BLANK appears in the breakdown")

        print("\n-- summary --")
        summary = pd.read_csv(out / "construction_summary.csv")
        check(len(summary) == 2, "one summary row per input file")
        check(
            bool(((summary["construction"] + summary["blank"] + summary["other"])
                  == summary["rows_read"]).all()),
            "construction + blank + other == rows_read for every file",
        )

        print("\n-- refuses a census column of the wrong vintage --")
        wrong = workdir / "wrong.csv"
        pd.DataFrame({"IncidentID": ["1"], "census_2010": ["6230"]}).to_csv(wrong, index=False)
        try:
            quiet(fc.filter_construction, [wrong], output_dir=workdir / "c6")
            check(False, "should refuse a 2010-only file")
        except SystemExit as exc:
            check("could not find a census_2018 column" in str(exc), "refuses to guess")
            check("census_2010" in str(exc) and "not interchangeable" in str(exc).lower(),
                  "explains that the 2010 list is a different code list")

        print("\n-- empty result still gets a header --")
        nonec = workdir / "none.csv"
        pd.DataFrame({"IncidentID": ["1"], "census_2018": ["3130"]}).to_csv(nonec, index=False)
        r7, out7txt = quiet(fc.filter_construction, [nonec], output_dir=workdir / "c7")
        empty = pd.read_csv(workdir / "c7" / "none_construction.csv")
        check(len(empty) == 0 and list(empty.columns) == ["IncidentID", "census_2018"],
              "empty construction file has the header and no rows")
        check("WARNING: no construction codes found" in out7txt,
              "warns loudly when nothing matched")

        print("\n-- chunk size does not change the output --")
        out8 = workdir / "c8"
        quiet(fc.filter_construction, [data_dir / "nvdrs_age_18_27.csv"],
              output_dir=out8, chunk_size=2)
        a = pd.read_csv(out / "nvdrs_age_18_27_construction.csv", dtype=str)
        b = pd.read_csv(out8 / "nvdrs_age_18_27_construction.csv", dtype=str)
        check(a.equals(b), "chunk_size 2 gives the same construction file")
        text = (out8 / "nvdrs_age_18_27_construction.csv").read_text()
        check(text.count("IncidentID") == 1, "header written exactly once")

        print("\n-- CLI --")
        code, cli = quiet(fc.main, ["--input", str(data_dir), "--output-dir", str(workdir / "c9")])
        check(code == 0, "exit 0 when construction rows were found")
        code, _ = quiet(fc.main, ["--input", str(nonec), "--output-dir", str(workdir / "c10")])
        check(code == 1, "exit 1 when none were found")
        code, insp = quiet(fc.main, ["--input", str(data_dir), "--inspect"])
        check(code == 0 and "6230" in insp and "Carpenters" in insp,
              "--inspect lists the codes present with titles")
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
