#!/usr/bin/env python3
"""Tests for run_electrician_split.py.

The things that would quietly ruin the analysis:
  - reading Census2018_Industry as if it were the occupation column
  - All_industry not equalling the sum of its parts
  - unknown-industry electricians silently padding the comparison group
  - a non-electrician leaking into any output

Run with:  python tests/test_run_electrician_split.py
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

import run_electrician_split as res  # noqa: E402

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


# (occupation, industry, expected group; "" = not an electrician at all)
ROWS = [
    ("6330", "0770", "Construction_electrician"),      # 建筑业电工
    ("6330", "0770", "Construction_electrician"),
    ("6330", "7860", "Non_construction_electrician"),  # 学校的电工
    ("6330", "3360", "Non_construction_electrician"),  # 制造业的电工
    ("6330", "0370", "Non_construction_electrician"),  # 采矿业，默认不算 construction
    ("6330", "", "Unknown_industry_electrician"),      # 行业空白
    ("6330", "n/a", "Unknown_industry_electrician"),   # 行业读不出
    ("6230", "0770", ""),                              # 建筑业木匠：不是电工
    ("6331", "0770", ""),                              # 紧邻 6330，不是电工
    ("6329", "0770", ""),
    ("3130", "7970", ""),                              # 护士
    ("", "0770", ""),                                  # 职业空白
    ("unknown", "0770", ""),                           # 职业读不出
]

N_ELEC = sum(1 for r in ROWS if r[2])
N_CONSTR = sum(1 for r in ROWS if r[2] == "Construction_electrician")
N_NONC = sum(1 for r in ROWS if r[2] == "Non_construction_electrician")
N_UNK = sum(1 for r in ROWS if r[2] == "Unknown_industry_electrician")


def build(directory: Path, reps: int = 3) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for band in BANDS:
        rows = []
        for rep in range(reps):
            for i, (occupation, industry, expected) in enumerate(ROWS):
                rows.append({
                    "IncidentID": f"{band}-{rep}-{i}",
                    "Age": int(band.split("_")[0]) + 1,
                    "Census2018_Industry": industry,
                    "Census2018_Occupation": occupation,
                    "circumstance_known_c": "Yes" if i % 2 else "No",
                    "expected_group": expected,
                })
        pd.DataFrame(rows).to_csv(directory / f"nvdrs_age_{band}.csv", index=False)
    pd.DataFrame({"age_value": [18], "n": [1]}).to_csv(
        directory / "age_distribution.csv", index=False
    )


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="elec-"))
    try:
        data = workdir / "age_chunks"
        build(data)
        out = workdir / "out"
        reps = 3
        per_band = len(ROWS) * reps
        total = per_band * len(BANDS)

        print("\n-- the script is self-contained: no local imports --")
        source = Path(res.__file__).read_text(encoding="utf-8")
        import re as _re
        local_imports = _re.findall(
            r"^(?:from|import)\s+(census_2018\w*|nvdrs_split|filter_\w+|split_by_age)",
            source, _re.M,
        )
        check(not local_imports,
              f"no imports of sibling repo modules (found {local_imports})")
        third_party = set(_re.findall(r"^import (\w+)", source, _re.M)) - {
            "argparse", "re", "sys"
        }
        check(third_party == {"pandas"},
              f"pandas is the only third-party dependency (found {sorted(third_party)})")

        alone = workdir / "alone"
        alone.mkdir()
        shutil.copy(res.__file__, alone / "run_electrician_split.py")
        import subprocess
        proc = subprocess.run(
            [sys.executable, "run_electrician_split.py",
             "--input-dir", str(data), "--output-dir", str(workdir / "alone_out")],
            cwd=alone, capture_output=True, text=True,
        )
        check(proc.returncode == 0,
              f"runs from a folder containing only itself (exit {proc.returncode})"
              + ("" if proc.returncode == 0 else f"\n{proc.stderr[-600:]}"))
        check((workdir / "alone_out" / "All_industry_electrician_all_ages.csv").is_file(),
              "and produces its output there")

        print("\n-- inlined code tables match the shared modules (no drift) --")
        import census_2018 as _occ
        import census_2018_industry as _ind
        check(res.CONSTRUCTION_INDUSTRY == _ind.CONSTRUCTION,
              f"construction industry code: {res.CONSTRUCTION_INDUSTRY} == {_ind.CONSTRUCTION}")
        check(res.MINING_INDUSTRY == _ind.MINING_EXTRACTION,
              f"mining range: {res.MINING_INDUSTRY} == {_ind.MINING_EXTRACTION}")
        check(res.INDUSTRY_SECTORS == _ind.SECTORS, "industry sector table identical")
        check(tuple(res.IND_COL_CANDIDATES) == tuple(_ind.COL_CANDIDATES),
              "industry column candidates identical")
        check(set(res.OCC_COL_CANDIDATES) <= set(_occ.CENSUS_2018_COL_CANDIDATES),
              "occupation column candidates are a subset of the shared list")
        check(res.ELECTRICIANS == 6330 and _occ.TITLES[6330] == "Electricians",
              "6330 is Electricians in both")
        for code, title in res.OCCUPATION_TITLES.items():
            if _occ.TITLES.get(code) and _occ.TITLES[code] != title:
                check(False, f"title for {code} differs from the shared table")
                break
        else:
            check(True, "every inlined occupation title matches the shared table")
        for raw in ["6330", "06330", "6330.0", "", "n/a", "0770"]:
            check(res.parse_code(raw) == _occ.parse_code(raw),
                  f"parse_code({raw!r}) agrees with the shared parser")

        print("\n-- column resolution: industry must not be read as occupation --")
        import census_2018 as occ
        import census_2018_industry as ind
        cols = ["IncidentID", "Census2018_Industry", "Census2018_Occupation"]
        i_col = ind.find_industry_column(cols)
        o_col = occ.find_occupation_column(cols, exclude=[i_col])
        check(i_col == "Census2018_Industry", f"industry -> {i_col}")
        check(o_col == "Census2018_Occupation", f"occupation -> {o_col}")
        check(
            occ.find_occupation_column(["IncidentID", "Census2018_Industry"]) is None,
            "an industry-only file yields NO occupation column "
            "(census2018industry contains census2018)",
        )

        print("\n-- the split --")
        result, output = quiet(res.run, str(data), [""] * 5, str(out), chunk_size=5)
        check(result["rows_read"] == total, f"read {total} rows")

        merged = result["merged_rows"]
        check(merged["All_industry_electrician"] == N_ELEC * reps * len(BANDS),
              f"All_industry = {merged['All_industry_electrician']} "
              f"(expected {N_ELEC * reps * len(BANDS)})")
        check(merged["Construction_electrician"] == N_CONSTR * reps * len(BANDS),
              f"Construction = {merged['Construction_electrician']} "
              f"(expected {N_CONSTR * reps * len(BANDS)})")
        check(merged["Non_construction_electrician"] == N_NONC * reps * len(BANDS),
              f"Non_construction = {merged['Non_construction_electrician']}")
        check(merged["Unknown_industry_electrician"] == N_UNK * reps * len(BANDS),
              f"Unknown_industry = {merged['Unknown_industry_electrician']}")

        print("\n-- All = Construction + Non_construction + Unknown --")
        parts = (merged["Construction_electrician"]
                 + merged["Non_construction_electrician"]
                 + merged["Unknown_industry_electrician"])
        check(merged["All_industry_electrician"] == parts,
              f"{merged['All_industry_electrician']} == {parts}")
        check("All = Construction + Non_construction + Unknown_industry" in output,
              "the run states the reconciliation")
        check("并集" in output, "and warns that All overlaps the other files by design")

        print("\n-- only electricians in the outputs --")
        for group in res.GROUPS:
            df = pd.read_csv(out / f"{group}_all_ages.csv", dtype=str)
            check(
                set(df["Census2018_Occupation"]) == {"6330"},
                f"{group}: only occupation 6330 present "
                f"({sorted(set(df['Census2018_Occupation']))})",
            )
            if group != "All_industry_electrician":
                check(
                    set(df["expected_group"]) == {group},
                    f"{group}: every row expected in this group",
                )
                check(set(df["electrician_group"]) == {group},
                      f"{group}: electrician_group column stamped correctly")

        constr = pd.read_csv(out / "Construction_electrician_all_ages.csv", dtype=str)
        check(set(constr["Census2018_Industry"]) == {"0770"},
              f"construction group is industry 0770 only "
              f"({sorted(set(constr['Census2018_Industry']))})")
        nonc = pd.read_csv(out / "Non_construction_electrician_all_ages.csv", dtype=str)
        check("0770" not in set(nonc["Census2018_Industry"]),
              "no 0770 leaked into the non-construction group")
        check(not nonc["Census2018_Industry"].isna().any(),
              "no blank-industry row in the non-construction group")

        print("\n-- All is exactly the union of the parts, row for row --")
        all_ids = set(pd.read_csv(out / "All_industry_electrician_all_ages.csv",
                                  dtype=str)["IncidentID"])
        part_ids = set()
        for group in res.GROUPS:
            if group == "All_industry_electrician":
                continue
            part_ids |= set(
                pd.read_csv(out / f"{group}_all_ages.csv", dtype=str)["IncidentID"]
            )
        check(all_ids == part_ids, "same IncidentIDs in All as across the three parts")

        print("\n-- age stratification --")
        for band in BANDS:
            for group in res.GROUPS:
                path = out / f"{group}_{band}.csv"
                check(path.exists(), f"{group}_{band}.csv written")
            df = pd.read_csv(out / f"Construction_electrician_{band}.csv", dtype=str)
            check(set(df["age_band"]) == {band.replace("_", "-")},
                  f"{band}: age_band column correct")
            check(len(df) == N_CONSTR * reps,
                  f"{band}: {len(df)} construction electricians (expected {N_CONSTR * reps})")

        print("\n-- merged == concatenation of the strata --")
        for group in res.GROUPS:
            m = pd.read_csv(out / f"{group}_all_ages.csv", dtype=str)
            parts_df = pd.concat(
                [pd.read_csv(out / f"{group}_{b}.csv", dtype=str) for b in BANDS],
                ignore_index=True,
            )
            check(m.equals(parts_df), f"{group}: merged == 5 strata concatenated")
        text = (out / "All_industry_electrician_all_ages.csv").read_text()
        check(text.count("IncidentID") == 1, "header written once in the merged file")

        print("\n-- unknown-industry electricians are not padded into the control --")
        out2 = workdir / "out2"
        r2, o2 = quiet(res.run, str(data), [""] * 5, str(out2),
                       unknown_industry_goes_to="nonconstruction", chunk_size=100)
        check(not (out2 / "Unknown_industry_electrician_all_ages.csv").exists(),
              "no unknown file in that mode")
        nonc2 = pd.read_csv(out2 / "Non_construction_electrician_all_ages.csv", dtype=str)
        check(len(nonc2) == (N_NONC + N_UNK) * reps * len(BANDS),
              f"unknowns folded in ({len(nonc2)})")
        check(r2["merged_rows"]["All_industry_electrician"]
              == r2["merged_rows"]["Construction_electrician"] + len(nonc2),
              "All still reconciles in that mode")
        check("污染对照组" in output,
              "the default mode explains why unknowns are kept out of the control group")

        print("\n-- --include-mining --")
        out3 = workdir / "out3"
        r3, _ = quiet(res.run, str(data), [""] * 5, str(out3),
                      include_mining=True, chunk_size=100)
        c3 = pd.read_csv(out3 / "Construction_electrician_all_ages.csv", dtype=str)
        check("0370" in set(c3["Census2018_Industry"]),
              "mining electricians counted as construction when enabled")

        print("\n-- widening the electrician code set --")
        out4 = workdir / "out4"
        r4, _ = quiet(res.run, str(data), [""] * 5, str(out4),
                      electrician_codes=[6330, 6230], chunk_size=100)
        c4 = pd.read_csv(out4 / "All_industry_electrician_all_ages.csv", dtype=str)
        check(set(c4["Census2018_Occupation"]) == {"6330", "6230"},
              "ELECTRICIAN_CODES is honoured")

        print("\n-- file_map.csv carries input and output paths --")
        fmap = pd.read_csv(out / "file_map.csv")
        check(set(fmap.columns) >= {"age_band", "group", "input_file", "output_file", "rows"},
              f"columns: {list(fmap.columns)}")
        check(len(fmap) == 5 * 4 + 4, f"one row per output file, 24 (got {len(fmap)})")
        check(fmap["output_file"].map(lambda p: Path(p).is_file()).all(),
              "every output_file exists")
        check(fmap.loc[fmap["age_band"] != "ALL", "input_file"]
              .map(lambda p: Path(p).is_file()).all(), "every input_file exists")
        check("输出:" in output and "输入:" in output,
              "console report prints both paths")

        print("\n-- industry breakdown of electricians --")
        bd = pd.read_csv(out / "electrician_industry_breakdown.csv", dtype=str)
        check(int(bd["n_electricians"].astype(int).sum()) == N_ELEC * reps * len(BANDS),
              "breakdown covers every electrician")
        check(
            bd.loc[bd["Census2018_Industry"] == "770", "sector"].iloc[0] == "Construction",
            "sector names attached",
        )

        print("\n-- clear errors, not tracebacks --")
        msg = expect_exit(res.run, "", [""] * 5, str(workdir / "o5"))
        check("输入路径还没填" in msg, "empty input names the blank")
        msg = expect_exit(res.run, str(data), [""] * 5, "")
        check("输出路径还没填" in msg and "OUTPUT_DIR" in msg, "empty output names the blank")

        bad = workdir / "bad"
        bad.mkdir()
        pd.DataFrame({"IncidentID": ["1"], "Census2018_Industry": ["0770"]}).to_csv(
            bad / "nvdrs_age_18_27.csv", index=False
        )
        msg = expect_exit(res.run, str(bad), [""] * 5, str(workdir / "o6"))
        check("缺少必需的列" in msg and "Census2018_Occupation" in msg,
              "a missing occupation column is named, not silently substituted")

        print("\n-- chunk size does not change the output --")
        out7 = workdir / "out7"
        quiet(res.run, str(data), [""] * 5, str(out7), chunk_size=2)
        a = pd.read_csv(out / "Construction_electrician_all_ages.csv", dtype=str)
        b = pd.read_csv(out7 / "Construction_electrician_all_ages.csv", dtype=str)
        check(a.equals(b), "chunk_size 2 vs 5 gives identical output")

        print("\n-- CLI --")
        code, _ = quiet(res.main, ["--input-dir", str(data),
                                   "--output-dir", str(workdir / "out8")])
        check(code == 0, "CLI exit 0")
        check((workdir / "out8" / "All_industry_electrician_all_ages.csv").is_file(),
              "CLI produced the merged All file")
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
