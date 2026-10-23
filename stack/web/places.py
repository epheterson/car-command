"""Named places: Home, Work, whatever the driver labels. Geometry, and the table.

A position belongs to the nearest place whose radius contains it. Distances
use the haversine formula on a sphere, good to a metre at these scales.
"""

from __future__ import annotations

import logging
import math

from placeicons import HOME_ICON, icon_for_name

LOG = logging.getLogger("carcommand.places")

DDL = """
CREATE TABLE IF NOT EXISTS cc_places (
    id        SERIAL PRIMARY KEY,
    name      VARCHAR NOT NULL,
    latitude  DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    radius_m  DOUBLE PRECISION NOT NULL DEFAULT 150
);
ALTER TABLE cc_places ADD COLUMN IF NOT EXISTS auto BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE cc_places ADD COLUMN IF NOT EXISTS visits INTEGER;
ALTER TABLE cc_places ADD COLUMN IF NOT EXISTS address VARCHAR;
ALTER TABLE cc_places ADD COLUMN IF NOT EXISTS icon VARCHAR;
-- What charging here costs, as placeprice.py describes it; NULL is not set (Home then
-- takes the home plan, anywhere else nothing).
ALTER TABLE cc_places ADD COLUMN IF NOT EXISTS price JSONB;
-- The address the owner typed over the looked-up one (the picker, the Places card); shown
-- wherever the place's address is, never geocoded or sent anywhere. NULL: use `address`.
ALTER TABLE cc_places ADD COLUMN IF NOT EXISTS user_address VARCHAR;
-- A place merged into another (that place's id): its circle answers as that place
-- (places.resolve_merged), so the stops inside it keep their place, and history never
-- finds it again. Removing the place removes these with it (app.delete_place).
ALTER TABLE cc_places ADD COLUMN IF NOT EXISTS merged_into INTEGER;
"""

EARTH_M = 6_371_000.0


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(a))


CELL = 0.02  # degrees, about 2 km: larger than any place radius, so a point's place is in its cell or a neighbour


class PlaceIndex:
    """Places bucketed on a 0.02-degree grid so a lookup tests a handful, not all of them.
    With 568 places and 13,000 trips the flat scan cost 2.4 s a page; this is milliseconds."""

    def __init__(self, places: list[dict]) -> None:
        self.cells: dict[tuple[int, int], list[dict]] = {}
        self.places = places
        for p in places:
            self.cells.setdefault((int(p["latitude"] // CELL), int(p["longitude"] // CELL)), []).append(p)

    def near(self, lat: float, lon: float) -> list[dict]:
        ci, cj = int(lat // CELL), int(lon // CELL)
        out: list[dict] = []
        for i in (ci - 1, ci, ci + 1):
            for j in (cj - 1, cj, cj + 1):
                out.extend(self.cells.get((i, j), ()))
        return out

    def __iter__(self):
        return iter(self.places)

    def __len__(self):
        return len(self.places)


def place_for(lat: float | None, lon: float | None, places) -> dict | None:
    """The place containing (lat, lon), nearest first; None when no place's radius covers it.
    `places` is a list or a PlaceIndex; a list is scanned whole."""
    if lat is None or lon is None:
        return None
    candidates = places.near(lat, lon) if isinstance(places, PlaceIndex) else places
    best = None
    for p in candidates:
        d = distance_m(lat, lon, p["latitude"], p["longitude"])
        if d <= p["radius_m"] and (best is None or d < best[0]):
            best = (d, p)
    return best[1] if best else None


def label(lat: float | None, lon: float | None, places: list[dict], fallback: str | None) -> str:
    p = place_for(lat, lon, places)
    return p["name"] if p else (fallback or "Unnamed spot")


def is_home_name(name: str | None) -> bool:
    """Whether a name says home: the word Home anywhere in it as a whole word ("Home",
    "home 2", "SF Home", "Mom's home"), any case, but not a business or a street with Home
    in its name ("Home Depot", "Homewood Suites", "Home Ave"). The default icon's rule
    (placeicons.icon_for_name); whether a place is home follows its icon (is_home)."""
    return icon_for_name(name) == HOME_ICON


def starts_with_word(name: str | None, word: str) -> bool:
    """Whether `name` is `word` on its own or followed by a number or punctuation, any
    case: the name rule for home before home followed the icon. migrate_home keeps the
    places it said were home at home."""
    n = (name or "").strip().lower()
    if not n.startswith(word):
        return False
    rest = n[len(word) :].lstrip()
    return not rest or not rest[0].isalpha()


def posted_address(address: str | None, lookup: str | None) -> tuple[bool, str | None]:
    """What a form's address field says to store as the place's own address: (whether to
    touch it, the value). Blank leaves it as it was; the looked-up address again clears it
    (the place shows the looked-up one); anything else is the owner's correction."""
    typed = " ".join((address or "").split())[:200]
    if not typed:
        return False, None
    return True, None if typed == " ".join((lookup or "").split()) else typed


def is_home(p: dict | None) -> bool:
    """Whether a place is home: its icon is the house (HOME_ICON, and only that one: a
    house with a heart or a pin with a house is a place like any other). A place whose icon nobody chose
    (NULL) takes the guess from its name (is_home_name), unless it was found from history
    and is named by its address. The one definition: the facts' pricing, the importers,
    the live pollers, Now and the alerts all ask this."""
    if not p:
        return False
    icon = p.get("icon")
    if icon is not None:
        return icon == HOME_ICON
    return not p.get("auto") and is_home_name(p.get("name"))


# The fields a place merged into another (merged_into) takes from it: it answers as that
# place everywhere, with its own circle.
IDENTITY = ("id", "name", "icon", "price", "auto", "address", "user_address")


def resolve_merged(rows: list[dict]) -> list[dict]:
    """Every circle as the place it belongs to. A place merged into another keeps its own
    latitude, longitude and radius, so the stops inside it stay where they were, and
    answers with the other's id, name, icon and price ("part" is its own id). A row whose
    place is gone (it should not be: removing a place removes its parts) is left out."""
    rows = [dict(r) for r in rows]
    by_id = {r["id"]: r for r in rows}
    out = []
    for r in rows:
        parent = r.get("merged_into")
        if parent is None:
            out.append(r)
            continue
        top = by_id.get(parent)
        if top is None:
            continue
        out.append({**r, **{k: top.get(k) for k in IDENTITY if k in top}, "part": r["id"], "merged_into": parent})
    return out


def parents(rows: list[dict]) -> list[dict]:
    """The places a list shows: none merged into another, each with the visits of the
    places merged into it added to its own, and `parts`, their circles."""
    rows = [dict(r) for r in rows]
    tops = {r["id"]: {**r, "parts": []} for r in rows if r.get("merged_into") is None}
    for r in rows:
        top = tops.get(r.get("merged_into"))
        if top is not None:
            top["parts"].append({k: r.get(k) for k in ("id", "latitude", "longitude", "radius_m")})
            if r.get("visits"):
                top["visits"] = (top.get("visits") or 0) + r["visits"]
    return [t for t in (tops.get(r["id"]) for r in rows) if t is not None]


RADIUS_MIN, RADIUS_MAX = 25.0, 1000.0  # metres: a parking space to a campus


def clamp_radius(r) -> float:
    return max(RADIUS_MIN, min(float(r), RADIUS_MAX))


HOME_MIGRATION = "places_home_follows_icon"  # cc_state: done once


def home_changes(rows: list[dict]) -> tuple[list[int], list[dict]]:
    """Moving home from the name to the icon: (the ids to give the house icon, the places
    whose home status changes). A place home today (its name starts with the word Home)
    stays home: it gets the house unless it shows one already. A place newly home by the
    new rule ("SF Home", or one whose icon is already the house) changes, and its charges
    are priced again. Pure; migrate_home applies it."""
    give, changed = [], []
    for r in rows:
        if r.get("merged_into") is not None:
            continue
        was = starts_with_word(r.get("name"), "home")
        now = is_home(r)
        if was and not now:
            give.append(r["id"])
        elif now and not was:
            changed.append(r)
    return give, changed


def migrate_home(conn, queue_reprice) -> int:
    """Once, at startup: home follows the icon (home_changes). The newly home places'
    charges are queued to be priced again in the background; a newly home place with no
    icon of its own gets the house, so what it is shows. Returns how many changed."""
    if conn.execute("SELECT 1 FROM cc_state WHERE key = %s", (HOME_MIGRATION,)).fetchone():
        return 0
    rows = conn.execute("SELECT id, name, latitude, longitude, radius_m, auto, icon, merged_into FROM cc_places").fetchall()
    rows = [dict(r) for r in rows]
    give, changed = home_changes(rows)
    names = {r["id"]: r.get("name") for r in rows}
    for pid in give:
        LOG.info("places: %r (id %s) stays home and gets the house icon", names[pid], pid)
    for r in changed:
        LOG.info("places: %r (id %s) is home now (icon %s); its charges are priced again", r.get("name"), r["id"], r.get("icon") or "from its name")
    give += [r["id"] for r in changed if r.get("icon") is None]
    if give:
        conn.execute("UPDATE cc_places SET icon = 'home' WHERE id = ANY(%s)", (give,))
    for r in changed:
        queue_reprice(conn, r)
    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (HOME_MIGRATION, "1"))
    conn.commit()
    return len(give) + len(changed)


def other_homes(rows: list[dict], keep_id: int) -> list[dict]:
    """The places that are home besides `keep_id`. There is one home: the place that becomes
    it takes the house from these. Pure; app.demote_other_homes applies it."""
    return [r for r in rows if r["id"] != keep_id and r.get("merged_into") is None and is_home(r)]


def car_at_home(car: dict) -> bool:
    """Whether a car is at home: what the site worked out from the place it is at
    (`at_home`, by is_home); a car dict from elsewhere with only a place name takes the
    name's default (is_home_name)."""
    return bool(car["at_home"]) if "at_home" in car else is_home_name(car.get("place"))
