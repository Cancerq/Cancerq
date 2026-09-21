#!/usr/bin/env python3
"""nvdrs_split.py must group on census_2018 alone when that column is present.

The point of these tests: prove no other feature can influence the grouping.
Each fixture row carries a census_2018 code plus deliberately CONTRADICTORY
occupation text, occupation code and industry columns, so if any of the old
keyword/industry stages still fired, the result would differ.

Run with:  python tests/test_census_2018_only.py
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

import census_2018  # noqa: E402
import nvdrs_split  # noqa: E402

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


# census_2018, contradictory text, contradictory 2010 code, industry, expected
ROWS = [
    # Real construction code, but the text says nurse and industry says nothing.
    ("6230", "Registered nurse", "3130", "", "construction"),
    ("6260", "Retired", "", "", "construction"),
    ("6200", "Unknown", "", "", "construction"),
    ("6765", "Student", "", "", "construction"),
    # NOT construction by code, even though every other feature screams it is.
    ("3130", "Construction laborer", "6260", "0770", "non_construction"),
    ("9130", "Carpenter", "6230", "0770", "non_construction"),
    ("6800", "Construction laborer", "6260", "0770", "non_construction"),  # extraction
    ("0220", "Construction manager", "0220", "0770", "non_construction"),  # managers
    # Military and non-workforce by code.
    ("9820", "Carpenter", "6230", "0770", "military"),
    ("9920", "Carpenter", "6230", "0770", "non_workforce"),
    # No usable code: unclassified regardless of how clear the text is.
    ("", "Construction laborer", "6260", "0770", "unclassified"),
    ("unknown", "Construction laborer", "6260", "0770", "unclassified"),
]

HEADER_EXTRA = {"Occupation": 1, "OccupationCode": 2, "IndustryCode": 3}


def build(path: Path) -> None:
    rows = []
    for i, (code, text, occ2010, industry, expected) in enumerate(ROWS):
        rows.append(
            {
                "IncidentID": f"r{i}",
                "circumstance_known_c": "Yes" if i % 2 else "No",
                "census_2018": code,
                "Occupation": text,
                "OccupationCode": occ2010,
                "IndustryCode": industry,
                "expected_group": expected,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="c2018-"))
    try:
        print("\n-- census_2018.classify, in isolation --")
        for code, want in [
            ("6200", "construction"), ("6765", "construction"),
            ("6199", "non_construction"), ("6766", "non_construction"),
            ("6800", "non_construction"), ("6950", "non_construction"),
            ("0220", "non_construction"),
            ("9800", "military"), ("9830", "military"),
            ("9920", "non_workforce"),
            ("", "unclassified"), ("abc", "unclassified"),
        ]:
            group, _ = census_2018.classify(code)
            check(group == want, f"classify({code!r}) -> {group}")

        group, _ = census_2018.classify("6800", include_extraction=True)
        check(group == "construction", "include_extraction moves 6800 to construction")
        group, _ = census_2018.classify("0220", include_managers=True)
        check(group == "construction", "include_managers moves 0220 to construction")

        print("\n-- one definition of construction, shared with filter_construction --")
        import filter_construction as fc

        check(
            fc.CONSTRUCTION_TRADES is census_2018.CONSTRUCTION_TRADES,
            "filter_construction reuses the census_2018 range object (cannot drift)",
        )
        check(
            fc.build_construction_ranges(False, False)
            == census_2018.build_construction_ranges(False, False),
            "both modules build identical construction ranges",
        )

        print("\n-- nvdrs_split groups on census_2018 and ignores everything else --")
        data = workdir / "nvdrs_age_18_27.csv"
        build(data)
        out = workdir / "out"
        _, output = quiet(
            nvdrs_split.main, ["--input", str(data), "--output-dir", str(out)]
        )

        labeled = pd.read_csv(out / "labeled" / "18_27_labeled.csv", dtype=str)
        mismatch = labeled.loc[
            labeled["occupation_group"] != labeled["expected_group"],
            ["census_2018", "Occupation", "OccupationCode", "IndustryCode",
             "expected_group", "occupation_group", "occupation_group_rule"],
        ]
        check(
            mismatch.empty,
            "every row grouped by census_2018 despite contradictory text/code/industry"
            + ("" if mismatch.empty else f"\n{mismatch.to_string(index=False)}"),
        )

        rules_used = set(labeled["occupation_group_rule"])
        check(
            all(r.startswith("census_2018") for r in rules_used),
            f"only census_2018 rules fired: {sorted(rules_used)}",
        )
        for banned in ("occupation_keyword", "industry_code", "industry_keyword",
                       "fallback_text", "occupation_code"):
            check(banned not in rules_used, f"rule {banned!r} never fired")

        print("\n-- the blank and unreadable codes are distinguishable --")
        blank_rule = labeled.loc[labeled["IncidentID"] == "r10", "occupation_group_rule"].iloc[0]
        bad_rule = labeled.loc[labeled["IncidentID"] == "r11", "occupation_group_rule"].iloc[0]
        check(blank_rule == "census_2018_blank", f"blank code -> {blank_rule}")
        check(bad_rule == "census_2018_unparseable", f"unreadable code -> {bad_rule}")

        print("\n-- --include-extraction / --include-managers flow through --")
        out2 = workdir / "out2"
        quiet(nvdrs_split.main, ["--input", str(data), "--output-dir", str(out2),
                                 "--include-extraction", "--include-managers"])
        l2 = pd.read_csv(out2 / "labeled" / "18_27_labeled.csv", dtype=str)
        check(
            l2.loc[l2["census_2018"] == "6800", "occupation_group"].iloc[0] == "construction",
            "6800 becomes construction with --include-extraction",
        )
        check(
            l2.loc[l2["census_2018"] == "0220", "occupation_group"].iloc[0] == "construction",
            "0220 becomes construction with --include-managers",
        )

        print("\n-- the audit reports census_2018 values, not occupation text --")
        audit = pd.read_csv(out / "occupation_group_audit.csv", dtype=str)
        check(
            set(audit["occupation_value"].dropna()) <= {r[0] for r in ROWS if r[0]},
            f"audit keyed on census_2018 codes: {sorted(set(audit['occupation_value'].dropna()))}",
        )
        unmapped = pd.read_csv(out / "unmapped_occupations.csv")
        check(len(unmapped) == 0, "no fallback rows: census_2018 mode has no fallback")

        print("\n-- --inspect names the active mode --")
        _, insp = quiet(nvdrs_split.main, ["--input", str(data), "--inspect"])
        check("occupation grouping mode: census_2018 only" in insp,
              "--inspect says census_2018 only")

        print("\n-- without a census_2018 column the old classifier still runs --")
        legacy = workdir / "legacy.csv"
        pd.DataFrame({
            "IncidentID": ["a", "b"],
            "circumstance_known_c": ["Yes", "No"],
            "Occupation": ["Carpenter", "Registered nurse"],
        }).to_csv(legacy, index=False)
        out3 = workdir / "out3"
        quiet(nvdrs_split.main, ["--input", str(legacy), "--output-dir", str(out3)])
        l3 = pd.read_csv(out3 / "labeled" / "legacy_labeled.csv", dtype=str)
        check(
            list(l3["occupation_group"]) == ["construction", "non_construction"],
            f"keyword classifier still works when census_2018 is absent: "
            f"{list(l3['occupation_group'])}",
        )
        _, insp3 = quiet(nvdrs_split.main, ["--input", str(legacy), "--inspect"])
        check("multi-feature" in insp3, "--inspect reports the multi-feature mode there")

        print("\n-- circumstance splitting is unaffected --")
        band_dir = out / "splits" / "18_27"
        files = sorted(p.name for p in band_dir.glob("construction__*.csv"))
        check(files, f"construction splits written: {files}")
        total = sum(len(pd.read_csv(p)) for p in band_dir.glob("*.csv"))
        check(total == len(ROWS), f"splits still hold all {len(ROWS)} rows (got {total})")
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
