#!/usr/bin/env python3
"""Generate small synthetic NVDRS-shaped CSVs for testing nvdrs_split.py.

The values mimic the two shapes NVDRS exports come in: a free-text occupation
column and a census occupation code column. No real NVDRS data is included.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

AGE_BANDS = ["18-30", "31-40", "41-50", "51-60", "61-70"]

# (occupation text, occupation code, industry code, circumstance known, expected)
ROWS = [
    ("Construction laborer", "6260", "", "Yes", "construction"),
    ("Carpenter", "6230", "", "No", "construction"),
    ("Electrician", "", "", "Yes", "construction"),  # keyword path
    ("Roofer", "6515", "", "", "construction"),
    ("US Army sergeant", "9820", "", "Yes", "military"),
    ("Marine Corps corporal", "", "", "No", "military"),  # keyword path
    ("Retired", "", "", "Yes", "non_workforce"),
    ("Unemployed", "9920", "", "Yes", "non_workforce"),
    ("Student", "", "", "No", "non_workforce"),
    ("Homemaker", "", "", "Yes", "non_workforce"),
    ("Disabled, unable to work", "", "", "yes", "non_workforce"),
    ("Registered nurse", "3130", "", "Yes", "non_construction"),
    ("Truck driver", "9130", "", "No", "non_construction"),
    ("Software developer", "1020", "", "Yes", "non_construction"),
    ("Restaurant cook", "4020", "", "Unknown", "non_construction"),
    ("Barber", "", "", "Yes", "non_construction"),  # fallback_text path
    ("Nanny", "", "", "No", "non_construction"),  # must not match "na"
    ("Mother of three", "", "", "Yes", "non_construction"),  # must not match "other"
    ("", "", "0770", "Yes", "construction"),  # industry-code path
    ("Unknown", "", "", "Yes", "unclassified"),
    ("", "", "", "No", "unclassified"),
]

HEADER = [
    "IncidentID",
    "Age",
    "Sex",
    "CircumstancesKnown",
    "Occupation",
    "OccupationCode",
    "IndustryCode",
    "expected_occupation_group",
]


def main(out_dir: str = "tests/sample_data") -> int:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    for band_index, band in enumerate(AGE_BANDS):
        low = int(band.split("-")[0])
        path = target / f"nvdrs_{band}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(HEADER)
            for row_index, (occ, code, industry, known, expected) in enumerate(ROWS):
                writer.writerow(
                    [
                        f"{band_index + 1}{row_index:04d}",
                        low + (row_index % 5),
                        "M" if row_index % 2 else "F",
                        known,
                        occ,
                        code,
                        industry,
                        expected,
                    ]
                )
        print(f"wrote {path} ({len(ROWS)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
