#!/usr/bin/env python3
"""Split a large NVDRS file into age-band CSVs, streaming so memory stays flat.

Default bands cover 18-67 in ten-year steps:

    18-27, 28-37, 38-47, 48-57, 58-67

The input is read in chunks and each chunk is appended straight to the band
files, so the whole table is never held in memory. Measured on a 1.8 GB,
63-column, 5.15 M-row file: 164 s at ~330 MB peak RSS with the default chunk
size. The output is identical whatever chunk size you pick.

Rows outside 18-67, and rows whose age is blank or unreadable, are excluded --
but every one of them is counted and reported, so the numbers always add up.

    # 1) see the age column and its distribution without writing anything
    python split_by_age.py --input nvdrs_big.csv --inspect

    # 2) write the band files
    python split_by_age.py --input nvdrs_big.csv --output-dir age_chunks/

    # keep the excluded rows for checking, and gzip the output
    python split_by_age.py --input nvdrs_big.csv --output-dir age_chunks/ \
        --keep-out-of-range --gzip

From Python:

    from split_by_age import split_by_age
    result = split_by_age("nvdrs_big.csv", output_dir="age_chunks/")
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from filter_years import collect_inputs  # noqa: E402
from nvdrs_split import _norm_colname, _norm_value, read_csv  # noqa: E402

DEFAULT_BANDS = "18-27,28-37,38-47,48-57,58-67"

# Numeric age columns, most specific first.
AGE_COL_CANDIDATES = (
    "age",
    "agec",
    "ageyears",
    "ageinyears",
    "victimage",
    "ageatdeath",
    "ageatinjury",
    "decedentage",
)

# Columns that look like age but are a category, not a number. Filtering on one
# of these would silently produce nonsense, so we refuse and say why.
CATEGORICAL_AGE_COLS = {
    "agegroup": "a pre-binned age group",
    "agerange": "a pre-binned age range",
    "agecategory": "a pre-binned age category",
    "agegrp": "a pre-binned age group",
    "ageband": "a pre-binned age band",
}

MISSING = "MISSING"
UNPARSEABLE = "UNPARSEABLE"
OUT_OF_RANGE = "OUT_OF_RANGE"

# NVDRS and similar exports use high sentinels (999, 9999) for "unknown age".
# Treating one as a real age would put it in no band anyway, but we count them
# separately so they are never mistaken for genuinely old decedents.
SENTINEL_MIN = 900


def parse_age(value) -> int | str:
    """Return an integer age, or MISSING / UNPARSEABLE.

    Accepts "45", "45.0", 45. Infant wordings ("less than 1 year", "<1") come
    back as 0. A non-numeric value is UNPARSEABLE, never guessed at.
    """
    text = _norm_value(value)
    if not text:
        return MISSING
    whole = re.fullmatch(r"(\d{1,3})(?:\.0+)?", text)
    if whole:
        return int(whole.group(1))
    if re.search(r"(less than|under|<)\s*1\b", text) or text in {"<1", "under 1"}:
        return 0
    return UNPARSEABLE


def parse_bands(spec: str | list) -> list[tuple[int, int]]:
    """Parse '18-27,28-37' (or a list of such tokens) into sorted (low, high).

    Bands are inclusive at both ends. Overlaps are rejected: a row must belong
    to exactly one band, otherwise the outputs would double-count.
    """
    tokens: list[str] = []
    if isinstance(spec, str):
        tokens = [t for t in re.split(r"[,\s]+", spec) if t]
    else:
        for item in spec:
            tokens.extend(t for t in re.split(r"[,\s]+", str(item)) if t)

    bands: list[tuple[int, int]] = []
    for token in tokens:
        match = re.fullmatch(r"(\d{1,3})\s*[-:to]{1,2}\s*(\d{1,3})", token)
        if not match:
            raise SystemExit(f"cannot read {token!r} as an age band like '18-27'")
        low, high = int(match.group(1)), int(match.group(2))
        if low > high:
            raise SystemExit(f"age band {token!r} runs backwards")
        bands.append((low, high))

    bands.sort()
    for (a_low, a_high), (b_low, b_high) in zip(bands, bands[1:]):
        if b_low <= a_high:
            raise SystemExit(
                f"age bands {a_low}-{a_high} and {b_low}-{b_high} overlap; "
                "a row would land in two files"
            )
    if not bands:
        raise SystemExit("no age bands given")
    return bands


def band_label(low: int, high: int) -> str:
    return f"{low}-{high}"


def find_age_column(columns, override: str | None = None) -> str:
    if override:
        if override not in columns:
            raise SystemExit(
                f"--age-col {override!r} is not in the file. Columns: "
                + ", ".join(map(str, columns))
            )
        return override

    normalised = {_norm_colname(c): c for c in columns}
    for cand in AGE_COL_CANDIDATES:
        if cand in normalised:
            return normalised[cand]
    for cand in AGE_COL_CANDIDATES:
        for norm, original in normalised.items():
            if cand in norm and norm not in CATEGORICAL_AGE_COLS:
                return original

    look_alikes = {
        original: CATEGORICAL_AGE_COLS[norm]
        for norm, original in normalised.items()
        if norm in CATEGORICAL_AGE_COLS
    }
    message = [
        f"could not find a numeric age column (looked for: {', '.join(AGE_COL_CANDIDATES)})."
    ]
    if look_alikes:
        message.append(
            "Found these age-like columns, but they hold categories rather than "
            "a number: "
            + ", ".join(f"{c} ({what})" for c, what in look_alikes.items())
            + "."
        )
    message.append("Name the numeric age column with --age-col.")
    raise SystemExit(" ".join(message))


def _open_out(path: Path, use_gzip: bool, encoding: str):
    if use_gzip:
        return gzip.open(path, "wt", newline="", encoding=encoding)
    return open(path, "w", newline="", encoding=encoding)


def split_by_age(
    inputs,
    output_dir: str | Path = "age_chunks",
    *,
    bands=DEFAULT_BANDS,
    age_col: str | None = None,
    encoding: str = "utf-8",
    chunk_size: int = 50_000,
    use_gzip: bool = False,
    keep_out_of_range: bool = False,
    prefix: str = "nvdrs",
    progress_every: int = 1_000_000,
    quiet: bool = False,
) -> dict:
    """Stream `inputs` into one CSV per age band.

    All inputs are written into a single set of band files. Their headers must
    match, otherwise the concatenation would misalign columns and we stop.

    Returns a dict of per-band counts and the excluded-row tallies.
    """
    band_list = parse_bands(bands)
    files = collect_inputs(inputs)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def say(*parts):
        if not quiet:
            print(*parts, flush=True)

    reference_header = list(read_csv(files[0], encoding, nrows=0).columns)
    column = find_age_column(reference_header, age_col)

    for path in files[1:]:
        header = list(read_csv(path, encoding, nrows=0).columns)
        if header != reference_header:
            only_a = [c for c in reference_header if c not in header]
            only_b = [c for c in header if c not in reference_header]
            raise SystemExit(
                f"{path.name} has a different header from {files[0].name}; "
                "combining them would misalign the columns. "
                f"Only in {files[0].name}: {only_a or 'none'}. "
                f"Only in {path.name}: {only_b or 'none'}."
            )

    say(f"age column : {column}")
    say(f"bands      : {', '.join(band_label(*b) for b in band_list)}")
    say(f"input files: {len(files)}")
    suffix = ".csv.gz" if use_gzip else ".csv"

    handles: dict[str, object] = {}
    wrote_header: dict[str, bool] = {}
    band_counts: dict[str, int] = {band_label(*b): 0 for b in band_list}
    out_paths: dict[str, Path] = {}

    for low, high in band_list:
        label = band_label(low, high)
        path = out_dir / f"{prefix}_age_{low}_{high}{suffix}"
        out_paths[label] = path
        handles[label] = _open_out(path, use_gzip, encoding)
        wrote_header[label] = False
    if keep_out_of_range:
        path = out_dir / f"{prefix}_age_excluded{suffix}"
        out_paths[OUT_OF_RANGE] = path
        handles[OUT_OF_RANGE] = _open_out(path, use_gzip, encoding)
        wrote_header[OUT_OF_RANGE] = False

    lows = [b[0] for b in band_list]
    highs = [b[1] for b in band_list]
    labels = [band_label(*b) for b in band_list]

    def assign(age) -> str:
        if isinstance(age, str):  # MISSING / UNPARSEABLE
            return age
        for low, high, label in zip(lows, highs, labels):
            if low <= age <= high:
                return label
        return OUT_OF_RANGE

    age_counts: dict[object, int] = {}
    total = 0
    n_missing = n_unparseable = n_out = n_sentinel = 0
    started = time.time()
    next_progress = progress_every

    try:
        for path in files:
            reader = pd.read_csv(
                path,
                dtype=str,
                keep_default_na=True,
                encoding=encoding,
                chunksize=chunk_size,
            )
            for chunk in reader:
                ages = chunk[column].map(parse_age)
                assigned = ages.map(assign)

                for value, n in ages.value_counts().items():
                    age_counts[value] = age_counts.get(value, 0) + int(n)
                    if isinstance(value, int) and value >= SENTINEL_MIN:
                        n_sentinel += int(n)

                total += len(chunk)
                n_missing += int((assigned == MISSING).sum())
                n_unparseable += int((assigned == UNPARSEABLE).sum())
                n_out += int((assigned == OUT_OF_RANGE).sum())

                for label in labels:
                    subset = chunk.loc[assigned == label]
                    if subset.empty:
                        continue
                    band_counts[label] += len(subset)
                    subset.to_csv(
                        handles[label], index=False, header=not wrote_header[label]
                    )
                    wrote_header[label] = True

                if keep_out_of_range:
                    excluded = chunk.loc[assigned.isin([MISSING, UNPARSEABLE, OUT_OF_RANGE])]
                    if not excluded.empty:
                        excluded = excluded.copy()
                        excluded["exclusion_reason"] = assigned[excluded.index].values
                        excluded.to_csv(
                            handles[OUT_OF_RANGE],
                            index=False,
                            header=not wrote_header[OUT_OF_RANGE],
                        )
                        wrote_header[OUT_OF_RANGE] = True

                if total >= next_progress:
                    rate = total / max(time.time() - started, 1e-9)
                    say(f"  ... {total:,} rows read ({rate:,.0f} rows/s)")
                    next_progress += progress_every
    finally:
        for handle in handles.values():
            handle.close()

    # A band that matched nothing still gets a header, so downstream steps do
    # not choke on a missing file.
    for label, path in out_paths.items():
        if not wrote_header.get(label):
            columns = reference_header + (
                ["exclusion_reason"] if label == OUT_OF_RANGE else []
            )
            with _open_out(path, use_gzip, encoding) as handle:
                pd.DataFrame(columns=columns).to_csv(handle, index=False)

    dist = pd.DataFrame(
        [
            {
                "age_value": str(value),
                "n": n,
                "band": assign(value) if not isinstance(value, str) else value,
            }
            for value, n in sorted(age_counts.items(), key=lambda kv: str(kv[0]))
        ]
    )
    dist_path = out_dir / "age_distribution.csv"
    dist.to_csv(dist_path, index=False)

    elapsed = time.time() - started
    kept = sum(band_counts.values())

    say(f"\n{'=' * 64}")
    summary = pd.DataFrame(
        [
            {"band": label, "n": band_counts[label], "file": out_paths[label].name}
            for label in labels
        ]
    )
    say(summary.to_string(index=False))
    say(f"\ntotal rows read : {total:,}")
    say(f"written to bands: {kept:,}")
    say(f"excluded        : {total - kept:,}")
    say(f"  age outside {band_list[0][0]}-{band_list[-1][1]}: {n_out:,}")
    say(f"  age blank                 : {n_missing:,}")
    say(f"  age unreadable            : {n_unparseable:,}")
    if n_sentinel:
        say(
            f"  NOTE: {n_sentinel:,} row(s) have an age of {SENTINEL_MIN}+, which is "
            "almost certainly an 'unknown' sentinel rather than a real age."
        )
    if kept + (total - kept) != total:  # defensive; should be impossible
        say("  WARNING: counts do not reconcile")
    say(f"\nelapsed: {elapsed:,.1f}s  ({total / max(elapsed, 1e-9):,.0f} rows/s)")
    say(f"wrote {len(labels)} band file(s) to {out_dir}/ plus {dist_path.name}")

    return {
        "age_column": column,
        "bands": labels,
        "band_counts": band_counts,
        "files": {label: out_paths[label] for label in labels},
        "rows_read": total,
        "rows_written": kept,
        "rows_out_of_range": n_out,
        "rows_missing_age": n_missing,
        "rows_unparseable_age": n_unparseable,
        "age_distribution": dist,
        "elapsed_seconds": elapsed,
    }


def inspect(files, args) -> None:
    for path in files:
        header = read_csv(path, args.encoding, nrows=0)
        print(f"\n=== {path} ===")
        try:
            column = find_age_column(header.columns, args.age_col)
        except SystemExit as exc:
            print(f"  {exc}")
            continue
        print(f"  numeric age column: {column}")
        counts: dict[object, int] = {}
        total = 0
        reader = pd.read_csv(
            path,
            dtype=str,
            usecols=[column],
            keep_default_na=True,
            encoding=args.encoding,
            chunksize=args.chunk_size,
        )
        for chunk in reader:
            total += len(chunk)
            for value, n in chunk[column].map(parse_age).value_counts().items():
                counts[value] = counts.get(value, 0) + int(n)

        band_list = parse_bands(args.bands)
        print(f"  {total:,} rows")
        for low, high in band_list:
            n = sum(
                c for v, c in counts.items() if isinstance(v, int) and low <= v <= high
            )
            print(f"    {low}-{high}: {n:,}")
        numeric = [v for v in counts if isinstance(v, int)]
        if numeric:
            print(f"  age range present: {min(numeric)} to {max(numeric)}")
        for key in (MISSING, UNPARSEABLE):
            if counts.get(key):
                print(f"  {key}: {counts[key]:,}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input", nargs="+", required=True,
        help="CSV file(s), a directory of CSVs, or a .txt with one path per line",
    )
    parser.add_argument("--output-dir", default="age_chunks")
    parser.add_argument(
        "--bands", nargs="+", default=[DEFAULT_BANDS],
        help=f"inclusive age bands (default: {DEFAULT_BANDS})",
    )
    parser.add_argument("--age-col", help="numeric age column, if auto-detection fails")
    parser.add_argument("--prefix", default="nvdrs", help="output filename prefix")
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument(
        "--chunk-size", type=int, default=50_000,
        help="rows held in memory at once (default 50000; measured ~330 MB peak "
             "on a 1.8 GB / 63-column file -- raising it buys almost no speed)",
    )
    parser.add_argument("--gzip", action="store_true", help="write .csv.gz instead of .csv")
    parser.add_argument(
        "--keep-out-of-range", action="store_true",
        help="also write the excluded rows, with an exclusion_reason column",
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="report the age column and band sizes, write nothing",
    )
    args = parser.parse_args(argv)

    if args.inspect:
        inspect(collect_inputs(args.input), args)
        return 0

    result = split_by_age(
        args.input,
        output_dir=args.output_dir,
        bands=args.bands,
        age_col=args.age_col,
        encoding=args.encoding,
        chunk_size=args.chunk_size,
        use_gzip=args.gzip,
        keep_out_of_range=args.keep_out_of_range,
        prefix=args.prefix,
    )
    return 0 if result["rows_written"] else 1


if __name__ == "__main__":
    sys.exit(main())
