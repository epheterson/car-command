#!/usr/bin/env python3
"""Build stack/web/rates/utilities.json: the US electric utilities that sell to homes, for
Settings › Money's plan search to pick from, so nobody has to type a utility's name the way
OpenEI spells it. Standard library only.

    scripts/build-utilities.py [--year 2024] [--zip PATH] [--out PATH]

Source: EIA Form 861 annual data (public domain), https://www.eia.gov/electricity/data/eia861/,
the f861<year>.zip release. From it:
  Sales_Ult_Cust_<year>.xlsx  which utilities have residential customers, in which states.
                              Only Bundled and Delivery service count: those are the
                              utilities whose tariffs set the price of a home's energy and
                              wires. Energy-only retailers (Texas REPs, CCAs) are left out.
  Short_Form_<year>.xlsx      the small utilities that file the short form instead (no
                              sector split there: every one with customers is kept).
  Utility_Data_<year>.xlsx    each utility's name.
The EIA utility number is the `eia` OpenEI's rate database files a utility's rates under.

Output: {"source", "year", "sha256" of the zip, "utilities": [[eia id, name, "CA,NV",
residential customers], ...] largest first, "aliases": {"pg&e": eia id, ...}}. The same zip
gives the same file, byte for byte.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile

# The workbook XML is EIA's own, read from a zip this script downloads and fingerprints;
# Python's expat does not fetch external entities.
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "stack" / "web" / "rates" / "utilities.json"
URL = "https://www.eia.gov/electricity/data/eia861/zip/f861{year}.zip"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# What people call the big ones, by EIA utility number. Initials are matched without
# being listed here (the page matches "pge" to Pacific Gas & Electric Co by its words).
ALIASES = {
    "pg&e": 14328,
    "pge": 14328,
    "sce": 17609,
    "socal edison": 17609,
    "sdg&e": 16609,
    "sdge": 16609,
    "ladwp": 11208,
    "smud": 16534,
    "coned": 4226,
    "con ed": 4226,
    "con edison": 4226,
    "pse&g": 15477,
    "pseg": 15477,
    "fpl": 6452,
    "aps": 803,
    "srp": 16572,
    "comed": 4110,
    "bge": 1167,
    "pepco": 15270,
    "dominion": 19876,
    "georgia power": 7140,
    "puget sound energy": 15500,
    "pse": 15500,
    "seattle city light": 16868,
    "nv energy": 13407,
    "peco": 14940,
    "dte": 5109,
    "consumers energy": 4254,
}


def sheet_rows(data: bytes):
    """Rows of a workbook's first sheet as {column letter: text}."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
    sheet = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))[0]
    for row in ET.fromstring(z.read(sheet)).iter(f"{NS}row"):
        out = {}
        for c in row.iter(f"{NS}c"):
            col = re.match(r"[A-Z]+", c.get("r")).group(0)
            v = c.find(f"{NS}v")
            if c.get("t") == "inlineStr":
                out[col] = "".join(t.text or "" for t in c.iter(f"{NS}t"))
            elif v is not None:
                out[col] = shared[int(v.text)] if c.get("t") == "s" else v.text
        yield out


def header(rows, first: str) -> tuple[dict, list]:
    """The column letters by header name, from the row whose column A is `first`, and the rows after it."""
    rows = list(rows)
    for i, r in enumerate(rows):
        if (r.get("A") or "").strip() == first:
            return {(v or "").strip(): k for k, v in r.items()}, rows[i + 1 :]
    raise SystemExit(f"no header row starting {first!r}")


def clean(name: str) -> str:
    return " ".join(name.split()).rstrip(".")


def build(data: bytes, year: int) -> dict:
    outer = zipfile.ZipFile(io.BytesIO(data))
    cols, rows = header(sheet_rows(outer.read(f"Sales_Ult_Cust_{year}.xlsx")), "Data Year")
    num, name, state, service = cols["Utility Number"], cols["Utility Name"], cols["State"], cols["Service Type"]
    customers = "L"  # RESIDENTIAL › Customers: the header names repeat per sector, so by position
    found: dict[int, dict] = {}
    for r in rows:
        try:
            eia, n = int(r.get(num) or 0), float(r.get(customers) or 0)
        except ValueError:
            continue
        if eia <= 0 or eia == 99999 or n <= 0 or r.get(service) not in ("Bundled", "Delivery"):
            continue  # 99999 is EIA's adjustment row, not a utility
        u = found.setdefault(eia, {"name": clean(r.get(name) or ""), "states": set(), "customers": 0.0})
        u["states"].add(r.get(state) or "")
        u["customers"] = max(u["customers"], n) if r.get(service) == "Delivery" else u["customers"] + n
    scols, srows = header(sheet_rows(outer.read(f"Short_Form_{year}.xlsx")), "Data Year")
    for r in srows:
        try:
            eia, n = int(r.get(scols["Utility Number"]) or 0), float(r.get(scols["Total Customers"]) or 0)
        except ValueError:
            continue
        if eia > 0 and n > 0:
            u = found.setdefault(eia, {"name": clean(r.get(scols["Utility Name"]) or ""), "states": set(), "customers": 0.0})
            u["states"].add(r.get(scols["State"]) or "")
            u["customers"] += n
    ucols, urows = header(sheet_rows(outer.read(f"Utility_Data_{year}.xlsx")), "Data Year")
    for r in urows:
        try:
            eia = int(r.get(ucols["Utility Number"]) or 0)
        except ValueError:
            continue
        if eia in found and r.get(ucols["Utility Name"]):
            found[eia]["name"] = clean(r[ucols["Utility Name"]])
    utilities = sorted(([eia, u["name"], ",".join(sorted(s for s in u["states"] if s)), int(u["customers"])] for eia, u in found.items() if u["name"]), key=lambda x: (-x[3], x[1], x[0]))
    known = {u[0] for u in utilities}
    missing = sorted(k for k, v in ALIASES.items() if v not in known)
    if missing:
        print(f"aliases to a utility not in the list: {missing}", file=sys.stderr)
    return {
        "source": f"EIA Form 861, {year} (Sales_Ult_Cust and Utility_Data): utilities with residential Bundled or Delivery customers",
        "year": year,
        "sha256": hashlib.sha256(data).hexdigest(),
        "utilities": utilities,
        "aliases": {k: v for k, v in sorted(ALIASES.items()) if v in known},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--zip", type=Path, help="a downloaded f861<year>.zip instead of fetching it")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    if args.zip:
        data = args.zip.read_bytes()
    else:
        req = urllib.request.Request(URL.format(year=args.year), headers={"User-Agent": "carcommand-build/1"})
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - a fixed https URL
            data = r.read()
    out = build(data, args.year)
    args.out.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(f"{args.out}: {len(out['utilities'])} utilities, {args.out.stat().st_size // 1024} KB, zip sha256 {out['sha256'][:12]}")


if __name__ == "__main__":
    main()
