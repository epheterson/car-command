"""Where the driver told the car it was going, per drive, and the names that gives places.

A Tesla streams its navigation (Fleet Telemetry, firmware 2024.26 on): the destination's
name and position, the route's origin, minutes and miles to arrival. The hourly replay
(tesla_live.rebuild) reads them out of the raw records and keeps, per drive, the last
destination set while it ran and the origin, in cc_tesla_nav, whichever source writes the
car's history. A drive that ended where it navigated to names that stop: the place's
suggested name, offered first when the owner names it, never written over a name the owner
gave. Nothing here leaves the machine.

Pure apart from save() and load(): records and samples in, rows out."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import tesla_stream
from places import PlaceIndex, distance_m, place_for

ARRIVED_M = 150.0  # a drive that ends this close to its destination arrived there
NAME_MAX = 60
NAV_KEYS = ("DestinationName", "DestinationLocation", "OriginLocation")
LIVE_KEYS = ("DestinationName", "MinutesToArrival", "MilesToArrival")
LIVE_FRESH = timedelta(minutes=10)  # a route reading older than this says nothing about now

DDL = """
CREATE TABLE IF NOT EXISTS cc_tesla_nav (
    vin        VARCHAR NOT NULL,
    started    TIMESTAMPTZ NOT NULL,
    ended      TIMESTAMPTZ NOT NULL,
    end_lat    DOUBLE PRECISION,
    end_lon    DOUBLE PRECISION,
    dest_name  VARCHAR,
    dest_lat   DOUBLE PRECISION,
    dest_lon   DOUBLE PRECISION,
    origin_lat DOUBLE PRECISION,
    origin_lon DOUBLE PRECISION,
    PRIMARY KEY (vin, started)
);
"""

_STREET = re.compile(r"^\d+[A-Za-z]?(-\d+)?\s+\S")  # "742 Evergreen Terrace", "12B Elm St"


def text_of(v) -> str | None:
    """A destination name as the car sent it. tesla_stream reads a string that looks like a
    number as one, so a station called "76" arrives as 76.0; it is a name again here."""
    if v is None or v is tesla_stream.INVALID:
        return None
    if isinstance(v, float):
        v = str(int(v)) if v.is_integer() else str(v)
    s = " ".join(str(v).split())
    return s or None


def clean_name(raw) -> str | None:
    """The name a place takes from a destination. A business with its address after it
    ("Bravo Market, 12 Elm Street, Springfield") is the business; an address alone ("742
    Evergreen Terrace, Springfield") or a town ("Springfield, IL") stays whole."""
    s = text_of(raw)
    if not s:
        return None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) > 1 and not _STREET.match(parts[0]) and any(_STREET.match(p) for p in parts[1:]):
        s = parts[0]
    return s[:NAME_MAX]


def _loc(v) -> tuple[float, float] | None:
    if isinstance(v, tuple) and len(v) == 2 and v[0] is not None and v[1] is not None:
        if v == (0.0, 0.0):
            return None  # what a car sends for "no route" on some firmware
        return (float(v[0]), float(v[1]))
    return None


def drives_in(samples: list[dict]) -> list[dict]:
    """Each drive the samples hold: {vin, start, end, lat, lon} (lat/lon where it ended). A
    drive under way at the first sample is left out: its start is before the window."""
    out: list[dict] = []
    first = samples[0]["time"] if samples else None
    cur = None
    for s in samples:
        n = s.get("drive_no") or 0
        key = (s["vin"], n)
        if n and cur is not None and cur["_key"] == key:
            cur["end"] = s["time"]
            if s.get("latitude") is not None:
                cur["lat"], cur["lon"] = s["latitude"], s["longitude"]
            continue
        if cur is not None:
            out.append(cur)
            cur = None
        if n:
            cur = {"_key": key, "vin": s["vin"], "start": s["time"], "end": s["time"], "lat": s.get("latitude"), "lon": s.get("longitude")}
    if cur is not None:
        out.append(cur)
    return [{k: v for k, v in d.items() if k != "_key"} for d in out if d["start"] != first]


def per_drive(recs: list[tuple[datetime, str, dict]], samples: list[dict]) -> list[dict]:
    """Per drive with any navigation: the last destination (name and position) set while it
    ran, the route as it stood at the start counting, and the origin. A route cleared before
    the drive is no route; one cleared on the way (the car clears it on arrival) still says
    where it went. Rows for save()."""
    events: dict[str, list[tuple[datetime, str, object]]] = defaultdict(list)
    for t, vin, fields in recs:
        for k in NAV_KEYS:
            if k in fields:
                events[vin].append((t, k, fields[k]))
    out = []
    for d in drives_in(samples):
        route = _route(events.get(d["vin"]) or [], d["start"], d["end"])
        name, dest, origin = text_of(route.get("DestinationName")), _loc(route.get("DestinationLocation")), _loc(route.get("OriginLocation"))
        if not (name or dest or origin):
            continue
        out.append(
            {
                "vin": d["vin"],
                "started": d["start"],
                "ended": d["end"],
                "end_lat": d["lat"],
                "end_lon": d["lon"],
                "dest_name": name,
                "dest_lat": dest[0] if dest else None,
                "dest_lon": dest[1] if dest else None,
                "origin_lat": origin[0] if origin else None,
                "origin_lon": origin[1] if origin else None,
            }
        )
    return out


def _route(events: list[tuple[datetime, str, object]], start: datetime, end: datetime) -> dict:
    """The route a drive ran: what was carried at its start (a clear before it clears it),
    then every value set until it ended (a clear on the way does not)."""
    route: dict[str, object] = {}
    for t, k, v in events:
        if t > end:
            break
        if t < start or (v is not tesla_stream.INVALID and v is not None):
            route[k] = None if v is tesla_stream.INVALID else v
    return {k: v for k, v in route.items() if v is not None}


COLUMNS = ("vin", "started", "ended", "end_lat", "end_lon", "dest_name", "dest_lat", "dest_lon", "origin_lat", "origin_lon")


def save(conn, rows: list[dict]) -> int:
    """Upsert the rows; a drive still going when the last replay saw it is brought up to date."""
    if not rows:
        return 0
    cols = ", ".join(COLUMNS)
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS[2:])
    conn.cursor().executemany(
        f"INSERT INTO cc_tesla_nav ({cols}) VALUES ({', '.join(['%s'] * len(COLUMNS))}) ON CONFLICT (vin, started) DO UPDATE SET {sets}",
        [tuple(r[c] for c in COLUMNS) for r in rows],
    )
    return len(rows)


def load(conn) -> list[dict]:
    """Every drive that navigated somewhere named, for the suggestions."""
    rows = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM cc_tesla_nav WHERE dest_name IS NOT NULL ORDER BY started").fetchall()
    return [dict(r) if isinstance(r, dict) else dict(zip(COLUMNS, r)) for r in rows]


def for_trip(rows: list[dict], start: datetime, end: datetime | None, slack: timedelta = timedelta(minutes=5)) -> dict | None:
    """The navigated drive that is this trip: it started within `slack` of the trip, or
    while the trip ran (another source may cut drives a minute apart from the stream)."""
    best = None
    for r in rows:
        s = r["started"]
        if s < start - slack or (end is not None and s > end):
            continue
        gap = abs((s - start).total_seconds())
        if best is None or gap < best[0]:
            best = (gap, r)
    return best[1] if best else None


def arrived(r: dict) -> bool:
    if r.get("end_lat") is None or r.get("dest_lat") is None:
        return False
    return distance_m(r["end_lat"], r["end_lon"], r["dest_lat"], r["dest_lon"]) <= ARRIVED_M


def _tally(found: list[dict], names: dict[str, str]) -> dict | None:
    """The most frequent name among these drives (the latest breaks a tie), the cars it came
    from, and the other names."""
    if not found:
        return None
    counts: Counter = Counter()
    latest: dict[str, datetime] = {}
    cars: dict[str, Counter] = defaultdict(Counter)
    for r in found:
        n = clean_name(r["dest_name"])
        if not n:
            continue
        counts[n] += 1
        latest[n] = max(latest.get(n, r["started"]), r["started"])
        cars[n][names.get(r["vin"]) or "the car"] += 1
    if not counts:
        return None
    order = sorted(counts, key=lambda n: (-counts[n], -latest[n].timestamp()))
    top = order[0]
    who = [c for c, _n in cars[top].most_common()]
    whose = who[0] + "'s" if len(who) == 1 else ", ".join(c + "'s" for c in who[:-1]) + " and " + who[-1] + "'s"
    return {"name": top, "n": counts[top], "source": f"from {whose} navigation", "alternates": order[1:4]}


def place_suggestions(rows: list[dict], places, names: dict[str, str]) -> dict[int, dict]:
    """place id -> its suggestion: the drives whose destination lies inside the place, and
    those that ended inside it within ARRIVED_M of where they navigated to."""
    index = places if isinstance(places, PlaceIndex) else PlaceIndex(list(places))
    hits: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        ids = set()
        p = place_for(r.get("dest_lat"), r.get("dest_lon"), index)
        if p is not None:
            ids.add(p["id"])
        if arrived(r):
            q = place_for(r["end_lat"], r["end_lon"], index)
            if q is not None:
                ids.add(q["id"])
        for i in ids:
            hits[i].append(r)
    return {i: s for i, s in ((i, _tally(found, names)) for i, found in hits.items()) if s}


def point_suggestion(lat: float, lon: float, rows: list[dict], names: dict[str, str], radius_m: float = ARRIVED_M) -> dict | None:
    """The suggestion for a spot no place covers yet (the picker): drives navigated to within
    `radius_m` of it, or that ended there having arrived."""
    found = []
    for r in rows:
        if r.get("dest_lat") is not None and distance_m(lat, lon, r["dest_lat"], r["dest_lon"]) <= radius_m:
            found.append(r)
        elif arrived(r) and distance_m(lat, lon, r["end_lat"], r["end_lon"]) <= radius_m:
            found.append(r)
    return _tally(found, names)


def live_route(carried: dict, at: datetime | None, now: datetime) -> dict | None:
    """What Now says while a car drives a route: {name, minutes, miles}, from the newest
    stream values; None when no route is active or the reading is stale."""
    if at is None or now - at > LIVE_FRESH:
        return None
    if carried.get("Gear") not in tesla_stream.DRIVING_GEARS and (carried.get("VehicleSpeed") or 0) <= tesla_stream.MOVING_MPH:
        return None
    name, minutes = clean_name(carried.get("DestinationName")), carried.get("MinutesToArrival")
    if not name:
        return None
    mins = round(minutes) if isinstance(minutes, (int, float)) and minutes >= 0 else None
    miles = carried.get("MilesToArrival")
    return {"name": name, "minutes": mins, "miles": miles if isinstance(miles, (int, float)) else None}


def route_from_records(records: list[tuple[datetime, dict]], now: datetime) -> dict | None:
    """The route a car is driving, from its raw V records newest first, for when the
    stream's memory has none (the site restarted mid-drive; the car sends its destination
    only when it changes, so it may not send it again). The newest value of each key wins;
    a cleared destination (invalid) ends the route; minutes and miles count only while fresh."""
    got: dict = {}
    for at, rec in records:
        for d in rec.get("data") or []:
            k = d.get("key") if isinstance(d, dict) else None
            if k in LIVE_KEYS and k not in got:
                got[k] = (at, tesla_stream.value_of(d.get("value") or {}))
        if len(got) == len(LIVE_KEYS):
            break
    name = got.get("DestinationName", (None, None))[1]
    if name is tesla_stream.INVALID or not clean_name(name):
        return None

    def recent(key):
        at, v = got.get(key, (None, None))
        return v if isinstance(v, (int, float)) and at is not None and now - at <= LIVE_FRESH else None

    minutes = recent("MinutesToArrival")
    return {"name": clean_name(name), "minutes": round(minutes) if minutes is not None and minutes >= 0 else None, "miles": recent("MilesToArrival")}
