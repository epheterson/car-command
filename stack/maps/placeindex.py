"""A small place index from one osmium export: towns and admin areas for "City", named
places for the picker, and address points for "just the address". SQLite, bbox-filtered,
exact containment in Python."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from pathlib import Path

from geo import bbox, contains, haversine_m

LOG = logging.getLogger("maps")

KIND_KEYS = ("shop", "amenity", "office", "leisure", "tourism", "building", "craft", "healthcare", "aeroway")
PLACE_KINDS = {"city", "town", "village", "hamlet", "suburb", "neighbourhood"}
NEAR_M = 400

SCHEMA = """
CREATE TABLE IF NOT EXISTS areas (osm_id TEXT, name TEXT, level INTEGER, kind TEXT, minlat REAL, minlon REAL, maxlat REAL, maxlon REAL, geom TEXT);
CREATE TABLE IF NOT EXISTS pois (osm_id TEXT, name TEXT, kind TEXT, lat REAL, lon REAL, area_m2 REAL, minlat REAL, minlon REAL, maxlat REAL, maxlon REAL, geom TEXT);
CREATE TABLE IF NOT EXISTS addrs (house_number TEXT, road TEXT, lat REAL, lon REAL);
CREATE INDEX IF NOT EXISTS areas_bb ON areas (minlat, maxlat, minlon, maxlon);
CREATE INDEX IF NOT EXISTS pois_ll ON pois (lat, lon);
CREATE INDEX IF NOT EXISTS addrs_ll ON addrs (lat, lon);
"""


def _area_m2(geometry: dict) -> float:
    if geometry["type"] not in ("Polygon", "MultiPolygon"):
        return 0.0
    a, b, c, d = bbox(geometry)
    return haversine_m(a, b, c, b) * haversine_m(a, b, a, d)


def _centre(geometry: dict) -> tuple[float, float]:
    if geometry["type"] == "Point":
        return geometry["coordinates"][1], geometry["coordinates"][0]
    a, b, c, d = bbox(geometry)
    return (a + c) / 2, (b + d) / 2


def _index_feature(con: sqlite3.Connection, f: dict) -> None:
    g, p, oid = f.get("geometry"), f.get("properties") or {}, str(f.get("id", ""))
    if not g:
        return
    name = p.get("name")
    if p.get("boundary") == "administrative" and name and g["type"] in ("Polygon", "MultiPolygon"):
        a, b, c, d = bbox(g)
        con.execute(
            "INSERT INTO areas VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, name, int(p.get("admin_level", "99") or 99), "admin", a, b, c, d, json.dumps(g)),
        )
    elif p.get("place") in PLACE_KINDS and name:
        lat, lon = _centre(g)
        con.execute(
            "INSERT INTO areas VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, name, 100, p["place"], lat, lon, lat, lon, json.dumps(g)),
        )
    elif name and any(k in p for k in KIND_KEYS):
        kind = next(p[k] for k in KIND_KEYS if k in p)
        kind = kind if kind != "yes" else next(k for k in KIND_KEYS if k in p)
        lat, lon = _centre(g)
        a, b, c, d = bbox(g) if g["type"] != "Point" else (lat, lon, lat, lon)
        con.execute(
            "INSERT INTO pois VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                oid,
                name,
                kind.replace("_", " "),
                lat,
                lon,
                _area_m2(g),
                a,
                b,
                c,
                d,
                json.dumps(g) if g["type"] != "Point" else None,
            ),
        )
    if p.get("addr:housenumber") and p.get("addr:street"):
        lat, lon = _centre(g)
        con.execute("INSERT INTO addrs VALUES (?,?,?,?)", (p["addr:housenumber"], p["addr:street"], lat, lon))


def build(seq: Path, db: Path) -> None:
    tmp = Path(str(db) + ".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    total = skipped = 0
    with open(seq, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip("\x1e\n ")
            if not line:
                continue
            f = json.loads(line)
            total += 1
            try:
                _index_feature(con, f)
            except Exception as err:  # noqa: BLE001 - one odd feature must not fail the whole build
                skipped += 1
                LOG.warning("placeindex: skipped feature %s: %s", f.get("id"), err)
    if skipped:
        LOG.warning("placeindex: skipped %d of %d features", skipped, total)
    con.commit()
    con.close()
    tmp.replace(db)


class Places:
    def __init__(self, db: Path) -> None:
        self.con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)

    def close(self) -> None:
        self.con.close()

    def city_for(self, lat: float, lon: float) -> dict | None:
        rows = self.con.execute(
            "SELECT name, level, kind, geom FROM areas WHERE kind = 'admin' AND minlat <= ? AND maxlat >= ? AND minlon <= ? AND maxlon >= ?",
            (lat, lat, lon, lon),
        ).fetchall()
        inside = sorted((lvl, name) for name, lvl, kind, geom in rows if contains(json.loads(geom), lat, lon))
        by = {lvl: name for lvl, name in inside}
        city = by.get(8) or next((name for lvl, name in inside if 7 <= lvl <= 10), None)
        kind = "city"
        if not city:
            d = 0.05
            near = self.con.execute(
                "SELECT name, kind, minlat, minlon FROM areas WHERE kind != 'admin' AND minlat BETWEEN ? AND ? AND minlon BETWEEN ? AND ?",
                (lat - d, lat + d, lon - d, lon + d),
            ).fetchall()
            near = sorted((haversine_m(lat, lon, a, b), n, k) for n, k, a, b in near)
            if near and near[0][0] <= 5000:
                city, kind = near[0][1], near[0][2]
        if not (city or by):
            return None
        return {"city": city, "county": by.get(6), "state": by.get(4), "kind": kind}

    def address_for(self, lat: float, lon: float, within_m: float = 40) -> dict | None:
        dlat = within_m / 111_000
        dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
        rows = self.con.execute(
            "SELECT house_number, road, lat, lon FROM addrs WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()
        best = min(((haversine_m(lat, lon, a, b), h, r) for h, r, a, b in rows), default=None)
        return {"house_number": best[1], "road": best[2]} if best and best[0] <= within_m else None

    def candidates(self, lat: float, lon: float, limit: int = 10) -> list[dict]:
        d = 0.005
        rows = self.con.execute(
            "SELECT osm_id, name, kind, lat, lon, area_m2, minlat, minlon, maxlat, maxlon, geom FROM pois WHERE maxlat >= ? AND minlat <= ? AND maxlon >= ? AND minlon <= ?",
            (lat - d, lat + d, lon - d, lon + d),
        ).fetchall()
        inside, near = [], []
        for oid, name, kind, plat, plon, area, a, b, c, e, geom in rows:
            if geom and a <= lat <= c and b <= lon <= e and contains(json.loads(geom), lat, lon):
                inside.append((area, {"name": name, "kind": kind, "distance_m": 0, "inside": True, "osm_id": oid}))
                continue
            dist = haversine_m(lat, lon, plat, plon)
            if dist <= NEAR_M:
                near.append((dist / (1 + math.log10(1 + area)), {"name": name, "kind": kind, "distance_m": round(dist), "inside": False, "osm_id": oid}))
        out, seen = [], set()
        for _, c in sorted(inside, key=lambda x: x[0]) + sorted(near, key=lambda x: x[0]):
            if c["name"].lower() not in seen:
                seen.add(c["name"].lower())
                out.append(c)
        return out[:limit]

    def search(self, q: str, lat: float | None = None, lon: float | None = None, limit: int = 8) -> list[dict]:
        """What a typed address or name could be: house addresses ("742 Evergreen"),
        streets ("Evergreen Ter") and named places ("Bravo Mar"), matched on the start of
        the street or name or of any word in it, any case, nearest to (lat, lon) first
        when given (within SEARCH_BOX_DEG of it, then anywhere). Only what this index
        holds: OpenStreetMap's address points and named places, so an address OSM has no
        house number for is found as its street. Never leaves this machine."""
        parsed = parse_query(q)
        if parsed is None:
            return []
        number, road, town = parsed
        near = (lat, lon) if lat is not None and lon is not None else None
        rows, pois = [], []
        for box in (SEARCH_BOX_DEG, None) if near else (None,):
            rows = self._match("SELECT house_number, road, lat, lon FROM addrs", "road", road, near, box, number)
            pois = [] if number else self._match("SELECT name, kind, lat, lon FROM pois", "name", road, near, box)
            if rows or pois:
                break
        out: dict[str, dict] = {}
        for label, kind, a, b in [(f"{h} {r}" if number else r, "address" if number else "street", a, b) for h, r, a, b in rows] + list(pois):
            d = haversine_m(near[0], near[1], a, b) if near else 0.0
            key = label.lower()
            if key not in out or d < out[key]["d"]:
                out[key] = {"label": label, "kind": kind, "lat": a, "lon": b, "d": d}
        cities: dict[tuple, str | None] = {}
        found = []
        for x in sorted(out.values(), key=lambda x: (x["d"], x["label"])):
            cell = (round(x["lat"], 2), round(x["lon"], 2))
            if cell not in cities:
                cities[cell] = (self.city_for(x["lat"], x["lon"]) or {}).get("city")
            city = cities[cell]
            if town and not (city and city.lower().startswith(town)):
                continue
            found.append({"label": x["label"], "city": city, "kind": x["kind"], "lat": round(x["lat"], 6), "lon": round(x["lon"], 6)})
            if len(found) >= limit:
                break
        return found

    def _match(self, select: str, column: str, text: str, near, box, number: str | None = None) -> list[tuple]:
        where, args = _like(column, text)
        if number:
            where, args = f"lower(house_number) = ? AND {where}", [number, *args]
        if box:
            where += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
            args += [near[0] - box, near[0] + box, near[1] - box, near[1] + box]
        return self.con.execute(f"{select} WHERE {where} LIMIT {SEARCH_ROWS}", args).fetchall()


SEARCH_BOX_DEG = 0.6  # about 60 km around the map's centre first, then everywhere
SEARCH_ROWS = 2000  # matching rows read at most per table: a street's house points, a chain's shops
_ABBREV = {
    "st": "street",
    "ave": "avenue",
    "av": "avenue",
    "rd": "road",
    "dr": "drive",
    "ln": "lane",
    "blvd": "boulevard",
    "ct": "court",
    "pl": "place",
    "ter": "terrace",
    "cir": "circle",
    "hwy": "highway",
    "pkwy": "parkway",
    "sq": "square",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
}


def parse_query(q: str | None) -> tuple[str | None, str, str | None] | None:
    """(house number, street or name, town) from what was typed: "742 Evergreen Ter,
    Spring" -> ("742", "evergreen terrace", "spring"). A finished abbreviation (one with
    a space or a comma after it) is spelled out the way OSM spells street names; the word
    still being typed is left as a prefix. None when there is too little to look for."""
    text = " ".join((q or "").lower().split())[:100]
    head, _, town = text.partition(",")
    words = head.split()
    number = words.pop(0) if len(words) > 1 and words[0][:1].isdigit() else None
    done = (q or "").endswith((" ", ",")) or bool(town)
    words = [_ABBREV.get(w.rstrip("."), w) if (i < len(words) - 1 or done) else w for i, w in enumerate(words)]
    road = " ".join(words)
    if len(road) < 2:
        return None
    return number, road, (town.strip() or None)


def _like(column: str, text: str) -> tuple[str, list[str]]:
    """The start of the value or of any word in it, any case (LIKE folds ASCII case)."""
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"({column} LIKE ? ESCAPE '\\' OR {column} LIKE ? ESCAPE '\\')", [esc + "%", "% " + esc + "%"]
