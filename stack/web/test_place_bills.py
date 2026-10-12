"""Per-place prices and Tesla bills: the charge page shows the stored
price, Home is a whole word, a typed-in price beats a bill, and the price inputs do not
zoom a phone."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import billed
from facts import charge_facts
from places import is_home, is_home_name
from test_empty import bare  # noqa: F401 - fixture

HERE = Path(__file__).parent
CHI = ZoneInfo("America/Chicago")
T0 = datetime(2026, 9, 20, 18, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize("name", ["Home", "home", "home 2", "Home (cabin)", "Home · Springfield", "HOME-2", "SF Home", "Home Springfield", "My Home", "Mom's home"])
def test_these_are_home(name):
    assert is_home_name(name) and is_home({"name": name})


@pytest.mark.parametrize(
    "name",
    ["Homewood Suites", "Home Depot", "HomeGoods", "Home Goods", "Home2 Suites", "Homeward", "Evergreen Nursing Home", "12 Home Ave", "Evergreen Home Ranch Road", "Mountain Home", "Work", "", None],
)
def test_these_are_not_home(name):
    """Home as a whole word anywhere, but not a business or a street with Home in its name,
    and not running on ("HomeGoods", "Homeward")."""
    assert not is_home_name(name) and not is_home({"name": name})


def test_a_places_page_takes_home_from_python_not_from_jinja():
    html = (HERE / "templates" / "place.html").read_text()
    assert "startswith('home')" not in html and "home=p.home" in html


def test_the_charge_page_shows_a_home_places_own_price(bare):  # noqa: F811
    """Home set to Free: the list says $0.00 Free at Home, and so must the charge's page."""
    calls = []
    session = {
        "cost": 0.0,
        "cost_source": "place",
        "rate_plan": "Free at Home",
        "at_home": True,
        "by_hand": False,
        "period_kwh": None,
        "session_position_latitude": 39.8,
        "session_position_longitude": -89.64,
    }
    bare.detail_cost(session, lambda: calls.append(1) or {"cost": 9.99, "plan": "Springfield EV", "kwh": {}, "labels": {}})
    assert session["cost"] == 0.0 and session["rate_plan"] == "Free at Home" and session["cost_source"] == "place" and not calls


def test_the_charge_page_shows_the_stored_plan_split_without_pricing_again(bare):  # noqa: F811
    calls = []
    session = {"cost": 4.2, "cost_source": "plan", "rate_plan": "Springfield EV", "at_home": True, "by_hand": False, "period_kwh": {"off_peak": 12.0, "peak": 1.5}}
    bare.detail_cost(session, lambda: calls.append(1))
    assert session["cost"] == 4.2 and session["cost_parts"] == {"off_peak": 12.0, "peak": 1.5} and not calls


def test_a_charge_still_going_at_home_is_priced_on_the_page(bare):  # noqa: F811
    session = {"cost": None, "cost_source": None, "at_home": None, "by_hand": False, "session_position_latitude": 39.8, "session_position_longitude": -89.64}
    bare.detail_cost(session, lambda: {"cost": 1.5, "plan": "Springfield EV", "kwh": {"off_peak": 5.0}, "labels": {"off_peak": "Off-peak"}}, home=lambda a, b: True)
    assert session["cost"] == 1.5 and session["cost_source"] == "plan" and session["cost_parts"] == {"off-peak": 5.0}


def test_what_was_typed_in_beats_a_bill_and_leaves_the_bill_for_another_charge():
    bill = {"id": "77", "vin": "V", "started_at": T0, "site": "Springfield, IL", "total": 19.95, "invoice_id": None}

    def s(sid, start, **kw):
        return {
            "id": sid,
            "session_start_date": start,
            "session_end_date": start + timedelta(minutes=25),
            "session_position_latitude": 39.7,
            "session_position_longitude": -89.6,
            "real_charged": 40.0,
            "charging_type": "DC",
            "real_cost": None,
            **kw,
        }

    rows = charge_facts([s(1, T0, real_cost=21.0), s(2, T0 + timedelta(minutes=3))], [], CHI, lambda a, b: False, bills=[bill])
    assert [(r[9], r[2]) for r in rows] == [("hand", 21.0), ("billed", 19.95)]


def test_the_price_inputs_do_not_zoom_a_phone():
    css = (HERE / "static" / "style.css").read_text()
    rule = re.search(r"\.pp-paid input, \.pp-paid select \{[^}]*\}", css)
    assert rule and "font-size:16px" in rule.group(0)
    assert "font-size:14px" not in re.search(r"\.pp-paid input \{[^}]*\}", css).group(0)


# --- reading the history: page bases, order, a failure partway ---------------------------


class Db:
    def __init__(self):
        self.state, self.bills = {}, {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        rows = []
        if s.startswith("SELECT value FROM cc_state"):
            rows = [{"value": self.state[args[0]]}] if args[0] in self.state else []
        elif s.startswith("INSERT INTO cc_state"):
            self.state[args[0]] = args[1]
        elif s.startswith("INSERT INTO cc_billed_charges"):
            if args["id"] not in self.bills:
                rows = [{"id": args["id"]}]
            self.bills[args["id"]] = dict(args)
        elif s.startswith("SELECT DISTINCT c.id"):
            rows = []
        else:
            raise AssertionError(s)

        class R:
            def fetchone(self):
                return rows[0] if rows else None

            def fetchall(self):
                return rows

        return R()


def item(n):
    at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)
    return {"sessionId": n, "vin": "5YJ3E1EA0SF000001", "chargeStartDateTime": at.isoformat(), "fees": [{"feeType": "CHARGING", "totalDue": 10.0 + n, "uom": "kwh", "usageBase": 30}]}


class Tesla:
    """Seven bills, two to a page; `base` is the number of its first page. A page below the
    base answers with the first page (as a clamping server would); `fail_at` raises on
    that call."""

    def __init__(self, base, fail_at=None, n=7, size=2):
        self.base, self.fail_at, self.n, self.size, self.asked = base, fail_at, n, size, []

    def charging_history(self, token, base, vin, start=None, page=1, size=50, order="ASC"):
        self.asked.append((page, start, order))
        if self.fail_at is not None and len(self.asked) == self.fail_at:
            import tesla_fleet

            raise tesla_fleet.TeslaError("Tesla refused (429): too many requests", 429)
        first = datetime.fromisoformat(start) if start else None
        items = [item(i) for i in range(self.n) if first is None or datetime.fromisoformat(item(i)["chargeStartDateTime"]) >= first]
        k = max(page - self.base, 0)
        chunk = items[k * self.size : (k + 1) * self.size]
        return {"data": chunk, "totalResults": len(items), "hasMoreData": (k + 1) * self.size < len(items)}


def run(db, tesla, now):
    import tesla_fleet
    from test_billed import jwt

    w = billed.BilledWorker(connect=lambda: db, access=lambda: (jwt(["vehicle_charging_cmds"]), tesla_fleet.NA), fleet=lambda: tesla, vins=lambda conn: ["5YJ3E1EA0SF000001"], clear=lambda c, i: None)
    w.page_size = 2
    return w.round(now)


@pytest.mark.parametrize("base", [0, 1])
def test_every_bill_is_read_whichever_number_the_first_page_has(base):
    db, tesla = Db(), Tesla(base)
    assert run(db, tesla, T0) == "7 new"
    assert sorted(db.bills, key=int) == [str(i) for i in range(7)]
    assert db.state[billed.BASE_KEY] == str(base), "decided once and kept"
    assert all(order == "ASC" for _p, _s, order in tesla.asked)
    tesla.asked.clear()
    assert run(db, tesla, T0 + timedelta(hours=7)) == "0 new"
    assert tesla.asked[0][0] == base, "a later read starts at the remembered first page"


def test_a_429_partway_keeps_what_was_read_and_resumes_from_the_same_place():
    db = Db()
    tesla = Tesla(1, fail_at=4)  # pages 0 and 1 to learn the base, page 2, then the 429
    assert run(db, tesla, T0) == "failed"
    held = set(db.bills)
    assert held and len(held) < 7
    assert billed.STAMP not in db.state and not any(k.startswith(billed.FROM_KEY) for k in db.state), "an unfinished read neither counts as done nor moves where the next one starts"
    assert run(db, tesla, T0 + timedelta(hours=1)) is None, "still at most every six hours"
    tesla.fail_at, tesla.asked = None, []
    assert run(db, tesla, T0 + timedelta(hours=6)) == f"{7 - len(held)} new"
    assert tesla.asked[0][1] is None, "the resumed read starts where the failed one did"
    assert len(db.bills) == 7 and billed.STAMP in db.state
