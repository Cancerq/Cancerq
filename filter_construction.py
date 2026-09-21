#!/usr/bin/env python3
"""Filter the age-band CSVs to construction workers using Census 2018 codes.

Takes the files produced by split_by_age.py (a list, a directory, or a .txt
path list) and writes, per input file:

    <stem>_construction.csv      census_2018 inside the construction range
    <stem>_blank.csv             census_2018 blank / missing
    <stem>_nonconstruction.csv   everything else (with --keep-nonconstruction)

plus three manifests so the next step can be chained:

    input_list.txt               the files that were read, in order
    construction_list.txt        the construction outputs
    blank_list.txt               the blank-code outputs

and two review files: a per-file summary and a per-code breakdown.

CENSUS 2018 OCCUPATION CODES
    Construction and Extraction Occupations occupy 6200-6950 in the 2018
    Census occupation code list, split as

        6200-6765   construction trades           <- kept by default
        6800-6950   extraction workers            <- --include-extraction

    Construction managers (0220) sit under Management, not here, so they are
    NOT included unless you pass --include-managers.

    Note the 2018 list differs from the 2010 one used elsewhere in this repo
    (2010 extraction ends at 6940, 2018 at 6950). Do not mix them.

Usage:

    # 1) see which codes are actually present before filtering
    python filter_construction.py --input age_chunks/ --inspect

    # 2) filter
    python filter_construction.py --input age_chunks/ --output-dir construction/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from filter_years import collect_inputs  # noqa: E402
from nvdrs_split import _norm_colname, _norm_value, read_csv  # noqa: E402

# Code ranges, titles and parsing all come from census_2018.py, which is the
# single definition shared with nvdrs_split.py.
from census_2018 import (  # noqa: E402
    title as census_title,
    BLANK,
    CENSUS_2018_COL_CANDIDATES,
    CONSTRUCTION_MANAGERS,
    CONSTRUCTION_TRADES,
    EXTRACTION_WORKERS,
    UNPARSEABLE,
    build_construction_ranges,
    describe_ranges,
    in_ranges,
    parse_code,
)


def find_census_column(columns, override: str | None = None) -> str:
    if override:
        if override not in columns:
            raise SystemExit(
                f"--census-col {override!r} is not in the file. Columns: "
                + ", ".join(map(str, columns))
            )
        return override

    normalised = {_norm_colname(c): c for c in columns}
    for cand in CENSUS_2018_COL_CANDIDATES:
        if cand in normalised:
            return normalised[cand]
    for cand in CENSUS_2018_COL_CANDIDATES:
        for norm, original in normalised.items():
            if cand in norm:
                return original

    # A 2010 column is a different code list; using it here would be wrong.
    wrong_vintage = [
        original
        for norm, original in normalised.items()
        if "census" in norm and "2018" not in norm
    ]
    message = [
        "could not find a census_2018 column "
        f"(looked for: {', '.join(CENSUS_2018_COL_CANDIDATES)})."
    ]
    if wrong_vintage:
        message.append(
            "Found census-like columns of a different vintage, which use a "
            f"different code list and are NOT interchangeable: {', '.join(wrong_vintage)}."
        )
    message.append("Name the right column with --census-col.")
    raise SystemExit(" ".join(message))


def filter_construction(
    inputs,
    output_dir: str | Path = "construction",
    *,
    census_col: str | None = None,
    include_extraction: bool = False,
    include_managers: bool = False,
    keep_nonconstruction: bool = False,
    encoding: str = "utf-8",
    chunk_size: int = 50_000,
    quiet: bool = False,
) -> dict:
    """Split each input into construction / blank / non-construction.

    Returns a dict with the manifests, per-file counts and the code breakdown.
    """
    ranges = build_construction_ranges(include_extraction, include_managers)
    files = collect_inputs(inputs)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def say(*parts):
        if not quiet:
            print(*parts, flush=True)

    say(f"census 2018 construction codes: {describe_ranges(ranges)}")
    say(f"  extraction workers (6800-6950): {'included' if include_extraction else 'excluded'}")
    say(f"  construction managers (0220)  : {'included' if include_managers else 'excluded'}")
    say(f"input files: {len(files)}")

    summary_rows: list[dict] = []
    code_counts: dict[object, int] = {}
    construction_paths: list[Path] = []
    blank_paths: list[Path] = []
    totals = {"read": 0, "construction": 0, "blank": 0, "unparseable": 0, "other": 0}

    for path in files:
        header = read_csv(path, encoding, nrows=0)
        column = find_census_column(header.columns, census_col)

        targets = {
            "construction": out_dir / f"{path.stem}_construction.csv",
            "blank": out_dir / f"{path.stem}_blank.csv",
        }
        if keep_nonconstruction:
            targets["nonconstruction"] = out_dir / f"{path.stem}_nonconstruction.csv"

        handles = {k: open(v, "w", newline="", encoding=encoding) for k, v in targets.items()}
        wrote_header = {k: False for k in targets}
        counts = {k: 0 for k in targets}
        n_read = n_unparseable = 0

        try:
            reader = pd.read_csv(
                path, dtype=str, keep_default_na=True, encoding=encoding,
                chunksize=chunk_size,
            )
            for chunk in reader:
                codes = chunk[column].map(parse_code)
                for value, n in codes.value_counts().items():
                    code_counts[value] = code_counts.get(value, 0) + int(n)

                is_blank = codes == BLANK
                is_bad = codes == UNPARSEABLE
                is_constr = codes.map(
                    lambda c: isinstance(c, int) and in_ranges(c, ranges)
                )

                n_read += len(chunk)
                n_unparseable += int(is_bad.sum())

                buckets = {
                    "construction": is_constr,
                    "blank": is_blank,
                }
                if keep_nonconstruction:
                    # Unreadable codes are not construction and not blank, so
                    # they belong with the rest rather than being dropped.
                    buckets["nonconstruction"] = ~(is_constr | is_blank)

                for key, mask in buckets.items():
                    subset = chunk.loc[mask]
                    if subset.empty:
                        continue
                    counts[key] += len(subset)
                    subset.to_csv(handles[key], index=False, header=not wrote_header[key])
                    wrote_header[key] = True
        finally:
            for handle in handles.values():
                handle.close()

        # Empty result still gets a header so downstream steps do not break.
        for key, target in targets.items():
            if not wrote_header[key]:
                header.to_csv(target, index=False)

        construction_paths.append(targets["construction"])
        blank_paths.append(targets["blank"])
        totals["read"] += n_read
        totals["construction"] += counts["construction"]
        totals["blank"] += counts["blank"]
        totals["unparseable"] += n_unparseable
        totals["other"] += n_read - counts["construction"] - counts["blank"]

        summary_rows.append(
            {
                "input_file": path.name,
                "census_column": column,
                "rows_read": n_read,
                "construction": counts["construction"],
                "blank": counts["blank"],
                "unparseable_code": n_unparseable,
                "other": n_read - counts["construction"] - counts["blank"],
                "construction_file": targets["construction"].name,
                "blank_file": targets["blank"].name,
            }
        )

        pct = counts["construction"] / n_read * 100 if n_read else 0.0
        say(
            f"  {path.name}: {n_read:,} rows -> construction {counts['construction']:,}"
            f" ({pct:.2f}%), blank {counts['blank']:,}"
        )

    # --- manifests ----------------------------------------------------------
    manifests = {
        "input_list.txt": [str(p) for p in files],
        "construction_list.txt": [str(p) for p in construction_paths],
        "blank_list.txt": [str(p) for p in blank_paths],
    }
    for name, lines in manifests.items():
        (out_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_dir / "construction_summary.csv", index=False)

    breakdown = pd.DataFrame(
        [
            {
                "census_2018": str(value),
                "title": (
                    census_title(value)
                    if isinstance(value, int)
                    else ""
                ),
                "n": n,
                "kept_as_construction": isinstance(value, int) and in_ranges(value, ranges),
            }
            for value, n in sorted(code_counts.items(), key=lambda kv: str(kv[0]))
        ]
    )
    breakdown.to_csv(out_dir / "census_2018_breakdown.csv", index=False)

    say(f"\n{'=' * 70}")
    if not summary.empty:
        say(
            summary[
                ["input_file", "rows_read", "construction", "blank", "other"]
            ].to_string(index=False)
        )
    say(f"\ntotal rows read     : {totals['read']:,}")
    say(f"construction        : {totals['construction']:,}")
    say(f"blank census_2018   : {totals['blank']:,}")
    say(f"other (non-constr.) : {totals['other']:,}")
    if totals["unparseable"]:
        say(
            f"  of 'other', {totals['unparseable']:,} had a non-numeric code "
            "(reported, never treated as construction)"
        )

    kept_codes = breakdown.loc[breakdown["kept_as_construction"]]
    if not kept_codes.empty:
        say("\nconstruction codes actually present:")
        say(kept_codes[["census_2018", "title", "n"]].to_string(index=False))
        untitled = kept_codes.loc[kept_codes["title"] == "", "census_2018"].tolist()
        if untitled:
            say(
                f"\n  NOTE: {len(untitled)} kept code(s) are in range but not in the "
                f"local title table: {', '.join(untitled)}. They were kept on the "
                "range alone -- confirm them against the official 2018 list."
            )
    else:
        say(
            "\nWARNING: no construction codes found at all. Check that "
            f"{summary['census_column'].iloc[0] if not summary.empty else 'the column'!r}"
            " really holds Census 2018 occupation codes."
        )

    say(f"\nwrote to {out_dir}/:")
    say(f"  {len(construction_paths)} construction file(s), {len(blank_paths)} blank file(s)")
    say("  input_list.txt, construction_list.txt, blank_list.txt")
    say("  construction_summary.csv, census_2018_breakdown.csv")

    return {
        "input_files": files,
        "construction_files": construction_paths,
        "blank_files": blank_paths,
        "summary": summary,
        "breakdown": breakdown,
        "totals": totals,
        "ranges": ranges,
    }


def inspect(files, args) -> None:
    ranges = build_construction_ranges(args.include_extraction, args.include_managers)
    print(f"construction codes: {describe_ranges(ranges)}")
    for path in files:
        header = read_csv(path, args.encoding, nrows=0)
        print(f"\n=== {path} ===")
        try:
            column = find_census_column(header.columns, args.census_col)
        except SystemExit as exc:
            print(f"  {exc}")
            continue
        print(f"  census column: {column}")
        counts: dict[object, int] = {}
        total = 0
        reader = pd.read_csv(
            path, dtype=str, usecols=[column], keep_default_na=True,
            encoding=args.encoding, chunksize=args.chunk_size,
        )
        for chunk in reader:
            total += len(chunk)
            for value, n in chunk[column].map(parse_code).value_counts().items():
                counts[value] = counts.get(value, 0) + int(n)

        n_constr = sum(
            c for v, c in counts.items() if isinstance(v, int) and in_ranges(v, ranges)
        )
        print(f"  {total:,} rows; construction {n_constr:,}; blank {counts.get(BLANK, 0):,}")
        present = sorted(
            (v for v in counts if isinstance(v, int) and in_ranges(v, ranges))
        )
        for code in present:
            title = census_title(code) or "(not in local title table)"
            print(f"    {code}  {counts[code]:>8,}  {title}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input", nargs="+", required=True,
        help="CSV file(s), a directory, or a .txt with one path per line",
    )
    parser.add_argument("--output-dir", default="construction")
    parser.add_argument("--census-col", help="the Census 2018 code column, if auto-detection fails")
    parser.add_argument(
        "--include-extraction", action="store_true",
        help="also count extraction workers (6800-6950) as construction",
    )
    parser.add_argument(
        "--include-managers", action="store_true",
        help="also count construction managers (0220) as construction",
    )
    parser.add_argument(
        "--keep-nonconstruction", action="store_true",
        help="also write the non-construction rows",
    )
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--chunk-size", type=int, default=50_000)
    parser.add_argument(
        "--inspect", action="store_true",
        help="report the codes present without writing anything",
    )
    args = parser.parse_args(argv)

    if args.inspect:
        inspect(collect_inputs(args.input), args)
        return 0

    result = filter_construction(
        args.input,
        output_dir=args.output_dir,
        census_col=args.census_col,
        include_extraction=args.include_extraction,
        include_managers=args.include_managers,
        keep_nonconstruction=args.keep_nonconstruction,
        encoding=args.encoding,
        chunk_size=args.chunk_size,
    )
    return 0 if result["totals"]["construction"] else 1


if __name__ == "__main__":
    sys.exit(main())
