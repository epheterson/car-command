#!/usr/bin/env python3
"""Build stack/web/rates/averages.json (the copy the image bundles; the workflow writes it
to rates/averages.json on the rates-data branch): average household electricity and petrol prices by region
and month, for pricing charges no plan covers and for the comparison with a petrol car.
Standard library only, so the scheduled workflow needs nothing installed.

    EIA_API_KEY=... scripts/build-rate-averages.py [--out PATH] [--cache DIR]

Sources (all public; nothing about any car is sent):
  US electricity  EIA retail sales, residential average price, every state, DC and the
                  US, monthly from 2001 (cents per kWh, stored as dollars).
  US petrol       EIA regular gasoline retail price, monthly, for the states EIA publishes
                  on their own; every other state takes its PADD district (PADD 1 by its
                  sub-districts, and the West Coast without California for the other
                  West Coast states). Dollars per gallon.
  EU electricity  Eurostat nrg_pc_204, household consumers using 2,500-4,999 kWh a year,
                  all taxes included, half-yearly from 2007; each half-year's value is
                  given to its six months. Euro for euro countries, national currency for
                  the rest.
  EU petrol       The European Commission's Weekly Oil Bulletin history, Euro-super 95
                  with taxes, the mean of each month's weeks, per litre, in the same
                  currency as the electricity.

EIA_API_KEY unset falls back to DEMO_KEY, which works but is rate limited (about ten
requests an hour); a run needs six. A rate limit is waited out (60 s doubling, about half
an hour in all), then the run fails without writing; so does one missing any state, DC,
the US or most of Europe. --cache keeps each raw answer in a directory and
reuses it, for working on this script without asking twice.

Output: {"version", "built", "regions": {code: {"name", "electricity": series,
"petrol": series}}}, where a series is {"area", "source", "unit", "currency", "as_of",
"values": {"YYYY-MM": price}}. Codes are 'US-CA', 'US' or an ISO country code ('DE').
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # noqa: S405 - parses the Commission's own workbook, not user input
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "stack" / "web" / "rates" / "averages.json"  # the copy the image bundles
EIA = "https://api.eia.gov/v2"
EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_pc_204"
OIL_BULLETIN_PAGE = "https://energy.ec.europa.eu/data-and-analysis/weekly-oil-bulletin_en"
OIL_BULLETIN_XLSX = "https://energy.ec.europa.eu/document/download/906e60ca-8b6a-44e7-8589-652854d2fd3f_en?filename=Weekly_Oil_Bulletin_Prices_History_maticni_4web.xlsx"
START = "2001-01"
AGENT = "car-command-rate-averages/1 (+https://github.com/epheterson/car-command)"

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}  # fmt: skip

# EIA petrol areas: the states published on their own, then the districts.
PETROL_AREAS = {
    "SCA": "California", "SCO": "Colorado", "SFL": "Florida", "SMA": "Massachusetts", "SMN": "Minnesota", "SNY": "New York",
    "SOH": "Ohio", "STX": "Texas", "SWA": "Washington", "R1X": "New England (PADD 1A)", "R1Y": "Central Atlantic (PADD 1B)",
    "R1Z": "Lower Atlantic (PADD 1C)", "R20": "Midwest (PADD 2)", "R30": "Gulf Coast (PADD 3)", "R40": "Rocky Mountain (PADD 4)",
    "R5XCA": "West Coast except California (PADD 5)", "NUS": "United States",
}  # fmt: skip
DISTRICT = {
    **dict.fromkeys(("CT", "ME", "MA", "NH", "RI", "VT"), "R1X"),
    **dict.fromkeys(("DE", "DC", "MD", "NJ", "NY", "PA"), "R1Y"),
    **dict.fromkeys(("FL", "GA", "NC", "SC", "VA", "WV"), "R1Z"),
    **dict.fromkeys(("IL", "IN", "IA", "KS", "KY", "MI", "MN", "MO", "NE", "ND", "SD", "OH", "OK", "TN", "WI"), "R20"),
    **dict.fromkeys(("AL", "AR", "LA", "MS", "NM", "TX"), "R30"),
    **dict.fromkeys(("CO", "ID", "MT", "UT", "WY"), "R40"),
    **dict.fromkeys(("AK", "AZ", "HI", "NV", "OR", "WA"), "R5XCA"),
    "CA": "SCA",
}
OWN = {"SCA": "CA", "SCO": "CO", "SFL": "FL", "SMA": "MA", "SMN": "MN", "SNY": "NY", "SOH": "OH", "STX": "TX", "SWA": "WA"}

# Europe: the source's code -> ISO code, and each country's currency today. Euro countries
# are asked for euro throughout (a country that joined later has its old currency
# converted at the fixed rate); the rest in their own currency.
ISO = {"EL": "GR", "UK": "GB"}
EURO = {"AT", "BE", "BG", "CY", "DE", "EE", "ES", "FI", "FR", "GR", "HR", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PT", "SI", "SK", "ME", "XK"}
CURRENCY = {
    "CZ": "CZK", "DK": "DKK", "HU": "HUF", "PL": "PLN", "RO": "RON", "SE": "SEK", "IS": "ISK", "LI": "CHF", "NO": "NOK", "GB": "GBP",
    "BA": "BAM", "MD": "MDL", "MK": "MKD", "GE": "GEL", "AL": "ALL", "RS": "RSD", "TR": "TRY", "UA": "UAH", "CH": "CHF",
}  # fmt: skip
COUNTRY_NAMES = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany", "DK": "Denmark", "EE": "Estonia",
    "ES": "Spain", "FI": "Finland", "FR": "France", "GR": "Greece", "HR": "Croatia", "HU": "Hungary", "IE": "Ireland", "IT": "Italy",
    "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "MT": "Malta", "NL": "Netherlands", "PL": "Poland", "PT": "Portugal",
    "RO": "Romania", "SE": "Sweden", "SI": "Slovenia", "SK": "Slovakia", "IS": "Iceland", "LI": "Liechtenstein", "NO": "Norway",
    "GB": "United Kingdom", "BA": "Bosnia and Herzegovina", "ME": "Montenegro", "MD": "Moldova", "MK": "North Macedonia",
    "GE": "Georgia", "AL": "Albania", "RS": "Serbia", "TR": "Türkiye", "UA": "Ukraine", "XK": "Kosovo", "CH": "Switzerland",
}  # fmt: skip


RETRIES = 5  # rate-limit waits before giving up: 60, 120, 240, 480, 900 s


class Fetcher:
    """GET with a polite pause, retries on rate limits, and an optional on-disk cache."""

    def __init__(self, cache: Path | None, pause: float = 2.0) -> None:
        self.cache, self.pause, self.calls = cache, pause, 0

    def get(self, url: str, secret: str | None = None) -> bytes:
        # The cache key leaves the API key out, so a cached run works with any key.
        public = url.replace(secret, "KEY") if secret else url
        path = self.cache / (hashlib.sha256(public.encode()).hexdigest()[:24] + ".bin") if self.cache else None
        if path and path.exists():
            return path.read_bytes()
        for attempt in range(RETRIES + 1):
            if self.calls:
                time.sleep(self.pause)
            self.calls += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": AGENT})
                with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310 - fixed https sources
                    body = r.read()
                break
            except urllib.error.HTTPError as err:
                said = err.read()[:300]
                # DEMO_KEY allows about ten requests an hour: api.data.gov answers 429, or
                # 403 with OVER_RATE_LIMIT. Wait it out, doubling, up to about half an hour
                # in all; a 403 for any other reason (a bad key) fails at once.
                limited = err.code in (429, 503) or (err.code == 403 and b"RATE_LIMIT" in said)
                if limited and attempt < RETRIES:
                    wait = int(err.headers.get("Retry-After") or 0) or min(60 * 2**attempt, 900)
                    print(f"  {err.code} from {public[:80]}...; waiting {wait} s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise SystemExit(f"{public[:120]}: HTTP {err.code} {said!r}") from err
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return body


def _months(ym_from: str, ym_to: str) -> list[str]:
    y, m = map(int, ym_from.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= ym_to:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def series(area: str, source: str, unit: str, currency: str, values: dict[str, float], digits: int) -> dict | None:
    values = {k: round(v, digits) for k, v in sorted(values.items()) if v is not None and v > 0}
    if not values:
        return None
    return {"area": area, "source": source, "unit": unit, "currency": currency, "as_of": max(values), "values": values}


def eia_rows(fetch: Fetcher, key: str, route: str, params: list[tuple[str, str]]) -> list[dict]:
    """Every row of an EIA v2 query, a page of 5,000 at a time."""
    rows, offset = [], 0
    while True:
        q = [("api_key", key), ("frequency", "monthly"), ("start", START), ("sort[0][column]", "period"), ("sort[0][direction]", "asc"), ("offset", str(offset)), ("length", "5000"), *params]
        body = json.loads(fetch.get(f"{EIA}/{route}/data/?{urllib.parse.urlencode(q)}", secret=key))
        if "response" not in body:
            raise SystemExit(f"EIA {route}: {str(body)[:300]}")
        page = body["response"].get("data") or []
        rows += page
        offset += len(page)
        if not page or offset >= int(body["response"].get("total") or 0):
            return rows


def us_electricity(fetch: Fetcher, key: str) -> dict[str, dict]:
    ids = [*STATES, "US"]
    rows = eia_rows(fetch, key, "electricity/retail-sales", [("data[]", "price"), ("facets[sectorid][]", "RES"), *[("facets[stateid][]", s) for s in ids]])
    by: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        if r.get("price") not in (None, ""):
            by[r["stateid"]][r["period"]] = float(r["price"]) / 100  # cents -> dollars
    out = {}
    for s in ids:
        code = "US" if s == "US" else f"US-{s}"
        got = series(STATES.get(s, "United States"), "US EIA, average residential electricity price", "kWh", "USD", by.get(s, {}), 4)
        if got:
            out[code] = got
    return out


def us_petrol(fetch: Fetcher, key: str) -> dict[str, dict]:
    params = [("data[]", "value"), ("facets[product][]", "EPMR"), ("facets[process][]", "PTE"), *[("facets[duoarea][]", a) for a in PETROL_AREAS]]
    rows = eia_rows(fetch, key, "petroleum/pri/gnd", params)
    by: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        if r.get("value") not in (None, ""):
            by[r["duoarea"]][r["period"]] = float(r["value"])
    areas = {a: series(name, "US EIA, regular gasoline retail price", "gal", "USD", by.get(a, {}), 3) for a, name in PETROL_AREAS.items()}
    out = {"US": areas["NUS"]} if areas.get("NUS") else {}
    for s in STATES:
        own = next((a for a, st in OWN.items() if st == s), None)
        got = areas.get(own) if own else None
        if not got:
            got = areas.get(DISTRICT.get(s, "")) or areas.get("NUS")
        if got:
            out[f"US-{s}"] = got
    return out


def _half_months(period: str) -> list[str]:
    """'2025-S2' -> ['2025-07', ..., '2025-12']."""
    year, half = period.split("-S")
    first = 1 if half == "1" else 7
    return [f"{year}-{m:02d}" for m in range(first, first + 6)]


def eu_electricity(fetch: Fetcher) -> dict[str, dict]:
    out = {}
    for currency in ("EUR", "NAC"):
        q = {"format": "JSON", "nrg_cons": "KWH2500-4999", "tax": "I_TAX", "currency": currency, "unit": "KWH"}
        d = json.loads(fetch.get(f"{EUROSTAT}?{urllib.parse.urlencode(q)}"))
        geo = d["dimension"]["geo"]["category"]["index"]
        times = d["dimension"]["time"]["category"]["index"]
        nt, values = len(times), d["value"]
        for src, gi in geo.items():
            code = ISO.get(src, src)
            if len(code) != 2 or code == "EA" or (code in EURO) != (currency == "EUR"):
                continue  # aggregates (EU27_2020, EA), and each country asked once in its own currency
            monthly = {}
            for period, ti in times.items():
                v = values.get(str(gi * nt + ti))
                if v is not None:
                    for ym in _half_months(period):
                        monthly[ym] = float(v)
            got = series(COUNTRY_NAMES.get(code, code), "Eurostat nrg_pc_204, household electricity price with all taxes", "kWh", "EUR" if code in EURO else CURRENCY.get(code, "?"), monthly, 4)
            if got:
                out[code] = got
    return out


def bulletin_url(fetch: Fetcher) -> str:
    """The history workbook's link, read off the bulletin's page; the known link if the
    page does not name one."""
    try:
        page = fetch.get(OIL_BULLETIN_PAGE).decode("utf-8", "replace")
    except SystemExit:
        return OIL_BULLETIN_XLSX
    for href in re.findall(r'href="([^"]+)"', page):
        href = html.unescape(href)
        if "Prices_History" in href and "xlsx" in href.lower():
            return urllib.parse.urljoin(OIL_BULLETIN_PAGE, href)
    return OIL_BULLETIN_XLSX


NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _sheet_rows(z: zipfile.ZipFile, sheet: str):
    """Each row of one worksheet as {column letters: text}, streamed."""
    strings = [''.join(t.text or '' for t in si.iter(f"{NS}t")) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{NS}si")]  # noqa: S314
    for _ev, el in ET.iterparse(z.open(sheet)):  # noqa: S314
        if el.tag == f"{NS}row":
            row = {}
            for c in el.findall(f"{NS}c"):
                v = c.find(f"{NS}v")
                if v is not None and v.text is not None:
                    row[re.match(r"[A-Z]+", c.get("r")).group()] = strings[int(v.text)] if c.get("t") == "s" else v.text
            yield row
            el.clear()


def _sheet_path(z: zipfile.ZipFile, name: str) -> str:
    wb = ET.fromstring(z.read("xl/workbook.xml"))  # noqa: S314
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))  # noqa: S314
    rid = next(s.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id") for s in wb.iter(f"{NS}sheet") if s.get("name") == name)
    target = next(r.get("Target") for r in rels if r.get("Id") == rid)
    return "xl/" + target.lstrip("/").removeprefix("xl/")


def eu_petrol(fetch: Fetcher) -> dict[str, dict]:
    """Euro-super 95 with taxes, a month's weekly prices averaged, per litre."""
    z = zipfile.ZipFile(io.BytesIO(fetch.get(bulletin_url(fetch))))
    rows = _sheet_rows(z, _sheet_path(z, "Prices with taxes"))
    header = next(rows)
    price_col, rate_col = {}, {}
    for col, name in header.items():
        m = re.fullmatch(r"([A-Z]{2})_(price_with_tax_euro95|exchange_rate)", name or "")
        if m:
            (price_col if m.group(2).startswith("price") else rate_col)[ISO.get(m.group(1), m.group(1))] = col
    sums: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    epoch = date(1899, 12, 30)  # spreadsheet day zero
    for row in rows:
        try:
            day = epoch + timedelta(days=int(float(row.get("A", ""))))
        except ValueError:
            continue  # the title and unit rows
        ym = f"{day.year:04d}-{day.month:02d}"
        for code, col in price_col.items():
            try:
                eur = float(row[col]) / 1000  # per 1000 L -> per litre
                if code not in EURO:
                    eur /= float(row[rate_col[code]])  # euro per unit of national currency
            except (KeyError, ValueError, ZeroDivisionError):
                continue
            sums[code][ym].append(eur)
    out = {}
    for code, months in sums.items():
        monthly = {ym: sum(v) / len(v) for ym, v in months.items() if ym >= START}
        got = series(COUNTRY_NAMES.get(code, code), "EU Weekly Oil Bulletin, Euro-super 95 with taxes", "L", "EUR" if code in EURO else CURRENCY.get(code, "?"), monthly, 3)
        if got:
            out[code] = got
    return out


def build(fetch: Fetcher, key: str) -> dict:
    regions: dict[str, dict] = {}
    for fuel, part in (("electricity", us_electricity(fetch, key)), ("petrol", us_petrol(fetch, key)), ("electricity", eu_electricity(fetch)), ("petrol", eu_petrol(fetch))):
        for code, s in part.items():
            name = "United States" if code == "US" else STATES.get(code[3:], code) if code.startswith("US-") else COUNTRY_NAMES.get(code, code)
            regions.setdefault(code, {"name": name})[fuel] = s
    return {"version": 1, "built": date.today().isoformat(), "regions": dict(sorted(regions.items()))}


MIN_EUROPE = 25  # countries with electricity; Eurostat publishes about forty


def check_complete(data: dict) -> None:
    """Refuse a file that lost something a run should always have: every state, DC and
    the US with both fuels, and most of Europe. A partial file would replace a whole one."""
    regions = data["regions"]
    for code in [f"US-{s}" for s in STATES] + ["US"]:
        for fuel in ("electricity", "petrol"):
            if not (regions.get(code) or {}).get(fuel):
                raise SystemExit(f"no {fuel} series for {code}; not writing a partial file")
    europe = [c for c, r in regions.items() if not c.startswith("US") and r.get("electricity")]
    if len(europe) < MIN_EUROPE:
        raise SystemExit(f"electricity for only {len(europe)} countries in Europe; not writing a partial file")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--cache", type=Path, help="keep and reuse raw answers in this directory")
    args = ap.parse_args()
    key = os.environ.get("EIA_API_KEY") or "DEMO_KEY"
    if key == "DEMO_KEY":
        print("EIA_API_KEY not set; using DEMO_KEY (rate limited)", file=sys.stderr)
    data = build(Fetcher(args.cache), key)
    check_complete(data)
    regions = data["regions"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    n = {fuel: sum(1 for r in regions.values() if fuel in r) for fuel in ("electricity", "petrol")}
    print(f"{args.out}: {len(regions)} regions, {n['electricity']} with electricity, {n['petrol']} with petrol, {args.out.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
