"""Home follows the icon: the name only sets the default icon. The one-time migration keeps
every place that was home at home and makes the newly home ones home, with their charges
priced again; merging and moving a place queue the charges that changed place."""

from datetime import datetime, timezone

import pytest

from facts import place_sessions, queue_reprice
from nowpage import today_line
from notify import plug_in_reminder
from places import HOME_MIGRATION, car_at_home, clamp_radius, home_changes, is_home, migrate_home, parents, place_for, resolve_merged

SPRINGFIELD = (39.7817, -89.6501)


def place(pid, name, icon=None, auto=False, lat=SPRINGFIELD[0], lon=SPRINGFIELD[1], r=150.0, **kw):
    return {"id": pid, "name": name, "icon": icon, "auto": auto, "latitude": lat, "longitude": lon, "radius_m": r, "merged_into": None, **kw}


@pytest.mark.parametrize(
    "p,home",
    [
        (place(1, "SF Home", "home"), True),
        (place(2, "SF Home"), True),  # nobody chose an icon: the name's guess
        (place(3, "Home", "star"), False),  # the owner chose another icon: not home
        (place(4, "Cabin", "home"), True),  # the house, whatever the name
        (place(5, "Home Depot"), False),
        (place(6, "Home Ranch Road, Springfield", auto=True), False),  # found from history, named by its street
        (place(7, "Home", "none"), False),
        (None, False),
    ],
)
def test_home_follows_the_icon(p, home):
    assert is_home(p) is home


def test_the_owners_case_only_home_is_home_today_and_sf_home_becomes_home():
    """Live: Home (house icon) is home, SF Home was given the house by hand but its name did
    not start with the word, so it was not home. After: both are, SF Home's charges are
    queued, Home's are not touched."""
    rows = [place(1, "Home", "home"), place(2, "Daycare", "pin"), place(3, "SF Home", "home"), place(4, "Safeway", "shop"), place(5, "Work (Alpha HQ)", "work")]
    give, changed = home_changes(rows)
    assert give == [] and [r["id"] for r in changed] == [3]


def test_a_place_home_today_stays_home_whatever_its_icon():
    rows = [place(1, "Home", "star"), place(2, "home 2", "none"), place(3, "Home (cabin)"), place(4, "Bravo Market", "home")]
    give, changed = home_changes(rows)
    assert give == [1, 2], "home by the old rule keeps being home: it gets the house"
    assert [r["id"] for r in changed] == [4], "the house icon makes a place home"
    assert is_home(place(3, "Home (cabin)")), "nothing to do: the guess says home"


class Conn:
    """cc_places and cc_state, enough for migrate_home."""

    def __init__(self, rows):
        self.rows, self.state, self.sql = rows, {}, []

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        self.sql.append(s)
        if s.startswith("SELECT 1 FROM cc_state"):
            return Res([{"?": 1}] if args[0] in self.state else [])
        if s.startswith("SELECT id, name, latitude, longitude, radius_m, auto, icon, merged_into FROM cc_places"):
            return Res([dict(r) for r in self.rows])
        if s.startswith("UPDATE cc_places SET icon = 'home' WHERE id = ANY(%s)"):
            for r in self.rows:
                if r["id"] in args[0]:
                    r["icon"] = "home"
            return Res([])
        if s.startswith("INSERT INTO cc_state"):
            self.state.setdefault(args[0], args[1])
            return Res([])
        raise AssertionError(s)

    def commit(self):
        pass


class Res:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def test_the_migration_runs_once_and_keeps_todays_home():
    rows = [place(1, "Home", "none"), place(2, "SF Home"), place(3, "Daycare", "pin")]
    before = [is_home(r) or r["name"] == "Home" for r in rows]
    queued = []
    conn = Conn(rows)
    assert migrate_home(conn, lambda c, p: queued.append(p["id"])) == 3
    assert [r["icon"] for r in rows] == ["home", "home", "pin"], "Home keeps being home; SF Home shows the house it now is"
    assert [is_home(r) for r in rows] == [True, True, False] and before[0]
    assert queued == [2], "only the place whose home status changed is priced again"
    assert HOME_MIGRATION in conn.state
    assert migrate_home(conn, lambda c, p: queued.append(p["id"])) == 0 and queued == [2], "once"


def test_a_car_is_home_by_the_place_it_is_at():
    assert car_at_home({"place": "SF Home", "at_home": True})
    assert not car_at_home({"place": "Home", "at_home": False}), "the site's answer wins over the name"
    assert car_at_home({"place": "SF Home"}), "a dict with only a name takes the name's default"
    now = datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc)
    car = {"name": "Alpha", "plugged": False, "status": "Idle", "soc": 30.0, "place": "SF Home", "at_home": True}
    assert plug_in_reminder(car, now, None) is not None, "SF Home is home: the plug-in reminder goes out there"
    assert plug_in_reminder({**car, "place": "Home", "at_home": False}, now, None) is None
    lines = today_line([{"vin": "V1", "name": "Alpha", "place": "SF Home", "at_home": True}, {"vin": "V2", "name": "Bravo", "place": "Home", "at_home": False}], {})
    assert [x["text"] for x in lines] == ["stayed home", "stayed put"]


def test_a_merged_place_answers_as_the_one_it_joined():
    rows = [place(1, "Charlie School", "school", price={"kind": "free"}), place(2, "Charlie School lot", lat=39.7900, merged_into=1, visits=40), place(3, "Bravo Market", "shop", lat=39.80)]
    rows[0]["visits"] = 100
    circles = resolve_merged(rows)
    hit = place_for(39.7900, SPRINGFIELD[1], circles)
    assert hit["id"] == 1 and hit["name"] == "Charlie School" and hit["icon"] == "school" and hit["price"] == {"kind": "free"} and hit["part"] == 2
    assert hit["latitude"] == 39.7900, "its own circle"
    tops = parents(rows)
    assert [t["id"] for t in tops] == [1, 3] and tops[0]["visits"] == 140 and tops[0]["parts"][0]["id"] == 2


def test_an_orphaned_part_is_left_out():
    assert [r["id"] for r in resolve_merged([place(2, "lot", merged_into=9)])] == []


def test_the_radius_is_kept_sensible():
    assert clamp_radius(5) == 25 and clamp_radius(150) == 150 and clamp_radius(10_000) == 1000


class Db:
    """charging_sessions and cc_state for place_sessions and queue_reprice."""

    def __init__(self, sessions):
        self.sessions, self.state = sessions, {}

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT id, session_position_latitude"):
            a, b, c, d = args
            return Res([{"id": i, "lat": la, "lon": lo} for i, la, lo in self.sessions if a <= la <= b and c <= lo <= d])
        if s.startswith("SELECT value FROM cc_state"):
            return Res([{"value": self.state[args[0]]}] if args[0] in self.state else [])
        if s.startswith("INSERT INTO cc_state"):
            self.state[args[0]] = args[1]
            return Res([])
        raise AssertionError(s)


def test_a_moved_circle_gives_back_every_charge_inside_it():
    """Shrinking Home: a charge now in the Bravo Market circle next door changed place too,
    so the old circle (`every`) takes every charge in it, not only unclaimed ones."""
    home = place(1, "Home", "home", r=300.0)
    market = place(2, "Bravo Market", "shop", lat=SPRINGFIELD[0] + 0.0018)
    db = Db([(10, SPRINGFIELD[0], SPRINGFIELD[1]), (11, SPRINGFIELD[0] + 0.0018, SPRINGFIELD[1])])
    now = [{**home, "radius_m": 100.0}, market]
    assert place_sessions(db, home, now) == [10], "as before: only Home's own and the unclaimed"
    assert place_sessions(db, {**home, "every": True}, now) == [10, 11]


def test_the_queue_keeps_both_circles_of_a_move():
    import json

    db = Db([])
    home = place(1, "Home", "home")
    queue_reprice(db, home, every=True)
    queue_reprice(db, {**home, "latitude": 39.79})
    queue_reprice(db, {**home, "latitude": 39.79})  # the same circle again is one entry
    held = json.loads(db.state["reprice_places"])
    assert len(held) == 2 and held[0]["every"] is True and "every" not in held[1]


def test_the_live_trio():
    """The install this was built for: only Home is home today; SF Home (the house icon,
    chosen by hand) becomes home; Evergreen Home Ranch Road, with no icon, is a road."""
    rows = [
        place(1, "Home", "home"),
        place(2, "SF Home", "home"),
        place(3, "Evergreen Home Ranch Road"),
    ]
    assert [is_home(r) for r in rows] == [True, True, False]
    give, changed = home_changes(rows)
    assert give == [] and [r["id"] for r in changed] == [2]


def test_the_migration_says_what_moved(caplog):
    rows = [
        place(1, "Home", "none"),
        place(2, "SF Home", "home"),
        place(3, "Evergreen Home Ranch Road"),
    ]
    with caplog.at_level("INFO", logger="carcommand.places"):
        migrate_home(Conn(rows), lambda c, p: None)
    said = [r.getMessage() for r in caplog.records]
    assert said == [
        "places: 'Home' (id 1) stays home and gets the house icon",
        "places: 'SF Home' (id 2) is home now (icon home); its charges are priced again",
    ]


def test_a_charge_priced_at_nothing_is_reopened_when_its_place_is_queued():
    """A charge at a place that becomes home was looked at and priced at nothing
    (cost_source 'none'). Clearing the place's prices clears that too, and a cleared charge
    with energy is one fill_charge_facts picks up again, now as a home charge."""
    import inspect

    import facts

    assert "cost_source = NULL" in facts.CLEAR_PRICE and "cost_source IS DISTINCT FROM 'hand'" in inspect.getsource(facts.clear_prices)
    assert "OR (f.cost IS NULL AND f.cost_source IS NULL AND COALESCE(f.kwh, 0) > 0))" in inspect.getsource(facts.fill_charge_facts)
    assert "at_home = EXCLUDED.at_home" in inspect.getsource(facts.fill_charge_facts), "and takes its home status as it is now"
