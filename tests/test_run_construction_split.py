#!/usr/bin/env python3
"""Tests for run_construction_split.py.

What matters here: the merged files equal the concatenation of the stratified
ones, no row is lost or duplicated, the summary/age_distribution/excluded
files in the input directory are never read as data, and an unfilled path
blank produces an instruction rather than a traceback.

Run with:  python tests/test_run_construction_split.py
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

import run_construction_split as rcs  # noqa: E402

failures: list[str] = []
BANDS = ["18_27", "28_37", "38_47", "48_57", "58_67"]


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


# code, expected group with default settings
CODES = [
    ("6200", "construction"), ("6230", "construction"), ("6765", "construction"),
    ("6199", "nonconstruction"), ("6766", "nonconstruction"),
    ("6800", "nonconstruction"), ("0220", "nonconstruction"),
    ("3130", "nonconstruction"), ("9820", "nonconstruction"),
    ("unknown", "nonconstruction"),
    ("", "blank"),
]


def build_inputs(directory: Path, reps: int = 4) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for band in BANDS:
        rows = []
        for rep in range(reps):
            for i, (code, expected) in enumerate(CODES):
                rows.append(
                    {
                        "IncidentID": f"{band}-{rep}-{i}",
                        "Age": int(band.split("_")[0]) + 1,
                        "census_2018": code,
                        "circumstance_known_c": "Yes" if i % 2 else "No",
                        "expected_group": expected,
                    }
                )
        pd.DataFrame(rows).to_csv(directory / f"nvdrs_age_{band}.csv", index=False)

    # The two files that must never be treated as sample data.
    pd.DataFrame({"age_value": [18, 19], "n": [5, 6]}).to_csv(
        directory / "age_distribution.csv", index=False
    )
    pd.DataFrame(
        {"IncidentID": ["x"], "Age": [99], "census_2018": ["6230"],
         "circumstance_known_c": ["Yes"], "expected_group": ["construction"]}
    ).to_csv(directory / "nvdrs_age_excluded.csv", index=False)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="rcs-"))
    try:
        data = workdir / "age_chunks"
        build_inputs(data)
        out = workdir / "out"
        rows_per_band = len(CODES) * 4
        total_rows = rows_per_band * len(BANDS)

        print("\n-- runs from a directory --")
        result, output = quiet(
            rcs.run, str(data), [""] * 5, str(out), encoding="utf-8", chunk_size=7
        )
        check(len(result["input_files"]) == 5,
              f"picked up exactly the 5 band files ({len(result['input_files'])})")
        check("age_distribution.csv" in output and "已跳过" in output,
              "reports skipping age_distribution.csv")
        check(
            all("excluded" not in p.name for p in result["input_files"]),
            "nvdrs_age_excluded.csv never read as data",
        )

        print("\n-- no row lost or duplicated --")
        check(result["rows_read"] == total_rows, f"read {total_rows} rows")
        check(result["rows_written"] == total_rows,
              f"wrote the same {total_rows} rows (got {result['rows_written']})")
        check("读入 = 写出" in output, "reports the reconciliation")

        print("\n-- age-stratified files --")
        strat_ids = []
        for band in BANDS:
            for group in ("construction", "nonconstruction", "blank"):
                path = out / f"nvdrs_age_{band}_{group}.csv"
                check(path.exists(), f"nvdrs_age_{band}_{group}.csv written")
                part = pd.read_csv(path, dtype=str)
                strat_ids.extend(part["IncidentID"].tolist())
                if len(part):
                    check(
                        set(part["expected_group"]) == {group},
                        f"{band}/{group}: only rows expected in this group",
                    )
                    check(
                        set(part["occupation_group"]) == {group}
                        and set(part["age_band"]) == {band.replace("_", "-")},
                        f"{band}/{group}: age_band and occupation_group columns correct",
                    )
        check(len(strat_ids) == total_rows and len(set(strat_ids)) == total_rows,
              f"stratified files hold every row exactly once ({len(strat_ids)})")

        print("\n-- merged files equal the concatenation of the strata --")
        merged_ids = []
        for group in ("construction", "nonconstruction", "blank"):
            merged = pd.read_csv(out / f"all_ages_{group}.csv", dtype=str)
            merged_ids.extend(merged["IncidentID"].tolist())
            parts = pd.concat(
                [
                    pd.read_csv(out / f"nvdrs_age_{b}_{group}.csv", dtype=str)
                    for b in BANDS
                ],
                ignore_index=True,
            )
            check(
                merged.equals(parts),
                f"all_ages_{group}.csv == the 5 stratified {group} files concatenated",
            )
            check(
                set(merged["age_band"]) == {b.replace("_", "-") for b in BANDS},
                f"all_ages_{group}.csv keeps the age_band column for re-stratifying",
            )
        check(len(merged_ids) == total_rows and len(set(merged_ids)) == total_rows,
              "merged files also hold every row exactly once")

        print("\n-- header written once per file --")
        text = (out / "all_ages_construction.csv").read_text()
        check(text.count("IncidentID") == 1,
              f"merged file has one header despite 5 inputs x many chunks "
              f"(found {text.count('IncidentID')})")

        print("\n-- file_map.csv gives input and output paths --")
        fmap = pd.read_csv(out / "file_map.csv")
        check(
            set(fmap.columns) >= {"age_band", "occupation_group", "input_file",
                                  "output_file", "rows"},
            f"file_map columns: {list(fmap.columns)}",
        )
        check(len(fmap) == 5 * 3 + 3, f"one row per output file, 18 total (got {len(fmap)})")
        check(fmap["output_file"].map(lambda p: Path(p).is_file()).all(),
              "every output_file path in the map exists")
        strat_map = fmap.loc[fmap["age_band"] != "ALL"]
        check(strat_map["input_file"].map(lambda p: Path(p).is_file()).all(),
              "every input_file path in the map exists")
        check((fmap.loc[fmap["age_band"] == "ALL", "rows"].sum() == total_rows),
              "merged rows in the map sum to the input size")
        check("输入 ->" in output or "输出:" in output,
              "the console report prints input and output paths")

        print("\n-- BLANK_GOES_TO = nonconstruction --")
        out2 = workdir / "out2"
        r2, o2 = quiet(rcs.run, str(data), [""] * 5, str(out2),
                       blank_goes_to="nonconstruction", chunk_size=100)
        check(not (out2 / "all_ages_blank.csv").exists(), "no blank file in that mode")
        nonc = pd.read_csv(out2 / "all_ages_nonconstruction.csv", dtype=str)
        check(len(nonc) == (7 + 1) * 4 * 5,
              f"blanks folded into non-construction (got {len(nonc)})")
        check("并入非 construction" in o2 and "职业未知" in o2,
              "warns that blanks are unknown occupation, not known non-construction")
        check(r2["rows_read"] == r2["rows_written"] == total_rows,
              "still reconciles in that mode")

        print("\n-- include_extraction / include_managers --")
        out3 = workdir / "out3"
        r3, _ = quiet(rcs.run, str(data), [""] * 5, str(out3),
                      include_extraction=True, include_managers=True, chunk_size=100)
        c3 = pd.read_csv(out3 / "all_ages_construction.csv", dtype=str)
        check({"6800", "0220"} <= set(c3["census_2018"]),
              "6800 and 0220 counted as construction when enabled")
        check(r3["rows_read"] == r3["rows_written"] == total_rows, "still reconciles")

        print("\n-- INPUT_FILES list instead of a directory --")
        out4 = workdir / "out4"
        listed = [str(data / f"nvdrs_age_{b}.csv") for b in BANDS]
        r4, _ = quiet(rcs.run, "", listed, str(out4), chunk_size=100)
        check(r4["rows_read"] == total_rows, "explicit file list gives the same total")

        print("\n-- a duplicated path is not counted twice --")
        out5 = workdir / "out5"
        dup = listed + [listed[0]]
        r5, o5 = quiet(rcs.run, "", dup, str(out5), chunk_size=100)
        check(r5["rows_read"] == total_rows, f"deduplicated (read {r5['rows_read']})")
        check("重复路径" in o5, "says it ignored the duplicate")

        print("\n-- unfilled blanks give an instruction, not a traceback --")
        msg = expect_exit(rcs.run, "", [""] * 5, str(workdir / "o6"))
        check("输入路径还没填" in msg, "empty input tells you which blank to fill")
        check("INPUT_DIR" in msg and "INPUT_FILES" in msg, "names both blanks")
        msg = expect_exit(rcs.run, str(data), [""] * 5, "")
        check("输出路径还没填" in msg and "OUTPUT_DIR" in msg,
              "empty output names OUTPUT_DIR")
        msg = expect_exit(rcs.run, r"Z:\does\not\exist", [""] * 5, str(workdir / "o7"))
        check("不是一个目录" in msg, "a bad directory is reported clearly")
        msg = expect_exit(rcs.run, "", [r"Z:\nope.csv"], str(workdir / "o8"))
        check("找不到" in msg, "a missing listed file is reported clearly")

        print("\n-- a missing census_2018 column is explained --")
        bad_dir = workdir / "bad"
        bad_dir.mkdir()
        pd.DataFrame({"IncidentID": ["1"], "census_2010": ["6230"]}).to_csv(
            bad_dir / "nvdrs_age_18_27.csv", index=False
        )
        msg = expect_exit(rcs.run, str(bad_dir), [""] * 5, str(workdir / "o9"))
        check("找不到 census_2018 列" in msg, "says the column is missing")
        check("census_2010" in msg and "不通用" in msg,
              "explains the 2010 list is not a substitute")

        print("\n-- chunk size does not change the output --")
        out10 = workdir / "out10"
        quiet(rcs.run, str(data), [""] * 5, str(out10), chunk_size=3)
        a = pd.read_csv(out / "all_ages_construction.csv", dtype=str)
        b = pd.read_csv(out10 / "all_ages_construction.csv", dtype=str)
        check(a.equals(b), "chunk_size 3 vs 7 gives an identical merged file")

        print("\n-- CLI overrides the config blanks --")
        code, cli = quiet(
            rcs.main,
            ["--input-dir", str(data), "--output-dir", str(workdir / "out11")],
        )
        check(code == 0, "CLI exit 0")
        check((workdir / "out11" / "all_ages_construction.csv").is_file(),
              "CLI produced the merged construction file")
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
