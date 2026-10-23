"""Supercharger bills: Tesla's charging history read in the background with the token in
a header, stored, matched to our sessions by car and start time, and first in the chain;
the invoice PDF only through the site. No network: Tesla answers from a recorded-looking
page in the shape its Fleet API documents and the open clients read."""

import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

import billed
import tesla_fleet
from facts import charge_facts
from tesla_fleet import Fleet
from test_empty import bare, client  # noqa: F401 - fixtures

ALPHA = "5YJ3E1EA0SF000001"
CHI = ZoneInfo("America/Chicago")
T0 = datetime(2026, 9, 20, 18, 2, 11, tzinfo=timezone.utc)

# One page as Tesla sends it: two sessions, one with an idle fee, and the paging fields.
PAGE = {
    "data": [
        {
            "sessionId": 3151234,
            "vin": ALPHA,
            "siteLocationName": "Springfield, IL - Wabash Ave",
            "chargeStartDateTime": "2026-09-20T13:04:40-05:00",
            "chargeStopDateTime": "2026-09-20T13:31:02-05:00",
            "unlatchDateTime": "2026-09-20T13:33:15-05:00",
            "countryCode": "US",
            "billingType": "IMMEDIATE",
            "vehicleMakeType": "TSLA",
            "fees": [
                {
                    "sessionFeeId": 9001,
                    "feeType": "CHARGING",
                    "currencyCode": "USD",
                    "pricingType": "PAYMENT",
                    "rateBase": 0.46,
                    "rateTier1": 0,
                    "rateTier2": 0,
                    "usageBase": 41.2,
                    "usageTier1": 0,
                    "usageTier2": 0,
                    "totalBase": 18.95,
                    "totalTier1": 0,
                    "totalTier2": 0,
                    "totalDue": 18.95,
                    "netDue": 18.95,
                    "uom": "kwh",
                    "isPaid": True,
                    "status": "PAID",
                },
                {
                    "sessionFeeId": 9002,
                    "feeType": "PARKING",
                    "currencyCode": "USD",
                    "pricingType": "PAYMENT",
                    "rateBase": 0.5,
                    "usageBase": 2,
                    "totalDue": 1.0,
                    "netDue": 1.0,
                    "uom": "min",
                    "isPaid": True,
                    "status": "PAID",
                },
            ],
            "invoices": [{"fileName": "SC-0001-US.pdf", "contentId": "c0ffee-1234", "invoiceType": "IMMEDIATE"}],
        },
        {
            "sessionId": 3159999,
            "vin": ALPHA,
            "siteLocationName": "Bloomington, IL",
            "chargeStartDateTime": "2026-09-27T10:00:00Z",
            "chargeStopDateTime": "2026-09-27T10:25:00Z",
            "fees": [{"feeType": "CHARGING", "currencyCode": "USD", "usageBase": 30, "uom": "kwh", "totalDue": 12.6}],
            "invoices": [],
        },
        {"sessionId": None, "vin": ALPHA, "chargeStartDateTime": "2026-09-28T10:00:00Z", "fees": []},
    ],
    "totalResults": 3,
    "hasMoreData": False,
}


def jwt(scopes) -> str:
    part = base64.urlsafe_b64encode(json.dumps({"scp": scopes, "aud": ["https://fleet-api.prd.na.vn.cloud.tesla.com"]}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{part}.sig"


class Fake:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self, method, url, *, headers, data=None, json=None):
        self.sent.append((method, url, headers))
        return self.answers.pop(0)


# --- Tesla's answers -----------------------------------------------------------------------


def test_a_page_of_history_becomes_bills_with_every_fee_and_the_invoice():
    rows, more = billed.parse_page(PAGE)
    assert more is False and len(rows) == 2, "an item with no id or fees is skipped"
    a = rows[0]
    assert a["id"] == "3151234" and a["vin"] == ALPHA and a["site"] == "Springfield, IL - Wabash Ave"
    assert a["started_at"] == T0.replace(second=0) + timedelta(minutes=2, seconds=40) and a["started_at"].utcoffset() is not None
    assert a["total"] == 19.95 and a["kwh"] == 41.2 and a["currency"] == "USD" and a["invoice_id"] == "c0ffee-1234"
    assert rows[1]["invoice_id"] is None and rows[1]["total"] == 12.6
    assert billed.parse_page({"response": PAGE}) == ([], False), "parse_page reads the page; the client unwraps 'response'"
    assert billed.parse_page({"data": [PAGE["data"][1]] * 50}) == ([billed.parse_item(PAGE["data"][1])] * 50, True), "a full page with no flag may have more"


def test_history_is_asked_for_one_car_a_page_at_a_time_with_the_token_in_a_header():
    fake = Fake((200, {"response": PAGE}))
    f = Fleet("client-alpha", "ta-secret.springfield", call=fake)
    got = f.charging_history("acc-token", tesla_fleet.NA, ALPHA, start="2026-09-01T00:00:00+00:00", page=2, size=50)
    assert got == PAGE, "a page wrapped in 'response' is unwrapped"
    method, url, headers = fake.sent[0]
    q = parse_qs(urlparse(url).query)
    assert method == "GET" and urlparse(url).path == "/api/1/dx/charging/history"
    assert q == {"vin": [ALPHA], "pageNo": ["2"], "pageSize": ["50"], "sortOrder": ["ASC"], "startTime": ["2026-09-01T00:00:00+00:00"]}
    assert headers == {"Authorization": "Bearer acc-token"} and "acc-token" not in url


def test_the_invoice_is_a_pdf_or_a_tesla_error():
    seen = []

    def raw(method, url, *, headers):
        seen.append((url, headers))
        return 200, "application/pdf", b"%PDF-1.7 springfield"

    f = Fleet("client-alpha", "ta-secret.springfield", raw=raw)
    assert f.charging_invoice("acc-token", tesla_fleet.NA, "c0ffee-1234").startswith(b"%PDF")
    assert seen[0][0].endswith("/api/1/dx/charging/invoice/c0ffee-1234") and seen[0][1] == {"Authorization": "Bearer acc-token"}
    assert f.charging_invoice("t", tesla_fleet.NA, "../x") and seen[-1][0].endswith("/invoice/..%2Fx"), "an id cannot walk the path"
    with pytest.raises(tesla_fleet.TeslaError):
        Fleet("c", "s", raw=lambda m, u, headers: (200, "text/html", b"<html>")).charging_invoice("t", tesla_fleet.NA, "x")
    with pytest.raises(tesla_fleet.TeslaError):
        Fleet("c", "s", raw=lambda m, u, headers: (403, "application/json", b"{}")).charging_invoice("t", tesla_fleet.NA, "x")


def test_the_charging_scope_is_asked_for_and_read_back_from_the_token():
    assert tesla_fleet.BILLING_SCOPE in tesla_fleet.SCOPES.split()
    assert tesla_fleet.BILLING_SCOPE in tesla_fleet.granted_scopes(jwt(["openid", "vehicle_device_data", "vehicle_charging_cmds"]))
    assert tesla_fleet.granted_scopes(jwt("openid vehicle_charging_cmds")) == {"openid", "vehicle_charging_cmds"}
    assert tesla_fleet.BILLING_SCOPE not in tesla_fleet.granted_scopes(jwt(["openid", "vehicle_device_data"]))
    assert tesla_fleet.granted_scopes("not-a-jwt") == set() and tesla_fleet.granted_scopes(None) == set()


# --- matching and pricing ------------------------------------------------------------------


def session(start=T0, sid=40, kind="DC", **kw):
    return {
        "id": sid,
        "vin": ALPHA,
        "session_start_date": start,
        "session_end_date": start + timedelta(minutes=27),
        "session_position_latitude": 39.80,
        "session_position_longitude": -89.65,
        "real_charged": 40.0,
        "charging_type": kind,
        "real_cost": None,
        **kw,
    }


BILLS = billed.parse_page(PAGE)[0]


def test_a_bill_matches_by_car_and_start_within_ten_minutes_nearest_first():
    assert billed.match(session(T0 + timedelta(minutes=9)), BILLS)["id"] == "3151234"
    assert billed.match(session(T0 + timedelta(minutes=13)), BILLS) is None
    assert billed.match(session(T0, vin="LRW3E7EK0PC000002"), BILLS) is None, "another car's bill"
    near = dict(BILLS[0], id="near", started_at=T0 + timedelta(minutes=2, seconds=5))  # 5 s off; the other bill is 29 s
    assert billed.match(session(T0 + timedelta(minutes=2)), [BILLS[0], near])["id"] == "near"
    far = dict(BILLS[0], latitude=39.10, longitude=-89.65)
    assert billed.match(session(), [far]) is None, "both have a position and they are 78 km apart"


def test_a_bill_prices_the_charge_before_anything_else():
    away = lambda a, b: False  # noqa: E731
    priced_place = {"id": 3, "name": "Wabash", "price": {"kind": "free"}}
    (row,) = charge_facts([session()], [], CHI, away, dc_price=0.56, sc_price=0.40, place_of=lambda a, b: priced_place, bills=BILLS)
    assert row[2] == 19.95 and row[9] == "billed" and row[3] == "Billed by Tesla, Springfield, IL - Wabash Ave"
    rows = charge_facts([session(sid=1), session(T0 + timedelta(minutes=1), sid=2)], [], CHI, away, dc_price=0.56, bills=BILLS)
    assert [r[9] for r in rows] == ["billed", "dc"], "one bill prices one charge"


# --- the background read ---------------------------------------------------------------------


class Db:
    """cc_state, cc_billed_charges and the session lookup, in memory."""

    def __init__(self, sessions=()):
        self.state, self.bills, self.sessions, self.commits = {}, {}, list(sessions), 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        self.commits += 1

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        rows = []
        if s.startswith("SELECT value FROM cc_state"):
            rows = [{"value": self.state[args[0]]}] if args[0] in self.state else []
        elif s.startswith("INSERT INTO cc_state"):
            self.state[args[0]] = args[1]
        elif s.startswith("SELECT MAX(started_at)"):
            got = [b["started_at"] for b in self.bills.values() if b["vin"] == args[0]]
            rows = [{"t": max(got) if got else None}]
        elif s.startswith("INSERT INTO cc_billed_charges"):
            old = self.bills.get(args["id"])
            if old is None or old["total"] != args["total"]:
                self.bills[args["id"]] = dict(args)
                rows = [{"id": args["id"]}]
        elif s.startswith("SELECT DISTINCT c.id FROM cc_billed_charges"):
            starts = [self.bills[i]["started_at"] for i in args[0]]
            rows = [{"id": sid} for sid, at in self.sessions if any(abs((at - b).total_seconds()) <= 600 for b in starts)]
        else:
            raise AssertionError(s)

        class R:
            def fetchone(self):
                return rows[0] if rows else None

            def fetchall(self):
                return rows

        return R()


class Account:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.asked = []

    def charging_history(self, token, base, vin, start=None, page=1, size=50, order="ASC"):
        self.asked.append((vin, start, page))
        return self.pages.pop(0)


def worker(db, account, token, cleared):
    db.state.setdefault(billed.BASE_KEY, "1")  # learned already (test_place_bills learns it)
    return billed.BilledWorker(connect=lambda: db, access=lambda: (token, tesla_fleet.NA), fleet=lambda: account, vins=lambda conn: [ALPHA], clear=lambda conn, ids: cleared.extend(ids))


def test_without_the_scope_tesla_is_not_asked():
    db, account, cleared = Db(), Account(), []
    assert worker(db, account, jwt(["openid", "vehicle_device_data"]), cleared).round(T0) == "no scope"
    assert account.asked == [] and not db.bills


def test_a_read_stores_the_bills_reprices_their_sessions_and_waits_six_hours():
    db, cleared = Db([(40, T0 + timedelta(minutes=3)), (41, T0 + timedelta(days=3))]), []
    first = dict(PAGE, hasMoreData=True, data=PAGE["data"][:1])
    second = dict(PAGE, hasMoreData=False, data=PAGE["data"][1:])
    account = Account(first, second)
    w = worker(db, account, jwt(["vehicle_charging_cmds"]), cleared)
    assert w.round(T0 + timedelta(days=10)) == "2 new"
    assert [a[2] for a in account.asked] == [1, 2] and account.asked[0][1] is None, "every page, from the start the first time"
    assert set(db.bills) == {"3151234", "3159999"} and cleared == [40]
    assert w.round(T0 + timedelta(days=10, hours=5)) is None, "not again within six hours"
    account.pages = [PAGE]
    assert w.round(T0 + timedelta(days=10, hours=6)) == "0 new" and cleared == [40], "the same bills again change nothing"
    assert account.asked[-1][1] == (datetime(2026, 9, 27, 10, tzinfo=timezone.utc) - billed.OVERLAP).isoformat(), "later reads start just before the newest bill"


def test_a_failed_read_still_waits_six_hours():
    class Down(Account):
        def charging_history(self, *a, **k):
            raise tesla_fleet.TeslaError("Tesla refused (403): missing scopes", 403)

    db = Db()
    w = worker(db, Down(), jwt(["vehicle_charging_cmds"]), [])
    assert w.round(T0) == "failed" and billed.TRIED in db.state and billed.STAMP not in db.state
    assert w.round(T0 + timedelta(hours=1)) is None


# --- the site ------------------------------------------------------------------------------


def test_the_invoice_route_names_only_known_invoices_and_never_shows_the_token(bare, client, monkeypatch):  # noqa: F811
    assert client.get("/dx/charging/invoice/c0ffee-1234").status_code == 404, "an invoice no bill names is never asked for"

    class Known(Db):
        def execute(self, sql, args=()):
            if "FROM cc_billed_charges WHERE invoice_id" in sql:

                class R:
                    def fetchone(self):
                        return {"?column?": 1}

                return R()
            return super().execute(sql, args)

    monkeypatch.setattr(bare, "db", lambda: Known())
    monkeypatch.setattr(bare, "tesla_access", lambda: ("acc-token-secret", tesla_fleet.NA))
    monkeypatch.setattr(bare, "tesla_app", lambda: Fleet("c", "s", raw=lambda m, u, headers: (200, "application/pdf", b"%PDF-1.7 bill")))
    r = client.get("/dx/charging/invoice/c0ffee-1234")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert "acc-token-secret" not in r.text and "private" in r.headers["cache-control"]


def test_the_connect_page_says_what_the_scope_allows_and_asks_an_old_sign_in_to_sign_in_again():
    from pathlib import Path

    html = (Path(__file__).parent / "templates" / "settings_tesla.html").read_text()
    assert "vehicle_charging_cmds" in html and "sent only from Settings › Commands" in html
    assert "Sign in again to grant it" in html
