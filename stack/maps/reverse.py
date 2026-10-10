"""Answer a reverse lookup in Nominatim's shape, so the site's geocoder and the connector's
location service use this by changing a URL and nothing else."""

from __future__ import annotations

SKIP_USES = {"driveway", "parking_aisle", "alley", "drive_through", "ramp", "turn_channel"}
# A parked car is never on a freeway, though a lot beside one often reaches it first
SKIP_CLASSES = {"motorway", "trunk"}
# How far the street search reaches: a stop in a big lot is often 100 m from a named road
ROAD_REACH_M = 150


def _skip(edge: dict) -> bool:
    # Valhalla's verbose /locate nests the road class and use under edge.classification
    c = edge.get("classification") or {}
    return c.get("use") in SKIP_USES or c.get("classification") in SKIP_CLASSES or bool(c.get("link"))


def _road(locate: list) -> tuple[str | None, int | None]:
    edges = (locate[0] if locate else {}).get("edges") or []
    for e in sorted(edges, key=lambda e: e.get("distance", 1e9)):
        if _skip(e.get("edge") or {}):
            continue
        names = (e.get("edge_info") or {}).get("names") or []
        if names:
            return names[0], (e.get("edge_info") or {}).get("way_id")
    return None, None


def answer(lat: float, lon: float, locate: list, places) -> dict:
    road, way_id = _road(locate)
    addr: dict = {}
    name = None
    if places is not None:
        house = places.address_for(lat, lon)
        if house:
            addr["house_number"] = house["house_number"]
            road = road or house["road"]
        elif not road:
            near = places.address_for(lat, lon, ROAD_REACH_M)
            road = near["road"] if near else None
        town = places.city_for(lat, lon)
        if town:
            if town.get("city"):
                addr[{"city": "city", "town": "town", "village": "village", "hamlet": "village", "suburb": "city", "neighbourhood": "city"}.get(town["kind"], "city")] = town["city"]
            if town.get("county"):
                addr["county"] = town["county"]
            if town.get("state"):
                addr["state"] = town["state"]
        inside = next((c for c in places.candidates(lat, lon, 3) if c["inside"]), None)
        if inside:
            name = inside["name"]
            addr["amenity"] = name
    if road:
        addr["road"] = road
    if not addr:
        return {}
    street = " ".join(x for x in (addr.get("house_number"), addr.get("road")) if x)
    town = addr.get("city") or addr.get("town") or addr.get("village")
    # Build display_name with deduplication: skip parts equal (case-insensitive) to ones already included
    # For street part with a house number, also check if the road portion matches
    parts = [name, street, town, addr.get("county"), addr.get("state")]
    seen = set()
    display_parts = []
    for part in parts:
        if part:
            part_key = part.strip().lower()
            # If this is the street part and it has a house number, also check if the road alone matches
            if part == street and addr.get("house_number") and addr.get("road"):
                road_key = addr.get("road", "").strip().lower()
                if road_key in seen:
                    # Road has already been seen, skip the street part
                    continue
            if part_key not in seen:
                seen.add(part_key)
                display_parts.append(part)
    display = ", ".join(display_parts)
    return {
        "osm_id": way_id or abs(hash((round(lat, 4), round(lon, 4)))) % 10**12,
        "osm_type": "way",
        "lat": str(lat),
        "lon": str(lon),
        "name": name or "",
        "display_name": display,
        "address": addr,
    }
