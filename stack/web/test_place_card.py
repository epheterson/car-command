"""The naming card and the map-first Places page: every place name is tappable where it shows;
the card prefills the cars' navigation name and saves through the place routes as JSON; a
name that is already a place nearby is offered as a merge; Merge takes several at once; the
Places list is in the order the fast tagging walks it. None of it moves a place, renames one
from the Add page, or skips the background re-pricing."""

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jinja2
import pytest

import filters
import placespage
from test_empty import bare, client  # noqa: F401 - fixtures
from test_place_routes import ROWS, Db, placed, three, token  # noqa: F401 - fixtures

HERE = Path(__file__).parent
ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(HERE / "templates")))
filters.register(ENV)
T0 = datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)
ELM = {"name": "Elm Street, Springfield", "lat": 39.801, "lon": -89.65, "new": True}
HOME = {"name": "Home", "lat": 39.79, "lon": -89.66, "new": False}
PAGE = {
    "root": "",
    "now": T0,
    "car": {"name": "Alpha", "vin": "V1", "lat": 39.79, "lon": -89.66, "place": "Home", "address": None, "live": True, "updated_age": "now"},
    "active": "",
    "vehicles": [{"vin": "V1", "name": "Alpha", "live": True}],
    "q": "",
    "colors": {"V1": 0},
    "names": {"V1": "Alpha"},
    "cur": "$",
}


def tappable(html: str, ref: dict) -> bool:
    return f'data-lat="{ref["lat"]}" data-lon="{ref["lon"]}"' in html and re.search(r'class="pn( pn-new)?"', html) is not None


# ---- tappable names on every page type


def test_place_refs_marks_each_name_once_in_order_and_escapes():
    out = str(filters.place_refs("Home → Home", [HOME, dict(HOME, lat=39.9)], inlink=True))
    assert out.count('role="button" tabindex="0"') == 2 and out.index('data-lat="39.79"') < out.index('data-lat="39.9"')
    out = str(filters.place_refs("Parked at <b>", [dict(ELM, name="<b>")]))
    assert "&lt;b&gt;</button>" in out and "<b>" not in out, "a name is text, never markup"
    assert str(filters.place_refs("Asleep 2 h", [ELM, None])) == "Asleep 2 h", "a place not in the text: plain text"


def test_the_timeline_marks_drive_ends_charges_and_parked_places():
    from stats import timeline_days

    d = {"id": 1, "start_date": T0, "destination_date": T0 + timedelta(minutes=20), "vin": "V1", "from": "Home", "to": ELM["name"], "from_ref": HOME, "to_ref": ELM, "level_end": 70.0}
    c = {"id": 2, "session_start_date": T0 + timedelta(hours=3), "session_end_date": T0 + timedelta(hours=4), "vin": "V1", "where": ELM["name"], "where_ref": ELM, "level_start": 69.0}
    days = timeline_days([d], [c], [], timezone.utc)
    events = days[0]["events"]
    assert [e["refs"] for e in events] == [[ELM], [ELM], [HOME, ELM]], "charge, the parked stretch at the drive's end, the drive"
    html = ENV.get_template("timeline.html").render(
        **PAGE, days=days, window={"label": "Sep", "prev": "", "prev_label": "", "next": "", "next_label": "", "is_current": True, "ym": ""}, kind="all", kinds=["all"], counts={}
    )
    assert html.count('class="pn pn-new" role="button" tabindex="0"') == 2 and html.count('class="pn" role="button" tabindex="0"') == 1, "spans inside the rows' links"
    assert html.count('<button class="pn pn-new" type="button"') == 1, "the parked line is no link: a button"


def test_the_charge_page_the_lists_and_the_now_hero_mark_their_places():
    src = {n: (HERE / "templates" / n).read_text() for n in ("charge.html", "drives.html", "charges.html", "_now_hero.html", "calendar.html", "map.html", "drive.html")}
    assert "place_refs(session.where, [session.get('where_ref')])" in src["charge.html"]
    assert "[d.get('from_ref'), d.get('to_ref')]" in src["drives.html"] and "[c.get('where_ref')]" in src["charges.html"]
    assert "CCMap.place(p.where, p.lat, p.lon)" in src["calendar.html"] and "CCMap.place(p.where, p.lat, p.lon)" in src["map.html"], "map popups open the card too"
    assert "place_refs(car.place" in src["_now_hero.html"] and "place_refs(car.address" in src["_now_hero.html"]
    js = (HERE / "static" / "maps.js").read_text()
    assert 'b.className = "pn";' in js and "b.textContent = name;" in js, "a popup's name is text, never markup"


def test_the_car_card_marks_where_it_is_inside_its_link():
    c = {"vin": "A", "name": "Alpha", "status": "Parked", "place": "Home", "lat": 39.79, "lon": -89.66, "live": False, "updated_age": "now"}
    html = ENV.from_string('{% import "_parts.html" as p %}{{ p.car_card(c, "", {}, {"dist": "mi"}, {}, []) }}').render(c=c)
    assert '<span class="pn" role="button" tabindex="0" aria-haspopup="dialog" data-lat="39.79" data-lon="-89.66">' in html


def test_drives_and_charges_carry_their_refs_from_where_they_are_labelled(bare):  # noqa: F811
    from places import PlaceIndex

    known = PlaceIndex([{"id": 5, "name": "Alpha Daycare", "latitude": 39.80, "longitude": -89.65, "radius_m": 150.0, "auto": False}])
    r = {"start_position_latitude": 39.80, "start_position_longitude": -89.65, "destination_position_latitude": 39.9, "destination_position_longitude": -89.7}
    r.update(destination_odometer=10.0, start_odometer=5.0, destination_date=T0, start_date=T0 - timedelta(minutes=9), start_road="", start_city="", dest_road="Oak Lane", dest_city="Springfield")
    out = bare._finish_drive(r, known, cells={}, energy={}, by_year={})
    assert out["from"] == "Alpha Daycare" and out["from_ref"] == {"name": "Alpha Daycare", "lat": 39.80, "lon": -89.65, "new": False}
    assert out["to"] == "Oak Lane, Springfield" and out["to_ref"]["new"] is True and out["to_named"] is False


# ---- the card's lookup and Save


def test_the_card_lookup_names_the_place_here_its_neighbours_and_the_navigation_first(placed, client, monkeypatch):  # noqa: F811
    from places import PlaceIndex

    row = {"vin": "V1", "started": T0, "end_lat": 39.802, "end_lon": -89.65, "dest_name": "Bravo Market, 9 Oak Lane", "dest_lat": 39.802, "dest_lon": -89.65}
    monkeypatch.setattr(placed, "nav_rows", lambda: [row])
    monkeypatch.setattr(placed, "car_names", lambda: {"V1": "Alpha"})
    monkeypatch.setattr(placed, "places", lambda: PlaceIndex([dict(r) for r in ROWS]))
    monkeypatch.setattr(placed.MAPS, "candidates", lambda lat, lon: {"candidates": [{"name": "Charlie Coffee", "kind": "cafe", "distance_m": 30}], "address": None})
    got = client.get("/places/candidates?lat=39.8011&lon=-89.65").json()
    assert got["nav"]["name"] == "Bravo Market" and got["candidates"][0]["name"] == "Charlie Coffee"
    assert got["place"]["id"] == 6 and got["place"]["auto"] is True and got["place"]["circles"] == [[39.801, -89.65, 120.0]]
    assert got["nearby"] == [{"id": 5, "name": "Alpha Daycare", "m": 122}], "the named places within a kilometre, for a merge"
    got = client.get("/places/candidates?lat=39.80&lon=-89.65&place=6").json()
    assert got["place"]["id"] == 6, "opened on a place from the list: that place, even inside another"


def test_the_card_prefills_the_navigation_name_then_the_maps_places():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = (HERE / "static" / "placecard.js").read_text()
    fn = re.search(r"  function suggestions\(d\) \{.*?\n  \}\n", js, re.S).group(0)
    d = {
        "nav": {"name": "Bravo Market", "source": "from Alpha's navigation", "alternates": ["Bravo Grocery"]},
        "candidates": [{"name": "Charlie Coffee", "kind": "cafe", "distance_m": 30}, {"name": "bravo market", "kind": "supermarket"}],
    }
    out = subprocess.run(["node", "-e", fn + f"console.log(JSON.stringify(suggestions({json.dumps(d)}).map(s => s.name)))"], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == ["Bravo Market", "Bravo Grocery", "Charlie Coffee"], "navigation first, then the map's places, each name once"
    assert "var best = named ? p.name : sugg.length ? sugg[0].name : p ? p.name : \"\";" in js, "a named place keeps its name; anything else takes the best suggestion"
    assert "if (!nameIn.dataset.typed)" in js, "a name typed before the lookup answers is never written over"


def test_save_from_the_card_answers_with_the_place_and_never_moves_it(placed, client, monkeypatch):  # noqa: F811
    got = []
    monkeypatch.setattr(placed, "rename_place", lambda *a: got.append(a))
    form = {"csrf_token": token(placed), "name": "Alpha Daycare", "icon": "kids", "latitude": "39.9", "longitude": "-89.7", "radius_m": "900"}
    r = client.post("/places/5/edit", data=form, headers={"accept": "application/json"})
    assert r.status_code == 200 and "location" not in r.headers
    p = r.json()["place"]
    assert p["id"] == 5 and p["name"] == "Alpha Daycare" and p["ids"] == [5] and p["circles"] == [[39.80, -89.65, 150.0]]
    assert 'class="pl-ic pl-kids"' not in p["label"] and "Alpha Daycare" in p["label"] and p["price_text"] == "Not set"
    assert got[0][5] is None, "no moved or resized flag: nothing moves, whatever position is posted"
    r = client.post("/places/5/edit", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/places/5?saved=1", "a plain form still gets its redirect"


def test_a_pin_from_the_card_answers_with_the_new_place(placed, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(placed, "pin_place", lambda conn, lat, lon, name, **k: 5)
    monkeypatch.setattr(placed, "places", lambda: placed.PlaceIndex([]))
    r = client.post("/places/pin", data={"csrf_token": token(placed), "lat": "39.9", "lon": "-89.0", "name": "Charlie Coffee"}, headers={"accept": "application/json"})
    assert r.status_code == 200 and r.json()["place"]["id"] == 5


# ---- the same name nearby is a merge, not a second place


def test_the_same_name_nearby_is_found_by_any_of_its_circles():
    every = [
        {"id": 1, "name": "Alpha  Daycare", "latitude": 39.80, "longitude": -89.65, "radius_m": 150, "auto": False, "parts": [{"id": 9, "latitude": 39.81, "longitude": -89.65, "radius_m": 80}]},
        {"id": 2, "name": "alpha daycare", "latitude": 39.80, "longitude": -89.66, "radius_m": 150, "auto": True},
    ]
    assert placespage.same_name_near("ALPHA daycare", 39.8135, -89.65, every) == {"id": 1, "name": "Alpha  Daycare", "m": 389}, "measured to its part, any case and spacing"
    assert placespage.same_name_near("Alpha Daycare", 39.83, -89.65, every) is None, "farther than 500 m"
    assert placespage.same_name_near("Alpha Daycare", 39.80, -89.65, every, skip=1) is None, "never itself, never a found place"


def test_naming_a_stop_after_a_place_nearby_offers_the_merge(placed, client, monkeypatch):  # noqa: F811
    pinned = []
    monkeypatch.setattr(placed, "pin_place", lambda conn, *a, **k: pinned.append(a) or 7)
    monkeypatch.setattr(placed, "places", lambda: placed.PlaceIndex([]))
    form = {"csrf_token": token(placed), "lat": "39.803", "lon": "-89.65", "name": "alpha daycare"}
    r = client.post("/places/pin", data=form, headers={"accept": "application/json"})
    assert r.status_code == 409 and r.json() == {"offer": {"id": 5, "name": "Alpha Daycare", "m": 334, "confirm": False}} and pinned == [], "asked, nothing saved"
    r = client.post("/places/pin", data=dict(form, separate="1"), headers={"accept": "application/json"})
    assert r.status_code == 404 and len(pinned) == 1, "Keep both: saved as its own place"
    pinned.clear()
    assert client.post("/places/pin", data=form).status_code == 303 and len(pinned) == 1, "a plain form is never asked"


def test_renaming_a_found_place_after_its_neighbour_offers_the_merge(placed, client, monkeypatch):  # noqa: F811
    got = []
    monkeypatch.setattr(placed, "rename_place", lambda *a: got.append(a))
    r = client.post("/places/6/edit", data={"csrf_token": token(placed), "name": "Alpha Daycare"}, headers={"accept": "application/json"})
    assert r.status_code == 409 and r.json()["offer"]["id"] == 5 and r.json()["offer"]["confirm"] is False and got == [], "a found place: merged at once"
    r = client.post("/places/5/edit", data={"csrf_token": token(placed), "name": "Alpha Daycare", "icon": "kids"}, headers={"accept": "application/json"})
    assert r.status_code == 200 and len(got) == 1, "its own name again is no question"


def test_merging_a_stop_into_a_place_adds_a_circle_and_never_renames_the_place(bare, client, monkeypatch):  # noqa: F811
    db = Db([dict(ROWS[0], parts=None)])
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    assert bare.pin_into(39.803, -89.65, 5) is True
    new = db.rows[-1]
    assert new["merged_into"] == 5 and new["name"] == "Alpha Daycare" and (new["latitude"], new["radius_m"]) == (39.803, 120.0)
    assert db.row(5)["name"] == "Alpha Daycare" and (db.row(5)["latitude"], db.row(5)["radius_m"]) == (39.80, 150.0), "the kept place is untouched"
    assert [(q["id"], q.get("every")) for q in db.queued()] == [(new["id"], True)], "the stop's charges are priced again as the kept place's"
    db.state.clear()
    assert bare.pin_into(39.8001, -89.65, 5) is True and db.queued() == [] and len(db.rows) == 2, "a stop already in it: nothing to do"


def test_merging_a_found_place_by_its_stop_takes_the_found_one(bare, monkeypatch):  # noqa: F811
    db = three()
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    assert bare.pin_into(39.8012, -89.6501, 5) is True and db.row(6)["merged_into"] == 5 and db.row(7)["merged_into"] == 5
    db = Db([dict(ROWS[0], parts=None), dict(ROWS[1], auto=False, name="Bravo")])
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    assert bare.pin_into(39.8012, -89.6501, 5) is False and db.row(6)["merged_into"] is None, "a place somebody named is merged with Merge, not from here"


def test_merge_into_from_the_pin_route_and_never_from_the_add_page(placed, client, monkeypatch):  # noqa: F811
    asked, pinned = [], []
    monkeypatch.setattr(placed, "pin_into", lambda *a: asked.append(a) or True)
    monkeypatch.setattr(placed, "pin_place", lambda conn, *a, **k: pinned.append(k) or 9)
    form = {"csrf_token": token(placed), "lat": "39.803", "lon": "-89.65", "name": "Alpha Daycare", "merge_into": "5"}
    r = client.post("/places/pin", data=form, headers={"accept": "application/json"})
    assert r.status_code == 200 and r.json()["place"]["id"] == 5 and asked == [(39.803, -89.65, 5, 120.0)] and pinned == []
    asked.clear()
    r = client.post("/places/new", data=form)
    assert asked == [] and len(pinned) == 1 and pinned[0]["rename"] is False, "Add always adds, never merges and never renames"
    monkeypatch.setattr(placed, "pin_into", lambda *a: False)
    assert client.post("/places/pin", data=form).status_code == 400


# ---- Merge takes several at once


def test_merge_takes_several_in_one_go_and_re_prices_them_all(bare, monkeypatch):  # noqa: F811
    db = three()
    db.rows.append(dict(ROWS[1], id=8, name="Oak lot", latitude=39.81, auto=True, merged_into=None))
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    assert bare.merge_places(5, [6, 8]) is True
    assert {r["id"]: r["merged_into"] for r in db.rows} == {5: None, 6: 5, 7: 5, 8: 5}
    assert sorted(q["id"] for q in db.queued()) == [6, 7, 8] and all(q["every"] for q in db.queued())


def test_merge_is_all_or_nothing(bare, monkeypatch):  # noqa: F811
    db = three()
    monkeypatch.setattr(bare, "db", lambda *a, **k: db)
    assert bare.merge_places(5, [6, 99]) is False and db.row(6)["merged_into"] is None and db.queued() == []
    assert bare.merge_places(5, [7, 6]) is True and db.row(6)["merged_into"] == 5, "a part brings its place along, once"


def test_the_merge_route_takes_several_and_answers_the_card(placed, client, monkeypatch):  # noqa: F811
    got = []
    monkeypatch.setattr(placed, "merge_places", lambda keep, others: got.append((keep, others)) or True)
    r = client.post("/places/5/merge", data={"csrf_token": token(placed), "other": ["6", "7"]}, headers={"accept": "application/json"})
    assert r.status_code == 200 and got == [(5, [6, 7])] and r.json()["place"]["id"] == 5
    r = client.post("/places/5/merge", data={"csrf_token": token(placed), "other": "6"})
    assert r.status_code == 303 and r.headers["location"] == "/places/5?merged=1" and got[-1] == (5, [6])


def test_the_merge_preview_says_what_moves_and_changes_nothing(placed, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(placed, "place_sessions", lambda conn, p, index: [1, 2, 3] if p["id"] == 6 else [])
    r = client.get("/places/5/merge-preview?other=6&other=5&other=99")
    assert r.status_code == 200
    d = r.json()
    assert d["keep"]["name"] == "Alpha Daycare" and d["others"] == [{"id": 6, "name": "Elm Street, Springfield", "auto": True, "visits": 0, "charges": 3, "price_text": "Not set"}]
    assert client.post("/places/5/merge-preview?other=6", data={"csrf_token": token(placed)}).status_code == 405, "a look, never a change"


# ---- the Places page: one fetch, in the order the tagging walks


def test_the_index_lists_the_found_places_first_busiest_first_then_the_named(placed, client, monkeypatch):  # noqa: F811
    rows = ROWS + [dict(ROWS[1], id=8, name="Oak Lane, Springfield", visits=30), dict(ROWS[1], id=9, name="Pine Road, Springfield", visits=9)]
    monkeypatch.setattr(placed, "places_detail", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(placed, "place_stays", lambda: {6: {"n": 12}})
    monkeypatch.setattr(placed, "place_nav", lambda: {9: {"name": "Charlie Coffee"}})
    r = client.get("/places/index.json")
    assert r.headers["cache-control"] == "no-store"
    d = r.json()
    assert [p["id"] for p in d["places"]] == [6, 8, 9, 5], "found by visits (the stays alone), ties by id; then the named"
    assert [p["v"] for p in d["places"]] == [12, 0, 0, 0], "placespage.visits: never the stored column"
    assert d["places"][2]["s"] == "Charlie Coffee" and d["places"][3]["ic"] == "kids" and list(d["icons"]) == ["kids"], "only the icons shown"


def test_the_next_one_after_a_save_or_a_skip():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = (HERE / "static" / "placesmap.js").read_text()
    fn = re.search(r"  function nextIn\(ids, id\) \{.*?\n  \}\n", js, re.S).group(0)
    cases = [[[8, 6, 9], 6], [[8, 6, 9], 9], [[8], 8], [[8, 6], 99], [[], 1]]
    out = subprocess.run(["node", "-e", fn + f"console.log(JSON.stringify({json.dumps(cases)}.map(c => nextIn(c[0], c[1]))))"], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [9, 8, None, 8, None], "the following one; from the last, the first; none left, none"
    assert "onSkip: function () { go(nextIn(queue(), p.id)); }" in js and "after = p && wasFound ? nextIn(queue(), p.id) : null" in js


def test_the_places_page_opens_on_a_merge_from_a_places_page(placed, client):  # noqa: F811
    r = client.get("/places?merge=5")
    assert r.status_code == 200 and 'data-merge="5"' in r.text and ">1 place<" in r.text
    js = (HERE / "static" / "placesmap.js").read_text()
    assert "x.m <= 1000" in js and "metres(k.la, k.lo, p.la, p.lo) <= 1000" in js, "the places within a kilometre are the ones offered"
    assert "c.confirmMerge(" in js, "picking ends in the card's confirm, which says what moves"


def test_renaming_a_named_place_after_its_neighbour_asks_through_the_confirm(placed, client, monkeypatch):  # noqa: F811
    """A place somebody named is never absorbed in one tap: the offer says confirm, and the card
    sends it through Merge's confirm sheet, keeping what was typed until then."""
    rows = ROWS + [dict(ROWS[0], id=8, name="Bravo Market", latitude=39.802, visits=3)]
    monkeypatch.setattr(placed, "places_detail", lambda: [dict(r) for r in rows])
    got = []
    monkeypatch.setattr(placed, "rename_place", lambda *a: got.append(a))
    r = client.post("/places/5/edit", data={"csrf_token": token(placed), "name": "Bravo Market"}, headers={"accept": "application/json"})
    assert r.status_code == 409 and r.json()["offer"] == {"id": 8, "name": "Bravo Market", "m": 222, "confirm": True} and got == []
    js = (HERE / "static" / "placecard.js").read_text()
    fn = js[js.index("  function mergeOffered() {") :]
    fn = fn[: fn.index("\n  }\n") + 4]
    assert fn.index("if (o.confirm && p) {") < fn.index("confirmMerge({") < fn.index('send("/places/" + o.id + "/merge"'), "named: the confirm sheet; only a found place merges at once"
    assert "keep: o.id," in fn and "others: [p.id]," in fn and 'onBack: function () { st = was; view("form");' in fn, "Back returns to the card as it was"


def test_a_guess_never_ticks_the_house():
    """Home re-prices a place's charges, so a guess (a nav suggestion of "Home", a typed name)
    only marks the house; Enter in the queue then posts no icon and the place does not become
    home. Any other guess is ticked as before, and a hand-picked icon is never overridden."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = (HERE / "static" / "placecard.js").read_text()
    fn = re.search(r"  function guess\(form, key\) \{.*?\n  \}\n  var HOME = \"home\";\n", js, re.S).group(0)
    run = """
var calls = [];
function setIcon(box, key) { calls.push(["set", key]); }
function mark(box, key) { calls.push(["mark", key]); }
var form = { dataset: {}, querySelector: function () { return {}; } };
guess(form, "home"); guess(form, "shop"); form.dataset.iconHand = "1"; guess(form, "work");
console.log(JSON.stringify(calls));
"""
    out = subprocess.run(["node", "-e", fn + run], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [["set", None], ["mark", "home"], ["set", "shop"]]
    assert "else guess(form, (sugg[0] && kinds[sugg[0].kind]) || iconForName(nameIn.value, pairsOf(icons)));" in js, "the card's prefill guesses through the same rule"
    # A stored-NULL icon and the name "Home" would be home by the name rule; with the house only
    # marked, the card saves "none", so Enter in the queue never makes the place home.
    assert 'if (!fd.get("icon") && iconForName(fd.get("name"), pairsOf(icons)) === HOME && !(p && p.home)) fd.set("icon", "none");' in js
    from places import is_home

    assert is_home({"name": "Home", "icon": None, "auto": False}) and not is_home({"name": "Home", "icon": "none", "auto": False})


# ---- To name: the found places as a list, and the picked one marked on the map

# A page small enough to run placesmap.js in node: elements that hold children, classes,
# attributes and listeners; `closest` and `querySelector` by the few selectors the script uses.
FAKE_DOM = r"""
function El(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attrs = {}; this.className = ""; this.textContent = ""; this.hidden = false; this.parent = null; this.on = {}; this.style = {}; }
El.prototype.appendChild = function (c) { c.parent = this; this.children.push(c); return c; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.addEventListener = function (k, f) { this.on[k] = f; };
El.prototype.scrollIntoView = function () {};
El.prototype.insertAdjacentHTML = function () {};
Object.defineProperty(El.prototype, "innerHTML", { set: function () { this.children = []; }, get: function () { return ""; } });
Object.defineProperty(El.prototype, "classList", { get: function () { var el = this; return {
  contains: function (c) { return (" " + el.className + " ").indexOf(" " + c + " ") >= 0; },
  toggle: function (c, on) { var has = this.contains(c); if (on === undefined) on = !has; if (on && !has) el.className += " " + c; if (!on && has) el.className = (" " + el.className + " ").replace(" " + c + " ", " ").trim(); },
  add: function (c) { this.toggle(c, true); }, remove: function (c) { this.toggle(c, false); } }; } });
function hit(el, sel) {
  var m = /^\.([\w-]+)(?:\[data-id="(\d+)"\])?$/.exec(sel);
  return m && el.classList.contains(m[1]) && (!m[2] || String(el.dataset.id) === m[2]);
}
function walk(el, f) { for (var i = 0; i < el.children.length; i++) { var r = f(el.children[i]) || walk(el.children[i], f); if (r) return r; } return null; }
El.prototype.closest = function (sel) { for (var e = this; e; e = e.parent) if (hit(e, sel)) return e; return null; };
El.prototype.querySelector = function (sel) {
  var parts = sel.split(" "), first = parts[0], rest = parts.slice(1).join(" ");
  var found = walk(this, function (c) { return hit(c, first) ? c : null; });
  return found && rest ? found.querySelector(rest) : found;
};
var byId = {}, sections = {};
["pl-need", "pl-named", "pl-near", "pl-map", "pl-need-h"].forEach(function (id) { byId[id] = new El("ul"); });
var wrap = new El("div"); wrap.dataset.index = "/places/index.json";
["need", "named", "near"].forEach(function (k) { var s = new El("section"), n = new El("span"); s.className = "plsec plsec-" + k; n.className = "n"; s.appendChild(n); s.appendChild(byId["pl-" + k]); wrap.appendChild(s); sections[k] = s; });
var find = new El("input"), none = new El("p"), banner = new El("div"), say = new El("span"), cancel = new El("button"), mergeGo = new El("button");
say.className = "plmerge-say"; cancel.className = "plmerge-cancel"; mergeGo.className = "plmerge-go";
[say, cancel, mergeGo].forEach(function (c) { banner.appendChild(c); });
var meta = { content: "" };
global.document = {
  documentElement: new El("html"), body: new El("body"),
  querySelector: function (sel) {
    if (sel === ".plwrap") return wrap; if (sel === "[data-place-filter]") return find; if (sel === ".plnone") return none; if (sel === ".plmerge") return banner;
    if (sel === ".plempty") return null; if (sel.indexOf("meta[") === 0) return meta;
    var m = /^\.plsec-(\w+)(?: (.*))?$/.exec(sel); if (m) return m[2] ? sections[m[1]].querySelector(m[2]) : sections[m[1]];
    throw new Error("selector " + sel);
  },
  getElementById: function (id) { return byId[id]; },
  createElement: function (t) { return new El(t); },
};
global.window = global; global.matchMedia = function () { return { matches: true }; };
var data = null;
global.fetch = function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve(data); } }); };
function rows() { return byId["pl-need"].children.filter(function (li) { return li.dataset.id; }); }
function label(li) { var t = li.children[0].children[1]; return t.children[0].textContent + " | " + t.children[1].textContent + " | " + (li.children[0].children[2] ? li.children[0].children[2].textContent : "-"); }
function click(el) { wrap.on.click({ target: el }); }
"""


def _run_page(places: list[dict], then: str) -> object:
    js = (HERE / "static" / "placesmap.js").read_text()
    run = FAKE_DOM + f"data = {{places: {json.dumps(places)}, icons: {{}}}};\n" + js + "\nsetTimeout(function () {\n" + then + "\n}, 0);\n"
    out = subprocess.run(["node", "-e", run], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_to_name_lists_the_busiest_ten_then_show_all_expands_in_place():
    """The found places are a list, busiest first (the index's order): the top ten, each with its
    suggested name (the cars' navigation, else the looked-up name cut to the road), its town and
    visits and a Name it; Show all N lists the rest where they are. No single "N to name" row.
    """
    if not shutil.which("node"):
        pytest.skip("node not installed")
    found = [
        {
            "id": 100 + i,
            "n": f"Road {i}, Springfield",
            "la": 39.7,
            "lo": -89.6,
            "r": 120,
            "v": 40 - i,
            "c": "Springfield",
            "a": 1,
        }
        for i in range(14)
    ]
    found[0]["s"] = "Charlie Coffee"
    found[1]["n"] = "Delta Hardware"
    found[2]["n"], found[2]["c"] = "39.701, -89.650", ""
    named = [
        {
            "id": 5,
            "n": "Home",
            "la": 39.7,
            "lo": -89.6,
            "r": 150,
            "v": 400,
            "c": "Springfield",
            "ic": "home",
        }
    ]
    got = _run_page(
        found + named,
        """var a = rows().map(label), more = byId["pl-need"].children[byId["pl-need"].children.length - 1];
var b = more.children[0].textContent; click(more.children[0]);
console.log(JSON.stringify({first: a, more: b, after: rows().length, tail: byId["pl-need"].children.filter(function (li) { return !li.dataset.id; }).length,
  named: byId["pl-named"].children.length, n: sections.need.children[0].textContent}));""",
    )
    assert got["first"][:4] == [
        "Charlie Coffee | Springfield · 40 visits | Name it",
        "Delta Hardware | Springfield · 39 visits | Name it",
        "39.701, -89.650 | 38 visits | Name it",
        "Road 3 | Springfield · 37 visits | Name it",
    ]
    assert len(got["first"]) == 10 and got["more"] == "Show all 14"
    assert got["after"] == 14 and got["tail"] == 0, "Show all lists every one in place and goes"
    assert got["named"] == 1 and got["n"] == "14"
    js = (HERE / "static" / "placesmap.js").read_text()
    assert " to name" not in js and "plr-qb" not in js, "the one collapsed row is gone"


def test_the_picked_place_stands_out_and_the_rest_dim():
    """While the card is open: the picked marker ringed in the ink and on top (a found one
    redrawn in its own pane above the markers), every other found dot at 35%, the named ones
    dimmed by the map's class; closing puts everything back."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = (HERE / "static" / "placesmap.js").read_text()
    fn = re.search(r"  function highlight\(p, on\) \{.*?\n  \}\n", js, re.S).group(0)
    run = """
var log = [], cls = {};
function Mark(id, el) { this.id = id; this.el = el; }
Mark.prototype.setStyle = function (s) { log.push([this.id, s.opacity]); };
Mark.prototype.getElement = function () { return this.el; };
Mark.prototype.setZIndexOffset = function (z) { log.push([this.id, "z", z]); };
var named = { classList: { toggle: function (c, on) { cls[c] = on; } } };
var mapEl = { classList: { toggle: function (c, on) { cls["map." + c] = on; } } };
var all = [{ id: 1, a: 1, v: 9, la: 0, lo: 0 }, { id: 2, a: 1, v: 1, la: 0, lo: 0 }, { id: 3, v: 5 }];
var marks = { 1: new Mark(1), 2: new Mark(2), 3: new Mark(3, named) }, halo = null, selPane = {}, map = {};
var made = [];
var L = { circleMarker: function (ll, o) { made.push(o); return { addTo: function () { return this; }, remove: function () { made.push("removed"); } }; } };
var CCMap = { palette: ["", "", "", "#2F8F8F"] };
function dot(p) { return 4; }
global.getComputedStyle = function () { return { getPropertyValue: function () { return " #F0EDE4 "; } }; };
global.document = { documentElement: {} };
highlight(all[0], true); var a = { log: log.slice(), cls: Object.assign({}, cls), halo: made[0] };
log = []; highlight(all[0], false); var b = { log: log.slice(), cls: Object.assign({}, cls), removed: made.indexOf("removed") >= 0 };
log = []; highlight(all[2], true); var c = { log: log.slice(), cls: Object.assign({}, cls) };
console.log(JSON.stringify({ a: a, b: b, c: c }));
"""
    got = json.loads(subprocess.run(["node", "-e", fn + run], capture_output=True, text=True, check=True).stdout)
    assert got["a"]["cls"] == {"map.plfocus": True} and [2, 0.35] in got["a"]["log"] and [1, 0] in got["a"]["log"]
    assert got["a"]["halo"]["pane"] == "plsel" and got["a"]["halo"]["color"] == "#F0EDE4" and got["a"]["halo"]["weight"] == 3, "ringed in the ink, above the rest"
    assert got["b"]["cls"] == {"map.plfocus": False} and [2, 1] in got["b"]["log"] and [1, 1] in got["b"]["log"] and got["b"]["removed"]
    assert got["c"]["cls"]["on"] is True and [3, "z", 1000] in got["c"]["log"] and [1, 0.35] in got["c"]["log"]
    css = (HERE / "static" / "style.css").read_text()
    assert ".plfocus .leaflet-marker-icon.plm:not(.on) { opacity:.35; }" in css
    on = re.search(r"\.leaflet-marker-icon\.plm\.on \{[^}]*\}", css).group(0)
    assert "scale:1.3" in on and "var(--ink)" in on and "--gold" not in on and "--link" not in on, "selection takes the ink"
    assert 'map.createPane("plsel").style.zIndex = 650' in js


def test_the_card_names_the_picked_stop_and_points_at_it():
    js = (HERE / "static" / "placesmap.js").read_text()
    assert "title: title(p)," in js and 'function title(p) { return (p.a ? suggest(p) : p.n) + " · " + visitsText(p.v); }' in js
    card = (HERE / "static" / "placecard.js").read_text()
    assert 'head.textContent = st.o.title || "Name this place";' in card and 'head.textContent = st.o.title || (named ? "Edit place" : "Name this place");' in card
    assert 'tip.className = "pcard-tip";' in card and 'tip.classList.toggle("pcard-tip-r", left < r.left);' in card
    css = (HERE / "static" / "style.css").read_text()
    assert ".plmap { order:-1; position:relative; top:auto; height:min(40dvh, 360px); min-height:0; }" in css, "on a phone the map takes at most 40% of the screen"


def test_the_index_puts_the_most_visited_found_place_first(placed, client, monkeypatch):  # noqa: F811
    rows = ROWS + [dict(ROWS[1], id=i, name=f"Road {i}, Springfield") for i in range(10, 16)]
    monkeypatch.setattr(placed, "places_detail", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(
        placed,
        "place_stays",
        lambda: {i: {"n": n} for i, n in ((10, 3), (11, 30), (12, 7), (13, 30), (14, 1), (6, 12))},
    )
    monkeypatch.setattr(placed, "place_nav", lambda: {})
    found = [p for p in client.get("/places/index.json").json()["places"] if p.get("a")]
    assert [(p["id"], p["v"]) for p in found] == [
        (11, 30),
        (13, 30),
        (6, 12),
        (12, 7),
        (10, 3),
        (14, 1),
        (15, 0),
    ]
