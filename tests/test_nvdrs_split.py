#!/usr/bin/env python3
"""End-to-end check of nvdrs_split.py against the synthetic sample data.

Run with:  python tests/test_nvdrs_split.py
Exits non-zero on the first failed assertion.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nvdrs_split  # noqa: E402
from make_sample_data import AGE_BANDS, ROWS  # noqa: E402
import make_sample_data  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        failures.append(message)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="nvdrs-test-"))
    try:
        data_dir = workdir / "data"
        out_dir = workdir / "out"
        make_sample_data.main(str(data_dir))

        print("\n-- running nvdrs_split --")
        rc = nvdrs_split.main(
            ["--input", str(data_dir), "--output-dir", str(out_dir), "--write-empty"]
        )
        check(rc == 0, "exit code is 0")

        print("\n-- classification matches expectations --")
        for band in AGE_BANDS:
            labeled = out_dir / "labeled" / f"{band.replace('-', '_')}_labeled.csv"
            check(labeled.exists(), f"{labeled.name} written")
            if not labeled.exists():
                continue
            df = pd.read_csv(labeled, dtype=str)
            mismatch = df.loc[
                df["occupation_group"] != df["expected_occupation_group"],
                ["Occupation", "OccupationCode", "expected_occupation_group",
                 "occupation_group", "occupation_group_rule"],
            ]
            check(
                mismatch.empty,
                f"{band}: all {len(df)} rows classified as expected"
                + ("" if mismatch.empty else f"\n{mismatch.to_string(index=False)}"),
            )

        print("\n-- circumstance boolean --")
        df = pd.read_csv(
            out_dir / "labeled" / "18_30_labeled.csv",
            dtype={"CircumstancesKnown": str},
        )
        pairs = df[["CircumstancesKnown", "circumstance_known_bool"]]
        yes = pairs.loc[pairs["CircumstancesKnown"].isin(["Yes", "yes"])]
        no = pairs.loc[pairs["CircumstancesKnown"] == "No"]
        unknown = pairs.loc[pairs["CircumstancesKnown"] == "Unknown"]
        blank = pairs.loc[pairs["CircumstancesKnown"].isna()]
        check(bool(yes["circumstance_known_bool"].all()) and len(yes) > 0,
              f"Yes -> True ({len(yes)} rows)")
        check(not no["circumstance_known_bool"].any() and len(no) > 0,
              f"No -> False ({len(no)} rows)")
        check(unknown["circumstance_known_bool"].isna().all() and len(unknown) > 0,
              f"Unknown -> <NA> ({len(unknown)} rows)")
        check(blank["circumstance_known_bool"].isna().all() and len(blank) > 0,
              f"blank -> <NA> ({len(blank)} rows)")

        print("\n-- split files: 4 groups x circumstance, per age band --")
        for band in AGE_BANDS:
            band_dir = out_dir / "splits" / band.replace("-", "_")
            for group in ("construction", "non_construction", "non_workforce", "military"):
                for label in ("known_yes", "known_no"):
                    path = band_dir / f"{group}__circumstance_{label}.csv"
                    check(path.exists(), f"{band}/{path.name}")

        print("\n-- split files partition the rows without loss or overlap --")
        for band in AGE_BANDS:
            band_dir = out_dir / "splits" / band.replace("-", "_")
            ids: list[str] = []
            for path in band_dir.glob("*.csv"):
                part = pd.read_csv(path, dtype=str)
                ids.extend(part["IncidentID"].tolist())
            labeled = pd.read_csv(
                out_dir / "labeled" / f"{band.replace('-', '_')}_labeled.csv", dtype=str
            )
            check(
                len(ids) == len(labeled) and set(ids) == set(labeled["IncidentID"]),
                f"{band}: splits hold exactly the {len(labeled)} labeled rows "
                f"(got {len(ids)}, {len(set(ids))} distinct)",
            )

        print("\n-- summary counts --")
        summary = pd.read_csv(out_dir / "summary_counts.csv")
        check(
            len(summary) == len(AGE_BANDS) * len(nvdrs_split.OCCUPATION_GROUPS),
            f"summary has one row per age band x group ({len(summary)})",
        )
        check(
            int(summary["n_total"].sum()) == len(AGE_BANDS) * len(ROWS),
            f"summary totals equal the {len(AGE_BANDS) * len(ROWS)} input rows "
            f"(got {int(summary['n_total'].sum())})",
        )
        per_band = summary.groupby("age_band")["n_total"].sum()
        check(
            (per_band == len(ROWS)).all(),
            f"every age band totals {len(ROWS)} rows",
        )
        col_sum = summary[
            ["n_circumstance_known_yes", "n_circumstance_known_no",
             "n_circumstance_undetermined"]
        ].sum(axis=1)
        check(
            bool((col_sum == summary["n_total"]).all()),
            "yes + no + undetermined == total in every summary row",
        )

        print("\n-- review outputs --")
        unmapped = pd.read_csv(out_dir / "unmapped_occupations.csv")
        fallback_values = set(unmapped["occupation_value"])
        check(
            {"Barber", "Nanny", "Mother of three"} <= fallback_values,
            f"fallback occupations reported for review: {sorted(fallback_values)}",
        )
        audit = pd.read_csv(out_dir / "occupation_group_audit.csv")
        check(len(audit) > 0, f"occupation_group_audit.csv has {len(audit)} rows")

        print("\n-- --inspect mode runs without writing --")
        rc = nvdrs_split.main(["--input", str(data_dir), "--inspect"])
        check(rc == 0, "--inspect exit code is 0")

        print("\n-- --drop-unknown-circumstance --")
        out2 = workdir / "out2"
        nvdrs_split.main(
            ["--input", str(data_dir), "--output-dir", str(out2),
             "--drop-unknown-circumstance"]
        )
        check(
            not list((out2 / "splits").rglob("*known_undetermined*")),
            "no known_undetermined files when dropping",
        )
        s2 = pd.read_csv(out2 / "summary_counts.csv")
        check(
            int(s2["n_circumstance_undetermined"].sum()) == 0,
            "undetermined rows excluded from the summary",
        )

        print("\n-- --include-extraction --")
        out3 = workdir / "out3"
        nvdrs_split.main(
            ["--input", str(data_dir), "--output-dir", str(out3), "--include-extraction"]
        )
        check((out3 / "summary_counts.csv").exists(), "runs with extraction included")
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
