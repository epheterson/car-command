"""What the Places page says about a place: the drives that ended there (its stays), by
car, how long the car usually stayed and when it usually came, and a few of the arrival
points to draw on its map; the town on its row; the places it could take in. Pure; the
route passes one cached query of arrivals."""

from __future__ import annotations

from datetime import timedelta
from statistics import median

from filters import short_address
from places import PlaceIndex, distance_m, place_for


def _part(hour: int) -> str:
    return "mornings" if 5 <= hour < 12 else "afternoons" if 12 <= hour < 17 else "evenings" if 17 <= hour < 22 else "nights"


def stays(arrivals: list[tuple], places: list[dict], tz, keep: int = 40) -> dict[int, dict]:
    """`arrivals` are (vin, lat, lon, arrived, left) for each drive's end, `left` the car's
    next departure (None for its last drive). Each place that anything arrived at gets
    {points, by_car, n, median (a timedelta), usual}; `usual` reads like "weekday mornings" when one
    part of the week holds at least half the stays of a place with four or more."""
    index = PlaceIndex(places)
    out: dict[int, dict] = {}
    for vin, lat, lon, arrived, left in arrivals:
        if lat is None or lon is None:
            continue
        p = place_for(lat, lon, index)
        if p is None:
            continue
        s = out.setdefault(p["id"], {"points": [], "by_car": {}, "n": 0, "_secs": [], "_when": {}})
        s["n"] += 1
        s["by_car"][vin] = s["by_car"].get(vin, 0) + 1
        if len(s["points"]) < keep:
            s["points"].append([round(lat, 5), round(lon, 5)])
        if left is not None and left > arrived:
            s["_secs"].append((left - arrived).total_seconds())
        local = arrived.astimezone(tz)
        bucket = ("weekday " if local.weekday() < 5 else "weekend ") + _part(local.hour)
        s["_when"][bucket] = s["_when"].get(bucket, 0) + 1
    for s in out.values():
        secs, when = s.pop("_secs"), s.pop("_when")
        s["median"] = timedelta(seconds=round(median(secs) / 60) * 60) if secs else None  # to the minute
        top = max(when.items(), key=lambda kv: kv[1]) if when else None
        s["usual"] = top[0] if top and s["n"] >= 4 and top[1] * 2 >= s["n"] else None
    return out


def city_of(p: dict) -> str:
    """The town a place is in, for its row: the last part of the owner's own address when
    it has one ("12 Elm Street, Springfield"), else of the looked-up one cut to "Road,
    City" (short_address), else of its 100 m cell's "Road, City" (`street`), else of a
    found place's own "Road, City" name. "" when nothing says."""
    found = p.get("name") if p.get("auto") else None
    for text in (
        p.get("user_address"),
        short_address(p.get("address")) if p.get("address") else None,
        p.get("street"),
        found,
    ):
        if text and ", " in text:
            return text.rsplit(",", 1)[1].strip()
    return ""


def merge_choices(p: dict, every: list[dict], keep: int = 30) -> list[dict]:
    """The places `p` could take in, nearest first: {id, name, km, auto}. Never itself."""
    out = [
        {
            "id": o["id"],
            "name": o["name"],
            "auto": bool(o.get("auto")),
            "m": distance_m(p["latitude"], p["longitude"], o["latitude"], o["longitude"]),
        }
        for o in every
        if o["id"] != p["id"]
    ]
    return sorted(out, key=lambda o: o["m"])[:keep]


def distance_words(m: float) -> str:
    return f"{m:.0f} m" if m < 1000 else f"{m / 1000:.1f} km" if m < 100_000 else f"{m / 1000:.0f} km"


def traces_near(lat: float, lon: float, runs: list[list], reach_m: float = 800.0, keep: int = 40) -> list[list[list[float]]]:
    """The parts of these drives' traces within `reach_m` of (lat, lon), for a place's map
    when it has no street background: how the car actually comes and goes, from its own
    positions. A trace that leaves the reach and comes back is two lines, not one drawn
    straight across; each keeps at most `keep` points and needs two."""
    out = []
    for run in runs:
        line: list[list[float]] = []
        for p in list(run) + [None]:
            inside = p is not None and p[0] is not None and p[1] is not None and distance_m(lat, lon, p[0], p[1]) <= reach_m
            if inside:
                line.append([round(p[0], 5), round(p[1], 5)])
                continue
            if len(line) >= 2:
                step = max(1, -(-len(line) // keep))
                out.append(line[::step] if line[::step][-1] == line[-1] else line[::step] + [line[-1]])
            line = []
    return out


# The naming card and the map-first Places page.


def ref(name: str | None, lat, lon, p: dict | None) -> dict | None:
    """A place name as a page shows it, with what the naming card needs to open on it: where
    it is, and whether it still wants a name (`new`: no place covers it, or only one found
    from history). None when there is nothing to tap: no name or no position."""
    if not name or lat is None or lon is None:
        return None
    return {"name": name, "lat": lat, "lon": lon, "new": p is None or bool(p.get("auto"))}


def _key(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


def circles(p: dict) -> list[list[float]]:
    """Every circle that answers as `p`: its own, then those merged into it."""
    own = [[p["latitude"], p["longitude"], p["radius_m"]]]
    return own + [[c["latitude"], c["longitude"], c["radius_m"]] for c in p.get("parts") or []]


def same_name_near(name: str | None, lat: float, lon: float, every: list[dict], skip: int | None = None, within: float = 500.0) -> dict | None:
    """A place somebody named `name` (any case and spacing) within `within` metres of
    (lat, lon), measured to the nearest of its circles: naming a stop so that it would be a
    second one is offered as a merge instead. Found places and `skip` (the place being named)
    never count."""
    key, best = _key(name), None
    if not key:
        return None
    for p in every:
        if p.get("auto") or p["id"] == skip or p.get("merged_into") is not None or _key(p.get("name")) != key:
            continue
        m = min(distance_m(lat, lon, c[0], c[1]) for c in circles(p))
        if m <= within and (best is None or m < best["m"]):
            best = {"id": p["id"], "name": p["name"], "m": round(m)}
    return best


def nearby(lat: float, lon: float, every: list[dict], skip: int | None = None, within: float = 1000.0) -> list[dict]:
    """The named places within `within` metres, nearest first: {id, name, m}."""
    out = [{"id": p["id"], "name": p["name"], "m": round(distance_m(lat, lon, p["latitude"], p["longitude"]))} for p in every if not p.get("auto") and p["id"] != skip]
    return sorted((o for o in out if o["m"] <= within), key=lambda o: o["m"])


def visits(p: dict, stays: dict) -> int:
    """How many times the cars stopped at a place: the drives that ended inside it, as `stays`
    counts them. The one count every page shows, always called visits. (cc_places.visits is
    what discovery counted once from both ends of every drive, about twice this and never
    updated; nothing shows it.)"""
    st = stays.get(p["id"])
    return st["n"] if st else 0


def index_rows(every: list[dict], stays: dict, navs: dict, city, icon) -> list[dict]:
    """The Places page's list and map as one compact list: the found places first (they want a
    name), most stops first, each with the cars' navigation name when there is one; then the
    named ones, most visits first. `city(p)` and `icon(p)` give a place's town and the icon it
    shows. Short keys: this is the one thing the page fetches."""

    def row(p: dict) -> dict:
        r = {"id": p["id"], "n": p["name"], "la": round(p["latitude"], 6), "lo": round(p["longitude"], 6), "r": round(p["radius_m"])}
        r.update(v=visits(p, stays), c=city(p))
        if p.get("auto"):
            r["a"] = 1
            nv = navs.get(p["id"])
            if nv:
                r["s"] = nv["name"]
        else:
            r["ic"] = icon(p)
        return r

    rows = [row(p) for p in every]
    order = lambda r: (-r["v"], r["id"])  # noqa: E731
    return sorted((r for r in rows if r.get("a")), key=order) + sorted((r for r in rows if not r.get("a")), key=order)
