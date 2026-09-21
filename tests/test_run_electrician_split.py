#!/usr/bin/env python3
"""Tests for run_electrician_split.py (text matching, no code tables).

The rules under test are exactly two:
    electrician   = Census2018_Occupation CONTAINS "electrician"
    construction  = Census2018_Industry   EQUALS   "construction"

What would quietly ruin the analysis:
  - "equals" silently behaving like "contains" on the industry column
  - reading Census2018_Industry as if it were the occupation column
  - All_industry not equalling the sum of its parts
  - unknown-industry electricians padding the comparison group
  - a non-electrician leaking into any output

Run with:  python tests/test_run_electrician_split.py
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


# (occupation text, industry text, expected group; "" = not an electrician)
ROWS = [
    # --- electricians in construction -----------------------------------
    ("Electricians", "Construction", "Construction_electrician"),
    ("Electrician", "construction", "Construction_electrician"),       # 大小写
    ("electricians", "  Construction  ", "Construction_electrician"),  # 首尾空格
    ("Electrician, apprentice", "Construction", "Construction_electrician"),
    # --- electricians elsewhere ------------------------------------------
    ("Electricians", "Educational services", "Non_construction_electrician"),
    ("Electricians", "Manufacturing", "Non_construction_electrician"),
    ("Electricians", "Mining, quarrying, and oil and gas extraction",
     "Non_construction_electrician"),
    # "Construction" 作为子串出现，但整格不等于 Construction -> 不算建筑业
    ("Electricians", "Construction and extraction", "Non_construction_electrician"),
    ("Electricians", "Heavy construction contractors", "Non_construction_electrician"),
    # --- electricians with unknown industry -------------------------------
    ("Electricians", "", "Unknown_industry_electrician"),
    ("Electricians", "Unknown", "Unknown_industry_electrician"),
    ("Electricians", "Not specified", "Unknown_industry_electrician"),
    # --- not electricians -------------------------------------------------
    ("Carpenters", "Construction", ""),
    ("Construction laborers", "Construction", ""),
    ("Electrical power-line installers and repairers", "Utilities", ""),
    ("Electrical and electronics repairers", "Manufacturing", ""),
    ("Registered nurses", "Health care and social assistance", ""),
    ("", "Construction", ""),
    ("Unknown", "Construction", ""),
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
        total = len(ROWS) * reps * len(BANDS)

        print("\n-- no code tables left: only text matching --")
        source = Path(res.__file__).read_text(encoding="utf-8")
        for gone in ("6330", "0770", "SECTORS", "build_construction_ranges",
                     "parse_code", "MINING"):
            check(gone not in source, f"no {gone!r} left in the script")
        check("ELECTRICIAN_KEYWORD" in source and "CONSTRUCTION_VALUE" in source,
              "the two text rules are the configuration")

        print("\n-- self-contained --")
        import re as _re
        locals_ = _re.findall(
            r"^(?:from|import)\s+(census_2018\w*|nvdrs_split|filter_\w+|split_by_age)",
            source, _re.M,
        )
        check(not locals_, f"no imports of sibling repo modules (found {locals_})")
        alone = workdir / "alone"
        alone.mkdir()
        shutil.copy(res.__file__, alone / "run_electrician_split.py")
        proc = subprocess.run(
            [sys.executable, "run_electrician_split.py",
             "--input-dir", str(data), "--output-dir", str(workdir / "alone_out")],
            cwd=alone, capture_output=True, text=True,
        )
        check(proc.returncode == 0,
              f"runs from a folder containing only itself (exit {proc.returncode})"
              + ("" if proc.returncode == 0 else f"\n{proc.stderr[-600:]}"))

        print("\n-- column resolution: industry is not read as occupation --")
        cols = ["IncidentID", "Census2018_Industry", "Census2018_Occupation"]
        i_col = res.find_industry_column(cols)
        o_col = res.find_occupation_column(cols, exclude=[i_col])
        check(i_col == "Census2018_Industry", f"industry -> {i_col}")
        check(o_col == "Census2018_Occupation", f"occupation -> {o_col}")
        check(res.find_occupation_column(["IncidentID", "Census2018_Industry"]) is None,
              "an industry-only file yields NO occupation column")

        print("\n-- the split --")
        result, output = quiet(res.run, str(data), [""] * 5, str(out), chunk_size=5)
        check(result["rows_read"] == total, f"read {total} rows")
        merged = result["merged_rows"]
        for group, expected in (
            ("All_industry_electrician", N_ELEC),
            ("Construction_electrician", N_CONSTR),
            ("Non_construction_electrician", N_NONC),
            ("Unknown_industry_electrician", N_UNK),
        ):
            want = expected * reps * len(BANDS)
            check(merged[group] == want, f"{group} = {merged[group]} (expected {want})")

        print("\n-- every row landed in its expected group --")
        for group in res.GROUPS:
            if group == "All_industry_electrician":
                continue
            df = pd.read_csv(out / f"{group}_all_ages.csv", dtype=str)
            bad = df.loc[df["expected_group"] != group]
            check(bad.empty, f"{group}: no unexpected rows"
                  + ("" if bad.empty else "\n" + bad[
                      ["Census2018_Occupation", "Census2018_Industry", "expected_group"]
                  ].drop_duplicates().to_string(index=False)))

        print("\n-- 'equals' really means equals on the industry column --")
        nonc = pd.read_csv(out / "Non_construction_electrician_all_ages.csv", dtype=str)
        nonc_inds = set(nonc["Census2018_Industry"])
        check("Construction and extraction" in nonc_inds,
              "'Construction and extraction' is NOT counted as construction")
        check("Heavy construction contractors" in nonc_inds,
              "'Heavy construction contractors' is NOT counted as construction")
        constr = pd.read_csv(out / "Construction_electrician_all_ages.csv", dtype=str)
        check(
            {res.norm_text(v) for v in constr["Census2018_Industry"]} == {"construction"},
            f"construction group holds only exact matches "
            f"({sorted(set(constr['Census2018_Industry']))})",
        )
        check(len(set(constr["Census2018_Industry"])) >= 3,
              "case differences and surrounding spaces still match")

        print("\n-- 'contains' really means contains on the occupation column --")
        allelec = pd.read_csv(out / "All_industry_electrician_all_ages.csv", dtype=str)
        occs = set(allelec["Census2018_Occupation"])
        check("Electrician, apprentice" in occs, "a longer electrician title matches")
        check("Electricians" in occs and "Electrician" in occs, "singular and plural match")
        for not_elec in ("Carpenters", "Registered nurses",
                         "Electrical power-line installers and repairers",
                         "Electrical and electronics repairers"):
            check(not_elec not in occs, f"{not_elec!r} is not treated as an electrician")

        print("\n-- All = Construction + Non_construction + Unknown --")
        parts = sum(merged[g] for g in res.GROUPS if g != "All_industry_electrician")
        check(merged["All_industry_electrician"] == parts,
              f"{merged['All_industry_electrician']} == {parts}")
        all_ids = set(allelec["IncidentID"])
        part_ids = set()
        for group in res.GROUPS:
            if group != "All_industry_electrician":
                part_ids |= set(
                    pd.read_csv(out / f"{group}_all_ages.csv", dtype=str)["IncidentID"]
                )
        check(all_ids == part_ids, "same IncidentIDs in All as across the three parts")
        check("并集" in output, "the report says All overlaps the parts by design")

        print("\n-- age stratification and merging --")
        for band in BANDS:
            for group in res.GROUPS:
                check((out / f"{group}_{band}.csv").exists(), f"{group}_{band}.csv written")
            df = pd.read_csv(out / f"Construction_electrician_{band}.csv", dtype=str)
            check(set(df["age_band"]) == {band.replace("_", "-")},
                  f"{band}: age_band column correct")
            check(len(df) == N_CONSTR * reps, f"{band}: {len(df)} construction electricians")
        for group in res.GROUPS:
            m = pd.read_csv(out / f"{group}_all_ages.csv", dtype=str)
            parts_df = pd.concat(
                [pd.read_csv(out / f"{group}_{b}.csv", dtype=str) for b in BANDS],
                ignore_index=True,
            )
            check(m.equals(parts_df), f"{group}: merged == 5 strata concatenated")
        text = (out / "All_industry_electrician_all_ages.csv").read_text()
        check(text.count("IncidentID") == 1, "header written once in the merged file")

        print("\n-- the two review files --")
        iv = pd.read_csv(out / "electrician_industry_values.csv", dtype=str)
        check(int(iv["n_electricians"].astype(int).sum()) == N_ELEC * reps * len(BANDS),
              "industry-values file covers every electrician")
        check(
            iv.loc[iv["Census2018_Industry"] == "Construction", "counted_as"].iloc[0]
            == "Construction",
            "counted_as marks the construction value",
        )
        check(
            iv.loc[iv["Census2018_Industry"] == "Construction and extraction",
                   "counted_as"].iloc[0] == "Non_construction",
            "and marks the near-miss value as non-construction",
        )
        # 空格子在统计表里显示成占位符，标签必须跟它实际进的组一致
        blank_label = iv.loc[iv["Census2018_Industry"] == res.BLANK_LABEL, "counted_as"]
        check(list(blank_label) == ["Unknown"],
              f"blank industry is labelled Unknown, matching the group it went to "
              f"(got {list(blank_label)})")
        n_labelled_unknown = int(
            iv.loc[iv["counted_as"] == "Unknown", "n_electricians"].astype(int).sum()
        )
        check(n_labelled_unknown == merged["Unknown_industry_electrician"],
              f"counted_as totals match the group sizes "
              f"({n_labelled_unknown} vs {merged['Unknown_industry_electrician']})")
        for label, group in (("Construction", "Construction_electrician"),
                             ("Non_construction", "Non_construction_electrician")):
            n = int(iv.loc[iv["counted_as"] == label, "n_electricians"].astype(int).sum())
            check(n == merged[group], f"counted_as {label} totals {n} == {merged[group]}")
        ov = pd.read_csv(out / "matched_occupation_values.csv", dtype=str)
        check(set(ov["Census2018_Occupation"]) == occs,
              "occupation-values file lists exactly what matched")
        check("匹配到的职业原文" in output and "电工所在行业的原文取值" in output,
              "both review tables are printed so the wording can be checked")

        print("\n-- loud warnings when the wording does not match --")
        _, no_constr = quiet(res.run, str(data), [""] * 5, str(workdir / "o_nc"),
                             construction_value="Bauwesen", chunk_size=100)
        check("没有任何电工的行业等于" in no_constr,
              "warns when CONSTRUCTION_VALUE matches nothing")
        _, no_elec = quiet(res.run, str(data), [""] * 5, str(workdir / "o_ne"),
                           electrician_keyword="zzz", chunk_size=100)
        check("一个电工都没匹配到" in no_elec, "warns when the keyword matches nothing")
        check("数字码" in no_elec, "and suggests the column may hold codes instead of text")

        print("\n-- unknown industry is kept out of the control group by default --")
        out2 = workdir / "out2"
        r2, _ = quiet(res.run, str(data), [""] * 5, str(out2),
                      unknown_industry_goes_to="nonconstruction", chunk_size=100)
        check(not (out2 / "Unknown_industry_electrician_all_ages.csv").exists(),
              "no unknown file in that mode")
        nonc2 = pd.read_csv(out2 / "Non_construction_electrician_all_ages.csv", dtype=str)
        check(len(nonc2) == (N_NONC + N_UNK) * reps * len(BANDS),
              f"unknowns folded in ({len(nonc2)})")
        check(r2["merged_rows"]["All_industry_electrician"]
              == r2["merged_rows"]["Construction_electrician"] + len(nonc2),
              "All still reconciles in that mode")
        check("污染对照组" in output, "the default mode explains why they are kept out")

        print("\n-- file_map.csv carries input and output paths --")
        fmap = pd.read_csv(out / "file_map.csv")
        check(set(fmap.columns) >= {"age_band", "group", "input_file", "output_file", "rows"},
              f"columns: {list(fmap.columns)}")
        check(len(fmap) == 5 * 4 + 4, f"one row per output file, 24 (got {len(fmap)})")
        check(fmap["output_file"].map(lambda p: Path(p).is_file()).all(),
              "every output_file exists")
        check(fmap.loc[fmap["age_band"] != "ALL", "input_file"]
              .map(lambda p: Path(p).is_file()).all(), "every input_file exists")
        check("输出:" in output and "输入:" in output, "console prints both paths")

        print("\n-- clear errors, not tracebacks --")
        msg = expect_exit(res.run, "", [""] * 5, str(workdir / "o5"))
        check("输入路径还没填" in msg, "empty input names the blank")
        msg = expect_exit(res.run, str(data), [""] * 5, "")
        check("输出路径还没填" in msg and "OUTPUT_DIR" in msg, "empty output names the blank")
        bad = workdir / "bad"
        bad.mkdir()
        pd.DataFrame({"IncidentID": ["1"], "Census2018_Industry": ["Construction"]}).to_csv(
            bad / "nvdrs_age_18_27.csv", index=False
        )
        msg = expect_exit(res.run, str(bad), [""] * 5, str(workdir / "o6"))
        check("缺少必需的列" in msg and "Census2018_Occupation" in msg,
              "a missing occupation column is named, not substituted")

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
