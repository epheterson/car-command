"""The naming card: the name on top, prefilled with the best suggestion and the rest as chips,
the icon row, the price, Save. The icon guess from a typed name, the owner's own address on a
place's page, and a card that has its final size before the lookup answers."""

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import jinja2
import pytest

import filters
from placeicons import KIND_ICONS, NAME_ICONS, PLACE_ICONS, icon_for_name
from places import posted_address, starts_with_word
from test_empty import bare, client  # noqa: F401 - fixtures

HERE = Path(__file__).parent

NAMES = [
    ("Home", "home"),
    ("home 2", "home"),
    ("  HOME (cabin) ", "home"),
    ("SF Home", "home"),
    ("Mom's home", "home"),
    ("Springfield home", "home"),
    ("Home Depot", "store"),
    ("HomeGoods", "store"),
    ("Home Goods", "store"),
    ("Homewood Suites", "hotel"),
    ("Home2 Suites", "hotel"),
    ("Homeward Lane", None),
    ("Evergreen Nursing Home", None),
    ("12 Home Ave", None),
    ("Evergreen Home Ranch Road", None),
    ("Home Ranch Road, Springfield", None),
    ("Mom's Home on Elm Street", None),
    ("Work", "work"),
    ("Workshop", None),
    ("Office", "work"),
    ("Gym", "gym"),
    ("School", "school"),
    ("Alpha's Daycare", "kids"),
    ("Bravo Preschool", "kids"),
    ("Springfield Elementary School", "school"),
    ("Grocery", "shop"),
    ("Market Basket", "shop"),
    ("Bravo Market", "shop"),
    ("Airport", "travel"),
    ("Hotel 2", "hotel"),
    ("Supercharger", "charge"),
    ("Charger", "charge"),
    ("Dr. Bravo Dentist", "dentist"),
    ("Springfield Hospital", "doctor"),
    ("Charlie Coffee", "coffee"),
    ("Moe's Bar", "bar"),
    ("Barbara", None),
    ("Evergreen Park", "park"),
    ("Charlie Vet", "pet"),
    ("Springfield Station", "transit"),
    ("", None),
]


def env():
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(HERE / "templates")))
    filters.register(e)
    return e


def card_html() -> str:
    return env().get_template("_placecard.html").render(root="", cur="$")


@pytest.mark.parametrize("name,icon", NAMES)
def test_a_typed_name_suggests_an_icon_only_for_the_obvious_words(name, icon):
    assert icon_for_name(name) == icon


def test_the_old_word_rule_is_kept_for_the_migration():
    assert starts_with_word("home 2", "home") and not starts_with_word("Homewood Suites", "home") and not starts_with_word("Home Depot", "home")
    assert not starts_with_word("SF Home", "home"), "what the old rule missed"


def test_every_guess_is_a_real_icon():
    assert (set(NAME_ICONS.values()) - {None}) | set(KIND_ICONS.values()) <= set(PLACE_ICONS)


def test_the_page_runs_the_same_rule_and_a_hand_picked_icon_sticks():
    """shell.js's iconForName against the map the page carries, on the same cases; and the
    guess runs only while the form has no icon chosen by hand."""
    js = (HERE / "static" / "placecard.js").read_text()
    assert "if (!form || form.dataset.iconHand) return;" in js, "the guess is skipped once an icon is chosen by hand"
    assert 'input.form.dataset.iconHand = "1"' in js and "e.isTrusted" in js, "only a real tap marks the icon as chosen"
    if not shutil.which("node"):
        pytest.skip("node not installed")
    fn = re.search(r"  var patterns = \{\};.*?function iconForName\(.*?\n  \}\n", js, re.S).group(0)
    m = re.search(r"data-name-icons='([^']*)'", card_html()).group(1)
    names = [n for n, _ in NAMES]
    out = subprocess.run(["node", "-e", fn + f"var map = {m}; console.log(JSON.stringify({json.dumps(names)}.map(n => iconForName(n, map))))"], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [i for _, i in NAMES]


def test_the_card_reads_top_to_bottom_name_chips_icons_price_save():
    html = card_html()
    order = ['name="name"', 'class="pcard-chips"', 'class="icorow"', 'name="price_kind"', 'class="pcard-details"', 'class="pcard-save"']
    at = [html.index(x) for x in order]
    assert at == sorted(at), "the name, its suggestions, the icons, the price, then Details and Save"
    assert "blank for the address" in html and "data-auto-icon" in html, "a blank name saves as the address, and says so; a typed one guesses its icon"
    assert html.count('class="pchip-skel"') == 3, "the chips hold their row while the lookup runs"
    assert 'name="address_lookup"' in html and 'role="dialog"' in html and 'aria-labelledby="pcard-h"' in html
    assert json.loads(re.search(r"data-kind-icons='([^']*)'", html).group(1))["supermarket"] == "shop"
    assert 'class="pcard-confirm" hidden' in html, "the merge confirm is a second view of the same card"


def test_no_page_carries_the_card_until_a_name_is_tapped(bare, client):  # noqa: F811
    base = (HERE / "templates" / "base.html").read_text()
    assert "_picker.html" not in base and "_placecard.html" not in base, "the card's markup is fetched on the first tap"
    assert not (HERE / "templates" / "_picker.html").exists(), "the old dialog is gone: one card"
    r = client.get("/places/card")
    assert r.status_code == 200 and 'class="pcard"' in r.text and "private" in r.headers["cache-control"]


def test_nothing_in_the_card_changes_size_when_the_lookup_answers():
    css = (HERE / "static" / "style.css").read_text()
    rule = lambda sel: re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css).group(1).replace(" ", "")  # noqa: E731
    assert "min-height:28px" in rule(".pcard-chips"), "the chips' row is there, loading or loaded"
    assert "height:27px" in rule(".pchip-skel")
    assert "font-size:16px" in rule('.pcard input[name="name"]'), "16px, or iOS zooms on focus"
    assert "font-size:16px" in rule('.picker-body input[name="name"], .picker-body input[name="address"]')
    assert "min-height:20px" in rule(".pa-top"), "the reset link appearing does not move the address field"
    assert "font-size:16px" in rule(".plhead .plfind")


def test_posted_address_keeps_a_correction_and_clears_on_the_looked_up_one():
    assert posted_address("", "742 Evergreen Terrace") == (False, None), "blank leaves the place's own address alone"
    assert posted_address("  12  Elm Street ", "742 Evergreen Terrace") == (True, "12 Elm Street")
    assert posted_address("742 Evergreen Terrace", "742 Evergreen Terrace ") == (True, None), "the looked-up one again clears it"
    assert posted_address("12 Elm Street", "") == (True, "12 Elm Street")


PAGE = dict(
    root="",
    car={"name": "Alpha", "lat": None, "lon": None, "address": None},
    active="places",
    vehicles=[{"vin": "V1", "name": "Alpha", "live": True}],
    q="",
    colors={"V1": 0},
    names={"V1": "Alpha"},
    theme="system",
    week_start=0,
    alert_kinds=[],
    levels={},
    here="/places",
    css_v="x",
    js_v="y",
    csrf="t",
    auth_on=False,
    connector=False,
    findings=[],
    cur="$",
)


def place_page(p: dict) -> str:
    base = {"auto": False, "visits": 3, "address": None, "user_address": None, "icon": None, "home": False, "price": None, "parts": [], "city": ""}
    return env().get_template("place.html").render(**PAGE, now=datetime.now(timezone.utc), p={**base, **p}, st=None, choices=[], maps_on=True, new=False)


def test_a_places_page_shows_the_owners_address_with_a_reset():
    home = place_page(
        {"id": 1, "name": "Home", "latitude": 39.8, "longitude": -89.65, "radius_m": 150, "user_address": "12 Elm Street, Springfield", "home": True, "street": "Oak Avenue, Springfield"}
    )
    assert 'value="12 Elm Street, Springfield"' in home and 'name="address_lookup" value="Oak Avenue, Springfield"' in home
    assert 'data-reset-to="Oak Avenue, Springfield">Use the looked-up address' in home, "the reset shows when the owner's address differs"
    gym = place_page({"id": 2, "name": "Gym", "latitude": 39.8, "longitude": -89.65, "radius_m": 150, "street": "Pine Road, Springfield"})
    assert 'value="Pine Road, Springfield"' in gym and 'data-reset-to="Pine Road, Springfield" hidden>' in gym, "no reset with nothing typed"
    assert "data-auto-icon" in gym and "oninput=" not in gym, "the name guesses its icon through shell.js"


def test_every_row_says_its_town_and_the_owners_address_wins():
    from placespage import city_of, index_rows

    assert city_of({"user_address": "12 Elm Street, Shelbyville", "address": "742 Evergreen Terrace, Springfield, Sangamon County, Illinois, 62701, United States"}) == "Shelbyville"
    assert (
        city_of({"user_address": "the blue house", "address": "742, Evergreen Terrace, Springfield, Sangamon County, Illinois, 62701, United States"}) == "Springfield"
    ), "no town in the owner's own: the looked-up one"
    assert city_of({"street": "Pine Road, Springfield"}) == "Springfield"
    assert city_of({"auto": True, "name": "Maple Street, Capital City"}) == "Capital City"
    assert city_of({"name": "Gym"}) == ""
    every = [
        {"id": 1, "name": "Home", "latitude": 39.8, "longitude": -89.65, "radius_m": 150, "auto": False, "visits": 10, "icon": "home", "user_address": "12 Elm Street, Shelbyville"},
        {"id": 3, "name": "Maple Street, Springfield", "latitude": 39.81, "longitude": -89.64, "radius_m": 150, "auto": True, "visits": 2, "address": "Maple Street, Springfield"},
    ]
    rows = index_rows(every, {3: {"n": 4}}, {3: {"name": "Bravo Market"}}, city_of, lambda p: p.get("icon"))
    assert [r["id"] for r in rows] == [3, 1], "the found place, to name, comes first"
    assert rows[0] == {"id": 3, "n": "Maple Street, Springfield", "la": 39.81, "lo": -89.64, "r": 150, "v": 4, "c": "Springfield", "a": 1, "s": "Bravo Market"}
    assert rows[1]["c"] == "Shelbyville" and rows[1]["ic"] == "home" and rows[1]["v"] == 0 and "a" not in rows[1], "visits come from the stays, never the stored column"


class Conn:
    """cc_places in a list, for the routes."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        res = type("R", (), {"fetchall": lambda _s: [], "fetchone": lambda _s: None})()
        if s.startswith("SELECT id, name, latitude, longitude, radius_m"):
            res.fetchall = lambda: [dict(r) for r in self.rows]
        elif s.startswith("UPDATE cc_places SET user_address"):
            for r in self.rows:
                if r["id"] == args[1]:
                    r["user_address"] = args[0]
        elif s.startswith("UPDATE cc_places SET name"):
            for r in self.rows:
                if r["id"] == args[-1]:
                    r["name"] = args[0]
        return res

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_pinning_stores_the_typed_address_and_a_blank_name_saves_as_it(bare, client, monkeypatch):  # noqa: F811
    rows = [
        {
            "id": 7,
            "name": "Evergreen Terrace, Springfield",
            "latitude": 39.8010,
            "longitude": -89.6500,
            "radius_m": 150,
            "auto": True,
            "address": "742 Evergreen Terrace",
            "user_address": None,
            "price": None,
        }
    ]
    monkeypatch.setattr(bare, "db", lambda *a, **k: Conn(rows))
    form = {"lat": "39.8011", "lon": "-89.6501", "name": "", "address": "12 Elm Street", "address_lookup": "742 Evergreen Terrace", "csrf_token": bare.auth.csrf_token(bare.AUTH["secret"], "")}
    r = client.post("/places/pin", data=form)
    assert r.status_code == 303
    assert rows[0]["name"] == "12 Elm Street" and rows[0]["user_address"] == "12 Elm Street", "blank name: the place is named by the corrected address"
    # the candidates route hands the picker the owner's address and the looked-up one back
    monkeypatch.setattr(bare.MAPS, "candidates", lambda lat, lon: None)
    got = client.get("/places/candidates?lat=39.8011&lon=-89.6501").json()
    assert got["user_address"] == "12 Elm Street" and got["place_address"] == "742 Evergreen Terrace" and got["available"] is False
    # the looked-up address again: reset
    r = client.post("/places/pin", data=dict(form, name="Home", address="742 Evergreen Terrace"))
    assert rows[0]["name"] == "Home" and rows[0]["user_address"] is None
    # no address posted at all leaves it be
    rows[0]["user_address"] = "12 Elm Street"
    client.post("/places/pin", data=dict(form, name="Home", address=""))
    assert rows[0]["user_address"] == "12 Elm Street"


def test_the_now_hero_shows_the_places_own_address(bare, monkeypatch):  # noqa: F811
    from places import PlaceIndex

    here = {"id": 1, "name": "Home", "latitude": 39.8, "longitude": -89.65, "radius_m": 150, "price": None, "address": None, "user_address": "12 Elm Street, Springfield"}
    monkeypatch.setattr(bare, "places", lambda: PlaceIndex([here]))
    car = {"name": "Alpha", "position": {"latitude": {"val": 39.8}, "longitude": {"val": -89.65}, "position_location": {"road": {"val": "Evergreen Terrace"}, "city": {"val": "Springfield"}}}}
    monkeypatch.setattr(bare, "garage", lambda: {"V1": car})
    v = bare.live_vehicle("V1")
    assert v["place"] == "Home" and v["address"] == "12 Elm Street, Springfield"
    here["user_address"] = None
    assert bare.live_vehicle("V1")["address"] == "Evergreen Terrace, Springfield", "without one, the car's own lookup"
