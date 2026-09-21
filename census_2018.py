#!/usr/bin/env python3
"""Census 2018 occupation codes: the single definition used across this repo.

Both filter_construction.py and nvdrs_split.py import their code ranges from
here, so "construction" cannot come to mean two different things in two
scripts.

Ranges are inclusive. The 2018 list is NOT interchangeable with the 2010 one:
2010 extraction ends at 6940, 2018 at 6950.

    6200-6765   construction trades
    6800-6950   extraction workers
    0220        construction managers (sits under Management)
    9800-9830   military specific occupations
    9920        unemployed, no work experience / never worked
"""

from __future__ import annotations

import re

from nvdrs_split import _norm_value

CONSTRUCTION_TRADES = (6200, 6765)
EXTRACTION_WORKERS = (6800, 6950)
CONSTRUCTION_MANAGERS = (220, 220)
MILITARY = (9800, 9830)
NON_WORKFORCE = [(9920, 9920)]

# Highest valid civilian occupation code; above this the list is military and
# the special "not in the labor force" codes.
CIVILIAN_MAX = 9799

BLANK = "BLANK"
UNPARSEABLE = "UNPARSEABLE"

# Labels for review output only. Filtering depends on the numeric ranges
# alone, so an incomplete title table can never change which rows you get.
TITLES = {
    220: "Construction managers",
    6200: "First-line supervisors of construction trades and extraction workers",
    6210: "Boilermakers",
    6220: "Brickmasons, blockmasons, stonemasons, and reinforcing iron and rebar workers",
    6230: "Carpenters",
    6240: "Carpet, floor, and tile installers and finishers",
    6250: "Cement masons, concrete finishers, and terrazzo workers",
    6260: "Construction laborers",
    6300: "Construction equipment operators",
    6320: "Drywall installers, ceiling tile installers, and tapers",
    6330: "Electricians",
    6350: "Glaziers",
    6360: "Insulation workers",
    6400: "Painters and paperhangers",
    6410: "Pipelayers",
    6441: "Plumbers, pipefitters, and steamfitters",
    6460: "Plasterers and stucco masons",
    6515: "Roofers",
    6520: "Sheet metal workers",
    6530: "Structural iron and steel workers",
    6540: "Solar photovoltaic installers",
    6600: "Helpers, construction trades",
    6660: "Construction and building inspectors",
    6700: "Elevator and escalator installers and repairers",
    6710: "Fence erectors",
    6720: "Hazardous materials removal workers",
    6730: "Highway maintenance workers",
    6740: "Rail-track laying and maintenance equipment operators",
    6765: "Other construction and related workers",
    6800: "Derrick, rotary drill, and service unit operators, oil and gas",
    6825: "Earth drillers, except oil and gas",
    6835: "Explosives workers, ordnance handling experts, and blasters",
    6850: "Underground mining machine operators",
    6950: "Other extraction workers",
    9800: "Military officer special and tactical operations leaders",
    9810: "First-line enlisted military supervisors",
    9825: "Military enlisted tactical operations and air/weapons specialists",
    9830: "Military, rank not specified",
    9920: "Unemployed, with no work experience or never worked",
}

CENSUS_2018_COL_CANDIDATES = (
    "census2018occupation",
    "census2018occ",
    "census2018",
    "censusoccupation2018",
    "occupationcensus2018",
    "census2018code",
    "occ2018",
    "censuscode2018",
)


def find_occupation_column(columns, exclude=()) -> str | None:
    """Find the occupation-code column, never an industry column.

    "Census2018_Industry" normalises to "census2018industry", which CONTAINS
    the occupation candidate "census2018". Without this guard a substring
    match would hand back the industry column and every code would be read
    against the wrong list.
    """
    from nvdrs_split import _norm_colname

    excluded = set(exclude)
    normalised = {
        _norm_colname(c): c
        for c in columns
        if c not in excluded and "industry" not in _norm_colname(c)
    }
    for cand in CENSUS_2018_COL_CANDIDATES:
        if cand in normalised:
            return normalised[cand]
    for cand in CENSUS_2018_COL_CANDIDATES:
        for norm, original in normalised.items():
            if cand in norm:
                return original
    return None


def parse_code(value) -> int | str:
    """Return the census code as an int, or BLANK / UNPARSEABLE.

    Accepts "6230", "06230", "6230.0", 6230. A non-numeric value is never
    guessed at -- it comes back as UNPARSEABLE and is reported.
    """
    text = _norm_value(value)
    if not text:
        return BLANK
    match = re.fullmatch(r"(\d{1,5})(?:\.0+)?", text)
    if match:
        return int(match.group(1))
    return UNPARSEABLE


def in_ranges(code: int, ranges) -> bool:
    return any(low <= code <= high for low, high in ranges)


def build_construction_ranges(
    include_extraction: bool = False, include_managers: bool = False
) -> list[tuple[int, int]]:
    ranges = [CONSTRUCTION_TRADES]
    if include_extraction:
        ranges.append(EXTRACTION_WORKERS)
    if include_managers:
        ranges.append(CONSTRUCTION_MANAGERS)
    return sorted(ranges)


def describe_ranges(ranges) -> str:
    return ", ".join(f"{low}-{high}" if low != high else str(low) for low, high in ranges)


def classify(
    value,
    *,
    include_extraction: bool = False,
    include_managers: bool = False,
) -> tuple[str, str]:
    """Group one census_2018 value, using that value and nothing else.

    Returns (group, rule) where group is one of construction,
    non_construction, non_workforce, military, unclassified.
    """
    code = parse_code(value)
    if code == BLANK:
        return "unclassified", "census_2018_blank"
    if code == UNPARSEABLE:
        return "unclassified", "census_2018_unparseable"

    construction = build_construction_ranges(include_extraction, include_managers)
    if in_ranges(code, construction):
        return "construction", "census_2018_code"
    if in_ranges(code, [MILITARY]):
        return "military", "census_2018_code"
    if in_ranges(code, NON_WORKFORCE):
        return "non_workforce", "census_2018_code"
    if 0 < code <= CIVILIAN_MAX:
        return "non_construction", "census_2018_code"
    # A code above the civilian range that is neither military nor a known
    # not-in-labor-force code: do not guess.
    return "unclassified", "census_2018_out_of_list"


def title(code) -> str:
    return TITLES.get(code, "") if isinstance(code, int) else ""
