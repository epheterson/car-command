"""The glyph set for places: one icon per named place, drawn in the line style of
templates/_icons.html (24-unit box, currentColor stroke, round caps). Defined once
here so the `place_icon` Jinja macro (_icons.html) and the `places` filter
(filters.py) read the same paths instead of keeping two copies in sync.

Also the guess from a name ("SF Home" -> home, "Lily's Daycare" -> kids), which is the
default icon of a place nobody chose one for, and through it the default of whether a
place is home (places.is_home: home follows the icon).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# The icon set lives in one file, static/icons/places.json (scripts/build-place-icons.py
# writes it from Lucide, ISC licence: static/icons/LICENSE): key -> [title, inner SVG
# markup, search words]. Keys are stored in cc_places.icon, so a key is never renamed: the
# first ones are the set places stored before Lucide, each drawn now with a Lucide glyph.
# The page fetches the same file, cached, for the picker's search; the server reads it once
# here to draw a stored icon inline (the `place_icon` macro, the `places` filter).
ICONS_FILE = Path(__file__).parent / "static" / "icons" / "places.json"
ICONS_BYTES = ICONS_FILE.read_bytes()
ICONS_VERSION = hashlib.md5(ICONS_BYTES).hexdigest()[:8]  # the cached file's URL changes with it
_DATA: dict[str, list[str]] = json.loads(ICONS_BYTES)

PLACE_ICONS: dict[str, str] = {k: v[1] for k, v in _DATA.items()}
ICON_KEYS: tuple[str, ...] = tuple(PLACE_ICONS)
TITLES: dict[str, str] = {k: v[0] for k, v in _DATA.items()}

# The one icon that makes a place home (places.is_home). Other house-like glyphs (a house
# with a heart, houses, a pin with a house) are places like any other; the picker says so.
HOME_ICON = "home"

# The picker's first grid, drawn into the page: the keys places stored before the larger
# set came. Everything else comes from the cached file when the picker searches or shows more.
COMMON_KEYS: tuple[str, ...] = ICON_KEYS[: ICON_KEYS.index("pin") + 1]
# The icon row's own few, one line on a phone: the place's icon goes in front, More… has the rest.
QUICK_KEYS: tuple[str, ...] = ("home", "work", "kids", "shop", "food", "coffee")


def title(key: str) -> str:
    return TITLES.get(key) or key.replace("-", " ").capitalize()


# A road with Home in its name: "Home Ave", "Evergreen Home Ranch Road". A street word
# anywhere after Home makes the name a street's, not a home's.
_STREETS = (
    "st", "street", "ave", "avenue", "rd", "road", "dr", "drive", "ln", "lane", "way", "blvd", "boulevard", "ct", "court",
    "pl", "place", "ter", "terrace", "cir", "circle", "trail", "trl", "hwy", "highway", "pkwy", "parkway", "loop", "pike", "row", "path", "alley",
)  # fmt: skip

# The guess from a name: the first entry (in this order) whose words appear in the name as
# whole words ("SF Home", "Mom's home", "home 2"), any case. " * " in a phrase is any words
# between ("home * road": "Home Ranch Road"). A value of None is a phrase that names
# something else and stops the guess there: a business with Home in its name, a road or a
# town called something-Home. Obvious words only; anything else gets no guess.
NAME_ICONS: dict[str, str | None] = {
    **dict.fromkeys(("home depot", "home goods", "homegoods", "home hardware", "home improvement"), "store"),
    **dict.fromkeys(("homewood suites", "home2 suites", "home suites", "extended stay"), "hotel"),
    **dict.fromkeys(("nursing home", "funeral home", "care home", "rest home", "retirement home", "mobile home", "mountain home"), None),
    **{f"home * {s}": None for s in _STREETS},
    "home": "home",
    **dict.fromkeys(("work", "office", "hq", "headquarters", "campus"), "work"),
    **dict.fromkeys(("daycare", "day care", "preschool", "pre-school", "nursery", "kindergarten", "montessori", "childcare", "child care"), "kids"),
    **dict.fromkeys(("college", "university"), "graduation-cap"),
    **dict.fromkeys(("school", "elementary", "middle school", "high school", "academy"), "school"),
    **dict.fromkeys(("mom", "dad", "mum", "parents", "grandma", "grandpa", "granny", "nana", "papa", "family", "aunt", "uncle", "in-laws"), "family"),
    "friend": "friend",
    **dict.fromkeys(("friends",), "friend"),
    **dict.fromkeys(("grocery", "groceries", "market", "supermarket", "safeway", "trader joe's", "whole foods", "costco", "aldi", "kroger", "sprouts"), "shop"),
    **dict.fromkeys(("mall", "store", "shop", "shopping", "outlet", "outlets", "target", "ikea", "walmart"), "store"),
    **dict.fromkeys(("coffee", "cafe", "café", "starbucks", "peet's", "espresso"), "coffee"),
    **dict.fromkeys(("restaurant", "diner", "pizza", "pizzeria", "sushi", "taqueria", "bistro", "grill", "burger"), "food"),
    **dict.fromkeys(("bar", "pub", "brewery", "taproom", "winery", "tavern", "saloon"), "bar"),
    **dict.fromkeys(("gym", "fitness", "ymca", "crossfit", "yoga", "pilates", "climbing"), "gym"),
    **dict.fromkeys(("doctor", "hospital", "clinic", "medical", "urgent care", "kaiser", "pediatrician"), "doctor"),
    **dict.fromkeys(("dentist", "dental", "orthodontist", "orthodontics"), "dentist"),
    **dict.fromkeys(("pharmacy", "cvs", "walgreens", "rite aid", "drugstore"), "pharmacy"),
    **dict.fromkeys(("vet", "veterinary", "veterinarian", "animal hospital", "pet", "petco", "petsmart", "groomer", "dog park"), "pet"),
    **dict.fromkeys(("car wash", "mechanic", "auto repair", "tire", "tires", "dealership", "dealer", "service center", "body shop", "oil change", "smog"), "service"),
    **dict.fromkeys(("gas", "gas station", "fuel", "shell", "chevron", "arco", "exxon", "valero"), "gas"),
    **dict.fromkeys(("charger", "supercharger", "chargepoint", "electrify america", "evgo", "ev charging", "charging station"), "charge"),
    **dict.fromkeys(("parking", "parking lot", "car park"), "parking"),
    **dict.fromkeys(("park", "playground", "trail", "trailhead", "garden", "gardens", "preserve"), "park"),
    **dict.fromkeys(("beach",), "beach"),
    **dict.fromkeys(("ski", "mountain", "ski resort"), "mountain"),
    **dict.fromkeys(("cinema", "movies", "movie theater", "movie theatre", "amc", "regal"), "clapperboard"),
    **dict.fromkeys(("bakery", "boulangerie"), "croissant"),
    **dict.fromkeys(("ice cream", "gelato", "frozen yogurt"), "ice-cream-cone"),
    **dict.fromkeys(("donut", "donuts", "doughnut", "doughnuts"), "donut"),
    **dict.fromkeys(("bank", "credit union", "atm"), "landmark"),
    **dict.fromkeys(("post office", "usps", "fedex", "ups store"), "mail"),
    **dict.fromkeys(("salon", "barber", "barbershop", "hair", "nails"), "scissors"),
    **dict.fromkeys(("laundry", "laundromat", "dry cleaner", "dry cleaners"), "washing-machine"),
    **dict.fromkeys(("hardware", "lowe's", "ace hardware"), "hammer"),
    **dict.fromkeys(("pool", "swim", "swimming", "aquatic", "aquatics"), "waves-ladder"),
    **dict.fromkeys(("golf", "golf course", "country club"), "flag"),
    **dict.fromkeys(("farm", "ranch", "orchard"), "tractor"),
    **dict.fromkeys(("marina", "harbor", "harbour", "yacht club"), "sailboat"),
    **dict.fromkeys(("ferry",), "ship"),
    **dict.fromkeys(("bus", "bus stop"), "bus"),
    **dict.fromkeys(("camp", "campground", "campsite", "rv park"), "tent"),
    **dict.fromkeys(("zoo", "aquarium"), "panda"),
    **dict.fromkeys(("museum", "city hall", "town hall", "dmv"), "landmark"),
    **dict.fromkeys(("courthouse", "court house"), "gavel"),
    **dict.fromkeys(("police",), "shield"),
    **dict.fromkeys(("fire station",), "flame"),
    **dict.fromkeys(("storage", "self storage", "warehouse"), "warehouse"),
    **dict.fromkeys(("mosque", "masjid"), "mosque"),
    **dict.fromkeys(("stadium", "arena", "theater", "theatre", "venue", "concert", "ballpark"), "venue"),
    **dict.fromkeys(("church", "temple", "synagogue", "chapel", "cathedral", "gurdwara"), "worship"),
    **dict.fromkeys(("library",), "library"),
    **dict.fromkeys(("hotel", "motel", "inn", "lodge", "airbnb"), "hotel"),
    **dict.fromkeys(("airport", "terminal", "sfo"), "travel"),
    **dict.fromkeys(("station", "train", "bart", "caltrain", "amtrak", "transit", "metro", "subway"), "transit"),
}

# A nearby suggestion's map kind (an OpenStreetMap tag value, underscores as the add-on
# sends them or spaces) -> its icon, for the picker.
KIND_ICONS: dict[str, str] = {
    **dict.fromkeys(("supermarket", "convenience", "grocery", "greengrocer", "marketplace"), "shop"),
    **dict.fromkeys(("mall", "department_store", "clothes", "hardware", "doityourself", "electronics", "furniture"), "store"),
    **dict.fromkeys(("restaurant", "fast_food", "food_court", "bakery"), "food"),
    **dict.fromkeys(("cafe", "coffee"), "coffee"),
    **dict.fromkeys(("bar", "pub", "biergarten", "nightclub"), "bar"),
    "charging_station": "charge",
    **dict.fromkeys(("fuel",), "gas"),
    **dict.fromkeys(("parking", "parking_space", "parking_entrance"), "parking"),
    **dict.fromkeys(("kindergarten", "childcare"), "kids"),
    **dict.fromkeys(("school", "college", "university"), "school"),
    **dict.fromkeys(("fitness_centre", "sports_centre", "gym"), "gym"),
    **dict.fromkeys(("hospital", "clinic", "doctors"), "doctor"),
    "dentist": "dentist",
    **dict.fromkeys(("pharmacy", "chemist"), "pharmacy"),
    **dict.fromkeys(("veterinary", "pet"), "pet"),
    **dict.fromkeys(("car_repair", "car_wash", "car", "tyres"), "service"),
    **dict.fromkeys(("park", "playground", "garden", "nature_reserve"), "park"),
    **dict.fromkeys(("beach_resort", "beach"), "beach"),
    **dict.fromkeys(("stadium", "theatre", "cinema", "arts_centre", "sports_hall"), "venue"),
    "place_of_worship": "worship",
    "library": "library",
    **dict.fromkeys(("hotel", "motel", "guest_house", "hostel"), "hotel"),
    **dict.fromkeys(("aerodrome", "airport", "terminal"), "travel"),
    **dict.fromkeys(("station", "train_station", "bus_station", "ferry_terminal"), "transit"),
    **dict.fromkeys(("office", "company"), "work"),
    "bank": "landmark",
    "post_office": "mail",
    "hairdresser": "scissors",
    **dict.fromkeys(("laundry", "dry_cleaning"), "washing-machine"),
    "cinema": "clapperboard",
    "ice_cream": "ice-cream-cone",
    "bakery": "croissant",
    "swimming_pool": "waves-ladder",
    "bicycle": "bike",
    **dict.fromkeys(("camp_site", "caravan_site"), "tent"),
    **dict.fromkeys(("museum", "townhall"), "landmark"),
    "courthouse": "gavel",
    "police": "shield",
    "fire_station": "flame",
    "zoo": "panda",
    "bus_station": "bus",
    "ferry_terminal": "ship",
    **dict.fromkeys(("university", "college"), "graduation-cap"),
    **dict.fromkeys(("hardware", "doityourself"), "hammer"),
    "golf_course": "flag",
    "marina": "sailboat",
    "farm": "tractor",
    "books": "book",
    "florist": "flower",
}
KIND_ICONS.update({k.replace("_", " "): v for k, v in list(KIND_ICONS.items()) if "_" in k})


def _pattern(phrase: str) -> re.Pattern:
    # A whole word or phrase, no letter or digit right before or after it; " * " is any words
    # between two whole-word parts. shell.js builds the same pattern from the same phrase.
    parts = [r"(?<![^\W_])" + re.escape(part) + r"(?![^\W_])" for part in phrase.split(" * ")]
    return re.compile(".*".join(parts))


_PATTERNS = [(_pattern(p), icon) for p, icon in NAME_ICONS.items()]


def icon_for_name(name: str | None) -> str | None:
    """The icon a name suggests (NAME_ICONS, first match wins), or None. shell.js's
    iconForName is the same rule, reading the same map from the page."""
    n = " ".join((name or "").lower().split())
    if not n:
        return None
    for pattern, icon in _PATTERNS:
        if pattern.search(n):
            return icon
    return None


_WRAP = (
    '<svg class="pl-ic pl-{k}" viewBox="0 0 24 24" aria-hidden="true" focusable="false" '
    'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{d}</svg>'
)


def svg(key: str | None) -> str:
    """The full inline <svg> for an icon key, or "" when the key is unknown or None —
    the caller then renders the place name with no glyph at all."""
    d = PLACE_ICONS.get(key or "")
    return _WRAP.format(k=key, d=d) if d else ""


def resolve_icon(name: str | None, icon: str | None) -> str | None:
    """The icon key a place actually shows.

    An explicit stored value wins, including the literal "none" (no icon, even for a
    place named Home). With nothing stored (icon IS NULL), the guess from its name
    (icon_for_name): "SF Home" shows the house, "Lily's Daycare" the stroller.
    """
    if icon is not None:
        return None if icon == "none" or icon not in PLACE_ICONS else icon
    return icon_for_name(name)
