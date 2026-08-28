#!/usr/bin/env python3
"""Check that verify_circumstance.py actually catches a broken derived column.

Builds a labeled file with `circumstance_known_c` + `circumstance_known_bool`,
injects known errors, and asserts the script finds exactly those rows.

Run with:  python tests/test_verify_circumstance.py
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

import verify_circumstance  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def build_file(path: Path, n_clean: int = 2500) -> dict:
    """Clean rows cycling Yes/No/Unknown/blank, then 3 deliberately wrong rows."""
    raw_cycle = ["Yes", "No", "Unknown", "", "yes", "NO"]
    correct = {"yes": "True", "no": "False"}

    rows = []
    for i in range(n_clean):
        raw = raw_cycle[i % len(raw_cycle)]
        rows.append(
            {
                "IncidentID": f"C{i:05d}",
                "circumstance_known_c": raw,
                "circumstance_known_bool": correct.get(raw.strip().lower(), ""),
            }
        )

    injected = [
        # Yes must be True, not False
        {"IncidentID": "BAD001", "circumstance_known_c": "Yes",
         "circumstance_known_bool": "False"},
        # No must be False, not True
        {"IncidentID": "BAD002", "circumstance_known_c": "No",
         "circumstance_known_bool": "True"},
        # Unknown must stay <NA>, not silently become False
        {"IncidentID": "BAD003", "circumstance_known_c": "Unknown",
         "circumstance_known_bool": "False"},
    ]
    rows.extend(injected)

    pd.DataFrame(rows).to_csv(path, index=False)
    return {"n_total": len(rows), "bad_ids": {r["IncidentID"] for r in injected}}


def run(argv) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = verify_circumstance.main(argv)
    return code, buffer.getvalue()


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="verify-test-"))
    try:
        data = workdir / "18_30_labeled.csv"
        meta = build_file(data)
        out = workdir / "report"

        print("\n-- detects the injected errors --")
        code, output = run(
            ["--input", str(data), "--batch-size", "1000", "--output-dir", str(out)]
        )
        check(code == 1, "exit code 1 when mismatches exist")
        check(f"total rows checked: {meta['n_total']}" in output,
              f"all {meta['n_total']} rows checked (chunked read loses nothing)")
        check("mismatches        : 3" in output, "found exactly 3 mismatches")

        mismatches = pd.read_csv(out / "circumstance_mismatches.csv", dtype=str)
        check(set(mismatches["IncidentID"]) == meta["bad_ids"],
              f"flagged exactly the injected rows: {sorted(mismatches['IncidentID'])}")
        check(
            list(mismatches.loc[mismatches["IncidentID"] == "BAD003", "expected_bool"])
            == ["UNDETERMINED"],
            "Unknown -> expected UNDETERMINED, and it survives a CSV round-trip",
        )
        check(
            set(mismatches["row_number"].astype(int)) == {2502, 2503, 2504},
            f"row numbers point at the real lines: "
            f"{sorted(mismatches['row_number'].astype(int))}",
        )

        print("\n-- column auto-detection --")
        check("raw column : circumstance_known_c" in output,
              "picked circumstance_known_c as the raw column")
        check("bool column: circumstance_known_bool" in output,
              "picked circumstance_known_bool as the derived column")
        check("id column  : IncidentID" in output, "picked IncidentID as the id column")

        print("\n-- batching --")
        check(output.count("-- batch ") == 3, "2503 rows at 1000/batch -> 3 batches")
        code, small = run(["--input", str(data), "--batch-size", "500"])
        check(small.count("-- batch ") == 6, "at 500/batch -> 6 batches")

        code, paged = run(
            ["--input", str(data), "--batch-size", "500", "--max-batches", "2"]
        )
        check(paged.count("-- batch ") == 2, "--max-batches 2 shows 2 batches")
        check("--start-row 1000" in paged, "tells you how to continue paging")

        code, resumed = run(
            ["--input", str(data), "--batch-size", "500", "--start-row", "2000"]
        )
        check("total rows checked: 503" in resumed,
              "--start-row 2000 checks the remaining 503 rows")

        print("\n-- --mismatches-only --")
        code, only = run(["--input", str(data), "--batch-size", "500", "--mismatches-only"])
        check(only.count("-- batch ") == 1, "only the one bad batch is printed")
        check("mismatches        : 3" in only, "still counts all 3 mismatches")

        print("\n-- a clean file passes --")
        clean = workdir / "clean.csv"
        df = pd.read_csv(data, dtype=str)
        df = df.loc[~df["IncidentID"].isin(meta["bad_ids"])]
        df.to_csv(clean, index=False)
        code, clean_out = run(["--input", str(clean)])
        check(code == 0, "exit code 0 on a clean file")
        check("mismatches        : 0" in clean_out, "reports 0 mismatches")
        check("accuracy          : 100.0000%" in clean_out, "reports 100% accuracy")

        print("\n-- crosstab covers every raw value --")
        pairs = pd.read_csv(out / "circumstance_value_pairs.csv")
        check(
            set(pairs["raw_value"]) == {"Yes", "No", "yes", "NO", "Unknown", "<blank>"},
            f"all raw values represented: {sorted(set(pairs['raw_value']))}",
        )
        check(
            int(pairs["n"].sum()) == meta["n_total"],
            f"pair counts sum to {meta['n_total']} (got {int(pairs['n'].sum())})",
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
