#!/usr/bin/env python3
"""Batch-check the derived circumstance_known_bool against the raw source column.

Reads the file in chunks (so it works on large exports), and for every batch
reports how the raw values (`circumstance_known_c`: Yes / No / blank / ...) line
up with the derived boolean (`circumstance_known_bool`: True / False / <NA>).

A row is a MISMATCH when the derived value is not what the mapping rules say it
should be:

    Yes  -> True
    No   -> False
    anything else (blank, "Unknown", ...) -> UNDETERMINED (i.e. pandas <NA>)

Usage:

    # walk the whole file 1000 rows at a time
    python verify_circumstance.py --input out/labeled/18_30_labeled.csv

    # only show batches that actually contain a problem
    python verify_circumstance.py --input out/labeled/ --mismatches-only

    # page through interactively, 500 rows per batch, 5 batches at a time
    python verify_circumstance.py --input file.csv --batch-size 500 \
        --start-row 0 --max-batches 5

Exit code is 1 if any mismatch was found, else 0.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nvdrs_split import (  # noqa: E402
    CIRCUMSTANCE_COL_CANDIDATES,
    DEFAULT_RULES,
    _norm_value,
    find_column,
    load_rules,
    read_csv,
)

BOOL_COL_CANDIDATES = (
    "circumstanceknownbool",
    "circumstanceknownboolean",
    "circumstanceknownflag",
)

# How the derived boolean renders once it has been written to CSV and read back.
TRUE_TOKENS = {"true", "t", "1", "yes"}
FALSE_TOKENS = {"false", "f", "0", "no"}


def parse_bool_cell(value) -> object:
    """Read back a written-out nullable boolean: True / False / pd.NA."""
    text = _norm_value(value)
    if text in TRUE_TOKENS:
        return True
    if text in FALSE_TOKENS:
        return False
    return pd.NA


def expected_bool(raw_value, true_set: set, false_set: set) -> object:
    text = _norm_value(raw_value)
    if text in true_set:
        return True
    if text in false_set:
        return False
    return pd.NA


# Rendered label for the "neither Yes nor No" state. Deliberately not "<NA>"
# or "NA": pandas treats both as missing, so they would come back as NaN when
# the mismatch report is read into a DataFrame.
UNDETERMINED = "UNDETERMINED"


def render(value) -> str:
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    return UNDETERMINED


def resolve_pair(df: pd.DataFrame, args) -> tuple[str, str]:
    raw_col = args.raw_col or find_column(df, CIRCUMSTANCE_COL_CANDIDATES)
    bool_col = args.bool_col or find_column(
        df, BOOL_COL_CANDIDATES, exclude=[raw_col] if raw_col else []
    )
    if raw_col is None:
        raise SystemExit(
            "could not find the raw circumstance column; pass --raw-col "
            f"(looked for {', '.join(CIRCUMSTANCE_COL_CANDIDATES)})"
        )
    if bool_col is None:
        raise SystemExit(
            "could not find the derived boolean column; pass --bool-col "
            "(expected circumstance_known_bool)"
        )
    for name in (raw_col, bool_col):
        if name not in df.columns:
            raise SystemExit(f"column {name!r} not found in the input file")
    if raw_col == bool_col:
        raise SystemExit(
            f"--raw-col and --bool-col both resolved to {raw_col!r}; specify them explicitly"
        )
    return raw_col, bool_col


def check_file(path: Path, args, rules: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (pair_counts, mismatch_rows) for one file, reporting per batch."""
    true_set = {_norm_value(v) for v in rules["circumstance_true"]}
    false_set = {_norm_value(v) for v in rules["circumstance_false"]}

    header = read_csv(path, args.encoding, nrows=0)
    raw_col, bool_col = resolve_pair(header, args)
    id_col = args.id_col or next(
        (c for c in header.columns if _norm_value(c) in {"incidentid", "personid", "id"}),
        None,
    )

    print(f"\n{'=' * 78}\n{path}")
    print(f"raw column : {raw_col}")
    print(f"bool column: {bool_col}")
    if id_col:
        print(f"id column  : {id_col}")

    pair_counts: dict[tuple[str, str], int] = {}
    mismatches: list[pd.DataFrame] = []
    row_offset = 0
    batch_index = 0
    shown_batches = 0
    total_rows = 0
    total_mismatch = 0

    reader = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=True,
        encoding=args.encoding,
        chunksize=args.batch_size,
    )

    for chunk in reader:
        start = row_offset
        row_offset += len(chunk)
        batch_index += 1

        if start + len(chunk) <= args.start_row:
            continue
        if args.start_row > start:
            chunk = chunk.iloc[args.start_row - start :]
            start = args.start_row

        actual = chunk[bool_col].map(parse_bool_cell)
        wanted = chunk[raw_col].map(lambda v: expected_bool(v, true_set, false_set))

        # NA == NA is False under normal comparison, so compare the rendered form.
        actual_str = actual.map(render)
        wanted_str = wanted.map(render)
        bad = actual_str != wanted_str

        raw_display = chunk[raw_col].fillna("<blank>").replace("", "<blank>")
        for (raw_value, bool_value), n in (
            pd.DataFrame({"raw": raw_display, "bool": actual_str})
            .value_counts()
            .items()
        ):
            pair_counts[(raw_value, bool_value)] = (
                pair_counts.get((raw_value, bool_value), 0) + int(n)
            )

        total_rows += len(chunk)
        n_bad = int(bad.sum())
        total_mismatch += n_bad

        if n_bad:
            cols = [c for c in (id_col, raw_col, bool_col) if c]
            detail = chunk.loc[bad, cols].copy()
            detail.insert(0, "row_number", detail.index + 2)  # +2: header + 0-index
            detail["expected_bool"] = wanted_str[bad].values
            detail["actual_bool"] = actual_str[bad].values
            detail.insert(0, "source_file", path.name)
            mismatches.append(detail)

        if args.mismatches_only and not n_bad:
            continue

        shown_batches += 1
        status = "OK" if n_bad == 0 else f"{n_bad} MISMATCH"
        print(
            f"\n-- batch {batch_index}  rows {start + 1}-{start + len(chunk)}"
            f"  ({len(chunk)} rows)  [{status}]"
        )
        batch_pairs = (
            pd.DataFrame({"raw": raw_display, "bool": actual_str})
            .value_counts()
            .rename("n")
            .reset_index()
            .sort_values(["raw", "bool"])
        )
        print(batch_pairs.to_string(index=False))
        if n_bad:
            print(f"   mismatching rows (showing up to {args.show_rows}):")
            print(mismatches[-1].head(args.show_rows).to_string(index=False))

        if args.max_batches and shown_batches >= args.max_batches:
            print(
                f"\n(stopped after {args.max_batches} shown batches; "
                f"continue with --start-row {start + len(chunk)})"
            )
            break

    counts_df = pd.DataFrame(
        [
            {"source_file": path.name, "raw_value": raw, "bool_value": b, "n": n}
            for (raw, b), n in sorted(pair_counts.items())
        ]
    )
    mismatch_df = (
        pd.concat(mismatches, ignore_index=True) if mismatches else pd.DataFrame()
    )

    print(f"\n{path.name}: checked {total_rows} rows, {total_mismatch} mismatch(es)")
    if total_rows:
        print(f"   accuracy: {(total_rows - total_mismatch) / total_rows:.4%}")
    return counts_df, mismatch_df


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", nargs="+", required=True, help="CSV file(s) or a directory")
    parser.add_argument("--raw-col", help="raw column (default: auto, e.g. circumstance_known_c)")
    parser.add_argument("--bool-col", help="derived column (default: circumstance_known_bool)")
    parser.add_argument("--id-col", help="identifier column shown next to mismatches")
    parser.add_argument("--batch-size", type=int, default=1000, help="rows per batch (default 1000)")
    parser.add_argument("--start-row", type=int, default=0, help="skip this many data rows")
    parser.add_argument("--max-batches", type=int, default=0, help="stop after N shown batches (0 = all)")
    parser.add_argument("--show-rows", type=int, default=10, help="mismatch rows printed per batch")
    parser.add_argument("--mismatches-only", action="store_true", help="only print batches with a mismatch")
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--rules", type=Path, help="same rules JSON used for the split")
    parser.add_argument("--output-dir", help="write mismatch and crosstab CSVs here")
    args = parser.parse_args(argv)

    rules = load_rules(args.rules)

    files: list[Path] = []
    for item in args.input:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(path.glob("*.csv")))
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f"input not found: {path}")
    if not files:
        raise SystemExit("no CSV files found")

    all_counts, all_mismatches = [], []
    for path in files:
        counts, mismatch = check_file(path, args, rules)
        if not counts.empty:
            all_counts.append(counts)
        if not mismatch.empty:
            all_mismatches.append(mismatch)

    print(f"\n{'=' * 78}\nOVERALL")
    counts = (
        pd.concat(all_counts, ignore_index=True)
        if all_counts
        else pd.DataFrame(columns=["source_file", "raw_value", "bool_value", "n"])
    )
    if not counts.empty:
        crosstab = counts.pivot_table(
            index="raw_value", columns="bool_value", values="n", aggfunc="sum", fill_value=0
        )
        print("\nraw value x derived boolean:")
        print(crosstab.to_string())

    total = int(counts["n"].sum()) if not counts.empty else 0
    mismatches = (
        pd.concat(all_mismatches, ignore_index=True) if all_mismatches else pd.DataFrame()
    )
    n_bad = len(mismatches)
    print(f"\ntotal rows checked: {total}")
    print(f"mismatches        : {n_bad}")
    if total:
        print(f"accuracy          : {(total - n_bad) / total:.4%}")

    if args.output_dir:
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        counts.to_csv(out / "circumstance_value_pairs.csv", index=False)
        mismatches.to_csv(out / "circumstance_mismatches.csv", index=False)
        print(f"\nwrote {out}/circumstance_value_pairs.csv and circumstance_mismatches.csv")

    if n_bad:
        print("\nMismatches found -- the derived column does not follow the mapping rules.")
        return 1
    print(
        "\nAll rows follow the mapping rules "
        f"(Yes->TRUE, No->FALSE, other->{UNDETERMINED})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
