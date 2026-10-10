"""A price for every place: what the owner set on Places (or in the picker) prices the
charges there, after a bill and before the home plan; Home with none set is the home plan,
exactly as before; nothing is assumed from a name; changing a price re-prices only that
place's charges, in the background, a batch at a time."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import placeprice
from averages import Averages
from facts import BATCH, charge_facts, drain_reprices, place_sessions, queue_reprice
from places import PlaceIndex, is_home, place_for
from test_empty import bare  # noqa: F401 - fixture
from test_rates_data import averages_data

pytestmark = pytest.mark.usefixtures("springfield_plan")
CHI = ZoneInfo("America/Chicago")
T0 = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)  # 11pm in Springfield: off-peak
HOME = {"id": 1, "name": "Home", "latitude": 39.8000, "longitude": -89.6400, "radius_m": 150, "price": None}
WORK = {"id": 2, "name": "Work", "latitude": 39.7800, "longitude": -89.6500, "radius_m": 150, "price": None}
GARAGE = {"id": 3, "name": "Springfield Garage", "latitude": 39.7900, "longitude": -89.6450, "radius_m": 100, "price": None}


def at(place, kind="AC", kwh=20.0, minutes=120, sid=5, **kw):
    return {
        "id": sid,
        "session_start_date": T0,
        "session_end_date": T0 + timedelta(minutes=minutes),
        "session_position_latitude": place["latitude"],
        "session_position_longitude": place["longitude"],
        "real_charged": kwh,
        "charging_type": kind,
        "real_cost": None,
        **kw,
    }


def priced(session, *places, **kw):
    idx = PlaceIndex([dict(p) for p in places])
    (row,) = charge_facts([session], [], CHI, lambda a, b: is_home(place_for(a, b, idx)), place_of=lambda a, b: place_for(a, b, idx), **kw)
    return dict(zip(("id", "kwh", "cost", "plan", "home", "off", "part", "peak", "split", "source", "region", "month"), row))


# --- the price itself ---------------------------------------------------------------


def test_a_posted_price_becomes_the_stored_shape():
    assert placeprice.from_form({}) == (False, None), "a form without the line leaves the price alone"
    assert placeprice.from_form({"price_kind": ""}) == (True, None)
    assert placeprice.from_form({"price_kind": "free", "price_rate": "9"}) == (True, {"kind": "free"}), "a rate means nothing unless paid"
    assert placeprice.from_form({"price_kind": "paid", "price_per": "kwh", "price_rate": "0.30", "price_fee": ""}) == (True, {"kind": "kwh", "rate": 0.30})
    assert placeprice.from_form({"price_kind": "paid", "price_per": "minute", "price_rate": "0.25", "price_fee": "1"}) == (True, {"kind": "minute", "rate": 0.25, "fee": 1.0})
    for bad in ({"price_kind": "paid", "price_rate": ""}, {"price_kind": "paid", "price_rate": "abc"}, {"price_kind": "paid", "price_rate": "50"}, {"price_kind": "gratis"}):
        with pytest.raises(placeprice.BadPrice):
            placeprice.from_form(bad)


def test_a_price_reads_as_one_short_phrase():
    assert placeprice.describe(None) == "Not set" and placeprice.describe(None, home=True) == "Home plan"
    assert placeprice.describe({"kind": "kwh", "rate": 0.3, "fee": 1.0}) == "$0.30/kWh + $1.00 a session"
    assert placeprice.describe({"kind": "minute", "rate": 0.25}, currency="€") == "€0.25/min"
    assert placeprice.valid({"kind": "free"}) and not placeprice.valid({"kind": "kwh"}) and not placeprice.valid("free")


# --- pricing a charge by its place ------------------------------------------------------


def test_home_with_no_price_is_the_home_plan_exactly_as_before():
    s = at(HOME)
    idx = PlaceIndex([HOME])
    (before,) = charge_facts([s], [], CHI, lambda a, b: is_home(place_for(a, b, idx)))  # no places priced, as before this existed
    assert priced(s, HOME, WORK) == dict(zip(priced(s, HOME).keys(), before))
    assert priced(s, HOME)["source"] == "plan" and priced(s, HOME)["cost"] > 0
    chosen = priced(s, {**HOME, "price": {"kind": "home"}})
    assert (chosen["cost"], chosen["plan"], chosen["split"], chosen["source"]) == (priced(s, HOME)["cost"], priced(s, HOME)["plan"], priced(s, HOME)["split"], "plan")


def test_a_place_named_work_is_not_assumed_free():
    r = priced(at(WORK), HOME, WORK)
    assert r["cost"] is None and r["source"] == "none"


def test_free_at_work_once_the_owner_says_so():
    r = priced(at(WORK), HOME, {**WORK, "price": {"kind": "free"}})
    assert r["cost"] == 0.0 and r["source"] == "place" and r["plan"] == "Free at Work" and r["home"] is False


def test_a_flat_price_per_kwh_or_per_minute_with_a_session_fee():
    r = priced(at(GARAGE, kwh=20.0), {**GARAGE, "price": {"kind": "kwh", "rate": 0.30, "fee": 1.0}})
    assert r["cost"] == pytest.approx(20 * 0.30 + 1.0) and r["plan"] == "Springfield Garage at 0.30/kWh + 1.00/session"
    r = priced(at(GARAGE, minutes=90), {**GARAGE, "price": {"kind": "minute", "rate": 0.05}})
    assert r["cost"] == pytest.approx(90 * 0.05) and r["source"] == "place"


def test_a_place_price_beats_the_home_plan_and_the_fast_charging_default():
    free_home = priced(at(HOME), {**HOME, "price": {"kind": "free"}})
    assert free_home["cost"] == 0.0 and free_home["source"] == "place" and free_home["home"] is True
    fast = priced(at(GARAGE, kind="DC"), {**GARAGE, "price": {"kind": "kwh", "rate": 0.25}}, dc_price=0.56)
    assert fast["cost"] == pytest.approx(20 * 0.25) and fast["source"] == "place"


def test_what_was_paid_by_hand_beats_the_place():
    r = priced(at(WORK, real_cost=4.2), {**WORK, "price": {"kind": "free"}})
    assert r["cost"] == 4.2 and r["source"] == "hand"


def test_the_average_at_a_place_away_is_still_an_estimate():
    r = priced(at(WORK), {**WORK, "price": {"kind": "average"}}, averages=Averages(averages_data()))
    assert r["source"] == "average" and r["cost"] > 0 and r["region"] == "US-IL"


def test_a_home_plan_place_with_no_plan_falls_through():
    import cost

    cost.use_plan(None)
    r = priced(at(WORK), {**WORK, "price": {"kind": "home"}})
    assert r["cost"] is None and r["source"] == "none"


# --- re-pricing a place's charges ---------------------------------------------------------


class Db:
    """cc_state and the two statements the re-price path sends, against a list of
    sessions; enough of a psycopg connection to watch what it does."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.state = {}
        self.cleared = []  # one list of ids per UPDATE
        self.commits = 0

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        rows, count = [], 0
        if s.startswith("SELECT value FROM cc_state"):
            rows = [{"value": self.state[args[0]]}] if args[0] in self.state else []
        elif s.startswith("INSERT INTO cc_state"):
            self.state[args[0]] = args[1]
        elif s.startswith("DELETE FROM cc_state"):
            self.state.pop(args[0], None)
        elif s.startswith("SELECT id, session_position_latitude"):
            la, lb, oa, ob = args
            rows = [{"id": i, "a": a, "b": b} for i, a, b in self.sessions if la <= a <= lb and oa <= b <= ob]
        elif s.startswith("UPDATE cc_charge_facts SET cost = NULL"):
            assert "cost_source IS DISTINCT FROM 'hand'" in s, "a price typed in with the charge is kept"
            self.cleared.append(list(args[0]))
            count = len(args[0])
        else:
            raise AssertionError(s)

        class R:
            rowcount = count

            def fetchone(self):
                return rows[0] if rows else None

            def fetchall(self):
                return rows

        return R()

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_only_the_charges_the_site_would_label_with_the_place_are_re_priced():
    near = GARAGE["latitude"] + 0.0004, GARAGE["longitude"]  # 45 m from the garage
    sessions = [(1, *near), (2, WORK["latitude"], WORK["longitude"]), (3, GARAGE["latitude"] + 0.0012, GARAGE["longitude"])]  # 3 is 133 m: outside
    inner = {"id": 9, "name": "Bay 4", "latitude": near[0], "longitude": near[1], "radius_m": 30, "price": None}
    db = Db(sessions)
    assert place_sessions(db, GARAGE, PlaceIndex([HOME, WORK, GARAGE])) == [1]
    assert place_sessions(db, GARAGE, PlaceIndex([HOME, WORK, GARAGE, inner])) == [], "a nearer place inside it owns that charge, as on every page"
    assert place_sessions(db, GARAGE, PlaceIndex([HOME, WORK])) == [1], "a removed place takes back what nothing else claims"


def test_a_price_change_is_queued_and_the_background_clears_that_place_in_batches():
    sessions = [(i, WORK["latitude"], WORK["longitude"]) for i in range(1, 2 * BATCH + 51)] + [(9999, HOME["latitude"], HOME["longitude"])]
    db = Db(sessions)
    queue_reprice(db, {**WORK, "price": {"kind": "free"}})
    queue_reprice(db, WORK)  # twice is once
    assert len(json.loads(db.state["reprice_places"])) == 1 and not db.cleared, "the request only queues"
    assert drain_reprices(db, PlaceIndex([HOME, WORK])) == 2 * BATCH + 50
    assert [len(b) for b in db.cleared] == [BATCH, BATCH, 50] and 9999 not in sum(db.cleared, [])
    assert "reprice_places" not in db.state and drain_reprices(db, PlaceIndex([HOME, WORK])) == 0


def test_saving_the_same_price_queues_nothing_and_a_new_one_queues_its_place(bare):  # noqa: F811
    set_place_price = bare.set_place_price

    class Conn(Db):
        def __init__(self):
            super().__init__([])
            self.place = {**WORK, "price": {"kind": "free"}}
            self.writes = []
            self.parts = False

        def execute(self, sql, args=()):
            s = " ".join(sql.split())
            if s.startswith("SELECT id, latitude, longitude, radius_m, price FROM cc_places"):
                place = self.place

                class R:
                    def fetchone(self):
                        return place

                return R()
            if s.startswith("UPDATE cc_places SET price"):
                self.writes.append(args)
                return None
            if s.endswith("FROM cc_places WHERE merged_into = %s"):
                parts = [{**WORK, "id": 9, "merged_into": 2}] if self.parts else []
                return type("R", (), {"fetchall": lambda _s: parts})()
            return super().execute(sql, args)

    conn = Conn()
    assert set_place_price(conn, 2, {"kind": "free"}) is False and not conn.writes and not conn.state
    assert set_place_price(conn, 2, {"kind": "kwh", "rate": 0.3}) is True
    assert json.loads(conn.writes[0][0]) == {"kind": "kwh", "rate": 0.3} and json.loads(conn.state["reprice_places"])[0]["id"] == 2
    assert set_place_price(conn, 2, None) is True and conn.writes[-1] == (None, 2), "clearing it is a change too"
    conn.state.clear()
    conn.parts = True
    assert set_place_price(conn, 2, {"kind": "minute", "rate": 0.2}) is True
    assert sorted(p["id"] for p in json.loads(conn.state["reprice_places"])) == [2, 9], "a place merged into it answers with its price: queued too"
