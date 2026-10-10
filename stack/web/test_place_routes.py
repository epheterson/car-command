"""A place's own page and its routes: the page, Save (name, icon, price, address, where and
how big), Merge with…, Remove, each refused without the form's token; the address
suggestions, which only ever ask the local maps add-on."""

import json
import urllib.request

import pytest

from mapsclient import MapsClient
from test_empty import bare, client  # noqa: F401 - fixtures

ROWS = [
    {
        "id": 5,
        "name": "Alpha Daycare",
        "latitude": 39.80,
        "longitude": -89.65,
        "radius_m": 150.0,
        "auto": False,
        "visits": 40,
        "address": None,
        "user_address": "12 Elm Street, Springfield",
        "icon": "kids",
        "price": None,
        "merged_into": None,
        "parts": [],
    },
    {
        "id": 6,
        "name": "Elm Street, Springfield",
        "latitude": 39.801,
        "longitude": -89.65,
        "radius_m": 120.0,
        "auto": True,
        "visits": 9,
        "address": None,
        "user_address": None,
        "icon": None,
        "price": None,
        "merged_into": None,
        "parts": [],
    },
]


@pytest.fixture
def placed(bare, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "places_detail", lambda: [dict(r) for r in ROWS])
    monkeypatch.setattr(bare, "place_stays", lambda: {})
    monkeypatch.setattr(bare, "geocells", lambda: {})
    return bare


def token(bare):  # noqa: F811
    return bare.auth.csrf_token(bare.AUTH["secret"], "")


def test_a_places_page_has_its_map_its_fields_merge_and_remove(placed, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(placed, "place_stays", lambda: {5: {"points": [], "by_car": {"V1": 40}, "n": 40, "median": None, "usual": None}})
    r = client.get("/places/5")
    assert r.status_code == 200
    assert 'id="pd-map"' in r.text and 'action="/places/5/edit"' in r.text and 'value="12 Elm Street, Springfield"' in r.text
    assert 'href="/places?merge=5"><span class="set-l">Merge with…</span><span class="set-v">Elm Street, Springfield, 111 m</span>' in r.text and 'action="/places/5/delete"' in r.text
    assert "Springfield · 40 visits" in r.text, "the town, from the owner's own address"
    assert client.get("/places/99").status_code == 404


def test_the_add_page_starts_where_it_was_asked_or_among_the_places(placed, client):  # noqa: F811
    r = client.get("/places/new?lat=39.81&lon=-89.64")
    assert r.status_code == 200 and 'name="lat" value="39.81"' in r.text and 'action="/places/new"' in r.text and "Merge with" not in r.text
    r = client.get("/places/new")
    assert 'name="lat" value="39.8"' in r.text, "no car position: the first named place"


@pytest.mark.parametrize("path,form", [("/places/5/edit", {"name": "Bravo"}), ("/places/5/merge", {"other": "6"}), ("/places/5/delete", {})])
def test_every_change_needs_the_forms_token(placed, client, monkeypatch, path, form):  # noqa: F811
    done = []
    for fn in ("rename_place", "merge_places", "delete_place"):
        monkeypatch.setattr(placed, fn, lambda *a, **k: done.append(a) or True)
    assert client.post(path, data=form).status_code == 400 and done == []
    assert client.post(path, data={**form, "csrf_token": token(placed)}).status_code == 303 and len(done) == 1


def test_save_sends_everything_on_the_page_in_one_go(placed, client, monkeypatch):  # noqa: F811
    got = []
    monkeypatch.setattr(placed, "rename_place", lambda *a: got.append(a))
    form = {"csrf_token": token(placed), "name": "  Alpha   Daycare ", "icon": "kids", "address": "14 Elm Street, Springfield", "address_lookup": "Elm Street, Springfield", "price_kind": "free"}
    form.update(latitude="39.8004", longitude="-89.6502", radius_m="5000", moved="1", resized="1")
    r = client.post("/places/5/edit", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/places/5?saved=1"
    pid, name, icon, price, address, where = got[0]
    assert (pid, name, icon) == (5, "Alpha Daycare", "kids") and price == (True, {"kind": "free"}) and address == (True, "14 Elm Street, Springfield")
    assert where == (39.8004, -89.6502, 1000), "the size is kept to the slider's range"
    got.clear()
    client.post("/places/5/edit", data={"csrf_token": token(placed), "name": "Alpha Daycare"})
    assert got[0][2] is placed.KEEP and got[0][5] is None, "no icon or position posted: both left as they are"
    assert client.post("/places/5/edit", data={"csrf_token": token(placed), "latitude": "91", "longitude": "0", "moved": "1"}).status_code == 400
    got.clear()
    client.post("/places/5/edit", data={"csrf_token": token(placed), "name": "Alpha Daycare", "latitude": "39.800001", "longitude": "-89.65", "radius_m": "1000"})
    assert got[0][5] is None, "the page always posts where the pin is: unmarked, it is not a move"
    got.clear()
    client.post("/places/5/edit", data={"csrf_token": token(placed), "latitude": "39.81", "longitude": "-89.66", "radius_m": "300", "moved": "1"})
    assert got[0][5] == (39.81, -89.66, None), "the pin moved, the slider did not: the size is left alone"
    got.clear()
    client.post("/places/5/edit", data={"csrf_token": token(placed), "latitude": "39.81", "longitude": "-89.66", "radius_m": "300", "resized": "1"})
    assert got[0][5] == (None, None, 300), "the slider moved, the pin did not"


def test_merge_refuses_what_cannot_be_merged(placed, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(placed, "merge_places", lambda keep, other: False)
    assert client.post("/places/5/merge", data={"csrf_token": token(placed), "other": "5"}).status_code == 400


class Db:
    """cc_places and cc_state in memory: enough for merge_places, rename_place and delete_place."""

    def __init__(self, rows):
        self.rows, self.state = [dict(r) for r in rows], {}

    def row(self, pid):
        return next((r for r in self.rows if r["id"] == pid), None)

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT id, name, latitude") and s.endswith("WHERE id = %s"):
            r = self.row(args[0])
            return Res([dict(r)] if r else [])
        if s.startswith("SELECT id, name, latitude") and s.endswith("WHERE merged_into = %s"):
            return Res([dict(r) for r in self.rows if r.get("merged_into") == args[0]])
        if s == "UPDATE cc_places SET merged_into = %s WHERE merged_into = %s":
            for r in self.rows:
                if r.get("merged_into") == args[1]:
                    r["merged_into"] = args[0]
            return Res([])
        if s == "UPDATE cc_places SET merged_into = %s, auto = FALSE WHERE id = %s":
            self.row(args[1]).update(merged_into=args[0], auto=False)
            return Res([])
        if s == "UPDATE cc_places SET latitude = %s, longitude = %s, radius_m = %s WHERE id = %s":
            self.row(args[3]).update(latitude=args[0], longitude=args[1], radius_m=args[2])
            return Res([])
        if s == "UPDATE cc_places SET icon = %s WHERE id = %s":
            self.row(args[1])["icon"] = args[0]
            return Res([])
        if s == "DELETE FROM cc_places WHERE id = %s OR merged_into = %s":
            self.rows = [r for r in self.rows if r["id"] != args[0] and r.get("merged_into") != args[1]]
            return Res([])
        if s.startswith("SELECT value FROM cc_state"):
            return Res([{"value": self.state[args[0]]}] if args[0] in self.state else [])
        if s.startswith("INSERT INTO cc_state"):
            self.state[args[0]] = args[1]
            return Res([])
        if s == "SELECT id, latitude, longitude, radius_m, price FROM cc_places WHERE id = %s":
            r = self.row(args[0])
            return Res([dict(r)] if r else [])
        if s == "UPDATE cc_places SET price = %s WHERE id = %s":
            self.row(args[1])["price"] = json.loads(args[0]) if args[0] else None
            return Res([])
        if s == "UPDATE cc_places SET user_address = %s WHERE id = %s":
            self.row(args[1])["user_address"] = args[0]
            return Res([])
        if s == "SELECT id, name, latitude, longitude, radius_m, icon, auto, merged_into FROM cc_places":
            return Res([dict(r) for r in self.rows])
        if s.startswith("INSERT INTO cc_places (name, latitude, longitude, radius_m, auto, icon)"):
            pid = max(r["id"] for r in self.rows) + 1
            name, lat, lon, radius, icon = args
            self.rows.append({"id": pid, "name": name, "latitude": lat, "longitude": lon, "radius_m": radius, "auto": False, "icon": icon, "price": None, "merged_into": None})
            return Res([{"id": pid}])
        if s.startswith("UPDATE cc_places SET name = %s, auto = FALSE"):
            r = self.row(args[-1])
            r.update(name=args[0], auto=False, **({"icon": args[1]} if len(args) == 3 else {}))
            return Res([])
        if s == "UPDATE cc_places SET name = %s, auto = FALSE WHERE id = %s":
            return Res([])
        raise AssertionError(s)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def queued(self):
        return json.loads(self.state.get("reprice_places", "[]"))


class Res:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def three():
    return Db([dict(ROWS[0], parts=None), dict(ROWS[1], parts=None), dict(ROWS[1], id=7, name="Elm lot", merged_into=6, auto=False)])


def test_merging_makes_the_other_place_and_its_parts_part_of_this_one(bare, monkeypatch):  # noqa: F811
    db = three()
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    assert bare.merge_places(5, 6) is True
    assert db.row(6)["merged_into"] == 5 and db.row(6)["auto"] is False and db.row(7)["merged_into"] == 5, "its own part follows it"
    assert sorted((q["id"], q.get("every")) for q in db.queued()) == [(6, True), (7, True)], "every charge in both circles is priced again as the kept place's"
    from places import parents, place_for, resolve_merged

    assert [p["id"] for p in parents(db.rows)] == [5], "the other leaves the list"
    assert place_for(39.801, -89.65, resolve_merged(db.rows))["name"] == "Alpha Daycare", "its stops answer as the kept place"


@pytest.mark.parametrize("keep,other", [(5, 5), (5, 99), (7, 5), (6, 7)])
def test_what_cannot_be_merged(bare, monkeypatch, keep, other):  # noqa: F811
    db = three()
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    assert bare.merge_places(keep, other) is False and db.queued() == []


def test_resizing_queues_both_circles_and_a_new_icon_only_when_home_changes(bare, monkeypatch):  # noqa: F811
    db = three()
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    bare.rename_place(5, None, bare.KEEP, where=(39.80, -89.65, 300.0))
    q = db.queued()
    assert db.row(5)["radius_m"] == 300.0 and [(x["radius_m"], x.get("every")) for x in q] == [(150.0, True), (300.0, None)]
    db.state.clear()
    bare.rename_place(5, None, bare.KEEP, where=(39.80, -89.65, 300.0))
    assert db.queued() == [], "the same place again moves nothing"
    bare.rename_place(5, None, "star")
    assert db.queued() == [], "another icon that is not the house: no charge changes price"
    bare.rename_place(5, None, "home")
    assert [x["id"] for x in db.queued()] == [5], "becoming home re-prices its charges"


def test_removing_a_home_removes_its_parts_and_re_prices_them(bare, monkeypatch):  # noqa: F811
    db = Db([dict(ROWS[0], icon="home"), dict(ROWS[1], merged_into=5)])
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    bare.delete_place(5)
    assert db.rows == [] and sorted(x["id"] for x in db.queued()) == [5, 6]


def test_suggestions_ask_the_local_add_on_and_nothing_else(placed, client, monkeypatch):  # noqa: F811
    asked = []

    class Reply:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(req, timeout=None):
        asked.append(req.full_url if hasattr(req, "full_url") else str(req))
        return Reply(json.dumps({"results": [{"label": "742 Evergreen Terrace", "city": "Springfield", "kind": "address", "lat": 39.8, "lon": -89.65}], "built": True}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(placed.httpx, "get", lambda *a, **k: asked.append(a[0]))
    monkeypatch.setattr(placed, "MAPS", MapsClient("http://maps.internal:8090"))
    got = client.get("/places/search?q=742%20everg&lat=39.8&lon=-89.65").json()
    assert got == {"results": [{"label": "742 Evergreen Terrace", "city": "Springfield", "kind": "address", "lat": 39.8, "lon": -89.65}], "available": True}
    assert len(asked) == 1 and asked[0].startswith("http://maps.internal:8090/search?") and "q=742+everg" in asked[0]
    monkeypatch.setattr(placed, "MAPS", MapsClient(None))
    asked.clear()
    assert client.get("/places/search?q=742%20everg").json() == {"results": [], "available": False}
    assert asked == [], "no add-on: no suggestions, and the text goes nowhere"


def test_the_ring_and_the_pin_are_one_point_and_move_together(placed, client):  # noqa: F811
    """The ring is drawn at the place's own latitude and longitude (not swapped), from the
    same point as the pin, follows the pin while it is dragged, and the first view is
    fitted to the ring; a merged place far away is not allowed to zoom the view out."""
    import re

    html = client.get("/places/5").text
    assert 'data-lat="39.8" data-lon="-89.65" data-r="150.0"' in html
    js = html[html.index("function place(at, fit, byOwner)") :]
    assert "circle = L.circle(at, " in js and "pin = L.marker(at, " in js, "one point for both"
    assert 'pin.on("drag", function (e) { circle.setLatLng(e.latlng); });' in js and "circle.setLatLng(at); pin.setLatLng(at);" in js
    assert re.search(r"place\(\[lat, lon\], true\)", html), "the first view is fitted to the ring"
    assert "c.distanceTo(p.getCenter()) <= NEAR_PART_M" in html


def test_without_backgrounds_the_map_draws_the_cars_own_stops_and_drives(placed, client, monkeypatch):  # noqa: F811
    monkeypatch.setitem(placed.templates.env.globals, "public_tiles", False)
    html = client.get("/places/5").text
    assert 'data-around="/places/5/around"' in html and "pd-maphint" not in html, "no caption under the map; its corner says backgrounds are off"


def test_traces_are_cut_to_the_places_surroundings():
    from placespage import traces_near

    lat, lon = 39.80, -89.65
    inn = [(lat + i * 0.0005, lon) for i in range(5)]  # 0 to 220 m north
    far = [(lat + 0.05, lon), (lat + 0.06, lon)]  # 5 km away
    back = [(lat - 0.001, lon), (lat - 0.0012, lon)]
    got = traces_near(lat, lon, [inn + far + back, far, [(None, None), (lat, lon)]])
    assert got == [
        [[round(a, 5), lon] for a, _ in inn],
        [[round(a, 5), lon] for a, _ in back],
    ], "leaving and coming back is two lines; a lone point is none"
    long = [(lat + i * 0.00001, lon) for i in range(500)]
    assert len(traces_near(lat, lon, [long], keep=40)[0]) <= 41


def test_around_reads_the_drives_that_end_or_start_there(placed, client, monkeypatch):  # noqa: F811
    from datetime import datetime, timezone

    t = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
    asked = []

    class Conn:
        def execute(self, sql, args=()):
            s = " ".join(sql.split())
            asked.append(s)
            if "cc_trip_routes" in s:
                rows = []  # no drive here matched to the roads: the readings are the trace
            elif s.startswith("SELECT vin, start_date, destination_date"):
                rows = [
                    {
                        "vin": "V1",
                        "start_date": t,
                        "destination_date": t,
                        "arrived": True,
                    }
                ]
            else:
                rows = [
                    {"latitude": 39.8003, "longitude": -89.65},
                    {"latitude": 39.8, "longitude": -89.65},
                ]
            return type("R", (), {"fetchall": lambda _s: rows, "fetchone": lambda _s: None})()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(placed, "db", lambda *a, **k: Conn())
    assert client.get("/places/5/around").json() == {"traces": [[[39.8003, -89.65], [39.8, -89.65]]]}
    assert "FROM cc_positions WHERE vin = %s AND at BETWEEN" in asked[2], "by car and time: the table's index"
    assert client.get("/places/99/around").status_code == 404


# ---- pre-deploy review ----

HOME_ROW = {
    "id": 1,
    "name": "Home",
    "latitude": 39.7817,
    "longitude": -89.6010,
    "radius_m": 150.0,
    "auto": False,
    "visits": 400,
    "address": None,
    "user_address": None,
    "icon": "home",
    "price": None,
    "merged_into": None,
}


def test_parked_at_home_add_work_leaves_home_untouched(bare, client, monkeypatch):  # noqa: F811
    """The Add page always adds: a pin inside Home's circle is a new place, never Home renamed."""
    db = Db([dict(HOME_ROW)])
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    r = client.post("/places/new", data={"csrf_token": token(bare), "lat": "39.7818", "lon": "-89.6011", "name": "Work", "icon": "work", "radius_m": "120"})
    assert r.status_code == 303 and r.headers["location"] == "/places/2"
    assert db.row(1) == HOME_ROW, "Home is as it was"
    assert db.row(2)["name"] == "Work" and db.row(2)["icon"] == "work" and db.queued() == [], "Work is not home: nothing to price again"


def test_the_picker_still_names_the_place_it_is_in_and_a_home_flip_is_queued(bare, client, monkeypatch):  # noqa: F811
    db = Db([dict(HOME_ROW, icon=None, name="Evergreen Terrace, Springfield", auto=True)])
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    client.post("/places/pin", data={"csrf_token": token(bare), "lat": "39.7818", "lon": "-89.6011", "name": "SF Home", "back": "/places"})
    assert db.row(1)["name"] == "SF Home" and len(db.rows) == 1, "the picker names the place the stop is in"
    assert [q["id"] for q in db.queued()] == [1], "a found place named SF Home is home now: its charges are priced again"


def test_adding_a_home_queues_its_charges(bare, client, monkeypatch):  # noqa: F811
    db = Db([dict(HOME_ROW, id=1, name="Bravo Market", icon="shop", latitude=39.9)])
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    client.post("/places/new", data={"csrf_token": token(bare), "lat": "39.7818", "lon": "-89.6011", "name": "Cabin", "icon": "home"})
    assert [q["id"] for q in db.queued()] == [2]


@pytest.mark.parametrize("path", ["/places/pin", "/places/new", "/places"])
@pytest.mark.parametrize("lat,lon,radius", [("nan", "-89.6", "150"), ("39.8", "inf", "150"), ("95", "-89.6", "150"), ("39.8", "-89.6", "nan")])
def test_a_position_that_is_not_one_is_refused(bare, client, monkeypatch, path, lat, lon, radius):  # noqa: F811
    stored = []
    monkeypatch.setattr(bare, "pin_place", lambda *a, **k: stored.append(a) or 1)
    monkeypatch.setattr(bare, "add_place", lambda *a, **k: stored.append(a))
    names = {"/places": ("latitude", "longitude")}.get(path, ("lat", "lon"))
    r = client.post(path, data={"csrf_token": token(bare), names[0]: lat, names[1]: lon, "radius_m": radius, "name": "Charlie"})
    assert r.status_code in (400, 422) and stored == [], "nothing stored: a NaN in cc_places breaks every page"


AVERAGED = dict(HOME_ROW, id=1, name="Elm Street, Springfield", icon=None, auto=True, latitude=39.80012345678, longitude=-89.65098765432, radius_m=133.33333)


@pytest.mark.parametrize(
    "form",
    [
        {"name": "Charlie School"},
        {"price_kind": "free"},
        {"name": "Elm Street, Springfield", "latitude": "39.800123", "longitude": "-89.650988", "radius_m": "133"},
    ],
)
def test_a_save_that_moves_nothing_queues_nothing(bare, client, monkeypatch, form):  # noqa: F811
    """A found place's averaged position has more decimals than the page posts back: a name
    or price saved from its page is not a move, and an untouched size is not snapped."""
    db = Db([dict(AVERAGED)])
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    full = {"csrf_token": token(bare), "latitude": "39.800123", "longitude": "-89.650988", "radius_m": "133", **form}
    assert client.post("/places/1/edit", data=full).status_code == 303
    row = db.row(1)
    assert (row["latitude"], row["longitude"], row["radius_m"]) == (39.80012345678, -89.65098765432, 133.33333)
    if form.get("price_kind"):
        # a new price is a real change: the place's own charges once, at its own circle, not a move
        assert [(q["id"], q["latitude"], q.get("every")) for q in db.queued()] == [(1, 39.80012345678, None)]
    else:
        assert db.queued() == [], "nothing moved: nothing priced again"


def test_moving_within_rounding_is_no_move_and_a_big_untouched_radius_stays():
    from app import moved_to

    before = {"latitude": 39.80012345678, "longitude": -89.65098765432, "radius_m": 1500.0}
    assert moved_to(before, (39.800123, -89.650988, None)) is None, "six decimals of the same place"
    assert moved_to(before, (39.8011, -89.650988, None)) == (39.8011, -89.65098765432, 1500.0), "a real move keeps the 1500 m nobody touched"
    assert moved_to(before, (None, None, 1000.0)) == (39.80012345678, -89.65098765432, 1000.0)
    assert moved_to(before, None) is None


def test_a_home_flip_queues_every_place_merged_into_it(bare, monkeypatch):  # noqa: F811
    db = three()
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    assert bare.merge_places(5, 6)
    db.state.clear()
    bare.rename_place(5, None, "home")
    assert sorted(q["id"] for q in db.queued()) == [5, 6, 7], "they answer as it: home now too"


def test_removing_any_place_re_resolves_every_circle_it_held(bare, monkeypatch):  # noqa: F811
    """No price, not home: the charges inside it may still be another overlapping place's now."""
    db = Db([dict(ROWS[0], parts=None), dict(ROWS[1], parts=None, merged_into=5)])
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    bare.delete_place(5)
    assert db.rows == [] and sorted((q["id"], q.get("every")) for q in db.queued()) == [(5, True), (6, True)]


def test_remove_says_merged_places_go_with_it(placed, client, monkeypatch):  # noqa: F811
    rows = [dict(ROWS[0], parts=[{"id": 6, "latitude": 39.801, "longitude": -89.65, "radius_m": 120.0}])]
    monkeypatch.setattr(placed, "places_detail", lambda: rows)
    assert "Remove Alpha Daycare and 1 merged?" in client.get("/places/5").text


def test_the_page_marks_a_move_only_when_the_owner_makes_one(placed, client):  # noqa: F811
    html = client.get("/places/5").text
    assert '<input type="hidden" name="moved" value="" data-pd-moved><input type="hidden" name="resized" value="" data-pd-resized>' in html
    assert 'pin.on("dragend", function () { set(pin.getLatLng(), true); });' in html and 'if (byOwner && moved) { moved.value = "1"; editing(); }' in html
    assert "CCSize.bind(" in html and "resized, function (m, done)" in html and "place([lat, lon], true);" in html, "drawing the place at first is not a move"
    assert 'step="1" value="150"' in html


# ---- the name the cars' navigation gives a found place
NAV = {"name": "Bravo Market", "n": 3, "source": "from Alpha's navigation", "alternates": ["Bravo Grocery"]}


@pytest.fixture
def navved(placed, monkeypatch):
    monkeypatch.setattr(placed, "place_nav", lambda: {5: dict(NAV, name="Never Shown"), 6: dict(NAV)})
    monkeypatch.setattr(placed, "place_row", lambda conn, pid: next((dict(r) for r in ROWS if r["id"] == pid), None))
    return placed


def test_a_found_place_offers_its_navigation_name_and_an_owners_never(navved, client):  # noqa: F811
    r = client.get("/places/6")
    assert "Bravo Market" in r.text and "from Alpha&#39;s navigation, 3 drives; also Bravo Grocery" in r.text and 'action="/places/6/use-name"' in r.text
    r = client.get("/places/5")
    assert "Never Shown" not in r.text and "/use-name" not in r.text, "a name the owner gave is never offered over"
    rows = {r["id"]: r for r in client.get("/places/index.json").json()["places"]}
    assert rows[6]["s"] == "Bravo Market" and "s" not in rows[5], "the list shows a found place's suggestion, never one over an owner's name"


def test_use_names_a_found_place_and_never_an_owners(navved, client, monkeypatch):  # noqa: F811
    import contextlib

    got = []
    monkeypatch.setattr(navved, "db", lambda *a, **k: contextlib.nullcontext(None))
    monkeypatch.setattr(navved, "rename_place", lambda *a: got.append(a))
    assert client.post("/places/6/use-name", data={"pick": "0"}).status_code == 400 and got == [], "the form's token, as every change"
    r = client.post("/places/6/use-name", data={"csrf_token": token(navved), "pick": "0", "name": "Anything Typed", "back": "/places/6"})
    assert r.status_code == 303 and r.headers["location"] == "/places/6" and got == [(6, "Bravo Market", navved.KEEP)], "the stored suggestion, never posted text; icon kept"
    got.clear()
    client.post("/places/6/use-name", data={"csrf_token": token(navved), "pick": "1"})
    assert got == [(6, "Bravo Grocery", navved.KEEP)], "an alternate, by its place in the list"
    got.clear()
    for pick in ("2", "-1"):
        client.post("/places/6/use-name", data={"csrf_token": token(navved), "pick": pick})
    assert got == [], "no such alternate: nothing"
    monkeypatch.setattr(navved, "place_nav", lambda: {6: dict(NAV, name="Bravo Market of Springfield, the Big One on Elm")})
    client.post("/places/6/use-name", data={"csrf_token": token(navved)})
    assert got == [(6, "Bravo Market of Springfield, the Big One", navved.KEEP)], "40 characters at most, as a rename"
    got.clear()
    client.post("/places/5/use-name", data={"csrf_token": token(navved), "pick": "0"})
    assert got == [], "Alpha Daycare is the owner's name: never written over"


def test_the_picker_offers_the_navigation_name_first(placed, client, monkeypatch):  # noqa: F811
    from datetime import datetime, timezone

    from places import PlaceIndex

    row = {"vin": "V1", "started": datetime(2026, 9, 7, tzinfo=timezone.utc), "end_lat": 39.82, "end_lon": -89.60, "dest_name": "Charlie's Diner, 9 Oak Lane", "dest_lat": 39.82, "dest_lon": -89.60}
    monkeypatch.setattr(placed, "nav_rows", lambda: [row])
    monkeypatch.setattr(placed, "car_names", lambda: {"V1": "Alpha"})
    monkeypatch.setattr(placed, "places", lambda: PlaceIndex([dict(r) for r in ROWS]))
    monkeypatch.setattr(placed.MAPS, "candidates", lambda lat, lon: None)
    got = client.get("/places/candidates?lat=39.8201&lon=-89.6001").json()
    assert got["nav"]["name"] == "Charlie's Diner" and got["nav"]["source"] == "from Alpha's navigation"
    assert client.get("/places/candidates?lat=39.80&lon=-89.65").json()["nav"] is None, "inside the owner's Alpha Daycare: nothing offered"
