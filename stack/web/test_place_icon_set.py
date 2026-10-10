"""The larger icon set (Lucide, vendored as one cached file): old names keep drawing, only
the house makes a place home, the icon row carries a few and fetches the rest
for its search, and the list page does not grow for it. Also the size's typed number."""

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import placeicons
from placeicons import COMMON_KEYS, HOME_ICON, QUICK_KEYS, ICON_KEYS, ICONS_VERSION, KIND_ICONS, NAME_ICONS, PLACE_ICONS, icon_for_name, resolve_icon, svg
from places import is_home
from test_empty import bare, client  # noqa: F401 - fixtures
from test_picker import PAGE, card_html, env

HERE = Path(__file__).parent
ROOT = HERE.parent.parent

# Every key a place could have stored before the larger set; none may stop drawing.
OLD_KEYS = (
    "home work family friend kids school shop store food coffee bar gym doctor dentist pharmacy pet service gas charge "
    "parking park beach mountain venue worship library hotel travel transit star pin"
).split()


def test_every_old_name_keeps_its_place_and_draws_a_new_glyph():
    assert list(ICON_KEYS[: len(OLD_KEYS)]) == OLD_KEYS and COMMON_KEYS == tuple(OLD_KEYS), "the old keys lead, as the picker's common grid"
    for key in OLD_KEYS:
        assert resolve_icon("Somewhere", key) == key and svg(key).startswith("<svg"), key
    assert "M3 10a2 2 0 0 1 .709-1.528l7-6" in PLACE_ICONS["home"], "home is drawn with Lucide's house"
    assert "M7.5 4C5.2 4" in PLACE_ICONS["dentist"], "Lucide has no tooth: the dentist keeps its own"
    assert "house" not in PLACE_ICONS and "map-pin" not in PLACE_ICONS, "a glyph an old key took is not offered twice"
    assert "storefront" in PLACE_ICONS and placeicons.title("storefront") == "Storefront"


def test_the_set_is_a_few_hundred_icons_each_with_a_title_glyph_and_words():
    data = json.loads(placeicons.ICONS_FILE.read_text())
    assert 250 <= len(data) <= 400
    for key, (title, paths, words) in data.items():
        assert re.fullmatch(r"[a-z0-9-]+", key) and title and words, key
        assert paths.startswith("<") and "<script" not in paths and "on" not in re.sub(r'"[^"]*"', "", paths).replace("polygon", "").replace("polyline", ""), key
    for key in ("pizza", "dog", "cat", "bus", "tent", "landmark", "mosque", "graduation-cap", "dog", "hospital", "bike", "croissant"):
        assert key in data, key
    assert "pizza" in data["pizza"][2] and "dog" in data["dog"][2]


def test_the_licence_and_the_credit_go_with_the_icons():
    lic = (HERE / "static" / "icons" / "LICENSE").read_text()
    assert lic.startswith("ISC License") and "Lucide Icons and Contributors" in lic and "Permission to use, copy, modify" in lic
    assert "Cole Bemis" in lic, "the Feather-derived icons' MIT notice comes along"
    readme = (ROOT / "README.md").read_text()
    assert "[Lucide](https://lucide.dev)" in readme and "ISC" in readme


def test_only_the_house_makes_a_place_home():
    assert HOME_ICON == "home" and is_home({"icon": "home", "name": "Cabin"})
    housey = [k for k in ICON_KEYS if k != HOME_ICON and re.search(r"\b(house|home|houses)\b", json.loads(placeicons.ICONS_FILE.read_text())[k][2])]
    assert {"house-heart", "houses", "map-pin-house", "house-plus"} <= set(housey)
    for key in housey:
        assert not is_home({"icon": key, "name": "Home"}), f"{key} is a place like any other, even named Home"
    assert icon_for_name("Home") == "home" and icon_for_name("SF Home") == "home"
    assert "The house icon marks home." in card_html()


def test_new_guesses_reach_the_larger_set_and_every_guess_is_real():
    assert icon_for_name("Springfield Bakery") == "croissant" and icon_for_name("Springfield Mosque") == "mosque"
    assert icon_for_name("Springfield University") == "graduation-cap" and icon_for_name("Bravo Bus Station") == "bus"
    assert icon_for_name("Movie Theater") == "clapperboard" and icon_for_name("Springfield Theater") == "venue"
    assert KIND_ICONS["bakery"] == "croissant" and KIND_ICONS["cinema"] == "clapperboard" and KIND_ICONS["supermarket"] == "shop"
    assert (set(NAME_ICONS.values()) - {None}) | set(KIND_ICONS.values()) <= set(PLACE_ICONS)


def test_the_card_carries_a_few_icons_and_searches_the_rest():
    html = card_html()
    assert html.count('type="radio" name="icon"') == len(QUICK_KEYS) + 1, "the row's few and None; every other icon is fetched when More… opens"
    assert f'data-icons-v="{ICONS_VERSION}"' in html and ICONS_VERSION == hashlib.md5(placeicons.ICONS_FILE.read_bytes()).hexdigest()[:8]
    box = re.search(r"<input type=\"search\" class=\"icosearch\"[^>]*>", html).group(0)
    assert "name=" not in box and 'class="icofull" hidden' in html, "a search box the form never sends, in a box More… opens"
    assert ">More…</button>" in html
    js = (HERE / "static" / "placecard.js").read_text()
    assert '"/icons/places.json?v="' in js and "e.preventDefault();" in js[js.index('if (e.key === "Enter") {') :][:200], "Enter in the search picks, never submits"


def test_a_stored_icon_from_the_larger_set_shows_checked_in_the_row():
    base = {"auto": False, "visits": 3, "address": None, "user_address": None, "home": False, "price": None, "parts": [], "city": "", "latitude": 40.7, "longitude": -74.0, "radius_m": 150}
    html = env().get_template("place.html").render(**PAGE, now=datetime.now(timezone.utc), p={**base, "id": 4, "name": "Luigi's", "icon": "pizza"}, st=None, choices=[], maps_on=False, new=False)
    row = html[html.index('class="icoquick"') : html.index('class="icofull"')]
    assert 'value="pizza" aria-label="Icon: Pizza" checked' in row, "the row holds it first, checked, so a Save keeps it"


def test_the_icon_file_is_served_whole_and_kept(bare, client):  # noqa: F811
    r = client.get(f"/icons/places.json?v={ICONS_VERSION}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json() == json.loads(placeicons.ICONS_FILE.read_text())
    cache = r.headers["cache-control"]
    assert "max-age=31536000" in cache and "immutable" in cache and "private" in cache
    r = client.get(f"/icons/places.json?v={ICONS_VERSION}", headers={"accept-encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip", "about a third of its size on the wire"


def test_a_posted_icon_from_the_larger_set_is_accepted_and_a_made_up_one_is_not(bare):  # noqa: F811
    from fastapi import HTTPException

    assert bare._valid_icon("pizza") == "pizza" and bare._valid_icon("home") == "home" and bare._valid_icon("none") == "none"
    with pytest.raises(HTTPException):
        bare._valid_icon("house")  # Lucide's name for it; the stored key is home


def test_the_list_page_is_its_frame_and_no_more():
    """Before the larger set this page weighed 63.7 KB (30 named, 6 found, the picker in it); with
    every row in it, a few hundred found places would have made it many times that. Now the page
    is its frame, the same at any count: the places come from /places/index.json and the map's
    scripts wait, unrun, in a template. (To name holds the room of at most ten rows.)"""
    sizes = [len(env().get_template("places.html").render(**PAGE, now=datetime.now(timezone.utc), named=n, found=f, merge=None).encode()) for n, f in ((30, 12), (60, 600))]
    assert abs(sizes[0] - sizes[1]) < 16 and sizes[0] <= 24_000, sizes


SIZES = [
    # (typed, size now, leaving the field) -> (size, changed, what the field then shows)
    (("150", 150, False), (150, False, None)),
    (("15", 150, False), (150, False, None)),  # on the way to 150: nothing yet
    (("300", 150, False), (300, True, None)),
    (("300", 300, True), (300, False, None)),  # the same number again is no resize
    (("5000", 150, True), (1000, True, 1000)),  # clamped once let go
    (("10", 150, True), (25, True, 25)),
    (("abc", 150, True), (150, False, 150)),  # not a number: the size comes back
    (("", 150, False), (150, False, None)),
    (("1500", 1500, True), (1500, False, None)),  # a place bigger than the slider stays so untouched
    (("1400", 1500, True), (1000, True, 1000)),
    (("212.6", 150, False), (213, True, None)),
]


def test_a_typed_size_follows_the_slider_rules():
    js = (HERE / "static" / "shell.js").read_text()
    assert 'if (flag) flag.value = "1";' in js and "if (!t.changed) return;" in js, "resized is set only by a real change"
    assert 'number.form.addEventListener("submit", function () { typed(true); }, true)' in js
    html = (HERE / "templates" / "place.html").read_text()
    assert 'id="pd-r-n" type="number"' in html and 'name="radius_m"' in html and html.count('name="radius_m"') == 1, "the slider carries the size; the number has no name"
    num = re.search(r'<input id="pd-r-n"[^>]*>', html).group(0)
    assert " min=" not in num and " max=" not in num and "data-min" in num, "no browser range check that would block a Save of a 1500 m place"
    if not shutil.which("node"):
        pytest.skip("node not installed")
    fn = re.search(r"  function sizeTyped\(.*?\n  \}\n", js, re.S).group(0)
    cases = [list(a) for a, _ in SIZES]
    out = subprocess.run(["node", "-e", fn + f"console.log(JSON.stringify({json.dumps(cases)}.map(c => sizeTyped(c[0], c[1], 25, 1000, c[2]))))"], capture_output=True, text=True, check=True).stdout
    assert [(o["v"], o["changed"], o["show"]) for o in json.loads(out)] == [b for _, b in SIZES]
