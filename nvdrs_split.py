#!/usr/bin/env python3
"""Split NVDRS age-group CSVs by circumstance-known (boolean) and occupation group.

For every input CSV (one per age band: 18-30, 31-40, 41-50, 51-60, 61-70) this
script adds two derived columns

    circumstance_known_bool   True / False / <NA>
    occupation_group          military | construction | non_workforce |
                              non_construction | unclassified

and then writes the 2 x 4 cross-tabulated subsets, plus summary counts and a
review file listing every occupation value that only matched the fallback rule.

Typical use:

    # 1. look at what the script detects before writing anything
    python nvdrs_split.py --input data/ --inspect

    # 2. write the splits
    python nvdrs_split.py --input data/ --output-dir out/

See README.md for the classification rules and how to adjust them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

OCCUPATION_GROUPS = (
    "military",
    "construction",
    "non_workforce",
    "non_construction",
    "unclassified",
)

# Candidate source column names, matched case/space/underscore-insensitively.
CIRCUMSTANCE_COL_CANDIDATES = (
    "circumstanceknown",
    "circumstancesknown",
    "circumstanceknownflag",
    "circumstancesknownflag",
    "knowncircumstances",
)
OCCUPATION_TEXT_COL_CANDIDATES = (
    "occupation",
    "occupationdescription",
    "occupationtext",
    "usualoccupation",
    "occupationtitle",
    "occupationgrouptext",
)
OCCUPATION_CODE_COL_CANDIDATES = (
    "occupationcode",
    "censusoccupationcode",
    "occupationcensuscode",
    "occcode",
    "occupationcode2010",
)
INDUSTRY_CODE_COL_CANDIDATES = (
    "industrycode",
    "censusindustrycode",
    "industrycensuscode",
    "indcode",
    "industrycode2010",
)
INDUSTRY_TEXT_COL_CANDIDATES = (
    "industry",
    "industrydescription",
    "industrytext",
    "usualindustry",
)

# Default rules. Dump them with --dump-rules, edit, and feed back via --rules.
DEFAULT_RULES = {
    "circumstance_true": ["yes", "y", "true", "t", "1", "是"],
    "circumstance_false": ["no", "n", "false", "f", "0", "否"],
    # Census 2010 occupation code ranges (inclusive) used by NVDRS.
    "code_ranges": {
        "military": [[9800, 9830]],
        # 6200-6765 construction trades & their supervisors;
        # 6800-6940 extraction workers, only used with --include-extraction.
        "construction": [[6200, 6765]],
        "construction_extraction": [[6800, 6940]],
        # 9840 = never worked / no work experience in the last 5 years,
        # 9920 = unemployed, with no work experience.
        "non_workforce": [[9840, 9840], [9920, 9920]],
    },
    # Census 2010 industry code for Construction. Only consulted when an
    # industry column is supplied and the occupation itself is unclassified.
    "construction_industry_codes": [770],
    # Keyword rules for free-text occupation values. Checked as substrings of
    # the lower-cased, whitespace-normalised value.
    "keywords": {
        # Cell values that mean "we do not know the occupation" -> unclassified.
        # Matched against the WHOLE cell, because short tokens like "na" or
        # "other" would otherwise hit real occupations ("nanny", "mother").
        "unknown_exact": [
            "unknown",
            "unk",
            "u",
            "not available",
            "not applicable",
            "unspecified",
            "missing",
            "refused",
            "blank",
            "n/a",
            "na",
            "none",
            "other",
            "unemployed unknown",
        ],
        # Matched as substrings; every entry is long enough to be unambiguous.
        "unknown_contains": [
            "not specified",
            "not stated",
            "not reported",
            "no information",
            "unknown occupation",
            "occupation unknown",
        ],
        "military": [
            "military",
            "armed forces",
            "armed service",
            "army",
            "navy",
            "naval",
            "marine corps",
            "air force",
            "space force",
            "coast guard",
            "national guard",
            "soldier",
            "sailor",
            "airman",
            "usmc",
            "us army",
            "u.s. army",
            "service member",
            "servicemember",
            "active duty",
            "enlisted",
        ],
        "construction": [
            "construction",
            "carpenter",
            "carpentry",
            "electrician",
            "plumber",
            "plumbing",
            "pipefitter",
            "pipe fitter",
            "steamfitter",
            "roofer",
            "roofing",
            "mason",
            "masonry",
            "brickmason",
            "brick layer",
            "bricklayer",
            "cement",
            "concrete",
            "drywall",
            "sheetrock",
            "plasterer",
            "stucco",
            "painter",
            "painting contractor",
            "glazier",
            "insulation worker",
            "ironworker",
            "iron worker",
            "steel worker",
            "structural steel",
            "sheet metal",
            "welder",
            "boilermaker",
            "framer",
            "framing",
            "drilling",
            "excavat",
            "scaffold",
            "paving",
            "asphalt",
            "highway maintenance",
            "heavy equipment operator",
            "crane operator",
            "backhoe",
            "bulldozer",
            "general contractor",
            "building contractor",
            "construction laborer",
            "hvac",
            "heating and air",
            "air conditioning install",
            "surveying technician",
            "septic",
            "well driller",
        ],
        "non_workforce": [
            "unemployed",
            "not employed",
            "no occupation",
            "never worked",
            "never employed",
            "not in labor force",
            "not in the labor force",
            "out of work",
            "jobless",
            "retired",
            "retiree",
            "pensioner",
            "student",
            "pupil",
            "school child",
            "schoolchild",
            "homemaker",
            "housewife",
            "house wife",
            "househusband",
            "house husband",
            "stay at home",
            "stay-at-home",
            "domestic duties",
            "disabled",
            "disability",
            "unable to work",
            "on ssi",
            "ssi recipient",
            "ssdi",
            "social security disability",
            "on welfare",
            "institutionalized",
            "inmate",
            "prisoner",
            "incarcerated",
            "infant",
            "toddler",
            "child not in school",
            "volunteer",
            "unpaid",
            "non-paid worker",
            "nonpaid worker",
            "homeless",
            "veteran",
            "retired military",
        ],
    },
    # Age bands recognised in file names when scanning a directory.
    "age_band_pattern": r"(\d{2})\s*[-_to]{1,3}\s*(\d{2})",
}

MISSING_TOKENS = {"", ".", "-", "--", "nan", "none", "null", "<na>"}


# ---------------------------------------------------------------- utilities


def _norm_colname(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _norm_value(value) -> str:
    """Lower-case, collapse whitespace; empty string for missing values."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if pd.isna(value):
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip().lower()
    return "" if text in MISSING_TOKENS else text


def find_column(df: pd.DataFrame, candidates, exclude=()) -> str | None:
    """Exact normalised match first, then a substring match.

    `exclude` holds columns already claimed for another role, so that e.g.
    "OccupationCode" is not also picked up as the free-text occupation column
    by the substring pass.
    """
    excluded = set(exclude)
    normalised = {
        _norm_colname(c): c for c in df.columns if c not in excluded
    }
    for cand in candidates:
        if cand in normalised:
            return normalised[cand]
    for cand in candidates:
        for norm, original in normalised.items():
            if cand in norm:
                return original
    return None


def load_rules(path: Path | None) -> dict:
    rules = json.loads(json.dumps(DEFAULT_RULES))  # deep copy
    if path is None:
        return rules
    overrides = json.loads(path.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(rules.get(key), dict):
            rules[key].update(value)
        else:
            rules[key] = value
    return rules


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(text)).strip("_") or "unknown"


# ------------------------------------------------------- derived columns


def to_circumstance_bool(series: pd.Series, rules: dict) -> pd.Series:
    """Map the circumstance-known column to a nullable boolean.

    Anything that is neither an explicit yes nor an explicit no (blank,
    "Unknown", ...) becomes <NA> rather than silently counting as False.
    """
    true_set = {_norm_value(v) for v in rules["circumstance_true"]}
    false_set = {_norm_value(v) for v in rules["circumstance_false"]}

    def convert(value):
        text = _norm_value(value)
        if text in true_set:
            return True
        if text in false_set:
            return False
        return pd.NA

    return series.map(convert).astype("boolean")


def _code_to_int(value) -> int | None:
    text = _norm_value(value)
    if not text:
        return None
    match = re.fullmatch(r"(\d+)(?:\.0+)?", text)
    return int(match.group(1)) if match else None


def _in_ranges(code: int, ranges) -> bool:
    return any(low <= code <= high for low, high in ranges)


def classify_occupation(
    df: pd.DataFrame,
    rules: dict,
    *,
    text_col: str | None,
    code_col: str | None,
    industry_code_col: str | None = None,
    industry_text_col: str | None = None,
    include_extraction: bool = False,
) -> pd.DataFrame:
    """Return a frame with `occupation_group` and `occupation_group_rule`.

    Precedence: numeric census code -> free-text keywords -> industry
    (construction only) -> fallback. Group precedence within each stage is
    military > construction > non_workforce > non_construction, so a value
    matching several keyword lists lands in the most specific group.
    """
    code_ranges = rules["code_ranges"]
    construction_ranges = list(code_ranges["construction"])
    if include_extraction:
        construction_ranges += code_ranges.get("construction_extraction", [])
    keywords = rules["keywords"]
    unknown_exact = {_norm_value(v) for v in keywords["unknown_exact"]}
    industry_codes = set(rules.get("construction_industry_codes", []))

    def classify_row(row):
        # --- stage 1: census occupation code -----------------------------
        code = _code_to_int(row[code_col]) if code_col else None
        if code is not None:
            if _in_ranges(code, code_ranges["military"]):
                return "military", "occupation_code"
            if _in_ranges(code, construction_ranges):
                return "construction", "occupation_code"
            if _in_ranges(code, code_ranges["non_workforce"]):
                return "non_workforce", "occupation_code"
            if code < 9800:  # any other valid civilian occupation code
                return "non_construction", "occupation_code"

        # --- stage 2: free-text occupation -------------------------------
        text = _norm_value(row[text_col]) if text_col else ""
        if text:
            if text in unknown_exact or any(
                k in text for k in keywords["unknown_contains"]
            ):
                return "unclassified", "occupation_unknown_value"
            if any(k in text for k in keywords["military"]):
                return "military", "occupation_keyword"
            if any(k in text for k in keywords["construction"]):
                return "construction", "occupation_keyword"
            if any(k in text for k in keywords["non_workforce"]):
                return "non_workforce", "occupation_keyword"

        # --- stage 3: industry, construction only ------------------------
        if industry_code_col:
            ind_code = _code_to_int(row[industry_code_col])
            if ind_code is not None and ind_code in industry_codes:
                return "construction", "industry_code"
        if industry_text_col:
            ind_text = _norm_value(row[industry_text_col])
            if ind_text and "construction" in ind_text:
                return "construction", "industry_keyword"

        # --- stage 4: fallback -------------------------------------------
        if text:
            # A real occupation string that matched nothing: employed, and not
            # construction or military -> non_construction. Reported in
            # unmapped_occupations.csv so the keyword lists can be reviewed.
            return "non_construction", "fallback_text"
        return "unclassified", "missing_occupation"

    used = list(
        dict.fromkeys(
            c for c in (text_col, code_col, industry_code_col, industry_text_col) if c
        )
    )
    if not used:
        raise ValueError("no occupation or industry column available to classify on")

    results = df[used].apply(classify_row, axis=1, result_type="expand")
    results.columns = ["occupation_group", "occupation_group_rule"]
    return results


# ------------------------------------------------------------- pipeline


def resolve_columns(df: pd.DataFrame, args) -> dict:
    """Pick one source column per role; each column is claimed at most once.

    Roles are resolved code-before-text so that a "...Code" column is not also
    taken as the free-text column by the substring pass.
    """
    cols: dict[str, str | None] = {}
    claimed: list[str] = []

    for role, override, candidates in (
        ("circumstance", args.circumstance_col, CIRCUMSTANCE_COL_CANDIDATES),
        ("occupation_code", args.occupation_code_col, OCCUPATION_CODE_COL_CANDIDATES),
        ("occupation_text", args.occupation_col, OCCUPATION_TEXT_COL_CANDIDATES),
        ("industry_code", args.industry_code_col, INDUSTRY_CODE_COL_CANDIDATES),
        ("industry_text", args.industry_col, INDUSTRY_TEXT_COL_CANDIDATES),
    ):
        found = override or find_column(df, candidates, exclude=claimed)
        cols[role] = found
        if found is not None:
            claimed.append(found)

    for role, value in cols.items():
        if value is not None and value not in df.columns:
            raise SystemExit(f"column {value!r} (for {role}) not found in the input file")
    return cols


def age_band_from_name(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.stem)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return path.stem


def collect_inputs(inputs, encoding: str) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(p for p in path.glob("*.csv") if p.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f"input not found: {path}")
    if not files:
        raise SystemExit("no CSV files found in the given input(s)")
    return files


def read_csv(path: Path, encoding: str, **kwargs) -> pd.DataFrame:
    try:
        return pd.read_csv(
            path, dtype=str, keep_default_na=True, encoding=encoding, **kwargs
        )
    except UnicodeDecodeError:
        # NVDRS exports are frequently latin-1 / cp1252 rather than utf-8.
        return pd.read_csv(
            path, dtype=str, keep_default_na=True, encoding="latin-1", **kwargs
        )


def inspect(files: list[Path], args) -> None:
    for path in files:
        df = read_csv(path, args.encoding)
        cols = resolve_columns(df, args)
        print(f"\n=== {path} ===")
        print(f"rows: {len(df)}  columns: {len(df.columns)}")
        print("detected columns:")
        for key, value in cols.items():
            print(f"  {key:<16} {value if value else '<not found>'}")
        if cols["circumstance"]:
            print(f"\nvalue counts for {cols['circumstance']!r}:")
            print(
                df[cols["circumstance"]]
                .fillna("<missing>")
                .value_counts(dropna=False)
                .to_string()
            )
        for key in ("occupation_code", "occupation_text"):
            if cols[key]:
                values = df[cols[key]].fillna("<missing>").value_counts(dropna=False)
                print(f"\ntop 25 values for {cols[key]!r} ({len(values)} distinct):")
                print(values.head(25).to_string())


def process(files: list[Path], args, rules: dict) -> None:
    out_dir = Path(args.output_dir)
    (out_dir / "labeled").mkdir(parents=True, exist_ok=True)
    (out_dir / "splits").mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict] = []
    unmapped: list[dict] = []
    audit_rows: list[pd.DataFrame] = []

    for path in files:
        df = read_csv(path, args.encoding)
        cols = resolve_columns(df, args)
        band = age_band_from_name(path, rules["age_band_pattern"])

        if cols["circumstance"] is None:
            raise SystemExit(
                f"{path}: could not find a circumstance-known column; pass "
                "--circumstance-col explicitly (run --inspect to list columns)"
            )
        if cols["occupation_text"] is None and cols["occupation_code"] is None:
            raise SystemExit(
                f"{path}: could not find an occupation column; pass "
                "--occupation-col and/or --occupation-code-col explicitly"
            )

        df["circumstance_known_bool"] = to_circumstance_bool(
            df[cols["circumstance"]], rules
        )
        df[["occupation_group", "occupation_group_rule"]] = classify_occupation(
            df,
            rules,
            text_col=cols["occupation_text"],
            code_col=cols["occupation_code"],
            industry_code_col=cols["industry_code"],
            industry_text_col=cols["industry_text"],
            include_extraction=args.include_extraction,
        )
        df["age_band"] = band

        labeled_path = out_dir / "labeled" / f"{slugify(band)}_labeled.csv"
        df.to_csv(labeled_path, index=False)

        if cols["occupation_text"]:
            audit = (
                df.groupby(
                    [cols["occupation_text"], "occupation_group", "occupation_group_rule"],
                    dropna=False,
                )
                .size()
                .reset_index(name="n")
            )
            audit.columns = ["occupation_value", "occupation_group", "rule", "n"]
            audit.insert(0, "age_band", band)
            audit_rows.append(audit)

            # occupation values that only matched the catch-all rule
            fallback = df.loc[df["occupation_group_rule"] == "fallback_text"]
            if len(fallback):
                counts = fallback[cols["occupation_text"]].value_counts()
                unmapped.extend(
                    {"age_band": band, "occupation_value": value, "n": int(n)}
                    for value, n in counts.items()
                )

        work = df
        n_undetermined = int(work["circumstance_known_bool"].isna().sum())
        if args.drop_unknown_circumstance:
            work = work.loc[work["circumstance_known_bool"].notna()]

        band_dir = out_dir / "splits" / slugify(band)
        band_dir.mkdir(parents=True, exist_ok=True)

        known = work["circumstance_known_bool"]
        circumstance_masks = {
            "known_yes": known.fillna(False).astype(bool),
            "known_no": (~known.fillna(True)).astype(bool),
        }
        if not args.drop_unknown_circumstance:
            circumstance_masks["known_undetermined"] = known.isna()

        for group in OCCUPATION_GROUPS:
            if group == "unclassified" and not args.keep_unclassified:
                continue
            in_group = work["occupation_group"] == group
            for label, mask in circumstance_masks.items():
                subset = work.loc[in_group & mask]
                if subset.empty and not args.write_empty:
                    continue
                subset.to_csv(band_dir / f"{group}__circumstance_{label}.csv", index=False)

        for group in OCCUPATION_GROUPS:
            in_group = work["occupation_group"] == group
            summary_rows.append(
                {
                    "age_band": band,
                    "occupation_group": group,
                    "n_total": int(in_group.sum()),
                    "n_circumstance_known_yes": int(
                        (in_group & circumstance_masks["known_yes"]).sum()
                    ),
                    "n_circumstance_known_no": int(
                        (in_group & circumstance_masks["known_no"]).sum()
                    ),
                    "n_circumstance_undetermined": int(
                        (in_group & known.isna()).sum()
                    ),
                }
            )

        n_yes = int(df["circumstance_known_bool"].fillna(False).sum())
        n_no = int((~df["circumstance_known_bool"].fillna(True)).sum())
        print(
            f"{path.name}: {len(df)} rows -> band {band}; "
            f"circumstance yes/no/undetermined = {n_yes}/{n_no}/{n_undetermined}"
        )

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_dir / "summary_counts.csv", index=False)

    crosstab = summary.pivot_table(
        index="age_band",
        columns="occupation_group",
        values="n_circumstance_known_yes",
        fill_value=0,
        aggfunc="sum",
    )
    crosstab.to_csv(out_dir / "summary_crosstab_circumstance_known_yes.csv")

    pd.DataFrame(unmapped, columns=["age_band", "occupation_value", "n"]).to_csv(
        out_dir / "unmapped_occupations.csv", index=False
    )

    if audit_rows:
        pd.concat(audit_rows, ignore_index=True).to_csv(
            out_dir / "occupation_group_audit.csv", index=False
        )

    print(f"\nwrote outputs under {out_dir}/")
    print("\n" + summary.to_string(index=False))
    if unmapped:
        total = sum(row["n"] for row in unmapped)
        print(
            f"\n{total} rows in {len(unmapped)} distinct occupation values fell through "
            "to non_construction via the fallback rule. Review "
            f"{out_dir}/unmapped_occupations.csv and extend the keyword lists if any "
            "of them are really construction, military, or non-workforce."
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        nargs="+",
        help="CSV file(s) or a directory of CSVs (one per age band)",
    )
    parser.add_argument("--output-dir", default="out", help="output directory (default: out)")
    parser.add_argument("--encoding", default="utf-8", help="input encoding (default: utf-8)")
    parser.add_argument("--rules", type=Path, help="JSON file overriding the default rules")
    parser.add_argument(
        "--dump-rules",
        action="store_true",
        help="print the default rules as JSON and exit",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="report detected columns and value counts, write nothing",
    )
    parser.add_argument("--circumstance-col")
    parser.add_argument("--occupation-col", help="free-text occupation column")
    parser.add_argument("--occupation-code-col", help="census occupation code column")
    parser.add_argument("--industry-col", help="free-text industry column (optional)")
    parser.add_argument("--industry-code-col", help="census industry code column (optional)")
    parser.add_argument(
        "--include-extraction",
        action="store_true",
        help="count extraction occupations (census 6800-6940) as construction",
    )
    parser.add_argument(
        "--drop-unknown-circumstance",
        action="store_true",
        help="exclude rows whose circumstance-known value is neither yes nor no",
    )
    parser.add_argument(
        "--keep-unclassified",
        action="store_true",
        default=True,
        help="also write the unclassified occupation subsets (default: on)",
    )
    parser.add_argument(
        "--no-keep-unclassified",
        dest="keep_unclassified",
        action="store_false",
    )
    parser.add_argument(
        "--write-empty",
        action="store_true",
        help="write split files even when the subset has no rows",
    )
    args = parser.parse_args(argv)

    if args.dump_rules:
        print(json.dumps(DEFAULT_RULES, indent=2, ensure_ascii=False))
        return 0
    if not args.input:
        parser.error("--input is required (unless --dump-rules)")

    rules = load_rules(args.rules)
    files = collect_inputs(args.input, args.encoding)

    if args.inspect:
        inspect(files, args)
    else:
        process(files, args, rules)
    return 0


if __name__ == "__main__":
    sys.exit(main())
