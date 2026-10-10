"""How well local maps names the places the cars actually stop: pick 20 busy spots the
cars have visited in the last year that the connector named itself (its `locations` road
and city, from the public geocoder), skip anywhere already a named place, and print that
public original next to what local maps' own `/reverse` says for the same spot, plus its
top 5 candidates, and how often local named a freeway or no road at all. Nothing here is interactive -- a person reads the output and marks it
up -- and nothing it prints is a coordinate, so the output is safe to paste
anywhere: a stop is named only by number.

    python3 maps_check.py          # make maps-check, inside the web container
"""

from __future__ import annotations

import math
import os
import re
import sys

from places import distance_m

CELL = 0.01  # degrees: coarser than a place radius, so distinct cells really are distinct stops


def normalize(text: str | None) -> str:
    """Case- and whitespace-insensitive, for comparing a road or city name across sources
    that capitalise differently or trail a space."""
    return (text or "").strip().lower()


def cell_key(lat: float, lon: float) -> tuple[int, int]:
    """A 0.01-degree grid cell. `math.floor` of the true quotient, not `//`, because a few
    boundary values (e.g. 39.90) land a cell short under plain floor division -- 39.90's
    binary float is a hair below the real value, and `//` truncates on that representation
    directly where `/` rounds it back first."""
    return (math.floor(lat / CELL), math.floor(lon / CELL))


def in_named_place(lat: float, lon: float, places: list[dict]) -> bool:
    return any(distance_m(lat, lon, p["latitude"], p["longitude"]) <= p.get("radius_m", 150) for p in places)


def sample_stops(endpoints: list[dict], places: list[dict], limit: int = 20) -> list[dict]:
    """Group endpoint rows (each a dict with at least `lat`/`lon`) into 0.01-degree cells,
    drop any cell whose point already falls inside a named place's radius, and return the
    `limit` busiest distinct cells -- deterministic, since ties break on the cell's own
    key rather than arrival order. Each result keeps the first endpoint seen in its cell
    (the caller orders its query so that is the most recent one) plus the cell's count."""
    buckets: dict[tuple[int, int], list[dict]] = {}
    for e in endpoints:
        lat, lon = e.get("lat"), e.get("lon")
        if lat is None or lon is None:
            continue
        buckets.setdefault(cell_key(lat, lon), []).append(e)
    reps = []
    for key, rows in buckets.items():
        rep = rows[0]
        if in_named_place(rep["lat"], rep["lon"], places):
            continue
        reps.append({"lat": rep["lat"], "lon": rep["lon"], "road": rep.get("road"), "city": rep.get("city"), "count": len(rows), "cell": key})
    reps.sort(key=lambda r: (-r["count"], r["cell"]))
    return reps[:limit]


def public_display(road: str | None, city: str | None) -> dict:
    """The connector's own name for a stop: the road and city it stored in `locations`."""
    return {"road": road, "city": city, "raw": ", ".join(x for x in (road, city) if x) or None}


def local_display(reverse: dict | None) -> dict:
    """The same road/city shape as `public_display`, out of local maps' `/reverse` answer (Nominatim's
    shape: road/city/town under `address`)."""
    addr = (reverse or {}).get("address") or {}
    return {"road": addr.get("road"), "city": addr.get("city") or addr.get("town") or addr.get("village")}


FREEWAY = re.compile(r"^(I|US|SR|CA|Route|Highway|Hwy|Interstate)[ -]?\d|\b(Freeway|Expressway|Interstate)\b")


def is_freeway(road: str | None) -> bool:
    """A road name that reads as a freeway ("I 280 South", "US 101"): a parked car is never on one."""
    return bool(road and FREEWAY.search(road))


def agrees(a: str | None, b: str | None) -> bool:
    a, b = normalize(a), normalize(b)
    return bool(a) and a == b


def format_candidates(candidates: list[dict], limit: int = 5) -> str:
    if not candidates:
        return "(none)"
    return ", ".join(f"{c['name']} · {c['kind']} · {c.get('distance_m', 0)}m" for c in candidates[:limit])


def format_line(n: int, public: dict, local: dict, candidates: list[dict]) -> str:
    """One line, never a coordinate: a stop is identified only by its number."""
    public_s = public["raw"] or "(unnamed)"
    local_s = ", ".join(x for x in (local.get("road"), local.get("city")) if x) or "(unnamed)"
    return f"#{n:<3d} public: {public_s}   local: {local_s}   top5: {format_candidates(candidates)}"


def totals_line(rows: list[dict]) -> str:
    n = len(rows)
    roads = sum(1 for r in rows if r["road_agree"])
    cities = sum(1 for r in rows if r["city_agree"])
    freeways = sum(1 for r in rows if r["freeway"])
    no_road = sum(1 for r in rows if r["no_road"])
    return f"{n} stops: road agrees {roads}/{n}, city agrees {cities}/{n}, freeway named {freeways}, no road {no_road}"


FETCH_ENDPOINTS_SQL = """
SELECT lat, lon, road, city FROM (
    SELECT t.start_position_latitude AS lat, t.start_position_longitude AS lon, sl.road AS road, sl.city AS city, t.start_date AS d
    FROM trips t LEFT JOIN locations sl ON sl.uid = t.start_location_uid
    WHERE t.start_date >= now() - interval '12 months'
    UNION ALL
    SELECT t.destination_position_latitude, t.destination_position_longitude, dl.road, dl.city, t.destination_date
    FROM trips t LEFT JOIN locations dl ON dl.uid = t.destination_location_uid
    WHERE t.destination_date >= now() - interval '12 months'
) e WHERE e.lat IS NOT NULL ORDER BY e.d DESC
"""


def fetch_endpoints(conn) -> list[dict]:
    rows = conn.execute(FETCH_ENDPOINTS_SQL).fetchall()
    return [{"lat": float(r[0]), "lon": float(r[1]), "road": r[2], "city": r[3]} for r in rows]


def fetch_places(conn) -> list[dict]:
    rows = conn.execute("SELECT latitude, longitude, radius_m FROM cc_places").fetchall()
    return [{"latitude": float(r[0]), "longitude": float(r[1]), "radius_m": float(r[2])} for r in rows]


def run(conn, client, limit: int = 20) -> int:
    """The whole check: sample, look up, print, total. Returns the process's exit code."""
    status = client.status()
    if not status:
        print("maps not answering", file=sys.stderr)
        return 2
    # Only stops the connector named have a public original to compare against
    endpoints = [e for e in fetch_endpoints(conn) if e["road"] or e["city"]]
    places = fetch_places(conn)
    stops = sample_stops(endpoints, places, limit)
    rows = []
    for i, stop in enumerate(stops, 1):
        public = public_display(stop.get("road"), stop.get("city"))
        reverse = client.reverse(stop["lat"], stop["lon"])
        local = local_display(reverse)
        candidates = (client.candidates(stop["lat"], stop["lon"], 5) or {}).get("candidates") or []
        print(format_line(i, public, local, candidates))
        rows.append(
            {
                "road_agree": agrees(public["road"], local["road"]),
                "city_agree": agrees(public["city"], local["city"]),
                "freeway": is_freeway(local["road"]),
                "no_road": not local["road"],
            }
        )
    print(totals_line(rows))
    return 0


def main(argv: list[str]) -> int:
    from mapsclient import MapsClient

    import psycopg

    client = MapsClient(os.environ.get("MAPS_URL"), timeout=3.0)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        return run(conn, client)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
