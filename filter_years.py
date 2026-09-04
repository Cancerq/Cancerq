#!/usr/bin/env python3
"""Filter NVDRS files down to a range of incident years (default 2020-2023).

Meant to run FIRST, before the age-band split and the occupation grouping, so
everything downstream only ever sees the years you want.

From Python, pass your existing list of paths straight in:

    from filter_years import filter_year_range

    kept = filter_year_range(input_list, years=(2020, 2023), output_dir="filtered/")

From the shell:

    # look at what year column and year values are in the files first
    python filter_years.py --input input_list.txt --inspect

    # then filter
    python filter_years.py --input raw/ --years 2020-2023 --output-dir filtered/

`--input` accepts CSV paths, a directory of CSVs, or a .txt file holding one
path per line (an `input_list` written out to disk).

Only *incident* year columns are auto-detected. NVDRS also carries death year
and injury year, which can differ from the incident year, so this script will
NOT silently fall back to one of those -- if it cannot find an incident year
column it stops and asks you to name the column with --year-col.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nvdrs_split import _norm_colname, _norm_value, read_csv  # noqa: E402

# Incident-year column names, matched case/space/underscore-insensitively.
# Ordered most-specific first.
YEAR_COL_CANDIDATES = (
    "incidentyear",
    "incidentyearc",
    "incyear",
    "yearofincident",
    "incidentdate",
    "year",
)

# Columns that look like a year but are NOT the incident year. If one of these
# is all we can find, we stop rather than quietly filtering on the wrong thing.
WRONG_YEAR_COLS = {
    "deathyear": "death year",
    "yearofdeath": "death year",
    "dodyear": "death year",
    "injuryyear": "injury year",
    "yearofinjury": "injury year",
    "injurydate": "injury date",
    "deathdate": "death date",
    "dod": "date of death",
    "filingyear": "filing year",
    "reportyear": "report year",
    "abstractionyear": "abstraction year",
}

UNPARSEABLE = "UNPARSEABLE"
MISSING = "MISSING"


def parse_year(value) -> int | str:
    """Pull a 4-digit year out of a cell.

    Handles a bare year ("2020", "2020.0", 2020) and a date that contains a
    4-digit year ("2020-05-13", "5/13/2020"). A 2-digit year is deliberately
    NOT guessed at -- it comes back as UNPARSEABLE and is reported, never
    silently dropped or assumed to be 20xx.
    """
    text = _norm_value(value)
    if not text:
        return MISSING
    bare = re.fullmatch(r"(\d{4})(?:\.0+)?", text)
    if bare:
        return int(bare.group(1))
    # A date string: take the only plausible 4-digit year in it.
    years = re.findall(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)", text)
    if len(set(years)) == 1:
        return int(years[0])
    return UNPARSEABLE


def find_year_column(columns, override: str | None = None) -> str:
    """Locate the incident-year column, refusing look-alike columns."""
    if override:
        if override not in columns:
            raise SystemExit(
                f"--year-col {override!r} is not in the file. Columns: "
                + ", ".join(map(str, columns))
            )
        return override

    normalised = {_norm_colname(c): c for c in columns}
    for cand in YEAR_COL_CANDIDATES:
        if cand in normalised:
            return normalised[cand]
    for cand in YEAR_COL_CANDIDATES:
        for norm, original in normalised.items():
            if cand in norm and norm not in WRONG_YEAR_COLS:
                return original

    look_alikes = {
        original: WRONG_YEAR_COLS[norm]
        for norm, original in normalised.items()
        if norm in WRONG_YEAR_COLS
    }
    message = [
        "could not find an incident-year column "
        f"(looked for: {', '.join(YEAR_COL_CANDIDATES)})."
    ]
    if look_alikes:
        message.append(
            "Found these year-like columns, but they are NOT the incident year: "
            + ", ".join(f"{c} ({what})" for c, what in look_alikes.items())
            + "."
        )
    message.append("Name the right column explicitly with --year-col.")
    raise SystemExit(" ".join(message))


def parse_years_arg(tokens) -> set[int]:
    """Accept '2020-2023', '2020:2023', or '2020 2021 2022 2023'."""
    years: set[int] = set()
    for token in tokens:
        token = str(token).strip()
        span = re.fullmatch(r"(\d{4})\s*[-:to]{1,2}\s*(\d{4})", token)
        if span:
            low, high = int(span.group(1)), int(span.group(2))
            if low > high:
                raise SystemExit(f"year range {token!r} runs backwards")
            years.update(range(low, high + 1))
        elif re.fullmatch(r"\d{4}", token):
            years.add(int(token))
        else:
            raise SystemExit(f"cannot read {token!r} as a year or year range")
    if not years:
        raise SystemExit("no years given")
    return years


def collect_inputs(inputs) -> list[Path]:
    """Expand CSV paths, directories, and .txt path-lists into a file list."""
    files: list[Path] = []
    for item in inputs:
        path = Path(str(item))
        if path.is_dir():
            files.extend(sorted(p for p in path.glob("*.csv") if p.is_file()))
        elif path.suffix.lower() == ".txt" and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    entry = Path(line)
                    if not entry.is_file():
                        raise SystemExit(f"path listed in {path} not found: {entry}")
                    files.append(entry)
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f"input not found: {path}")
    # De-duplicate while keeping order; a repeated path would double the rows.
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(f)
    if not unique:
        raise SystemExit("no CSV files found in the given input(s)")
    return unique


def filter_year_range(
    inputs,
    years=(2020, 2023),
    output_dir: str | Path = "filtered",
    *,
    year_col: str | None = None,
    encoding: str = "utf-8",
    batch_size: int = 100_000,
    suffix: str = "_2020_2023",
    quiet: bool = False,
) -> dict[str, object]:
    """Filter each input file to `years` and write it to `output_dir`.

    `inputs` is anything collect_inputs accepts, including a plain list of
    paths (your input_list). `years` is a (low, high) tuple, an iterable of
    years, or a string like "2020-2023".

    Returns a dict with the written paths and the per-year counts.
    """
    if isinstance(years, str):
        wanted = parse_years_arg([years])
    elif (
        isinstance(years, tuple)
        and len(years) == 2
        and all(isinstance(y, int) for y in years)
    ):
        wanted = set(range(years[0], years[1] + 1))
    else:
        wanted = {int(y) for y in years}

    files = collect_inputs(inputs)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def say(*parts):
        if not quiet:
            print(*parts)

    year_label = f"{min(wanted)}-{max(wanted)}"
    say(f"keeping incident years: {sorted(wanted)}")
    say(f"input files: {len(files)}")

    written: list[Path] = []
    distribution: list[dict] = []
    grand_total = grand_kept = 0

    for path in files:
        header = read_csv(path, encoding, nrows=0)
        column = find_year_column(header.columns, year_col)

        counts: dict[object, int] = {}
        out_path = out_dir / f"{path.stem}{suffix}.csv"
        total = kept = 0
        wrote_header = False

        reader = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=True,
            encoding=encoding,
            chunksize=batch_size,
        )
        for chunk in reader:
            parsed = chunk[column].map(parse_year)
            for value, n in parsed.value_counts().items():
                counts[value] = counts.get(value, 0) + int(n)

            keep_mask = parsed.map(lambda y: isinstance(y, int) and y in wanted)
            subset = chunk.loc[keep_mask]
            total += len(chunk)
            kept += len(subset)

            # Append chunk by chunk so memory stays flat on large files.
            subset.to_csv(
                out_path,
                index=False,
                mode="w" if not wrote_header else "a",
                header=not wrote_header,
            )
            wrote_header = True

        if not wrote_header:  # empty input file: still write a header-only file
            header.to_csv(out_path, index=False)

        written.append(out_path)
        grand_total += total
        grand_kept += kept

        for value, n in counts.items():
            distribution.append(
                {
                    "source_file": path.name,
                    "year_column": column,
                    "year_value": value,
                    "n": n,
                    "kept": isinstance(value, int) and value in wanted,
                }
            )

        say(f"\n{path.name}  (year column: {column})")
        table = (
            pd.DataFrame(
                [
                    {"year": str(v), "n": n, "kept": isinstance(v, int) and v in wanted}
                    for v, n in counts.items()
                ]
            )
            .sort_values("year")
            .to_string(index=False)
        )
        say(table)
        dropped = total - kept
        say(f"  {total} rows -> kept {kept}, dropped {dropped}  -> {out_path.name}")
        if kept == 0 and total > 0:
            say(
                f"  WARNING: nothing matched {year_label} in this file. "
                f"Check that {column!r} is really the incident year."
            )

    dist = pd.DataFrame(
        distribution,
        columns=["source_file", "year_column", "year_value", "n", "kept"],
    )
    dist_path = out_dir / "year_distribution.csv"
    dist.to_csv(dist_path, index=False)

    unparseable = int(dist.loc[dist["year_value"] == UNPARSEABLE, "n"].sum())
    missing = int(dist.loc[dist["year_value"] == MISSING, "n"].sum())

    say(f"\n{'=' * 70}")
    say(f"total rows read : {grand_total}")
    say(f"kept ({year_label}): {grand_kept}")
    say(f"dropped         : {grand_total - grand_kept}")
    if missing:
        say(f"  of which blank year   : {missing}")
    if unparseable:
        say(
            f"  of which unreadable year: {unparseable}  <-- inspect these, they were "
            "dropped without being assigned a year"
        )
    say(f"wrote {len(written)} file(s) to {out_dir}/ plus {dist_path.name}")

    return {
        "files": written,
        "year_distribution": dist,
        "rows_read": grand_total,
        "rows_kept": grand_kept,
        "rows_missing_year": missing,
        "rows_unparseable_year": unparseable,
    }


def inspect(files: list[Path], args) -> None:
    for path in files:
        header = read_csv(path, args.encoding, nrows=0)
        print(f"\n=== {path} ===")
        try:
            column = find_year_column(header.columns, args.year_col)
        except SystemExit as exc:
            print(f"  {exc}")
            continue
        print(f"  detected incident-year column: {column}")
        counts: dict[object, int] = {}
        reader = pd.read_csv(
            path,
            dtype=str,
            usecols=[column],
            keep_default_na=True,
            encoding=args.encoding,
            chunksize=args.batch_size,
        )
        total = 0
        for chunk in reader:
            total += len(chunk)
            for value, n in chunk[column].map(parse_year).value_counts().items():
                counts[value] = counts.get(value, 0) + int(n)
        print(f"  {total} rows; year values:")
        table = pd.DataFrame(
            [{"year": str(v), "n": n} for v, n in counts.items()]
        ).sort_values("year")
        print("    " + table.to_string(index=False).replace("\n", "\n    "))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input",
        nargs="+",
        required=True,
        help="CSV file(s), a directory of CSVs, or a .txt with one path per line",
    )
    parser.add_argument(
        "--years",
        nargs="+",
        default=["2020-2023"],
        help="years to keep: '2020-2023' or '2020 2021 2022 2023' (default 2020-2023)",
    )
    parser.add_argument("--output-dir", default="filtered")
    parser.add_argument(
        "--year-col",
        help="incident-year column, if auto-detection picks wrong or gives up",
    )
    parser.add_argument("--suffix", default=None, help="output filename suffix")
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument(
        "--batch-size", type=int, default=100_000, help="rows read per chunk"
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="report the year column and its value counts, write nothing",
    )
    args = parser.parse_args(argv)

    if args.inspect:
        inspect(collect_inputs(args.input), args)
        return 0

    wanted = parse_years_arg(args.years)
    suffix = args.suffix
    if suffix is None:
        suffix = f"_{min(wanted)}_{max(wanted)}"
    result = filter_year_range(
        args.input,
        years=wanted,
        output_dir=args.output_dir,
        year_col=args.year_col,
        encoding=args.encoding,
        batch_size=args.batch_size,
        suffix=suffix,
    )
    return 0 if result["rows_kept"] else 1


if __name__ == "__main__":
    sys.exit(main())
