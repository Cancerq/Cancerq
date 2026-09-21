#!/usr/bin/env python3
"""Census 2018 INDUSTRY codes -- a different code list from the occupation one.

census_2018.py holds OCCUPATION codes (what the person does: carpenter = 6230).
This module holds INDUSTRY codes (what the employer does: construction = 0770).
They are not interchangeable and they do not select the same people:

    an accountant at a construction firm   -> industry YES, occupation NO
    a carpenter employed by a school board -> industry NO,  occupation YES

In the Census industry code list, Construction is the SINGLE code 0770 -- not
a range. Mining, quarrying and oil & gas extraction is a separate sector
(0370-0490) and is opt-in, the way extraction is on the occupation side.
"""

from __future__ import annotations

from census_2018 import BLANK, UNPARSEABLE, parse_code  # noqa: F401  (shared parser)

# --- the only ranges this module decides on -------------------------------
CONSTRUCTION = (770, 770)          # 0770 Construction
MINING_EXTRACTION = (370, 490)     # 0370-0490 Mining, quarrying, oil & gas

# Lowest and highest codes that appear in the Census industry list. Anything
# outside is treated as unclassified rather than quietly called
# non-construction.
INDUSTRY_MIN = 170
INDUSTRY_MAX = 9920

COL_CANDIDATES = (
    "census2018industry",
    "censusindustry2018",
    "industrycensus2018",
    "census2018ind",
    "industry2018",
    "ind2018",
)

# Sector labels for the breakdown output only. Nothing here decides what is
# kept -- filtering uses CONSTRUCTION / MINING_EXTRACTION alone, so a wrong or
# missing label can never change which rows you get. Verify against the
# official Census industry code list before quoting these in a paper.
SECTORS = [
    ((170, 290), "Agriculture, forestry, fishing and hunting"),
    ((370, 490), "Mining, quarrying, and oil and gas extraction"),
    ((570, 690), "Utilities"),
    ((770, 770), "Construction"),
    ((1070, 3990), "Manufacturing"),
    ((4070, 4590), "Wholesale trade"),
    ((4670, 5790), "Retail trade"),
    ((6070, 6390), "Transportation and warehousing"),
    ((6470, 6780), "Information"),
    ((6870, 6992), "Finance and insurance"),
    ((7071, 7190), "Real estate and rental and leasing"),
    ((7270, 7490), "Professional, scientific, and technical services"),
    ((7570, 7570), "Management of companies and enterprises"),
    ((7580, 7790), "Administrative, support and waste management services"),
    ((7860, 7890), "Educational services"),
    ((7970, 8470), "Health care and social assistance"),
    ((8561, 8590), "Arts, entertainment, and recreation"),
    ((8660, 8690), "Accommodation and food services"),
    ((8770, 9290), "Other services, except public administration"),
    ((9370, 9590), "Public administration"),
    ((9670, 9870), "Military"),
    ((9920, 9920), "Unemployed, with no work experience or never worked"),
]


def in_ranges(code: int, ranges) -> bool:
    return any(low <= code <= high for low, high in ranges)


def build_construction_ranges(include_mining: bool = False) -> list[tuple[int, int]]:
    ranges = [CONSTRUCTION]
    if include_mining:
        ranges.append(MINING_EXTRACTION)
    return sorted(ranges)


def describe_ranges(ranges) -> str:
    return ", ".join(
        f"{low:04d}" if low == high else f"{low:04d}-{high:04d}" for low, high in ranges
    )


def sector(code) -> str:
    """Sector name for the breakdown, or '' when the code is not in the list."""
    if not isinstance(code, int):
        return ""
    for (low, high), name in SECTORS:
        if low <= code <= high:
            return name
    return ""


def classify(value, *, include_mining: bool = False) -> tuple[str, str]:
    """Group one census2018_industry value, using that value and nothing else.

    Returns (group, rule) with group in construction / nonconstruction /
    blank / unclassified.
    """
    code = parse_code(value)
    if code == BLANK:
        return "blank", "industry_blank"
    if code == UNPARSEABLE:
        return "unclassified", "industry_unparseable"
    if in_ranges(code, build_construction_ranges(include_mining)):
        return "construction", "industry_code"
    if INDUSTRY_MIN <= code <= INDUSTRY_MAX:
        return "nonconstruction", "industry_code"
    return "unclassified", "industry_out_of_list"
