"""scripts/build-rate-averages.py against recorded answers: EIA's pages, Eurostat's
JSON-stat and a minimal Weekly Oil Bulletin workbook. No network."""

import importlib.util
import io
import json
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build-rate-averages.py"
spec = importlib.util.spec_from_file_location("build_rate_averages", SCRIPT)
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)

HOSTS = {"api.eia.gov", "ec.europa.eu", "energy.ec.europa.eu"}


class Recorded:
    """A Fetcher that answers from a function of the URL and keeps every URL asked."""

    def __init__(self, answer):
        self.answer, self.urls = answer, []

    def get(self, url, secret=None):
        self.urls.append(url)
        return self.answer(url)


def eia(url):
    q = parse_qs(urlparse(url).query)
    offset = int(q["offset"][0])
    if "retail-sales" in url:
        rows = [{"period": "2026-07", "stateid": "IL", "price": "19.22"}, {"period": "2026-06", "stateid": "IL", "price": "19.89"}, {"period": "2026-07", "stateid": "CA", "price": "33.61"}]
    else:
        rows = [{"period": "2026-09", "duoarea": "R20", "value": "4.166"}, {"period": "2026-09", "duoarea": "SCA", "value": "5.924"}, {"period": "2026-09", "duoarea": "NUS", "value": "4.355"}]
    page = rows[offset : offset + 2]  # two rows a page, so paging is exercised
    return json.dumps({"response": {"total": str(len(rows)), "data": page}}).encode()


def test_eia_pages_through_and_states_take_their_own_series_or_their_district():
    f = Recorded(eia)
    power = B.us_electricity(f, "KEY")
    assert power["US-IL"]["values"] == {"2026-06": 0.1989, "2026-07": 0.1922} and power["US-IL"]["unit"] == "kWh" and power["US-CA"]["as_of"] == "2026-07"
    petrol = B.us_petrol(f, "KEY")
    assert petrol["US-IL"]["area"] == "Midwest (PADD 2)" and petrol["US-CA"]["values"]["2026-09"] == 5.924 and petrol["US"]["values"]["2026-09"] == 4.355
    assert petrol["US-WY"]["area"] == "United States", "a district with no figures falls back to the national series"
    assert len(f.urls) == 4 and all(urlparse(u).hostname in HOSTS for u in f.urls)


def test_eurostat_half_years_become_six_months_each_in_the_countrys_currency():
    def answer(url):
        cur = parse_qs(urlparse(url).query)["currency"][0]
        geo = {"DE": 0, "EL": 1, "EA": 2, "PL": 3}
        values = {"0": 0.3835, "1": 0.3869, "2": 0.21, "3": 0.22, "4": 0.30, "5": 0.31, "6": 1.10, "7": 1.1505} if cur == "EUR" else {"6": 1.10, "7": 1.1505}
        return json.dumps({"dimension": {"geo": {"category": {"index": geo}}, "time": {"category": {"index": {"2025-S1": 0, "2025-S2": 1}}}}, "value": values}).encode()

    got = B.eu_electricity(Recorded(answer))
    assert set(got) == {"DE", "GR", "PL"}, "the euro area aggregate is not a country"
    assert got["DE"]["values"]["2025-01"] == 0.3835 and got["DE"]["values"]["2025-12"] == 0.3869 and len(got["DE"]["values"]) == 12
    assert got["PL"]["currency"] == "PLN" and got["PL"]["values"]["2025-07"] == 1.1505 and got["DE"]["currency"] == "EUR"


def workbook(rows: list[list]) -> bytes:
    """The smallest xlsx the reader accepts: a "Prices with taxes" sheet of inline numbers
    and shared strings."""
    strings = sorted({c for r in rows for c in r if isinstance(c, str)})
    col = lambda i: "ABCDEFGHIJ"[i]  # noqa: E731
    cells = []
    for n, r in enumerate(rows, 1):
        cs = "".join(f'<c r="{col(i)}{n}" t="s"><v>{strings.index(v)}</v></c>' if isinstance(v, str) else f'<c r="{col(i)}{n}"><v>{v}</v></c>' for i, v in enumerate(r) if v is not None)
        cells.append(f'<row r="{n}">{cs}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} xmlns:r="{rel}"><sheets><sheet name="Prices with taxes" sheetId="2" r:id="rId1"/></sheets></workbook>')
        z.writestr(
            "xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
        )
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{''.join(cells)}</sheetData></worksheet>")
    return buf.getvalue()


def test_the_oil_bulletin_is_averaged_by_month_per_litre_in_the_countrys_currency():
    header = ["Consumer prices", "CTR", "DE_price_with_tax_euro95", "CTR", "PL_exchange_rate", "PL_price_with_tax_euro95"]
    # 46265 is 2026-08-31, 46272 and 46279 are Sep 7 and 14 (spreadsheet day numbers)
    rows = [header, ["Date", None, "1000 l"], [46279, "DE_", 2400, "PL_", 0.25, 2000], [46272, "DE_", 2300, "PL_", 0.25, 1900], [46265, "DE_", 2200, "PL_", 0.25, 1800]]
    xlsx = workbook(rows)
    got = B.eu_petrol(Recorded(lambda url: b"<html>no link here</html>" if url.endswith("_en") else xlsx))
    assert got["DE"]["values"] == {"2026-08": 2.2, "2026-09": 2.35} and got["DE"]["unit"] == "L"
    assert got["PL"]["values"]["2026-09"] == pytest.approx((2.0 + 1.9) / 2 / 0.25) and got["PL"]["currency"] == "PLN"


def test_the_requests_carry_no_location():
    f = Recorded(eia)
    B.us_electricity(f, "KEY")
    B.us_petrol(f, "KEY")
    for u in f.urls:
        q = parse_qs(urlparse(u).query)
        assert not {"lat", "lon", "latitude", "longitude", "zip", "address"} & set(q) and urlparse(u).hostname in HOSTS


def test_the_default_output_is_the_copy_the_image_bundles():
    assert B.OUT == Path(__file__).resolve().parent / "rates" / "averages.json"


def http_error(code, body):
    import urllib.error

    return urllib.error.HTTPError("https://api.eia.gov/v2/x", code, "limited", {}, io.BytesIO(body))


def test_a_rate_limit_is_waited_out_and_then_fails_loudly(monkeypatch):
    waits, answers = [], [http_error(429, b"OVER_RATE_LIMIT"), http_error(403, b'{"error":{"code":"OVER_RATE_LIMIT"}}')]

    class Body:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def urlopen(req, timeout=0):
        if answers:
            raise answers.pop(0)
        return Body()

    def always(code, body):
        def urlopen(req, timeout=0):
            raise http_error(code, body)

        return urlopen

    monkeypatch.setattr(B.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(B.time, "sleep", waits.append)
    assert B.Fetcher(None).get("https://api.eia.gov/v2/x") == b"ok" and len([w for w in waits if w >= 60]) == 2, "a 429 and a rate-limit 403 are each waited out"
    monkeypatch.setattr(B.urllib.request, "urlopen", always(429, b"OVER_RATE_LIMIT"))
    with pytest.raises(SystemExit, match="429"):
        B.Fetcher(None).get("https://api.eia.gov/v2/x")
    waits.clear()
    monkeypatch.setattr(B.urllib.request, "urlopen", always(403, b"API_KEY_INVALID"))
    with pytest.raises(SystemExit, match="403"):
        B.Fetcher(None).get("https://api.eia.gov/v2/x")
    assert not [w for w in waits if w >= 60], "a bad key is not a rate limit: no waiting"


def test_a_short_file_is_refused_not_written():
    def series(v):
        return {"area": "x", "source": "s", "unit": "kWh", "currency": "USD", "as_of": "2026-07", "values": {"2026-07": v}}

    whole = {f"US-{s}": {"electricity": series(0.2), "petrol": series(4.0)} for s in B.STATES} | {"US": {"electricity": series(0.2), "petrol": series(4.0)}}
    whole |= {c: {"electricity": series(0.3), "petrol": series(1.9)} for c in list(B.COUNTRY_NAMES)[:30]}
    B.check_complete({"regions": whole})
    short = dict(whole)
    del short["US-WY"]
    with pytest.raises(SystemExit, match="US-WY"):
        B.check_complete({"regions": short})
    few = {k: v for k, v in whole.items() if k.startswith("US")}
    with pytest.raises(SystemExit, match="Europe"):
        B.check_complete({"regions": few})
