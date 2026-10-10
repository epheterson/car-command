"""Places from history: cluster where the cars stop, name the clusters once.

Naming places by hand takes years; the history already says
where the cars go. Trip and charge endpoints are binned to 0.001 degrees
(about 100 m), bins are merged into any place within its radius (existing
places first, then the busiest bins), and each new place is named by one
reverse-geocode of its centre. The centre is sent at three decimals, so the
lookup says "this block", not "this driveway". Names are editable on the
Places page; auto places carry a visit count so the list reads by importance.

    python3 discover.py [--min-visits 3] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

from places import DDL, distance_m

# cc_places and its columns are declared once, in places.py, and re-exported here. This
# module can run before the site ever has — `make discover` on a fresh install — so it
# cannot assume some other process created the table first.
NOMINATIM = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
USER_AGENT = "carcommand/0.1 (self-hosted EV logger)"
DEFAULT_RADIUS_M = 150.0


def cluster(points: list[tuple[float, float]], existing: list[dict], min_visits: int = 3, radius_m: float = DEFAULT_RADIUS_M) -> tuple[list[dict], dict[int, int]]:
    """Greedy: count 0.001-degree bins, walk them busiest first, attach each to
    the first place within its radius or start a new one. Returns the new
    places (lat, lon, visits) and visit counts for existing place ids."""
    bins = Counter((round(lat, 3), round(lon, 3)) for lat, lon in points if lat is not None and lon is not None)
    known = [dict(p) for p in existing]
    seen: dict[int, int] = {p["id"]: 0 for p in existing}
    new: list[dict] = []
    for (blat, blon), n in bins.most_common():
        home = None
        for p in known:
            if distance_m(blat, blon, p["latitude"], p["longitude"]) <= p.get("radius_m", radius_m):
                home = p
                break
        if home is not None:
            if "id" in home:
                seen[home["id"]] += n
            else:
                # move the centre toward the busier bins it absorbs
                w = home["visits"] + n
                home["latitude"] = (home["latitude"] * home["visits"] + blat * n) / w
                home["longitude"] = (home["longitude"] * home["visits"] + blon * n) / w
                home["visits"] = w
            continue
        if n < min_visits:
            continue  # bins are sorted busiest first, so nothing after this qualifies either
        place = {"latitude": blat, "longitude": blon, "radius_m": radius_m, "visits": n}
        known.append(place)
        new.append(place)
    return new, seen


def town_from_display(display_name: str | None) -> str | None:
    """The town out of Nominatim's display_name, which lists road, neighbourhoods,
    town, county, state, postcode, country. The town is the last part before the
    county/state/postcode/country tail. Works when the address dict files the
    town under a key we did not think of (one real town came back with none)."""
    if not display_name:
        return None
    parts = [p.strip() for p in display_name.split(",")]
    core = parts[1:]
    while core and _is_tail(core[-1]):
        core.pop()
    return core[-1] if core else None


def _is_tail(part: str) -> bool:
    """A display_name part past the town: county, state, postcode or country."""
    return part == "United States" or part.endswith(" County") or part.replace("-", "").isdigit() or part in US_STATES


def specificity(name: str | None, display_name: str | None = None) -> int:
    """How much a cell name says: 2 for a road or a place ("Road, City", "Bravo Market"),
    1 for a town alone, 0 for nothing. A name with no comma is a town when everything after
    it in its display_name is county, state, postcode or country."""
    if not name or name == "Unnamed":
        return 0
    if "," in name:
        return 2
    parts = [p.strip() for p in (display_name or "").split(",") if p.strip()]
    if name not in parts:
        return 2 if parts else 1
    return 1 if all(_is_tail(p) for p in parts[parts.index(name) + 1 :]) else 2


US_STATES = {
    "Alabama",
    "Alaska",
    "Arizona",
    "Arkansas",
    "California",
    "Colorado",
    "Connecticut",
    "Delaware",
    "Florida",
    "Georgia",
    "Hawaii",
    "Idaho",
    "Illinois",
    "Indiana",
    "Iowa",
    "Kansas",
    "Kentucky",
    "Louisiana",
    "Maine",
    "Maryland",
    "Massachusetts",
    "Michigan",
    "Minnesota",
    "Mississippi",
    "Missouri",
    "Montana",
    "Nebraska",
    "Nevada",
    "New Hampshire",
    "New Jersey",
    "New Mexico",
    "New York",
    "North Carolina",
    "North Dakota",
    "Ohio",
    "Oklahoma",
    "Oregon",
    "Pennsylvania",
    "Rhode Island",
    "South Carolina",
    "South Dakota",
    "Tennessee",
    "Texas",
    "Utah",
    "Vermont",
    "Virginia",
    "Washington",
    "West Virginia",
    "Wisconsin",
    "Wyoming",
    "District of Columbia",
}


def name_from(address: dict, fallback_name: str | None = None, display_name: str | None = None) -> str:
    """A short, human name: the POI when there is one, else road and town."""
    for k in ("amenity", "shop", "leisure", "tourism", "building", "office", "aeroway"):
        if address.get(k) and not address[k].isdigit() and len(address[k]) < 40:
            return address[k]
    if fallback_name and len(fallback_name) < 40 and not fallback_name[0].isdigit():
        return fallback_name
    road = address.get("road") or address.get("pedestrian") or address.get("footway")
    town = address.get("city") or address.get("town") or address.get("village") or address.get("hamlet") or address.get("municipality") or town_from_display(display_name) or address.get("county")
    if road and town:
        return f"{road}, {town}"
    return road or town or address.get("state") or "Unnamed"


def geocoder_url(maps_url: str | None, maps_up: bool, public_allowed: bool) -> str | None:
    """Where place names come from: the local maps add-on when it is up, OpenStreetMap's
    public Nominatim only when Settings › Maps allows it, otherwise nowhere."""
    if maps_url and maps_up:
        return maps_url.rstrip("/") + "/reverse"
    return NOMINATIM if public_allowed else None


DOWN_STREAK_LIMIT = 5


def geocoder_route(maps_url: str | None, up: bool, public_ok: bool, down_streak: int, current: str | None) -> tuple[str, int]:
    """What `cc_state.geocoder_url` should become this tick, and the next down-streak
    count. `current` is what is stored there now (what the connector is running on;
    None when nothing is stored yet). The answer is always a string: a local URL,
    "public", or "" for off, because the connector reads a missing row as off.

    Switching *to* local happens the moment it is confirmed up — no reason to wait.
    Switching *away* from local only happens after DOWN_STREAK_LIMIT consecutive down
    ticks, so one missed health check does not restart the connector. When nothing is
    allowed to leak out while it is down (public not allowed), there is nothing to
    debounce: `local` is always correct, since a dead local endpoint still names
    nothing (privacy._Redirect fails closed) and the connector never restarts for it.
    """
    if not maps_url:
        return ("public" if public_ok else ""), 0
    local = maps_url.rstrip("/") + "/"
    if not public_ok:
        return local, 0
    if up:
        return local, 0
    streak = down_streak + 1
    if current == local and streak < DOWN_STREAK_LIMIT:
        return current, streak  # a blip: keep running on local through it
    return "public", streak


def reverse_geocode(lat: float, lon: float, client, url: str = NOMINATIM) -> tuple[str, str | None]:
    r = client.get(url, params={"lat": f"{lat:.3f}", "lon": f"{lon:.3f}", "format": "jsonv2", "zoom": 17}, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise ValueError(f"unable: {body['error']}")
    address = body.get("address") or {}
    return name_from(address, body.get("name"), body.get("display_name")), body.get("display_name")


def discover(conn, min_visits: int, dry_run: bool, pause_s: float = 1.1, url: str | None = NOMINATIM) -> int:
    """`url=None` means Settings > Maps allows no geocoder at all: places still cluster
    and save (visit counts are the point of discovery), just unnamed, by coordinates."""
    conn.execute(DDL)
    existing = conn.execute("SELECT id, name, latitude, longitude, radius_m FROM cc_places").fetchall()
    existing = [dict(zip(("id", "name", "latitude", "longitude", "radius_m"), r)) for r in existing]
    pts = conn.execute("""
        SELECT start_position_latitude, start_position_longitude FROM trips
        UNION ALL SELECT destination_position_latitude, destination_position_longitude FROM trips
        UNION ALL SELECT session_position_latitude, session_position_longitude FROM charging_sessions""").fetchall()
    new, seen = cluster([tuple(p) for p in pts], existing, min_visits)
    print(f"{len(pts)} endpoints, {len(existing)} places already, {len(new)} new places with >= {min_visits} visits")
    for pid, n in seen.items():
        if not dry_run:
            conn.execute("UPDATE cc_places SET visits = %s WHERE id = %s", (n, pid))
    if dry_run:
        # Counts only: a position never goes to a log, rounded or not.
        for p in new[:20]:
            print(f"  {p['visits']:5d} visits, radius {p['radius_m']:.0f} m")
        return len(new)
    if url is None:
        print("no geocoder allowed (Settings > Maps): naming nothing, saving places by coordinates", file=sys.stderr)

    import contextlib

    import httpx

    added = 0
    with httpx.Client() if url is not None else contextlib.nullcontext(None) as client:
        for p in new:
            if url is None:
                name, address = f"{p['latitude']:.3f}, {p['longitude']:.3f}", None
            else:
                try:
                    name, address = reverse_geocode(p["latitude"], p["longitude"], client, url)
                except Exception as err:  # noqa: BLE001 - one bad lookup should not stop the rest
                    # The class only: an HTTP error's text carries the URL, and the URL the position.
                    print(f"  geocode failed for one place ({type(err).__name__}); saved unnamed", file=sys.stderr)
                    name, address = f"{p['latitude']:.3f}, {p['longitude']:.3f}", None
            conn.execute(
                "INSERT INTO cc_places (name, latitude, longitude, radius_m, auto, visits, address) VALUES (%s, %s, %s, %s, TRUE, %s, %s)",
                (name[:40], p["latitude"], p["longitude"], p["radius_m"], p["visits"], address),
            )
            conn.commit()
            added += 1
            print(f"  {p['visits']:5d} visits, {'named' if address else 'unnamed'}")
            time.sleep(pause_s)
    return added


# The ALTER runs once, at startup (apply_schema in app.py) — never per call, per the
# project's DDL rule. GEOCELL_BACKFILL is a one-time startup statement too: every row
# that already had a name came from the public Nominatim, since there was no other
# source before this task.
GEOCELL_DDL = """
CREATE TABLE IF NOT EXISTS cc_geocells (
    lat3     NUMERIC(8,3) NOT NULL,
    lon3     NUMERIC(8,3) NOT NULL,
    name     VARCHAR,
    address  VARCHAR,
    tried_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lat3, lon3)
);
ALTER TABLE cc_geocells ADD COLUMN IF NOT EXISTS source VARCHAR;
ALTER TABLE cc_geocells ADD COLUMN IF NOT EXISTS requeue BOOLEAN NOT NULL DEFAULT FALSE;
"""
GEOCELL_BACKFILL = "UPDATE cc_geocells SET source = 'public' WHERE source IS NULL AND name IS NOT NULL"
RETRY_AFTER_H = 6  # a cell that failed waits this long before it is tried again

# Run once whenever the maps worker sees the add-on's build version change (app.py). Marks
# every publicly-named cell pending for the local pass without touching its name or address,
# so pages keep showing the public name until a local lookup actually replaces it — and
# resets tried_at to the epoch so pending_cells offers it on the very next pass, not after
# RETRY_AFTER_H. One cheap statement; no per-row work here, no DDL outside GEOCELL_DDL.
# A new local maps version: publicly-named cells go to the local namer, and cells nothing
# could name get another try now rather than after the retry wait.
REQUEUE_PUBLIC = "UPDATE cc_geocells SET requeue = TRUE, tried_at = to_timestamp(0) WHERE source = 'public' OR name IS NULL"


# One-time, at startup: cells named locally before the road search reached past parking lots
# (and skipped freeways) go back to the local namer. Many got only a town, overwriting a
# public "Road, City"; the re-pass keeps the better of old and new (specificity) and, for a
# town-only answer, falls back to the connector's own stored road. The cc_state marker makes
# it run once, not on every restart.
RENAME_LOCAL_ONCE = """
UPDATE cc_geocells SET requeue = TRUE, tried_at = to_timestamp(0)
 WHERE source = 'local' AND NOT EXISTS (SELECT 1 FROM cc_state WHERE key = 'geocells_renamed_v2');
INSERT INTO cc_state (key, value) VALUES ('geocells_renamed_v2', 'done') ON CONFLICT (key) DO NOTHING;
"""


def requeue_public_geocells(conn) -> None:
    conn.execute(REQUEUE_PUBLIC)
    conn.commit()


def pending_cells(conn, limit: int) -> list[tuple[float, float]]:
    """Endpoint cells (100 m) with no name yet, busiest first: never tried, tried and
    failed more than RETRY_AFTER_H hours ago, or flagged `requeue` (a public name waiting
    for the local pass to take over) and not retried within RETRY_AFTER_H. The table is
    created at startup (apply_schema); this never runs the DDL itself."""
    rows = conn.execute(
        """
        WITH ends AS (
            SELECT ROUND(start_position_latitude::numeric, 3) AS la, ROUND(start_position_longitude::numeric, 3) AS lo FROM trips
            UNION ALL SELECT ROUND(destination_position_latitude::numeric, 3), ROUND(destination_position_longitude::numeric, 3) FROM trips
            UNION ALL SELECT ROUND(session_position_latitude::numeric, 3), ROUND(session_position_longitude::numeric, 3) FROM charging_sessions)
        SELECT e.la, e.lo, COUNT(*) AS n FROM ends e LEFT JOIN cc_geocells g ON g.lat3 = e.la AND g.lon3 = e.lo
        WHERE e.la IS NOT NULL
          AND (g.lat3 IS NULL
               OR (g.name IS NULL AND g.tried_at < now() - (%s * INTERVAL '1 hour'))
               OR (g.requeue AND g.tried_at < now() - (%s * INTERVAL '1 hour')))
        GROUP BY e.la, e.lo ORDER BY n DESC LIMIT %s""",
        (RETRY_AFTER_H, RETRY_AFTER_H, limit),
    ).fetchall()
    return [(float(r[0] if not isinstance(r, dict) else r["la"]), float(r[1] if not isinstance(r, dict) else r["lo"])) for r in rows]


def _save_cell(conn, lat: float, lon: float, name: str | None, address: str | None, source: str | None) -> None:
    """A failed lookup still writes a row (name NULL) so it is throttled by RETRY_AFTER_H
    rather than retried every pass, but it is never counted as named: pending_cells keeps
    offering it once the throttle window passes.

    `name`/`address`/`source` are COALESCEd onto the existing row rather than overwriting
    it: a requeued public cell that the local add-on fails to geocode keeps its public name
    (the page still has somewhere to point) instead of going blank. `requeue` always clears
    here — tried once, it waits for the next version bump rather than hot-looping."""
    conn.execute(
        "INSERT INTO cc_geocells (lat3, lon3, name, address, source) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (lat3, lon3) DO UPDATE SET "
        "name = COALESCE(EXCLUDED.name, cc_geocells.name), address = COALESCE(EXCLUDED.address, cc_geocells.address), "
        "tried_at = now(), source = COALESCE(EXCLUDED.source, cc_geocells.source), requeue = FALSE",
        (round(lat, 3), round(lon, 3), (name or "")[:60] or None, address, source),
    )
    conn.commit()


# The road and city the connector stored for its own trips ending in this cell, busiest
# first: names it already looked up, so nothing is sent anywhere to read them.
CONNECTOR_NAME_SQL = """
SELECT road, city FROM (
    SELECT l.road, l.city FROM trips t JOIN locations l ON l.uid = t.start_location_uid
     WHERE ROUND(t.start_position_latitude::numeric, 3) = %s AND ROUND(t.start_position_longitude::numeric, 3) = %s
    UNION ALL
    SELECT l.road, l.city FROM trips t JOIN locations l ON l.uid = t.destination_location_uid
     WHERE ROUND(t.destination_position_latitude::numeric, 3) = %s AND ROUND(t.destination_position_longitude::numeric, 3) = %s
) e WHERE road IS NOT NULL AND city IS NOT NULL
GROUP BY road, city ORDER BY COUNT(*) DESC LIMIT 1"""


def _keep_best(conn, lat: float, lon: float, name: str | None, address: str | None, source: str | None) -> tuple[str | None, str | None, str | None]:
    """What to save for a cell after a lookup, never less specific than what it has. A
    town-only (or failed) answer for a cell with no road name takes the connector's stored
    road instead, when it has one. All NULLs means keep the stored row as it is."""
    key = (round(lat, 3), round(lon, 3))
    row = conn.execute("SELECT name, address FROM cc_geocells WHERE lat3 = %s AND lon3 = %s", key).fetchone()
    old = specificity(*row) if row else 0
    if specificity(name, address) < 2 and old < 2:
        hit = conn.execute(CONNECTOR_NAME_SQL, key + key).fetchone()
        if hit:
            name = address = f"{hit[0]}, {hit[1]}"
            source = "public"  # the connector got it from a geocoder; a later local pass may better it
    if specificity(name, address) < old:
        return None, None, None
    return name, address, source


PUBLIC_PAUSE_S = 1.1  # OSM's Nominatim usage policy: at most one request a second
MAX_PUBLIC_FALLBACKS = 30  # a pass of local-fast cells must not turn into 200 public hits


def geocode_cells(
    conn,
    limit: int = 30,
    pause_s: float = 1.1,
    url: str = NOMINATIM,
    source: str = "public",
    fallback_url: str | None = None,
    sleep=time.sleep,
) -> int:
    """Name up to `limit` unnamed endpoint cells, one lookup at a time. Meant to run in the
    background until nothing is pending.

    `url`/`source` are the primary geocoder (local maps add-on or the public Nominatim).
    When the primary raises ValueError ("unable to geocode" — a working service that
    just doesn't know this point) and `fallback_url` is given (only ever the public
    Nominatim, and only when Settings > Maps allows it), that cell gets one more try
    there, recorded as source='public'. Every request that reaches the public Nominatim —
    primary or fallback — is followed by >= PUBLIC_PAUSE_S regardless of `pause_s`
    (`pause_s` can be 0 when the primary is local, which has no such limit), and at most
    MAX_PUBLIC_FALLBACKS cells fall back per pass, so a batch of local-fast cells can
    never turn into hundreds of public requests a minute.

    A connection-level failure talking to the primary (ConnectionError, timeout — local
    maps itself is down, not "doesn't know this point") stops the pass instead of
    guessing cell by cell: geocoder_route's debounce is what decides to stop routing to
    local, not a one-off fallback here. A failed lookup with no fallback (or a failed
    fallback) leaves the cell unnamed but throttled, not retried every pass.
    """
    import httpx

    cells = pending_cells(conn, limit)
    if not cells:
        return 0
    done, fallbacks = 0, 0
    with httpx.Client() as client:
        for lat, lon in cells:
            name, address, used, public_hit = None, None, source, url == NOMINATIM
            try:
                name, address = reverse_geocode(lat, lon, client, url)
            except httpx.TransportError as err:
                where = "the public geocoder" if url == NOMINATIM else "local maps"
                print(f"{where} did not answer, stopping this pass: {err}", file=sys.stderr)
                if url != NOMINATIM:
                    break  # local itself is down: don't fall back cell by cell, just stop
            except Exception as err:  # noqa: BLE001 - "unable to geocode" or a bad status, not a dead service
                print(f"geocell lookup unable at {lat:.3f},{lon:.3f} ({url}): {err}", file=sys.stderr)
                if fallback_url and fallbacks < MAX_PUBLIC_FALLBACKS:
                    fallbacks += 1
                    public_hit = True
                    try:
                        name, address = reverse_geocode(lat, lon, client, fallback_url)
                        used = "public"
                    except Exception as fallback_err:  # noqa: BLE001
                        print(f"geocell fallback also failed at {lat:.3f},{lon:.3f}: {fallback_err}", file=sys.stderr)
            name, address, used = _keep_best(conn, lat, lon, name, address, used if name else None)
            _save_cell(conn, lat, lon, name, address, used)
            done += 1
            sleep(max(pause_s, PUBLIC_PAUSE_S) if public_hit else pause_s)
    return done


def fix_names(conn) -> int:
    """Re-derive auto names that came out as a bare road, from the stored address, no lookups."""
    rows = conn.execute("SELECT id, name, address FROM cc_places WHERE auto AND address IS NOT NULL AND position(',' in name) = 0").fetchall()
    fixed = 0
    for pid, name, address in rows:
        town = town_from_display(address)
        if town and town != name:
            conn.execute("UPDATE cc_places SET name = %s WHERE id = %s", (f"{name}, {town}"[:40], pid))
            fixed += 1
    conn.commit()
    return fixed


def resolve_url(conn) -> str | None:
    """The same geocoder decision the background loop makes: `cc_state.geocoder_url` when
    the site has written one (a local URL, "public", or "" for "nothing allowed"), else today's live
    settings and the maps add-on's own status — so `make discover` on the command line
    never calls a geocoder Settings > Maps has turned off."""
    row = conn.execute("SELECT value FROM cc_state WHERE key = %s", ("geocoder_url",)).fetchone()
    if row is not None:
        v = row[0] if not isinstance(row, dict) else row["value"]
        if v == "public":
            return NOMINATIM
        return geocoder_url(v, True, False) if v else None
    import settings as settings_mod
    from mapsclient import MapsClient, available

    st = settings_mod.load(conn)
    maps = MapsClient(os.environ.get("MAPS_URL"))
    return geocoder_url(os.environ.get("MAPS_URL"), available(maps.status()), bool(st.get("public_geocoder")))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-visits", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fix-names", action="store_true", help="only re-derive bare road names from stored addresses")
    args = ap.parse_args(argv)
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if args.fix_names:
            print(f"renamed {fix_names(conn)} places")
            return 0
        n = discover(conn, args.min_visits, args.dry_run, url=None if args.dry_run else resolve_url(conn))
    print(f"{'would add' if args.dry_run else 'added'} {n} places")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
