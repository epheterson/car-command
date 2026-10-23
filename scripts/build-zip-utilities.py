#!/usr/bin/env python3
"""Build stack/web/rates/zip_utilities.json: which utilities serve homes in each US ZIP code,
for Find my plan to suggest a utility from the home's ZIP code without asking anyone where
the home is. Standard library only.

    scripts/build-zip-utilities.py [--year 2023] [--dir PATH] [--out PATH]

Source: NREL's "U.S. Electric Utility Companies and Rates: Look-up by Zipcode", licensed
CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/), https://data.openei.org/submissions/6225 for 2023: iou_zipcodes_<year>.csv and
non_iou_zipcodes_<year>.csv, one row per ZIP code, utility and service type, built from EIA
Form 861 service territories. Only Bundled and Delivery rows count, as in utilities.json:
the utilities whose tariffs price a home's energy and wires. The ZIP lists are approximate
(a ZIP code a city utility serves can miss the larger utility around it), so the page offers
them as suggestions, never as an answer.

Output: {"source", "licence", "year", "sha256": {file: digest}, "utilities": {eia id: "ZIP codes"}},
each utility's ZIP codes ascending as hex steps from the one before, space separated (a
fifth of the size of a plain list). The same files give the same output, byte for byte.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "https://data.openei.org/files/{sub}/{kind}_zipcodes_{year}.csv"
SUBMISSIONS = {2023: 6225, 2020: 5650}


def fetch(kind: str, year: int, folder: Path | None) -> bytes:
    if folder:
        return (folder / f"{kind}_zipcodes_{year}.csv").read_bytes()
    url = URL.format(sub=SUBMISSIONS[year], kind=kind, year=year)
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - a fixed https URL
        return r.read()


def steps(zips: set[int]) -> str:
    out, prev = [], 0
    for z in sorted(zips):
        out.append(format(z - prev, "x"))
        prev = z
    return " ".join(out)


def build(files: dict[str, bytes], year: int) -> dict:
    served: dict[int, set[int]] = {}
    for body in files.values():
        for row in csv.DictReader(io.StringIO(body.decode("utf-8-sig"))):
            if row.get("service_type") not in ("Bundled", "Delivery"):
                continue
            try:
                served.setdefault(int(row["eiaid"]), set()).add(int(row["zip"]))
            except (KeyError, TypeError, ValueError):
                continue
    return {
        "source": f"NREL, U.S. Electric Utility Companies and Rates: Look-up by Zipcode ({year}): Bundled and Delivery service",
        "licence": "CC BY 4.0, https://creativecommons.org/licenses/by/4.0/ (National Renewable Energy Laboratory); filtered and re-encoded here",
        "year": year,
        "sha256": {name: hashlib.sha256(body).hexdigest() for name, body in sorted(files.items())},
        "utilities": {str(eia): steps(z) for eia, z in sorted(served.items())},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--year", type=int, default=2023, choices=sorted(SUBMISSIONS))
    ap.add_argument("--dir", type=Path, help="read the two CSV files from here instead of downloading them")
    ap.add_argument("--out", type=Path, default=ROOT / "stack" / "web" / "rates" / "zip_utilities.json")
    a = ap.parse_args()
    files = {f"{kind}_zipcodes_{a.year}.csv": fetch(kind, a.year, a.dir) for kind in ("iou", "non_iou")}
    out = build(files, a.year)
    a.out.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    print(f"{len(out['utilities'])} utilities -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
