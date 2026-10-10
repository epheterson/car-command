"""Car Command web: the window onto every car in the garage.

Live state comes from the CarConnectivity REST plugin (one HTTP call per page);
history comes straight from the database plugin's tables. No state of its own.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime  # the `time` module is in use below; the class gets an alias
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, quote, urlparse
from zoneinfo import ZoneInfo

import httpx
import psycopg
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import File, Form, UploadFile
import csv
import io

from starlette.background import BackgroundTask
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

import activity
import auth
import averages as averages_mod
import billed
from charts import line_chart
import cost
import planview
import urdb
import version
from cost import session_cost
import settings as settings_mod
from day import day_sessions, day_totals, elevation_profile, since_charge, time_at_places
from discover import GEOCELL_BACKFILL, GEOCELL_DDL, NOMINATIM, RENAME_LOCAL_ONCE, geocode_cells, geocoder_route, geocoder_url, requeue_public_geocells as _requeue_public_geocells
from facts import DDL as FACTS_DDL, clear_prices, drain_reprices, fill_facts, place_sessions, queue_reprice, reopen_average_prices
from history import IMPORT_DDL
from filters import places as place_markup, register as register_filters
import placeicons
from placeicons import ICON_KEYS
from places import DDL as PLACES_DDL, PlaceIndex, clamp_radius, is_home as is_home_place, migrate_home, parents as place_parents, place_for, posted_address, resolve_merged
import placeprice
import adminapi
from notify import PLUG_REMINDER_BELOW_PCT, plug_in_reminder
from teslafi_import import charge_limit, plugged_state
from runs import extend_or_start, within
from recorder import DDL as RECORDER_DDL, Recorder
from stats import (
    CHARGE_SORTS,
    DRIVE_SORTS,
    calendar_grid,
    charging_summary,
    drive_cost,
    efficiency_bins,
    efficiency_by_year,
    estimate_note,
    filter_charges,
    filter_drives,
    month_window,
    petrol_comparison,
    sort_rows,
    summarize,
    timeline_days,
    year_grids,
)
import service
from service import due_status
from samples import charge_samples, drive_samples, run_points, thinned, with_measured_power
import routematch
from thumbs import anchored, route_svg, routes_for
import autotag
import manual
import health as health_mod
import chartdata
import daypage
import statspage
import placespage
import settingsform
import sources
import connectors
import livestream
import connect
import proxyauth
from mapsclient import MapsClient, available as maps_available, region_name, region_reason, valid_region_id
from mapsworker import Worker as MapsWorker
import tesla_fleet
import tesla_jws
import tesla_keys
import history_writer
import tesla_compare
import tesla_live
import tesla_nav
import writerswitch
import identity
import tesla_net
import teslapage
import nowpage
import energy
import energy_cloud
import energypage
import units as units_mod
import raw
import notify as notify_mod
import weather
from summary import daily_summary_text, drive_announcement_due, summary_due
from teslafi_live import TeslaFiPoller
from teslamate_live import TeslaMatePoller
from units import (
    filter_idles,
    charge_minutes_left,
    idle_details,
    idles_by_day,
    ACTIVE as ACTIVE_UNITS,
    c_to_f,
    use_units,
    charge_stats,
    classify_status,
    climate_active,
    tread_mm,
    current_streak,
    daily_breakdown,
    days_between,
    drive_kwh,
    electrics_stats,
    humanize_age,
    humanize_duration,
    idle_periods,
    km_to_mi,
    rated_mile_kwh,
    ribbon_segments,
    trip_stats,
)

import logging

LOG = logging.getLogger("carcommand.web")
# Uvicorn configures its own loggers and leaves the root one bare, so anything this app
# logged went nowhere. An operator has to be able to read which mode sign-in is in.
LOG.setLevel(logging.INFO)
if not LOG.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    LOG.addHandler(_handler)
HERE = Path(__file__).parent
DB_URL = os.environ["DATABASE_URL"]
REST_URL = os.environ.get("CC_REST_URL", "http://carcommand-cc:40000")
REST_AUTH = base64.b64encode(f"{os.environ['REST_USER']}:{os.environ['REST_PASSWORD']}".encode()).decode()
TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
site = FastAPI(title="Car Command")
site.add_middleware(GZipMiddleware, minimum_size=2048)
site.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
register_filters(templates.env, TZ, place_icons=lambda: place_icon_map())


# ---- live ------------------------------------------------------------------


def leaf(node: dict, *path: str):
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node.get("val") if isinstance(node, dict) else None


def tristate(value, yes: str, no: str) -> bool | None:
    """True, False, or None for anything the car did not actually say."""
    if value == yes:
        return True
    if value == no:
        return False
    return None


def garage() -> dict:
    """Every vehicle the connector has, or an empty dict when it is not answering.

    It used to raise 503, which took the whole site down with it: someone running on
    imported history alone has no connector at all and could never get past the front
    page. A connector that is down is now a missing panel, not a missing site."""
    if (held := _cache.get("connector_down")) and (datetime.now(timezone.utc) - held[0]).total_seconds() < 15:
        return {}
    try:
        r = httpx.get(f"{REST_URL}/garage", headers={"Authorization": f"Basic {REST_AUTH}"}, timeout=8)
        r.raise_for_status()
    except httpx.HTTPError as err:
        # Remembered for a quarter minute: a hung connector must not cost every page its timeout.
        _cache["connector_down"] = (datetime.now(timezone.utc), f"CarConnectivity is not answering ({err.__class__.__name__}).")
        return {}
    _cache.pop("connector_down", None)
    _cache["connector_seen"] = True
    tree = r.json() or {}
    _cache["garage_tree"] = (datetime.now(timezone.utc), tree)  # the recorder's snapshot hook keeps it raw
    return tree


NO_CONNECTOR = "CarConnectivity is not running; every page reads logged history."


def connector_down() -> str | None:
    """The message to show beside a stale live panel, or None while the connector answers.

    An install that has never had a connector answer — history only, the way a fresh
    install with an import runs — gets a calm sentence, not a warning: nothing is wrong."""
    held = _cache.get("connector_down")
    if not held:
        return None
    return held[1] if _cache.get("connector_seen") else NO_CONNECTOR


def all_vehicles() -> list[dict]:
    """Every vehicle Car Command knows: live ones from the connector, imported ones from the database."""
    live = garage()
    out = [{"vin": vin, "name": leaf(v, "name") or vin, "live": True} for vin, v in live.items()]
    with db() as conn:
        for r in conn.execute("SELECT vin, name FROM vehicles ORDER BY name").fetchall():
            if r["vin"] not in live:
                out.append({"vin": r["vin"], "name": r["name"] or r["vin"], "live": False})
    return out


def reported_at(v: dict) -> datetime | None:
    """When the connector last heard from a car: the newest update stamp of a few of its leaves."""
    updated = None
    for key in ("state", "odometer", "name"):
        upd = v.get(key, {}).get("upd") if isinstance(v.get(key), dict) else None
        if upd:
            updated = max(updated or upd, upd)
    return datetime.fromisoformat(updated) if updated else None


def connector_status() -> tuple[datetime | None, str | None]:
    """How the connector's source is doing, from what the connector itself reports: the newest
    reading of any car it has, or why there is none. Lucid refusing a token shows up here as a
    connector with no car, since the connector keeps retrying rather than stopping."""
    g = garage()
    if g:
        times = [t for t in (reported_at(v) for v in g.values()) if t]
        return (max(times) if times else None, None)
    return (None, connector_down() or "CarConnectivity reports no car. Lucid may have stopped taking the token: sign in again.")


def live_vehicle(vin: str | None = None) -> dict:
    vin = vin or None  # the empty car has a blank vin; that is "no car", not "a car called ''"
    g = garage()
    if vin is not None and vin not in g:
        return stored_vehicle(vin)
    if not g:
        # No connector, or none configured yet. Fall back to imported history, and to a
        # car-shaped nothing when there is not even that, so the page can say what to do.
        with db() as conn:
            first = conn.execute("SELECT vin FROM vehicles ORDER BY name LIMIT 1").fetchone()
        return stored_vehicle(first["vin"]) if first else empty_car()
    vin, v = (vin, g[vin]) if vin else next(iter(g.items()))
    updated_dt = reported_at(v)
    status = classify_status(leaf(v, "state"), leaf(v, "connection_state"), leaf(v, "charging", "state"))
    loc = v.get("position", {}).get("position_location", {})
    address = ", ".join(x for x in (leaf(loc, "road"), leaf(loc, "city")) if x) or None
    here = place_for(leaf(v, "position", "latitude"), leaf(v, "position", "longitude"), places()) or {}
    return {
        "vin": vin,
        "name": leaf(v, "name") or vin,
        "make": identity.make_of(vin, leaf(v, "manufacturer")),
        "model": f"{leaf(v, 'manufacturer') or ''} {leaf(v, 'model') or ''}".strip(),
        "software": leaf(v, "software", "version"),
        "status": status,
        "status_class": status.lower(),
        "soc": leaf(v, "drives", "primary", "level"),
        "range_mi": km_to_mi(leaf(v, "drives", "primary", "range")),
        "range_full_mi": km_to_mi(leaf(v, "drives", "primary", "range_estimated_full")),
        "odometer_mi": km_to_mi(leaf(v, "odometer")),
        "outside_f": c_to_f(leaf(v, "outside_temperature")),
        "battery_f": c_to_f(leaf(v, "drives", "primary", "battery", "temperature_max")),
        "capacity_kwh": leaf(v, "drives", "primary", "battery", "available_capacity"),
        # Tri-state on purpose. Collapsing "unknown" to False says the car is unlocked,
        # and anything watching for locked -> unlocked then reports an open car that is
        # not open. None means the car has not said.
        "locked": tristate(leaf(v, "doors", "lock_state"), "locked", "unlocked"),
        "doors_open": tristate(leaf(v, "doors", "open_state"), "open", "closed"),
        "climate": leaf(v, "climatization", "state"),
        "charging_power_kw": leaf(v, "charging", "power"),
        "charge_target": leaf(v, "charging", "settings", "target_level"),
        # Whole minutes to the target, from the connector's own estimate or the power now; None when not charging.
        "charge_minutes_left": charge_minutes_left(
            status,
            leaf(v, "charging", "estimated_date_reached"),
            leaf(v, "charging", "power"),
            leaf(v, "drives", "primary", "level"),
            leaf(v, "charging", "settings", "target_level"),
            leaf(v, "battery_capacity_kwh") or leaf(v, "drives", "primary", "battery", "available_capacity"),
            datetime.now(timezone.utc),
        ),
        # Tri-state like `locked`: None until the connector publishes it, which the Lucid
        # one does from v0.1.7. A car that has not said is not a car left unplugged.
        "plugged": tristate(leaf(v, "charging", "connector", "connection_state"), "connected", "disconnected"),
        "windows_open": tristate(leaf(v, "windows", "open_state"), "open", "closed"),
        # Connector-specific, from v0.1.7 of the Lucid connector: None until then.
        "sentry": leaf(v, "sentry") if isinstance(leaf(v, "sentry"), bool) else None,
        "inside_f": c_to_f(leaf(v, "inside_temperature")) if leaf(v, "inside_temperature") is not None else None,
        # Tire pressures in psi, from bar, and whether the car itself is warning about any
        # wheel. The Lucid publishes both from connector v0.1.7; TeslaFi's export carries
        # no tire field at all, so this is the one car in the garage that can say.
        "tires_psi": {w: round(leaf(v, f"tire_pressure_{w}") * 14.5038, 1) for w in ("front_left", "front_right", "rear_left", "rear_right") if leaf(v, f"tire_pressure_{w}") is not None} or None,
        "tire_warning": leaf(v, "tire_warning") if isinstance(leaf(v, "tire_warning"), bool) else None,
        # From connector v0.1.8: Sentry reacting to something, the pack's own capacity and
        # energy, the car's battery-health flag, and whether an update is waiting.
        "sentry_threat": leaf(v, "sentry_threat") if isinstance(leaf(v, "sentry_threat"), bool) else None,
        "pack_capacity_kwh": leaf(v, "battery_capacity_kwh"),
        "pack_energy_kwh": leaf(v, "battery_energy_kwh"),
        "battery_warning": leaf(v, "battery_health_warning") if isinstance(leaf(v, "battery_health_warning"), bool) else None,
        "update_available": leaf(v, "software_update_available") if isinstance(leaf(v, "software_update_available"), bool) else None,
        "lat": leaf(v, "position", "latitude"),
        "lon": leaf(v, "position", "longitude"),
        "heading": leaf(v, "position", "heading"),
        # The connector publishes speed as its own attribute, because CarConnectivity's
        # model has no home for one. km/h from the car; the tables are imperial.
        "speed_mph": km_to_mi(leaf(v, "speed")),
        # the address the owner gave the place they are at wins over the car's own lookup
        "address": here.get("user_address") or address,
        "place": here.get("name"),
        # only a name the owner gave: a place the site named itself may be a street
        "place_named": here.get("name") if here and not here.get("auto") else None,
        "at_home": is_home_place(here),
        "updated": updated_dt,
        "updated_age": humanize_age(datetime.now(timezone.utc) - updated_dt) if updated_dt else "unknown",
        "live": True,
        "source": "connector",
    }


def stored_vehicle(vin: str) -> dict:
    """A vehicle with no live connector (imported history only): last known values from the tables."""
    with db() as conn:
        v = conn.execute("SELECT vin, name, manufacturer, model FROM vehicles WHERE vin = %s", (vin,)).fetchone()
        if v is None:
            raise HTTPException(404, "No such vehicle.")
        pos = conn.execute("SELECT at, latitude, longitude, level, odometer FROM cc_positions WHERE vin = %s ORDER BY at DESC LIMIT 1", (vin,)).fetchone()
        lvl = conn.execute("SELECT l.level, l.last_date FROM drive_levels l JOIN drives d ON d.id = l.drive_id WHERE d.vin = %s ORDER BY l.last_date DESC LIMIT 1", (vin,)).fetchone()
        rng = conn.execute("SELECT l.range FROM drive_ranges l JOIN drives d ON d.id = l.drive_id WHERE d.vin = %s ORDER BY l.last_date DESC LIMIT 1", (vin,)).fetchone()
        st = conn.execute("SELECT state::text AS s, last_date FROM states WHERE vin = %s ORDER BY last_date DESC LIMIT 1", (vin,)).fetchone()
        cap = conn.execute("SELECT capacity FROM drives WHERE vin = %s ORDER BY id LIMIT 1", (vin,)).fetchone()
        sw = conn.execute("SELECT version FROM cc_software WHERE vin = %s ORDER BY seen_at DESC LIMIT 1", (vin,)).fetchone()
        # the car's own trip meters, from the newest feed row when a feed is on for this car
        feed = (
            conn.execute(
                """SELECT at, data->>'SelfDrivingMilesSinceReset' AS fsd, data->>'MilesSinceReset' AS trip, data->>'inside_tempF' AS inside,
                                      data->>'outside_tempF' AS outside, data->>'locked' AS locked, data->>'sentry_mode' AS sentry,
                                      data->>'charging_state' AS charging_state, data->>'charge_limit_soc' AS charge_limit,
                                      data->>'carState' AS car_state, data->>'battery_level' AS level,
                                      data->>'latitude' AS lat, data->>'longitude' AS lon
                               FROM cc_teslafi_rows WHERE vin = %s AND data->>'battery_level' IS NOT NULL ORDER BY at DESC LIMIT 1""",
                (vin,),
            ).fetchone()
            if conn.execute("SELECT to_regclass('cc_teslafi_rows') AS t").fetchone()["t"]
            else None
        )
    updated = max(x for x in (pos["at"] if pos else None, lvl["last_date"] if lvl else None, st["last_date"] if st else None) if x) if (pos or lvl or st) else None
    status = {"DRIVING": "Driving", "PARKED": "Idle", "IGNITION_ON": "Ready", "OFFLINE": "Sleeping"}.get(st["s"] if st else None, "Unknown")
    known = places()
    lat, lon = (pos["latitude"], pos["longitude"]) if pos else (None, None)
    soc = lvl["level"] if lvl else (pos["level"] if pos else None)
    # The feed's history is rewritten once an hour; its newest row is the car now.
    clock = datetime.now(timezone.utc)
    now_status = nowpage.feed_status(feed, updated, clock)
    source, odometer = None, pos["odometer"] if pos else None
    if now_status:
        status, updated, source = now_status, feed["at"], "feed"
        soc = _float_or_none(feed["level"]) if _float_or_none(feed["level"]) is not None else soc
        if _float_or_none(feed["lat"]) is not None and _float_or_none(feed["lon"]) is not None:
            lat, lon = _float_or_none(feed["lat"]), _float_or_none(feed["lon"])
    # Sentry only from a reading of now: the feed's newest row, then the stream's memory
    sentry = activity.sentry_on(feed["sentry"]) if feed and feed["at"] and clock - feed["at"] <= nowpage.FEED_FRESH else None
    # The car's own stream, when it has said something newer than both (memory: no query)
    st = activity.stream_now(tesla_stream_worker.carried.get(vin), clock)
    if st and (updated is None or st["at"] > updated):
        status = "Driving" if st["driving"] else ("Idle" if status == "Driving" else status)
        updated, source = st["at"], "stream"
        soc, odometer = st.get("level", soc), st.get("odometer", odometer)
        sentry = st.get("sentry", sentry)
    # What the stream says of the lock, doors and windows right now; unsaid is unknown, not "unlocked"
    locked, doors_open, windows_open = st.get("locked"), st.get("doors_open", False), st.get("windows_open")
    here = place_for(lat, lon, known) or {}
    return {
        "vin": vin,
        "name": v["name"] or vin,
        "model": f"{v['manufacturer'] or ''} {v['model'] or ''}".strip(),
        "software": sw["version"] if sw else None,
        "status": status,
        "status_class": status.lower(),
        "soc": soc,
        "range_mi": rng["range"] if rng else None,
        "range_full_mi": None,
        "odometer_mi": odometer,
        "outside_f": None,
        "battery_f": None,
        "capacity_kwh": cap["capacity"] if cap else None,
        "make": identity.make_of(vin, v["manufacturer"]),
        "sentry": sentry,
        "source": source,
        "locked": locked,
        "plugged": plugged_state(feed["charging_state"]) if feed else None,
        "doors_open": doors_open,
        "windows_open": windows_open,
        "climate": None,
        "charging_power_kw": None,
        "charge_target": charge_limit(feed["charge_limit"]) if feed else None,
        "charge_minutes_left": None,
        "lat": lat,
        "lon": lon,
        "heading": None,
        "speed_mph": None,
        "address": None,
        "place": here.get("name"),
        "place_named": here.get("name") if here and not here.get("auto") else None,
        "at_home": is_home_place(here),
        "updated": updated,
        "updated_age": humanize_age(datetime.now(timezone.utc) - updated) if updated else "unknown",
        "live": False,
        "meters": meters_from_feed(feed),
    }


GARAGE = "all"


def garage_car() -> dict:
    """The whole garage as a pseudo-vehicle, for pages that thread every car together."""
    return {
        "vin": GARAGE,
        "name": "All cars",
        "model": "",
        "software": None,
        "status": "",
        "status_class": "",
        "soc": None,
        "range_mi": None,
        "range_full_mi": None,
        "odometer_mi": None,
        "outside_f": None,
        "battery_f": None,
        "capacity_kwh": None,
        "locked": None,
        "plugged": None,
        "doors_open": False,
        "climate": None,
        "charging_power_kw": None,
        "charge_target": None,
        "charge_minutes_left": None,
        "lat": None,
        "lon": None,
        "heading": None,
        "speed_mph": None,
        "address": None,
        "place": None,
        "place_named": None,
        "updated": None,
        "updated_age": "",
        "live": False,
        "garage": True,
    }


def empty_car() -> dict:
    """Nothing connected and nothing imported. Shaped like a car so every template still
    renders; `empty` is what the pages read to offer the first steps instead of blanks."""
    return {**garage_car(), "vin": "", "name": "No car yet", "garage": False, "empty": True}


def meters_from_feed(feed) -> dict | None:
    """FSD and trip meters since the car's last reset, from a feed row."""
    if not feed:
        return None

    def f(x):
        try:
            return float(x) if x not in (None, "", "None", "NULL") else None
        except ValueError:
            return None

    fsd, trip = f(feed["fsd"]), f(feed["trip"])
    if trip is None:
        return None
    return {"fsd": fsd, "trip": trip, "share": (fsd / trip) if fsd is not None and trip > 0 else None, "at": feed["at"], "outside_f": f(feed["outside"]), "inside_f": f(feed["inside"])}


def pick(request: Request) -> dict:
    """The vehicle this request is about: ?vin=..., ?vin=all for the whole garage, or the first live one."""
    vin = request.query_params.get("vin")
    if vin == GARAGE:
        return garage_car()
    return live_vehicle(vin)


def single(request: Request) -> dict:
    """Like pick, but a page that only makes sense for one car gets the first live one when the garage is selected."""
    car = pick(request)
    return live_vehicle(None) if car.get("garage") else car


# ---- history ---------------------------------------------------------------


_POOL = None
_POOL_LOCK = threading.Lock()


def _pool():
    """One pool for the process, opened on first use: scripts and tests import this module
    without a database. Each query used to open its own connection, 36 of them for the
    garage Day page at a few milliseconds each."""
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                from psycopg_pool import ConnectionPool

                pool = ConnectionPool(
                    DB_URL,
                    min_size=2,
                    max_size=16,
                    timeout=15,
                    kwargs={"row_factory": dict_row, "connect_timeout": 5},
                    check=ConnectionPool.check_connection,  # a restarted database leaves dead connections behind
                    name="web",
                    open=True,
                )
                atexit.register(pool.close)
                _POOL = pool
    return _POOL


def db(timeout: float | None = None):
    """A pooled connection for a `with` block: committed when the block ends, rolled back if
    it raises, then handed back for the next query. Anything SET on it must be SET LOCAL,
    or the next borrower inherits it. `timeout` overrides the pool's default wait (15 s)
    for a caller that would rather fail fast than block a page on a dead database."""
    return _pool().connection(timeout=timeout)


def db_plain():
    return psycopg.connect(DB_URL, connect_timeout=5)


SOFTWARE_DDL = """
CREATE TABLE IF NOT EXISTS cc_software (
    id      SERIAL PRIMARY KEY,
    vin     VARCHAR NOT NULL,
    version VARCHAR NOT NULL,
    seen_at TIMESTAMPTZ NOT NULL
);
"""


def software_history(vin: str) -> list[dict]:
    with db() as conn:
        # an import and the live recorder can both record the same version; show each change once
        return conn.execute(
            """SELECT version, seen_at FROM (SELECT version, seen_at, LAG(version) OVER (ORDER BY seen_at) AS prev
                                                           FROM cc_software WHERE vin = %s) x
                               WHERE prev IS DISTINCT FROM version ORDER BY seen_at DESC""",
            (vin,),
        ).fetchall()


def record_software(vin: str, version: str | None) -> None:
    if not version:
        return
    with db() as conn:
        last = conn.execute("SELECT version FROM cc_software WHERE vin = %s ORDER BY seen_at DESC LIMIT 1", (vin,)).fetchone()
        if last is None or last["version"] != version:
            conn.execute("INSERT INTO cc_software (vin, version, seen_at) VALUES (%s, %s, %s)", (vin, version, datetime.now(timezone.utc)))
            conn.commit()


def tires_same(a, b, tolerance: float = 0.3) -> bool:
    """Four pressures continue one run while none has moved more than `tolerance` psi: the
    sensor wobbles a tenth between polls, and a run per wobble is a table of noise."""
    return all(x is not None and y is not None and abs(float(x) - float(y)) <= tolerance for x, y in zip(a, b))


def record_runs(car: dict) -> None:
    """The live half of the interval tables the importer fills from an export.

    Modes need both halves known: cc_modes is NOT NULL on sentry and climate, and a row
    saying Sentry was off for a stretch where the car had not said is a lie with a
    timestamp. The Lucid connector publishes sentry and cabin temperature from v0.1.7;
    until then both writes skip, which is the honest outcome, not an error."""
    now = datetime.now(timezone.utc)
    with db() as conn:
        extend_or_start(conn, "cc_modes", car["vin"], now, ("sentry", "climate"), (car.get("sentry"), climate_active(car.get("climate"))))
        # Half a degree of drift is one run; the importer's runs are per distinct
        # reading, but a live sensor wobbling by a tenth would otherwise write a row a tick.
        extend_or_start(conn, "cc_inside_temperatures", car["vin"], now, ("inside_f",), (car.get("inside_f"),), same=within(0.5))
        psi = car.get("tires_psi") or {}
        four = tuple(psi.get(w) for w in ("front_left", "front_right", "rear_left", "rear_right"))
        extend_or_start(conn, "cc_tires", car["vin"], now, ("fl", "fr", "rl", "rr"), four, same=tires_same)
        conn.commit()


SERVICE_DDL = """
CREATE TABLE IF NOT EXISTS cc_tires (
    id         BIGSERIAL PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    first_date TIMESTAMPTZ NOT NULL,
    last_date  TIMESTAMPTZ NOT NULL,
    fl DOUBLE PRECISION NOT NULL, fr DOUBLE PRECISION NOT NULL, rl DOUBLE PRECISION NOT NULL, rr DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS cc_tires_vin_first ON cc_tires (vin, first_date);
CREATE TABLE IF NOT EXISTS cc_tire_sets (
    id              SERIAL PRIMARY KEY,
    vin             VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    fitted_on       DATE NOT NULL,
    fitted_odometer DOUBLE PRECISION,
    removed_on      DATE,
    notes           VARCHAR
);
CREATE TABLE IF NOT EXISTS cc_tread (
    id          SERIAL PRIMARY KEY,
    vin         VARCHAR NOT NULL,
    measured_on DATE NOT NULL,
    odometer    DOUBLE PRECISION,
    front_left  DOUBLE PRECISION,
    front_right DOUBLE PRECISION,
    rear_left   DOUBLE PRECISION,
    rear_right  DOUBLE PRECISION,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cc_service (
    id         SERIAL PRIMARY KEY,
    vin        VARCHAR NOT NULL,
    done_on    DATE,
    odometer   DOUBLE PRECISION,
    what       VARCHAR NOT NULL,
    cost       DOUBLE PRECISION,
    notes      TEXT,
    due_date   DATE,
    due_miles  DOUBLE PRECISION,
    done       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE cc_service ADD COLUMN IF NOT EXISTS every_months INTEGER;
ALTER TABLE cc_service ADD COLUMN IF NOT EXISTS every_miles DOUBLE PRECISION;
CREATE TABLE IF NOT EXISTS cc_service_files (
    id           SERIAL PRIMARY KEY,
    item_id      INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size         INTEGER NOT NULL,
    data         BYTEA NOT NULL,
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- No foreign key from the files to the record on purpose: pg_restore --clean of an older
-- dump drops cc_service's primary key first and fails when a newer table depends on it,
-- which is exactly what an upgrade-with-data does. The delete route removes the files.
ALTER TABLE cc_service_files DROP CONSTRAINT IF EXISTS cc_service_files_item_id_fkey;
CREATE INDEX IF NOT EXISTS cc_service_files_item ON cc_service_files (item_id);
"""


def service_items(vin: str | None) -> list[dict]:
    with db() as conn:
        if vin:
            items = conn.execute(
                "SELECT s.*, v.name AS car_name FROM cc_service s LEFT JOIN vehicles v ON v.vin = s.vin WHERE s.vin = %s ORDER BY done, done_on DESC NULLS FIRST, id DESC", (vin,)
            ).fetchall()
        else:
            items = conn.execute("SELECT s.*, v.name AS car_name FROM cc_service s LEFT JOIN vehicles v ON v.vin = s.vin ORDER BY done, done_on DESC NULLS FIRST, id DESC").fetchall()
        # The attachments, names and sizes only; the bytes are read when one is opened.
        files = conn.execute("SELECT id, item_id, filename, content_type, size FROM cc_service_files WHERE item_id = ANY(%s) ORDER BY id", ([it["id"] for it in items],)).fetchall() if items else []
    by_item: dict[int, list] = {}
    for f in files:
        by_item.setdefault(f["item_id"], []).append(dict(f, size_text=service.size_text(f["size"])))
    for it in items:
        it["files"] = by_item.get(it["id"], [])
    return items


def service_due(vin: str, odometer: float | None) -> list[dict]:
    """Open reminders for a car that are due or due soon, for the Now page."""
    today = datetime.now(TZ).date()
    out = []
    for it in service_items(vin):
        if it["done"]:
            continue
        st = due_status(it, today, odometer)
        if st:
            out.append(dict(it, status=st))
    return out


TAGS_DDL = """
CREATE TABLE IF NOT EXISTS cc_drive_tags (
    trip_id    INTEGER PRIMARY KEY,
    tag        VARCHAR NOT NULL,
    note       TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cc_drive_tags_tag ON cc_drive_tags (tag);
"""


def tag_names() -> list[str]:
    with db() as conn:
        return [r["tag"] for r in conn.execute("SELECT tag FROM cc_drive_tags GROUP BY tag ORDER BY COUNT(*) DESC, tag").fetchall()]


def set_tag(trip_id: int, tag: str, note: str) -> None:
    with db() as conn:
        if tag:
            conn.execute(
                """INSERT INTO cc_drive_tags (trip_id, tag, note, updated_at) VALUES (%s, %s, %s, now())
                            ON CONFLICT (trip_id) DO UPDATE SET tag = EXCLUDED.tag, note = EXCLUDED.note, updated_at = now()""",
                (trip_id, tag, note or None),
            )
        else:
            conn.execute("DELETE FROM cc_drive_tags WHERE trip_id = %s", (trip_id,))
        conn.commit()


_cache: dict[str, tuple[datetime, object]] = {}


def cached(key: str, ttl_s: int, load):
    hit = _cache.get(key)
    now = datetime.now(timezone.utc)
    if hit and (now - hit[0]).total_seconds() < ttl_s:
        return hit[1]
    value = load()
    _cache[key] = (now, value)
    return value


def place_arrivals() -> list[tuple]:
    """Every drive's end with the car's next departure: (vin, lat, lon, arrived, left).
    One query for every car, kept ten minutes; the Places page draws each place's stays
    from it."""

    def load():
        with db() as conn:
            rows = conn.execute("""SELECT vin, lat, lon, arrived, left_at FROM (
                       SELECT vin, destination_position_latitude AS lat, destination_position_longitude AS lon, destination_date AS arrived,
                              LEAD(start_date) OVER (PARTITION BY vin ORDER BY start_date) AS left_at
                       FROM trips) t
                   WHERE arrived IS NOT NULL AND lat IS NOT NULL""").fetchall()
        return [(r["vin"], r["lat"], r["lon"], r["arrived"], r["left_at"]) for r in rows]

    return cached("place_arrivals", 600, load)


def place_stays() -> dict[int, dict]:
    """Each place's stays, worked out once and kept ten minutes; adding, renaming or
    removing a place clears it."""
    return cached("place_stays", 600, lambda: placespage.stays(place_arrivals(), list(places()), TZ))


def nav_rows() -> list[dict]:
    """Every drive a car navigated somewhere named (tesla_nav), kept ten minutes: the
    hourly replay fills the table, so nothing fresher is lost."""

    def load():
        with db() as conn:
            return tesla_nav.load(conn)

    return cached("nav_rows", 600, load)


def car_names() -> dict[str, str]:
    return {v["vin"]: v["name"] for v in all_vehicles()}


def place_nav() -> dict[int, dict]:
    """place id -> the name the cars' navigation gives it (tesla_nav.place_suggestions),
    worked out once and kept ten minutes; a change to the places clears it."""
    return cached("place_nav", 600, lambda: tesla_nav.place_suggestions(nav_rows(), places(), car_names()))


def vehicle_energy() -> dict[str, tuple[float | None, float | None]]:
    """vin -> (usable capacity kWh, measured kWh per rated mile)."""

    def load():
        with db() as conn:
            rows = conn.execute("""SELECT d.vin, d.capacity, f.kwh_per_rated_mi FROM drives d LEFT JOIN cc_vehicle_facts f ON f.vin = d.vin""").fetchall()
        return {r["vin"]: (r["capacity"], r["kwh_per_rated_mi"]) for r in rows}

    return cached("vehicle_energy", 300, load)


def rated_mile_years() -> dict[tuple[str, int], float]:
    """(vin, year) -> what a rated mile cost that car that year, for the years that
    measured enough charges to say. Read beside vehicle_energy(), which holds the
    all-time figure every other year falls back to."""

    def load():
        with db() as conn:
            rows = conn.execute("SELECT vin, year, kwh_per_rated_mi FROM cc_vehicle_year_facts WHERE kwh_per_rated_mi IS NOT NULL").fetchall()
        return {(r["vin"], r["year"]): r["kwh_per_rated_mi"] for r in rows}

    return cached("rated_mile_years", 300, load)


def parked_rates(cars: list[dict]) -> list[dict]:
    """What an hour parked has cost each of these cars, by what was running.

    Measured from the car's own parked stretches and stored once a day; this is a read
    of `cc_vehicle_facts`, not a measurement. A figure with too few stretches behind it
    was stored as null and is simply absent here."""

    def load():
        with db() as conn:
            rows = conn.execute("""SELECT vin, idle_mi_per_h, idle_samples, sentry_mi_per_h, sentry_samples, climate_mi_per_h, climate_samples
                   FROM cc_vehicle_facts""").fetchall()
        return {r["vin"]: r for r in rows}

    by_vin = cached("parked_rates", 300, load)
    out = []
    for c in cars:
        r = by_vin.get(c["vin"])
        if not r or r["idle_mi_per_h"] is None:
            continue
        out.append(
            {
                "name": c["name"],
                "guard": identity.guard_name(c.get("make")),
                "idle": r["idle_mi_per_h"],
                "idle_n": r["idle_samples"],
                "sentry": r["sentry_mi_per_h"],
                "sentry_n": r["sentry_samples"],
                "climate": r["climate_mi_per_h"],
                "climate_n": r["climate_samples"],
            }
        )
    return out


def home_rates() -> dict[str, tuple[float | None, bool]]:
    """vin -> (what a kWh at home has cost this car, measured from its priced sessions;
    whether any of them was priced on a regional average)."""

    def load():
        with db() as conn:
            rows = conn.execute("SELECT vin, home_rate, home_rate_estimate FROM cc_vehicle_facts").fetchall()
        return {r["vin"]: (r["home_rate"], bool(r["home_rate_estimate"])) for r in rows}

    return cached("home_rates", 300, load)


def geocells() -> dict[tuple[float, float], str]:
    """100 m cell -> short address, for endpoints no named place covers. The table is
    created at startup (apply_schema); this never runs DDL per call."""

    def load():
        with db() as conn:
            rows = conn.execute("SELECT lat3, lon3, name FROM cc_geocells WHERE name IS NOT NULL").fetchall()
        return {(float(r["lat3"]), float(r["lon3"])): r["name"] for r in rows}

    return cached("geocells", 300, load)


def rough_label(lat, lon, cells: dict) -> str | None:
    if lat is None or lon is None:
        return None
    return cells.get((round(lat, 3), round(lon, 3)))


def with_street(rows: list[dict], cells: dict) -> list[dict]:
    """Each place with `street`: its cell's cached "Road, City" when it has no stored
    address, so a named place reads as a street rather than a coordinate."""
    return [{**r, "street": None if r.get("address") else rough_label(r.get("latitude"), r.get("longitude"), cells)} for r in rows]


PLACE_COLUMNS = "id, name, latitude, longitude, radius_m, auto, visits, address, user_address, icon, price, merged_into"


def place_rows() -> list[dict]:
    """Every row of cc_places, busiest first, merged ones included."""
    with db() as conn:
        return conn.execute(f"SELECT {PLACE_COLUMNS} FROM cc_places ORDER BY visits DESC NULLS LAST, name").fetchall()


def places() -> PlaceIndex:
    """Every circle a stop can be in, each answering as its place (a place merged into
    another answers as that one: places.resolve_merged)."""
    return PlaceIndex(resolve_merged(place_rows()))


def places_detail() -> list[dict]:
    """For the Places page and every list of places: busiest first, with how the name
    came to be; a place merged into another is not one of them (places.parents)."""
    return place_parents(place_rows())


def place_icon_map() -> dict[str, str | None]:
    """lower(name) -> the stored icon (possibly None, possibly the literal "none"),
    cached ten minutes; cleared wherever a place's name or icon changes. The `places`
    filter resolves the default (Home/Work) itself, so this carries the raw value.

    Registered as a Jinja global (filters.py), so this runs wherever `|places` runs —
    including a macro imported without context, and a card drawn straight from the live
    stream with no request context at all. A database that is unreachable fails in ~2 s,
    not the pool's full 15 s wait, and the empty result is cached like any other, so a
    whole test run (or a real outage) pays that once, not per render."""

    def load():
        try:
            with db(timeout=2) as conn:
                rows = conn.execute("SELECT name, icon FROM cc_places WHERE merged_into IS NULL").fetchall()
            return {r["name"].lower(): r["icon"] for r in rows}
        except Exception as err:  # noqa: BLE001 - no icon beats no page
            LOG.warning("place_icon_map: %s", err)
            return {}

    return cached("place_icons", 600, load)


def set_place_price(conn, place_id: int, price: dict | None) -> bool:
    """Store a place's price; when it differs from the one stored, its charges, and those
    of every place merged into it, are queued to be priced again in the background. The
    caller commits. True when it changed."""
    row = conn.execute("SELECT id, latitude, longitude, radius_m, price FROM cc_places WHERE id = %s", (place_id,)).fetchone()
    if not row or (row["price"] or None) == price:
        return False
    conn.execute("UPDATE cc_places SET price = %s WHERE id = %s", (json.dumps(price) if price else None, place_id))
    queue_with_parts(conn, dict(row))
    return True


def queue_with_parts(conn, row: dict, every: bool = False) -> list[dict]:
    """Queue a place's charges to be priced again, and those of every place merged into it
    (they answer as it). Returns the circles queued. The caller commits."""
    parts = [dict(r) for r in conn.execute(f"SELECT {PLACE_COLUMNS} FROM cc_places WHERE merged_into = %s", (row["id"],)).fetchall()]
    for c in [row, *parts]:
        queue_reprice(conn, c, every=every)
    return [row, *parts]


MOVE_DEG = 1e-6  # about 0.1 m: closer than this a posted position is the stored one
MOVE_M = 0.5


def moved_to(before: dict, where) -> tuple[float, float, float] | None:
    """The (latitude, longitude, radius_m) a place moves to, or None when `where` leaves
    it where it is. `where` is (lat, lon, radius) with None for a value not changed; a value
    within rounding of the stored one is not a change (the page posts six decimals)."""
    if where is None:
        return None
    lat, lon, radius = where
    lat = before["latitude"] if lat is None or abs(lat - before["latitude"]) < MOVE_DEG else lat
    lon = before["longitude"] if lon is None or abs(lon - before["longitude"]) < MOVE_DEG else lon
    radius = before["radius_m"] if radius is None or abs(radius - before["radius_m"]) < MOVE_M else radius
    after = (lat, lon, radius)
    return None if after == (before["latitude"], before["longitude"], before["radius_m"]) else after


KEEP = object()  # rename_place: leave this field as it is


def place_row(conn, place_id: int) -> dict | None:
    row = conn.execute(f"SELECT {PLACE_COLUMNS} FROM cc_places WHERE id = %s", (place_id,)).fetchone()
    return dict(row) if row else None


def forget_places() -> None:
    """What is worked out from the places, kept a while: cleared wherever one changes."""
    _cache.pop("place_stays", None)
    _cache.pop("place_nav", None)
    _cache.pop("place_icons", None)
    _cache.pop("geocells", None)


def rename_place(
    place_id: int,
    name: str | None,
    icon=None,
    price: tuple[bool, dict | None] = (False, None),
    address: tuple[bool, str | None] = (False, None),
    where: tuple[float, float, float] | None = None,
) -> None:
    """`price` is (whether the form carried one, the price), as placeprice.from_form says;
    `address` the same for the owner's own address (places.posted_address). A name of None
    and an icon of KEEP leave those as they are (the admin API's PATCH); a name given makes
    the place a named one. `where` is (latitude, longitude, radius_m) as the owner moved
    them, None in any one they did not touch (moved_to).

    Whatever changes which charges are this place's, or how they are priced, queues them
    to be priced again in the background (facts.queue_reprice): becoming home or not (the
    icon, or a name with no icon chosen), and moving or resizing it, for both circles."""
    with db() as conn:
        before = place_row(conn, place_id)
        if before is None:
            return
        if name is not None:
            conn.execute("UPDATE cc_places SET name = %s, auto = FALSE WHERE id = %s", (name, place_id))
        if icon is not KEEP:
            conn.execute("UPDATE cc_places SET icon = %s WHERE id = %s", (icon, place_id))
        if price[0]:
            set_place_price(conn, place_id, price[1])
        if address[0]:
            conn.execute("UPDATE cc_places SET user_address = %s WHERE id = %s", (address[1], place_id))
        moved = moved_to(before, where)
        if moved:
            conn.execute("UPDATE cc_places SET latitude = %s, longitude = %s, radius_m = %s WHERE id = %s", (*moved, place_id))
        after = place_row(conn, place_id)
        if moved:
            queue_reprice(conn, before, every=True)  # the stops it let go of
            queue_reprice(conn, after)
        if is_home_place(before) != is_home_place(after):
            queue_with_parts(conn, after)
        conn.commit()
    forget_places()


def merge_places(keep_id: int, other_ids) -> bool:
    """Make the others part of `keep`: each one's circle answers as `keep` from now on (its
    stops, visits and charges are `keep`'s), and it leaves every list. `other_ids` is one id
    or several; a place already merged into one of them follows it, and one merged into
    another place is taken with that place. Their charges are priced again as `keep`'s in
    the background. All or nothing: False, with nothing changed, when `keep` or any other is
    missing, `keep` is itself merged away, or nothing is left to take in."""
    others = [other_ids] if isinstance(other_ids, int) else list(other_ids)
    with db() as conn:
        if not _merge_into(conn, keep_id, others):
            return False
        conn.commit()
    forget_places()
    return True


def _merge_into(conn, keep_id: int, others: list[int]) -> bool:
    """merge_places' work inside the caller's transaction; the caller commits. Every check is
    made before anything is written."""
    keep = place_row(conn, keep_id)
    if not keep or keep.get("merged_into") is not None:
        return False
    tops: list[int] = []
    for oid in others:
        other = place_row(conn, oid)
        if not other:
            return False
        top = other["merged_into"] if other.get("merged_into") is not None else oid
        if top != keep_id and top not in tops:
            tops.append(top)
    if not tops or any(place_row(conn, t) is None for t in tops):
        return False
    for top in tops:
        circles = [place_row(conn, top)] + [dict(r) for r in conn.execute(f"SELECT {PLACE_COLUMNS} FROM cc_places WHERE merged_into = %s", (top,)).fetchall()]
        conn.execute("UPDATE cc_places SET merged_into = %s WHERE merged_into = %s", (keep_id, top))
        conn.execute("UPDATE cc_places SET merged_into = %s, auto = FALSE WHERE id = %s", (keep_id, top))
        for c in circles:
            queue_reprice(conn, c, every=True)
    return True


def pin_into(lat: float, lon: float, keep_id: int, radius_m: float = 120.0) -> bool:
    """A stop named after a place nearby, taken into that place instead of becoming a second
    one with the same name: a found place covering the stop is merged into it; with none, a
    new circle at the stop is added as its part. `keep` is never renamed, moved or resized,
    and the stop's charges are priced again as `keep`'s in the background. False when
    `keep` is missing or merged away, or when the stop is a place somebody named (that one
    is merged on purpose, with Merge, not from here)."""
    with db() as conn:
        rows = conn.execute("SELECT id, name, latitude, longitude, radius_m, icon, auto, merged_into FROM cc_places").fetchall()
        hit = place_for(lat, lon, resolve_merged([dict(r) for r in rows]))
        if hit and hit["id"] == keep_id:
            return place_row(conn, keep_id) is not None  # already its
        if hit and not hit.get("auto"):
            return False
        if hit is None:
            keep = place_row(conn, keep_id)
            if not keep or keep.get("merged_into") is not None:
                return False
            new = conn.execute(
                "INSERT INTO cc_places (name, latitude, longitude, radius_m, auto, icon) VALUES (%s, %s, %s, %s, FALSE, %s) RETURNING id",
                (keep["name"], lat, lon, radius_m, None),
            ).fetchone()
            other = new["id"]
        else:
            other = hit["id"]
        if not _merge_into(conn, keep_id, [other]):
            return False
        conn.commit()
    forget_places()
    return True


def add_place(name: str, lat: float, lon: float, radius_m: float, icon: str | None = None) -> None:
    """A new named place. One that is home (its icon, or its name's guess) has its charges
    priced again as home charges, in the background."""
    with db() as conn:
        new = conn.execute("INSERT INTO cc_places (name, latitude, longitude, radius_m, icon) VALUES (%s, %s, %s, %s, %s) RETURNING id", (name, lat, lon, radius_m, icon)).fetchone()
        row = {"id": new["id"], "name": name, "latitude": lat, "longitude": lon, "radius_m": radius_m, "icon": icon, "auto": False}
        if is_home_place(row):
            queue_reprice(conn, row)
        conn.commit()
    forget_places()


def delete_place(place_id: int) -> None:
    """Remove a place and every circle merged into it. Every charge inside them is priced
    again in the background (`every`): it lost this place's price or being home, and one
    where another place overlaps is that place's now, with that place's price."""
    with db() as conn:
        gone = place_row(conn, place_id)
        if gone is None:
            return
        circles = queue_with_parts(conn, gone, every=True)
        conn.execute("DELETE FROM cc_places WHERE id = %s OR merged_into = %s", (place_id, place_id))
        LOG.info("places: removed place %s and %d merged into it", place_id, len(circles) - 1)
        conn.commit()
    forget_places()


def pin_place(
    conn,
    lat: float,
    lon: float,
    name: str,
    radius_m: float = 120.0,
    icon: str | None = None,
    price: dict | None = None,
    address: tuple[bool, str | None] = (False, None),
    rename: bool = True,
) -> int:
    """Name the spot for good. A place already covering it is renamed and pinned, keeping its id
    and visits; otherwise a pinned place is added. Automatic naming never touches a pinned place.
    `rename=False` (the Add page) always adds one: adding Work while parked at Home must
    not rename Home. A place that becomes home or stops being has its charges queued.
    `icon` is whatever the caller already validated (`_valid_icon`); never validated here.
    `price`, when given (placeprice's shape), is stored with it; None leaves the price as it is.
    `address` is (whether to touch it, the owner's own address), as places.posted_address says."""
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("a name is required")
    rows = conn.execute("SELECT id, name, latitude, longitude, radius_m, icon, auto, merged_into FROM cc_places").fetchall()
    hit = place_for(lat, lon, resolve_merged([{"merged_into": None, **dict(r)} for r in rows])) if rename else None
    if hit:
        was = next(dict(r) for r in rows if r["id"] == hit["id"])
        # No icon chosen keeps the place's own; a chosen one, or "none", replaces it.
        if icon is None:
            conn.execute("UPDATE cc_places SET name = %s, auto = FALSE WHERE id = %s", (name, hit["id"]))
        else:
            conn.execute("UPDATE cc_places SET name = %s, auto = FALSE, icon = %s WHERE id = %s", (name, icon, hit["id"]))
        if price is not None:
            set_place_price(conn, hit["id"], price)
        if address[0]:
            conn.execute("UPDATE cc_places SET user_address = %s WHERE id = %s", (address[1], hit["id"]))
        now = {**was, "name": name, "auto": False, "icon": was.get("icon") if icon is None else icon}
        if is_home_place(was) != is_home_place(now):
            queue_with_parts(conn, now)
        conn.commit()
        return hit["id"]
    new = conn.execute(
        "INSERT INTO cc_places (name, latitude, longitude, radius_m, auto, icon) VALUES (%s, %s, %s, %s, FALSE, %s) RETURNING id",
        (name, lat, lon, radius_m, icon),
    ).fetchone()
    row = {"id": new["id"], "name": name, "latitude": lat, "longitude": lon, "radius_m": radius_m, "icon": icon, "auto": False}
    if is_home_place(row):
        queue_reprice(conn, row)
    if price is not None:
        set_place_price(conn, new["id"], price)
    if address[0]:
        conn.execute("UPDATE cc_places SET user_address = %s WHERE id = %s", (address[1], new["id"]))
    conn.commit()
    return new["id"]


def ribbon(vin: str, hours: int = 24, end: datetime | None = None) -> list[dict]:
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    with db() as conn:
        states = conn.execute("SELECT first_date, last_date, state::text AS s FROM states WHERE vin=%s AND last_date > %s ORDER BY first_date", (vin, start)).fetchall()
        charging = conn.execute("SELECT first_date, last_date, state::text AS s FROM charging_states WHERE vin=%s AND last_date > %s ORDER BY first_date", (vin, start)).fetchall()
    to_rows = lambda rows: [(r["first_date"], r["last_date"], r["s"]) for r in rows]
    return ribbon_segments(to_rows(states), to_rows(charging), start, end)


# A car left in accessory or ignition on the driveway is not a drive. The database
# plugin opens a trip the moment the state leaves park, so ninety minutes sitting in the
# car produced a ninety-minute, zero-mile "drive" in the list. The Day page already shows
# those minutes as idle, so they only need keeping out of the drive lists. An unfinished drive has no destination odometer yet and
# must not be caught by this.
def moved_sql() -> str:
    """Drives shorter than the Settings threshold are not drives: the shuffle in the driveway.
    The number is formatted, not bound, because the clause is glued onto queries that bind
    their own parameters; a float renders as a float and nothing else."""
    floor = f"{max(0.0, MIN_DRIVE_MI):.3f}"
    # An unfinished drive counts once the car's newest odometer has moved past the threshold:
    # ignition on in Park is idling, not a drive (the car's state says so on Now).
    # A car with no live odometer keeps its unfinished drive, as before.
    moving = f"COALESCE((SELECT p.odometer FROM cc_positions p WHERE p.vin = t.vin AND p.odometer IS NOT NULL ORDER BY p.at DESC LIMIT 1) - t.start_odometer >= {floor}, TRUE)"
    return HIDDEN_DRIVE + (f" AND (t.start_odometer IS NULL OR t.destination_odometer - t.start_odometer >= {floor} OR (t.destination_odometer IS NULL AND {moving}))")


def charged_sql() -> str:
    """A charge that added less than the Settings threshold is a plug-in blip, not a charge."""
    return (f" AND COALESCE(f.kwh, c.real_charged, 0) >= {max(0.0, MIN_CHARGE_KWH):.3f}" if MIN_CHARGE_KWH > 0 else "") + HIDDEN_CHARGE


# A drive or charge hidden by hand (the tow, the test at the dealer) leaves every list and
# total on the same clause the thresholds ride, and is found again on the Hidden list.
HIDDEN_DRIVE = " AND t.id NOT IN (SELECT ref_id FROM cc_hidden WHERE kind = 'drive')"
HIDDEN_CHARGE = " AND c.id NOT IN (SELECT ref_id FROM cc_hidden WHERE kind = 'charge')"


TRIP_SELECT = """
    SELECT t.id, t.start_date, t.destination_date, t.start_odometer, t.destination_odometer,
           t.start_position_latitude, t.start_position_longitude, t.destination_position_latitude, t.destination_position_longitude,
           sl.road AS start_road, sl.city AS start_city, dl.road AS dest_road, dl.city AS dest_city,
           f.level_start, f.level_end, NULLIF(f.temp_f, -999) AS temp_f, NULLIF(f.range_start, -999) AS range_start, NULLIF(f.range_end, -999) AS range_end,
           NULLIF(f.max_mph, -1) AS max_mph, t.vin, v.name AS car_name, g.tag, g.note, x.fsd_miles, w.code AS weather_code, w.wind_kmh AS weather_wind_kmh, (mt.ref_id IS NOT NULL) AS by_hand
    FROM trips t
    LEFT JOIN cc_manual mt ON mt.kind = 'drive' AND mt.ref_id = t.id
    LEFT JOIN vehicles v ON v.vin = t.vin
    LEFT JOIN cc_drive_tags g ON g.trip_id = t.id
    LEFT JOIN cc_trip_extras x ON x.vin = t.vin AND x.start_date = t.start_date
    LEFT JOIN cc_weather w ON w.vin = t.vin AND w.start_date = t.start_date
    LEFT JOIN locations sl ON sl.uid = t.start_location_uid
    LEFT JOIN locations dl ON dl.uid = t.destination_location_uid
    LEFT JOIN cc_trip_facts f ON f.trip_id = t.id
"""


def _window(col: str, start: datetime | None, end: datetime | None) -> tuple[str, list]:
    sql, args = "", []
    if start is not None:
        sql += f" AND {col} >= %s"
        args.append(start)
    if end is not None:
        sql += f" AND {col} < %s"
        args.append(end)
    return sql, args


def _finish_drive(r: dict, known: list[dict], cells: dict | None = None, energy: dict | None = None, by_year: dict | None = None) -> dict:
    cells = cells if cells is not None else geocells()
    energy = energy if energy is not None else vehicle_energy()
    by_year = by_year if by_year is not None else rated_mile_years()
    r["miles"] = (r["destination_odometer"] - r["start_odometer"]) if r["destination_odometer"] is not None and r["start_odometer"] is not None else None
    r["duration"] = (r["destination_date"] - r["start_date"]) if r["destination_date"] else None
    start_fallback = ", ".join(x for x in (r["start_road"], r["start_city"]) if x) or rough_label(r["start_position_latitude"], r["start_position_longitude"], cells)
    a, b = r["start_position_latitude"], r["start_position_longitude"]
    at = place_for(a, b, known)
    r["from"] = at["name"] if at else (start_fallback or "Unnamed spot")
    r["from_named"] = at is not None
    r["from_ref"] = placespage.ref(r["from"], a, b, at)  # the naming card opens on it
    if r["destination_date"] is None:
        r["to"] = "In progress"
        r["to_named"] = True
        r["to_ref"] = None
    else:
        a, b = r["destination_position_latitude"], r["destination_position_longitude"]
        dest_fallback = ", ".join(x for x in (r["dest_road"], r["dest_city"]) if x) or rough_label(a, b, cells)
        at = place_for(a, b, known)
        r["to"] = at["name"] if at else (dest_fallback or "Unnamed spot")
        r["to_named"] = at is not None
        r["to_ref"] = placespage.ref(r["to"], a, b, at)
    cap, k = energy.get(r.get("vin"), (None, None))
    k = rated_mile_kwh(by_year, r.get("vin"), r.get("start_date"), k, TZ)
    r["kwh_used"] = drive_kwh(r.get("level_start"), r.get("level_end"), cap, r.get("range_start"), r.get("range_end"), k)
    r["wh_per_mi"] = (r["kwh_used"] * 1000 / r["miles"]) if r["kwh_used"] and r.get("miles") and r["miles"] > 0.5 else None
    r["rated_used"] = (r["range_start"] - r["range_end"]) if r.get("range_start") is not None and r.get("range_end") is not None else None
    r["cost"], r["cost_estimate"] = drive_cost(r.get("kwh_used"), home_rates().get(r.get("vin")))
    r["weather"] = weather.describe(r.get("weather_code"), r.get("weather_wind_kmh"), WEATHER_METRIC)
    return r


def _vin_clause(vin: str | None) -> tuple[str, list]:
    """`vin` None or "all" means every car in the garage."""
    return ("", []) if vin in (None, GARAGE) else (" AND t.vin = %s", [vin])


def drives(vin: str | None, limit: int | None = 50, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    """Drives newest first, from stored facts: no per-row lookups. A window or a limit bounds the query, never both missing on a page.
    `vin` None or "all" threads every car together."""
    where, args = _window("t.start_date", start, end)
    vw, vargs = _vin_clause(vin)
    with db() as conn:
        rows = conn.execute(
            TRIP_SELECT + " WHERE TRUE" + vw + where + moved_sql() + " ORDER BY t.start_date DESC" + (" LIMIT %s" if limit else ""), [*vargs, *args, *([limit] if limit else [])]
        ).fetchall()
    known, cells, energy, by_year = places(), geocells(), vehicle_energy(), rated_mile_years()
    return [_finish_drive(r, known, cells, energy, by_year) for r in rows]


def drives_light(vin: str | None, since: datetime | None) -> list[dict]:
    """Drives without place labels, for pages that only add numbers up (efficiency bins, the odometer chart)."""
    where, args = _window("t.start_date", since, None)
    vw, vargs = _vin_clause(vin)
    energy, by_year = vehicle_energy(), rated_mile_years()
    with db() as conn:
        rows = conn.execute(TRIP_SELECT + " WHERE t.destination_date IS NOT NULL" + vw + where + moved_sql() + " ORDER BY t.start_date DESC", [*vargs, *args]).fetchall()
    for r in rows:
        r["miles"] = (r["destination_odometer"] - r["start_odometer"]) if r["destination_odometer"] is not None and r["start_odometer"] is not None else None
        r["duration"] = (r["destination_date"] - r["start_date"]) if r["destination_date"] else None
        cap, k = energy.get(r.get("vin"), (None, None))
        r["kwh_used"] = drive_kwh(r.get("level_start"), r.get("level_end"), cap, r.get("range_start"), r.get("range_end"), rated_mile_kwh(by_year, r.get("vin"), r.get("start_date"), k, TZ))
    return rows


def drive(vin: str | None, trip_id: int) -> dict | None:
    vw, vargs = _vin_clause(vin)
    with db() as conn:
        r = conn.execute(TRIP_SELECT + " WHERE t.id = %s" + vw, (trip_id, *vargs)).fetchone()
    return _finish_drive(r, places()) if r else None


def latest_dates(vin: str | None) -> dict:
    vw, vargs = _vin_clause(vin)
    with db() as conn:
        t = conn.execute("SELECT MAX(start_date) AS d FROM trips t WHERE TRUE" + vw, vargs).fetchone()
        c = conn.execute("SELECT MAX(session_start_date) AS d FROM charging_sessions t WHERE TRUE" + vw, vargs).fetchone()
    return {"drive": t["d"] if t else None, "charge": c["d"] if c else None}


CHARGE_SELECT = """
    SELECT c.id, c.session_start_date, c.session_end_date, c.plug_connected_date, c.plug_disconnected_date,
           c.start_level, c.end_level, c.charging_type::text AS charging_type, l.road, l.city,
           c.session_position_latitude, c.session_position_longitude,
           COALESCE(f.kwh, c.real_charged) AS kwh, COALESCE(f.cost, c.real_cost) AS cost, f.plan AS rate_plan, f.at_home, NULLIF(f.range_added, -999) AS range_added,
           f.off_peak_kwh, f.part_peak_kwh, f.peak_kwh, f.period_kwh, f.cost_source, f.region, f.rate_month, c.vin, v.name AS car_name, (m.ref_id IS NOT NULL) AS by_hand,
           x.status AS src_status, x.solar_kwh AS src_solar_kwh, x.battery_kwh AS src_battery_kwh, x.grid_kwh AS src_grid_kwh, x.battery_sun_frac AS src_sun_frac,
           x.grid_cost AS src_grid_cost, x.battery_cost AS src_battery_cost, x.approx AS src_approx
    FROM charging_sessions c
    LEFT JOIN cc_manual m ON m.kind = 'charge' AND m.ref_id = c.id
    LEFT JOIN vehicles v ON v.vin = c.vin
    LEFT JOIN locations l ON l.uid = c.location_uid
    LEFT JOIN cc_charge_facts f ON f.session_id = c.id
    LEFT JOIN cc_charge_sources x ON x.session_id = c.id
"""


def today_sql() -> str:
    """Drives and charges since local midnight, grouped by car, in one statement. The same
    thresholds and hidden rows as every list, so the Today line agrees with the lists."""
    return (
        "SELECT v.vin, COALESCE(d.drives, 0) AS drives, COALESCE(d.miles, 0) AS miles, COALESCE(ch.kwh, 0) AS kwh FROM vehicles v "
        "LEFT JOIN (SELECT t.vin, COUNT(t.destination_odometer) AS drives, SUM(GREATEST(t.destination_odometer - t.start_odometer, 0)) AS miles FROM trips t "
        "WHERE t.start_date >= %(since)s" + moved_sql() + " GROUP BY t.vin) d ON d.vin = v.vin "
        "LEFT JOIN (SELECT c.vin, SUM(COALESCE(f.kwh, c.real_charged, 0)) AS kwh FROM charging_sessions c LEFT JOIN cc_charge_facts f ON f.session_id = c.id "
        "WHERE c.session_start_date >= %(since)s" + charged_sql() + " GROUP BY c.vin) ch ON ch.vin = v.vin"
    )


def today_by_car() -> dict[str, dict]:
    def load():
        since = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        with db() as conn:
            rows = conn.execute(today_sql(), {"since": since}).fetchall()
        return {r["vin"]: {"drives": int(r["drives"]), "miles": float(r["miles"]), "kwh": float(r["kwh"])} for r in rows}

    return cached("today", 60, load)


def _finish_charge(r: dict, known: list[dict]) -> dict:
    fallback = ", ".join(x for x in (r["road"], r["city"]) if x) or rough_label(r["session_position_latitude"], r["session_position_longitude"], geocells())
    a, b = r["session_position_latitude"], r["session_position_longitude"]
    at = place_for(a, b, known)
    r["where"] = at["name"] if at else (fallback or "Unnamed spot")
    r["where_ref"] = placespage.ref(r["where"], a, b, at)
    r["gained"] = (r["end_level"] - r["start_level"]) if r["end_level"] is not None and r["start_level"] is not None else None
    r["duration"] = (r["session_end_date"] - r["session_start_date"]) if r["session_end_date"] and r["session_start_date"] else None
    r["cost_estimate"] = r.get("cost_source") == "average" and r.get("cost") is not None
    r["mix"] = energypage.mix_bar(r) if r.get("at_home") else None  # stored beside the facts: nothing worked out here
    return r


def charges(vin: str | None, limit: int | None = 50, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    """Charges newest first, energy and cost from stored facts (a session still running has none yet). `vin` None or "all" is every car."""
    where, args = _window("c.session_start_date", start, end)
    vw, vargs = _vin_clause(vin)
    with db() as conn:
        rows = conn.execute(
            CHARGE_SELECT + " WHERE TRUE" + vw.replace("t.vin", "c.vin") + where + charged_sql() + " ORDER BY c.session_start_date DESC NULLS LAST" + (" LIMIT %s" if limit else ""),
            [*vargs, *args, *([limit] if limit else [])],
        ).fetchall()
    known = places()
    return [_finish_charge(r, known) for r in rows]


def charge(vin: str | None, session_id: int) -> dict | None:
    vw, vargs = _vin_clause(vin)
    with db() as conn:
        r = conn.execute(CHARGE_SELECT + " WHERE c.id = %s" + vw.replace("t.vin", "c.vin"), (session_id, *vargs)).fetchone()
    return _finish_charge(r, places()) if r else None


def software_events(vin: str | None, start: datetime, end: datetime) -> list[dict]:
    """Version changes in the window: a row that repeats the car's previous version is not one."""
    vw, vargs = _vin_clause(vin)
    with db() as conn:
        return conn.execute(
            "SELECT t.vin, t.version, t.seen_at, v.name AS car_name FROM"
            " (SELECT t.vin, t.version, t.seen_at, LAG(t.version) OVER (PARTITION BY t.vin ORDER BY t.seen_at, t.id) AS prev FROM cc_software t WHERE TRUE" + vw + ") t"
            " LEFT JOIN vehicles v ON v.vin = t.vin"
            " WHERE t.prev IS DISTINCT FROM t.version AND t.seen_at >= %s AND t.seen_at < %s ORDER BY t.seen_at DESC",
            [*vargs, start, end],
        ).fetchall()


def is_home(lat, lon, known=None) -> bool:
    """Whether (lat, lon) is at a place named Home (places.is_home). `known` is the places
    to look in, read once by a caller that asks many times."""
    return is_home_place(place_for(lat, lon, places() if known is None else known))


def fill_missing_facts() -> None:
    """Facts for every finished drive and charge that has none. Runs in the recorder's thread; cheap when nothing is missing."""
    fast = settingsform.fast_rates(current_settings())
    known = places()  # once a pass, not once a charge
    home = lambda lat, lon: is_home(lat, lon, known)  # noqa: E731
    place_of = lambda lat, lon: place_for(lat, lon, known)  # noqa: E731
    with db() as conn:
        # Places whose price changed: their charges' prices are cleared here, and the
        # passes below price them again a batch at a time.
        drain_reprices(conn, known)
        for v in conn.execute("SELECT vin FROM vehicles").fetchall():
            fill_facts(conn, v["vin"], TZ, home, averages=averages_mod.active(), place_of=place_of, **fast)
    try:
        cached("energy_sources", 60, fill_energy_sources)  # at most once a minute, whatever the number of cars or snapshots
    except Exception as err:  # noqa: BLE001 - the facts above stand without it
        LOG.warning("energy sources: %s", err)


def fill_energy_sources() -> int:
    """The split of each finished home charge between sun, Powerwall and grid, for those that
    have none yet. Nothing at all when no house readings were ever kept."""
    done = 0
    with db() as conn:
        if not conn.execute("SELECT 1 FROM cc_energy LIMIT 1").fetchone():
            return 0
        energy.drop_orphan_sources(conn)
        for v in conn.execute("SELECT vin FROM vehicles").fetchall():
            done += energy.fill_charge_sources(conn, v["vin"], TZ)
        conn.commit()
    return done


def state_rows(vin: str, table: str, column: str, since: datetime, until: datetime | None = None) -> list[tuple[datetime, datetime, str]]:
    with db() as conn:
        rows = conn.execute(
            f"SELECT first_date, last_date, {column}::text AS s FROM {table} WHERE vin=%s AND last_date > %s AND first_date < %s ORDER BY first_date", (vin, since, until or datetime.now(timezone.utc))
        ).fetchall()
    return [(r["first_date"], r["last_date"], r["s"]) for r in rows]


def drive_series(vin: str, table: str, column: str, days: int) -> list[tuple[datetime, float]]:
    """Time series from one of the per-drive history tables (drive_levels, drive_ranges, ...)."""
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT l.first_date, l.{column} AS v FROM {table} l JOIN drives d ON d.id = l.drive_id
            WHERE d.vin = %s AND l.first_date > now() - (%s || ' days')::interval AND l.{column} IS NOT NULL
            ORDER BY l.first_date""",
            (vin, str(days)),
        ).fetchall()
    return [(r["first_date"], r["v"]) for r in rows]


def vehicle_series(vin: str, table: str, column: str, days: int) -> list[tuple[datetime, float]]:
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT first_date, {column} AS v FROM {table}
            WHERE vin = %s AND first_date > now() - (%s || ' days')::interval AND {column} IS NOT NULL ORDER BY first_date""",
            (vin, str(days)),
        ).fetchall()
    return [(r["first_date"], r["v"]) for r in rows]


def battery_series(vin: str, days: int = 7) -> list[tuple[datetime, float]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT l.first_date, l.level FROM drive_levels l JOIN drives d ON d.id = l.drive_id
            WHERE d.vin = %s AND l.first_date > now() - (%s || ' days')::interval AND l.level IS NOT NULL
            ORDER BY l.first_date""",
            (vin, str(days)),
        ).fetchall()
    return [(r["first_date"], r["level"]) for r in rows]


def sparkline(series: list[tuple[datetime, float]], width: int = 600, height: int = 80) -> str:
    """SVG polyline points for a level-over-time series, or '' when there is nothing to draw."""
    if len(series) < 2:
        return ""
    t0, t1 = series[0][0], series[-1][0]
    span = (t1 - t0).total_seconds() or 1
    pts = []
    for t, level in series:
        x = (t - t0).total_seconds() / span * width
        y = height - (level / 100) * height
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


# ---- routes ----------------------------------------------------------------


def car_levels_sql() -> str:
    """Each car's newest battery reading, for the sidebar: one indexed lookup per car
    (drive_levels is keyed on drive and date), a few milliseconds for the garage."""
    return "SELECT d.vin, (SELECT l.level FROM drive_levels l WHERE l.drive_id = d.id ORDER BY l.first_date DESC LIMIT 1) AS level FROM drives d"


def car_levels() -> dict[str, float]:
    def load():
        with db() as conn:
            rows = conn.execute(car_levels_sql()).fetchall()
        return {r["vin"]: float(r["level"]) for r in rows if r["level"] is not None}

    return cached("car_levels", 60, load)


def here_of(path: str, root: str, query: str) -> str:
    """The page's own path and query without the mount prefix: what the car switcher rewrites."""
    rel = path[len(root) :] if root and path.startswith(root) else path
    return (rel or "/") + (f"?{query}" if query else "")


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    # root_path is "/cc" when reached through the tunnel mount, "" on the LAN port.
    vehicles = all_vehicles()
    vin = ctx["car"]["vin"] if "car" in ctx else None
    q = f"?vin={vin}" if vin and len(vehicles) > 1 else ""
    colors = car_colors(vehicles)
    idents = car_identities(vehicles)
    return templates.TemplateResponse(
        request,
        name,
        {
            "root": request.scope.get("root_path", ""),
            "theme": THEME,
            "week_start": WEEK_START,
            "alert_kinds": notify_mod.kinds_for(i["make"] for i in idents.values()),
            "guard": {v: identity.guard_name(i["make"]) for v, i in idents.items()},
            "guard_all": identity.guard_label(i["make"] for i in idents.values()),
            "now": datetime.now(TZ),
            "vehicles": vehicles,
            "q": q,
            "levels": car_levels(),
            "here": here_of(request.url.path, request.scope.get("root_path", ""), request.url.query),
            "colors": colors,
            "idents": idents,
            "names": {v["vin"]: v["name"] for v in vehicles},
            "css_v": CSS_VERSION,
            "js_v": JS_VERSION,
            "csrf": csrf_for(request),
            "auth_on": AUTH["enabled"],
            "connector": connector_down(),
            "findings": health_findings(ctx["car"]) if "car" in ctx else [],
            "cur": current_settings().get("currency", "$"),
            "settings": current_settings(),
            **ctx,
        },
    )


# Browsers heuristically cache a stylesheet with no Cache-Control for days; a
# content hash in the URL makes every deploy's CSS take effect on the next load.
CSS_VERSION = hashlib.md5((HERE / "static" / "style.css").read_bytes()).hexdigest()[:8]
templates.env.globals["version"] = version.VERSION
JS_VERSION = hashlib.md5(
    (HERE / "static" / "prefetch.js").read_bytes() + (HERE / "static" / "shell.js").read_bytes() + (HERE / "static" / "chart.js").read_bytes() + (HERE / "static" / "maps.js").read_bytes()
).hexdigest()[:8]


def current_settings() -> dict:
    def load():
        with db() as conn:
            return settings_mod.load(conn)

    return cached("settings", 300, load)


def use_timezone(name: str | None) -> None:
    """Every date on the site in this zone, from now on.

    TZ is a module global that every route reads at call time, so rebinding it is most
    of the job. Two things captured it at import and have to be told: the template
    filters close over their zone, so they are registered again, and the TeslaFi poller
    (like the other pollers) holds its own. Nothing else keeps a copy — checked before this was written, and a
    test now checks that stays true."""
    global TZ  # noqa: PLW0603 - the one place the zone is set
    TZ = ZoneInfo(name) if settings_mod.valid_zone(name) else ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
    register_filters(templates.env, TZ, CLOCK, DATE_ORDER, place_icons=lambda: place_icon_map())
    teslafi.tz = TZ
    teslamate.tz = TZ
    tesla_stream_worker.tz = TZ


WEATHER_ON = False
WEATHER_METRIC = False
THEME = "system"
MIN_DRIVE_MI = 0.1
MIN_CHARGE_KWH = 0.0
WEEK_START = 0  # Monday
CLOCK = "12"
DATE_ORDER = "mdy"
SUMMARY_AT = dtime(7, 0)


def apply_settings(st: dict) -> None:
    global WEATHER_ON, WEATHER_METRIC, THEME, MIN_DRIVE_MI, MIN_CHARGE_KWH, WEEK_START, CLOCK, DATE_ORDER, SUMMARY_AT
    cost.use_plan(st.get("rate_plan"))
    notify_mod.TIRE_LOW, notify_mod.TIRE_HIGH = st.get("tire_low_psi"), st.get("tire_high_psi")
    use_units(st.get("units") == "metric")
    use_timezone(st.get("timezone"))
    WEATHER_ON = st.get("weather") is True
    WEATHER_METRIC = st.get("units") == "metric"
    THEME = st.get("theme") if st.get("theme") in ("light", "dark") else "system"
    MIN_DRIVE_MI = float(st.get("min_drive_mi") or 0.1)
    MIN_CHARGE_KWH = float(st.get("min_charge_kwh") or 0.0)
    WEEK_START = 6 if st.get("week_start") == "sunday" else 0
    CLOCK = "24" if str(st.get("clock")) == "24" else "12"
    DATE_ORDER = "dmy" if st.get("date_order") == "dmy" else "mdy"
    SUMMARY_AT = parse_hhmm(st.get("summary_at")) or dtime(7, 0)
    recorder.muted = set()
    recorder.routes = notify_mod.routes_from(st)
    register_filters(templates.env, TZ, CLOCK, DATE_ORDER, place_icons=lambda: place_icon_map())
    # a global, not a page() context value, so every render that draws a map reads it,
    # including one that never passes settings; reset here on startup and every save
    templates.env.globals["public_tiles"] = bool(st.get("public_tiles"))


def car_colors(vehicles: list[dict]) -> dict[str, int]:
    """Each car's chart colour (identity.PALETTE index): its own choice, else the free one
    nearest its paint (set here or reported by the car), never too close to another car's.
    Every page and map takes car colours from here."""
    rows, _ = identity_rows()
    paints = {vin: (i["paint"] or {}).get("hex") for vin, i in car_identities(vehicles).items()}
    return identity.chart_colors([v["vin"] for v in vehicles], {vin: r.get("chart") for vin, r in rows.items()}, paints)


def identity_rows() -> tuple[dict[str, dict], dict[str, tuple]]:
    """cc_car_identity by car, and the make and model each car's source reported; kept five
    minutes, dropped on a save. A database that cannot answer leaves the cars without their
    look rather than the page without its cars."""

    def load():
        with db() as conn:
            rows = identity.load(conn)
            made = {r["vin"]: (r["manufacturer"], r["model"]) for r in conn.execute("SELECT vin, manufacturer, model FROM vehicles").fetchall() or []}
        return rows, made

    try:
        return cached("identity", 300, load)
    except psycopg.Error as err:
        LOG.warning("car identity: %s", err)
        return {}, {}


def car_identities(vehicles: list[dict]) -> dict[str, dict]:
    """vin -> identity.describe: the year, make, model, trim and paint each page shows. The
    connector's make and model win, from the tree all_vehicles just fetched (no new call)."""
    rows, made = identity_rows()
    tree = (_cache.get("garage_tree") or (None, {}))[1] or {}
    out = {}
    for v in vehicles:
        live = tree.get(v["vin"]) or {}
        make, model = made.get(v["vin"]) or (None, None)
        out[v["vin"]] = identity.describe(v["vin"], leaf(live, "manufacturer") or make, leaf(live, "model") or model, rows.get(v["vin"]))
    return out


# ---- sign-in ---------------------------------------------------------------
# One household, one password. The state lives here rather than in a request-scoped
# dependency because the middleware runs before every route and must not open the
# database on each hit; it is filled at startup and again when the password changes.
FAILURES = auth.Failures()  # failed sign-ins, per client and in total; see auth.Failures
# A token in a URL can leak (logs, history), so it is read from a form field or the
# X-CSRF-Token header; the query string only for a photo upload, whose body is larger than
# FORM_LIMIT and so is never read here to find one.
QUERY_TOKEN_PATHS = re.compile(r"/service/\d+/files")
FORM_LIMIT = 1 << 20  # a form larger than this is not one of ours; it goes to the route unread and is refused for want of a token
AUTH = {"enabled": os.environ.get("CC_AUTH", "on").lower() not in ("off", "0", "false", "no"), "secret": "", "password": None, "admin_anywhere": False, "first_start": None}
# Sign-in through Cloudflare Access: the team and audience its JWTs must carry. Filled by load_auth from the
# environment or the database; empty means not in use.
CF: dict = {"team": None, "aud": None, "env_team": False, "env_aud": False}
PROXIES = proxyauth.parse_proxies(os.environ.get("CC_ACCESS_PROXY"))  # who may present the header; empty: any private, loopback or tailnet peer
EMAILS = proxyauth.parse_emails(os.environ.get("CC_ACCESS_EMAILS"))  # who counts as signed in; empty: Cloudflare's policy decides


def jwks_load(team: str):
    """The team's keys as last saved, so a restart or an outage does not wait on Cloudflare."""
    with db() as conn:
        text = auth.store_get(conn, f"cf_jwks:{team}")
    saved = json.loads(text) if text else None
    return (saved["at"], saved["doc"]) if saved else None


def jwks_save(team: str, at: float, doc: dict) -> None:
    with db() as conn:
        auth.store_set(conn, f"cf_jwks:{team}", json.dumps({"at": at, "doc": doc}))


PROXY = proxyauth.Verifier(load=jwks_load, save=jwks_save)


def cf_on() -> bool:
    """Cloudflare Access is a way in: sign-in is on and both the team and the application are pinned."""
    return bool(AUTH["enabled"] and CF["team"] and CF["aud"])


# Seals the sources kept in the app; the key lives in stack/.env, never in the database.
VAULT = sources.Vault(os.environ.get("CC_SECRET_KEY"))
# The optional local maps add-on. Unset MAPS_URL and every call answers None at once;
# only Settings > Maps and the user-initiated picker ever call it.
MAPS = MapsClient(os.environ.get("MAPS_URL"))


def load_auth() -> None:
    """Signing key and password hash: the environment wins, the database remembers."""
    with db() as conn:
        # Is this a fresh install? Judged before this start writes anything: no row in cc_auth (no key, password, switch or
        # token setting ever made) and no car. A key pinned in .env leaves no row, so the key alone cannot say.
        auth.store_get(conn, "first_start")  # makes the table
        history = conn.execute("SELECT 1 FROM cc_auth LIMIT 1").fetchone() is not None
        if not history and (conn.execute("SELECT to_regclass('vehicles') IS NOT NULL AS t").fetchone() or {}).get("t"):
            history = conn.execute("SELECT 1 FROM vehicles LIMIT 1").fetchone() is not None
        secret = os.environ.get("CC_SECRET") or auth.store_get(conn, "secret")
        if not secret:
            secret = auth.new_secret()  # generated once and kept, so a restart signs nobody out
            auth.store_set(conn, "secret", secret)
        env_password = os.environ.get("CC_PASSWORD")
        password = auth.hash_password(env_password) if env_password else auth.store_get(conn, "password")
        anywhere = auth.store_get(conn, ADMIN_ANYWHERE_KEY) == "1"
        switch = auth.store_get(conn, SIGNIN_KEY)
        # When this site first started with no password: the no-code window for the home network opens then, and a
        # restart does not reopen it.
        try:
            first_start = float(auth.store_get(conn, "first_start") or "")
        except ValueError:
            first_start = None
        if first_start is None and not password:
            # Only the start of an install with no history opens the window. A site that has run for a while gets 0
            # on record, so its window is over.
            first_start = 0.0 if history else time.time()
            auth.store_set(conn, "first_start", str(first_start))
        env_team, env_aud = os.environ.get("CF_ACCESS_TEAM"), os.environ.get("CF_ACCESS_AUD")
        team = proxyauth.normalize_team(env_team) if env_team else proxyauth.normalize_team(auth.store_get(conn, "cf_team"))
        aud = (env_aud or auth.store_get(conn, "cf_aud") or "").strip() or None
    # Turned on or off in Settings, that outranks CC_AUTH, so it survives a restart. A scripted install that
    # names its Cloudflare Access team and application means sign-in is on.
    enabled = AUTH["enabled"] if switch is None else switch == "1"
    if team and env_team and env_aud:
        enabled = True
    CF.update(team=team, aud=aud if team else None, env_team=bool(team and env_team), env_aud=bool(team and env_aud))
    PROXY.clear()
    # One assignment, so no request ever sees sign-in on without the password that goes with it.
    AUTH.update(enabled=enabled, secret=secret, password=password, admin_anywhere=anywhere, first_start=first_start)
    reload_tokens()
    admins = sum(1 for t in TOKENS.values() if t["kind"] == "admin")
    LOG.info(
        "sign-in %s%s, password %s, %d API token(s), %d of them admin%s",
        "on" if enabled else "off",
        f", Cloudflare Access {team}" if cf_on() else "",
        "set" if password else "not set yet",
        len(TOKENS),
        admins,
        ", allowed from anywhere" if anywhere and admins else "",
    )
    if enabled and not password and not cf_on():
        # Until a password is chosen, /setup answers to the home network; anyone else needs this code.
        LOG.warning("no password yet: open /setup from the home network to choose one; from anywhere else it asks for the setup code %s", auth.setup_code(secret))
    elif not enabled:
        LOG.info("sign-in is off: turning it on from any other machine asks for the setup code %s", auth.setup_code(secret))


SIGNIN_KEY = "enabled"  # in cc_auth: sign-in on or off as chosen in Settings; absent means CC_AUTH decides
ADMIN_ANYWHERE_KEY = "admin_anywhere"  # in cc_auth beside the password, not in the settings a page renders from
TOKENS: dict[str, dict] = {}  # token hash -> {"label", "vin", "kind", "last4"}; the middleware reads this, never the table

TOKENS_LOCK = threading.Lock()


def reload_tokens() -> None:
    # The read and the swap under one lock: a reload that read before a mint must not apply after the
    # reload that read once it was made, or it would take the new token out again.
    with TOKENS_LOCK, db() as conn:
        rows = conn.execute("SELECT hash, label, vin, kind, last4, share_place FROM cc_api_tokens").fetchall()
        fresh = {
            r["hash"]: {"label": r["label"], "vin": r["vin"] if r["kind"] != "admin" else None, "kind": auth.kind_of(r["kind"]), "last4": r["last4"], "share_place": bool(r.get("share_place"))}
            for r in rows
        }
        # In place and never emptied: a request in flight between a clear and an update would be told its valid token is not one.
        TOKENS.update(fresh)
        for gone in TOKENS.keys() - fresh.keys():
            TOKENS.pop(gone, None)


def session_of(request: Request) -> str | None:
    return request.cookies.get(auth.SESSION_COOKIE)


def csrf_subject(cookie: str | None, cf: dict | None) -> str | None:
    """What a form's token is bound to: the session cookie, else the Cloudflare Access identity, so a
    token loaded by one person is no good for another's browser."""
    who = proxyauth.subject(cf) if cf else None
    return cookie or (f"cf:{who}" if who else None)


def csrf_for(request: Request) -> str:
    return auth.csrf_token(AUTH["secret"], csrf_subject(session_of(request), getattr(request.state, "cf", None)))


def set_session(response: Response, request: Request, cookie: str | None) -> None:
    """One cookie at path /, so the LAN port and the /cc mount share a sign-in."""
    if cookie is None:
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
    else:
        response.set_cookie(auth.SESSION_COOKIE, cookie, max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")


def safe_next(target: str) -> str:
    """Where to go after signing in, but only if it is a path on this site.

    A leading "/" is not enough. Browsers normalise a backslash to a slash inside a URL,
    so "/\\evil.example" in a Location header navigates to http://evil.example — which
    turns the sign-in page into a way to hand someone a link that logs them in and then
    drops them somewhere else. Control characters can split the header outright."""
    if not target.startswith("/") or target.startswith("//") or "\\" in target or any(c < " " or c == "\x7f" for c in target):
        return "/"
    parsed = urlparse(target)
    return target if not parsed.scheme and not parsed.netloc else "/"


def to_site(request: Request, target: str, status_code: int = 303) -> RedirectResponse:
    """A redirect that stays on this site: the mount it came in on (one of three, checked by
    name) and a path safe_next accepts, or the start page. For every target built from what a
    request carried: a form's `back`, a vin, a message."""
    root = request.scope.get("root_path", "")
    if root not in ("/cc", "/carcommand"):
        root = ""
    return RedirectResponse(root + safe_next(target), status_code=status_code)


SLOW_MS = 1000.0


def slow_line(method: str, path: str, ms: float) -> str | None:
    """The log line for a request that took longer than SLOW_MS, or None. The live stream and
    static files are skipped: one is long-lived on purpose, the other isn't ours to speed up."""
    if ms < SLOW_MS or path.startswith(("/stream", "/static/")):
        return None
    return f"slow request: {method} {path} took {ms:.0f} ms"


@site.middleware("http")
async def timing(request: Request, call_next):
    t0 = time.monotonic()
    response = await call_next(request)
    line = slow_line(request.method, request.url.path, (time.monotonic() - t0) * 1000)
    if line:
        LOG.warning(line)
    return response


def site_path(request: Request) -> str:
    """The path without the mount prefix. Starlette stopped stripping the prefix from
    scope["path"] in 0.33; it keeps the whole path and names the prefix root_path."""
    root, raw = request.scope.get("root_path", ""), request.scope.get("path", "/")
    path = raw[len(root) :] if root and raw.startswith(root) else raw
    return path or "/"


@site.middleware("http")
async def guard(request: Request, call_next):
    # The rule is written in site-relative paths; the prefix goes back on every redirect.
    root, path = request.scope.get("root_path", ""), site_path(request)
    if path == connect.TOKEN_PATH and request.method == "POST":
        # Where an app trades its one-time code for a token: it has neither a session nor a
        # token yet, and the code with its verifier is the credential. This path and no other.
        return await call_next(request)
    submitted = None
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        submitted = request.headers.get("x-csrf-token")
        if submitted is None and QUERY_TOKEN_PATHS.fullmatch(path):
            # The one form whose body may be too large to read for its token (see QUERY_TOKEN_PATHS).
            submitted = request.query_params.get(auth.CSRF_FIELD)
        form_post = request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/form-data"))
        if submitted is None and form_post and int(request.headers.get("content-length") or 0) <= FORM_LIMIT:
            # body() first, and only then form(): Starlette replays a body that was read whole,
            # but a stream that was merely parsed reaches the route empty and every field is missing.
            await request.body()
            submitted = (await request.form()).get(auth.CSRF_FIELD)
    cookie = session_of(request)
    cf = None
    header = request.headers.get(proxyauth.HEADER) if cf_on() else None
    if header and proxyauth.peer_trusted(request.scope.get(TruePeer.KEY), PROXIES):
        # Remembered verdicts are a dict lookup; a new token is checked off the event loop, where fetching the keys may wait.
        verdict = PROXY.cached(header, CF["team"], CF["aud"]) or await asyncio.to_thread(PROXY.check, header, CF["team"], CF["aud"])
        claims, reason = verdict
        if claims is not None and proxyauth.allowed_identity(claims, EMAILS):
            cf = claims
        else:
            # A header from the proxy that does not hold up is a refusal, never a way on to the password or a token.
            cf_rejected(request, reason or "identity")
            if path.startswith("/api/"):
                return JSONResponse({"error": "Cloudflare Access did not accept this request"}, status_code=401)
            return refusal(request, 401, "Cloudflare Access did not accept this request.", "Sign in again through the Cloudflare address.")
    request.state.cf = cf
    if cf_on() and not AUTH["password"] and cf is None and path in ("/login", "/setup"):
        return cf_only_page(request)  # no way in but Cloudflare Access: these pages would be a way round it
    # An API token stands in for the cookie on /api/ paths and nowhere else, so a token
    # in a script cannot open a page. Scoped tokens answer for their car only.
    token = None
    if path.startswith("/api/"):
        raw = auth.bearer_of(request.headers.get("authorization"))
        token = TOKENS.get(auth.token_hash(raw)) if raw else None
        if path.startswith(auth.ADMIN_PREFIX):
            return await admin_call(request, call_next, token, path)
        if raw and token is None and cf is None and not (AUTH["enabled"] and auth.valid_session(AUTH["secret"], cookie)):
            # A token that was revoked, or never was one: say so, so the app reconnects, rather than sending it to a login page.
            return JSONResponse({"error": "this token is not valid; reconnect"}, status_code=401)
        if token is not None and token.get("kind") == "status" and path not in auth.STATUS_PATHS:
            return JSONResponse({"error": "a status token opens only /api/now and /api/stream"}, status_code=403)
        if token is not None and not auth.token_allows(token["vin"], request.query_params.get("vin")):
            return JSONResponse({"error": "this token is for one car; ask for it with ?vin="}, status_code=403)
    verdict = auth.gate(
        path,
        request.method,
        enabled=AUTH["enabled"],
        password_set=bool(AUTH["password"]) or cf_on(),
        signed_in=auth.valid_session(AUTH["secret"], cookie) or token is not None or cf is not None,
        csrf_valid=auth.csrf_ok(AUTH["secret"], csrf_subject(cookie, cf), submitted),
    )
    if verdict == "login" and cf_on() and not AUTH["password"]:
        return cf_only_page(request)
    if verdict == "pass":
        if token is not None:
            request.state.token = token
            # Remembered here and written when the token list is next read: the
            # middleware runs on every request and does not open the database.
            token["used"] = datetime.now(timezone.utc)
        return await call_next(request)
    if verdict == "setup":
        return RedirectResponse(f"{root}/setup", status_code=303)
    if verdict == "csrf":
        return refusal(request, 400, "That form was stale.", "Reload the page you came from and try again.")
    if request.method in ("GET", "HEAD"):
        nxt = path + (f"?{request.url.query}" if request.url.query else "")
        return to_site(request, f"/login?next={quote(nxt, safe='')}")
    return refusal(request, 401, "Sign in to do that.", "This install asks for a password before anything changes.")


# On every answer. The pages carry inline scripts and styles, so those stay allowed; what the
# policy holds is where anything else may come from: this site, the two CDNs the map scripts
# are pinned on (with integrity hashes), and, with public map backgrounds on in Settings ›
# Maps, OpenFreeMap's styles and tiles and OpenStreetMap's raster tiles. No page may be framed.
CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net",
        "img-src 'self' data: blob: https://unpkg.com https://tile.openstreetmap.org https://tiles.openfreemap.org",
        "connect-src 'self' https://tiles.openfreemap.org",
        "font-src 'self' data:",
        "worker-src 'self' blob:",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    )
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


@site.middleware("http")
async def security_headers(request: Request, call_next):
    """Outermost of the site's middleware (added last), so the guard's own redirects and
    refusals carry the headers too. A route that sets one of its own keeps it."""
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


async def admin_call(request: Request, call_next, token: dict | None, path: str):
    """/api/admin/: a bearer admin token opens it and nothing else does, not even a signed-in
    session. That is also why the CSRF check is not asked here: no cookie reaches these
    routes, and a browser cannot attach a bearer header to a cross-site request. Each call a
    known token makes is written to cc_api_log, refused or not."""
    if token is None:
        return JSONResponse({"error": "an admin token is needed, as Authorization: Bearer …"}, status_code=401)
    # The socket's own address, saved by TruePeer before the proxy headers replaced it;
    # request.client is whatever the leftmost X-Forwarded-For said, which anyone can write.
    peer = request.scope.get(TruePeer.KEY)
    if token.get("kind") != "admin":
        refused = "this token only reads; driving setup needs an admin token"
    else:
        refused = auth.admin_refusal(request.headers, peer, AUTH["admin_anywhere"], (TESLA_NET,))
    status = 403
    try:
        if refused:
            return JSONResponse({"error": refused}, status_code=403)
        request.state.token = token
        token["used"] = datetime.now(timezone.utc)
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception:
        status = 500
        raise
    finally:
        write, held = REFUSALS.admit(f"{token.get('label')}…{token.get('last4')}", bool(refused))
        if write:
            await asyncio.to_thread(audit, token, request.method, path, status, auth.client_ip(request.headers, request.client.host if request.client else None), held)


API_LOG_PRUNED = {"at": 0.0}
REFUSALS = auth.RefusalCap()  # ten refused calls a token a minute in the log; the rest counted
TESLA_NET = auth.network_of(os.environ.get("CC_TESLA_SUBNET"))


def audit(token: dict, method: str, path: str, status: int, ip: str, held: int = 0) -> None:
    """One admin call into cc_api_log (after a row for the refused calls held back before
    it, if any), and once a day the log trimmed to its 90 days. A log that cannot be written
    is said in the site's log; it never turns the call into an error."""
    try:
        with db() as conn:
            if held:
                auth.log_call(conn, token, "*", f"{held} more refused call(s) in the minute before, not listed", 403, ip)
            auth.log_call(conn, token, method, path, status, ip)
            conn.commit()
            if time.time() - API_LOG_PRUNED["at"] > 86400:
                API_LOG_PRUNED["at"] = time.time()
                auth.prune_log(conn)
    except Exception as err:  # noqa: BLE001 - the call itself has already been answered
        LOG.warning("api log: %s", err)


def refusal(request: Request, status: int, headline: str, detail: str, action: dict | None = None) -> HTMLResponse:
    """The middleware cannot raise: by the time call_next has started a response, the app's
    exception handlers can no longer take over. It answers with a page of its own instead,
    and that page is the doorway one, because there may be no database to build a header from."""
    return templates.TemplateResponse(
        request,
        "refused.html",
        {"root": request.scope.get("root_path", ""), "css_v": CSS_VERSION, "js_v": JS_VERSION, "status": status, "headline": headline, "detail": detail, "action": action, "csrf": csrf_for(request)},
        status_code=status,
    )


_CF_REJECTED: dict[tuple, float] = {}


def cf_rejected(request: Request, reason: str) -> None:
    """Say a header was refused, once a minute per sender and reason, so a flood cannot become a flood of log lines."""
    peer, now = request.scope.get(TruePeer.KEY), time.monotonic()
    if now - _CF_REJECTED.get((peer, reason), -60.0) >= 60.0:
        if len(_CF_REJECTED) > 256:
            _CF_REJECTED.clear()
        _CF_REJECTED[(peer, reason)] = now
        LOG.warning("Cloudflare Access: refused %s from %s (%s)", proxyauth.HEADER, peer, reason)


def cf_only_page(request: Request) -> HTMLResponse:
    """Cloudflare Access is the only way in and this request did not come through it."""
    # No link and no host from the request or the settings: nothing here for a stranger to turn into a phishing page.
    return refusal(request, 401, "Sign in through Cloudflare Access.", "Sign in through your Cloudflare Access address.")


def auth_page(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    """The sign-in pages render before there is a garage to talk about, so they skip page()."""
    ctx = {"root": request.scope.get("root_path", ""), "now": datetime.now(TZ), "css_v": CSS_VERSION, "js_v": JS_VERSION, "csrf": csrf_for(request), **ctx}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def setup_needs_code(request: Request) -> bool:
    """Whether the first password needs the setup code from this request; see auth.setup_keyless. Judged by the
    socket's own address (TruePeer) and the headers a proxy adds, never by what a client says about itself."""
    return not auth.setup_keyless(request.scope.get(TruePeer.KEY), request.headers, first_start=AUTH["first_start"])


def setup_flags(request: Request) -> dict:
    """What the setup page shows: whether it needs the code, and whether only the first hour has run out."""
    why = auth.setup_keyless_why(request.scope.get(TruePeer.KEY), request.headers, first_start=AUTH["first_start"])
    return {"keyless": why == "yes", "window_passed": why == "expired"}


@site.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    if AUTH["password"] or not AUTH["enabled"]:
        return RedirectResponse(f"{request.scope.get('root_path', '')}/", status_code=303)
    return auth_page(request, "setup.html", error=None, **setup_flags(request))


@site.post("/setup")
def setup_save(request: Request, password: str = Form(...), confirm: str = Form(""), code: str = Form("")):
    if AUTH["password"] or not AUTH["enabled"]:
        return RedirectResponse(f"{request.scope.get('root_path', '')}/", status_code=303)
    flags = setup_flags(request)
    need_code = not flags["keyless"]
    if need_code:
        key, now = sign_in_key(request), time.time()
        held = FAILURES.wait(key, now)
        if held > 0:
            return auth_page(request, "setup.html", 429, error=f"Too many tries. Wait {int(held) + 1} seconds.", **flags)
        FAILURES.record(key, now)  # counted before the check and forgiven after it, as everywhere else
        if not auth.setup_code_ok(AUTH["secret"], code):
            return auth_page(request, "setup.html", 403, error="That setup code is not right.", **flags)
        FAILURES.clear(key)
    problem = auth.password_problem(password, confirm)
    if problem:
        return auth_page(request, "setup.html", error=problem, **flags)
    with db() as conn:
        auth.store_set(conn, "password", auth.hash_password(password))
    load_auth()
    r = RedirectResponse(f"{request.scope.get('root_path', '')}/", status_code=303)
    set_session(r, request, auth.new_session(AUTH["secret"]))
    return r


@site.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = ""):
    if not AUTH["enabled"] or auth.valid_session(AUTH["secret"], session_of(request)):
        return RedirectResponse(f"{request.scope.get('root_path', '')}/", status_code=303)
    return auth_page(request, "login.html", error=None, next=safe_next(next))


def sign_in_key(request: Request) -> str:
    """Who a failed sign-in counts against: judged from the socket's own address (auth.throttle_key)."""
    peer = request.scope.get("cc_peer") or (request.client.host if request.client else None)
    return auth.throttle_key(request.headers, peer)


@site.post("/login")
def login_submit(request: Request, password: str = Form(...), next: str = Form("")):
    target, key, now = safe_next(next), sign_in_key(request), time.time()
    held = FAILURES.wait(key, now)
    if held > 0:
        return auth_page(request, "login.html", 429, error=f"Too many tries. Wait {int(held) + 1} seconds.", next=target)
    if not auth.verify_password(AUTH["password"], password):
        FAILURES.record(key, now)
        # No hint about which half was wrong, and none about whether a password is set at all.
        return auth_page(request, "login.html", 401, error="That password is not right.", next=target)
    FAILURES.clear(key)
    r = to_site(request, target)
    set_session(r, request, auth.new_session(AUTH["secret"]))
    # A login alert: who and from where, on every channel that is set up.
    agent = (request.headers.get("user-agent") or "")[:80]
    recorder.notify(
        f"Signed in to Car Command from {auth.client_ip(request.headers, request.client.host if request.client else None)} at {datetime.now(TZ).strftime('%b %-d, %-I:%M %p')}"
        + (f" · {agent}" if agent else ""),
        "login",
    )
    return r


APP_REDIRECTS = connect.allowlist(os.environ.get("CC_APP_REDIRECTS"))  # empty: any app scheme
# Bad code exchanges, counted per client and apart from sign-ins. No global brake: a code is 256 random
# bits, so guessing is hopeless, and a brake shared by all would only let a stranger slow every app.
CONNECT_FAILURES = auth.Failures(global_brake=False)
CONNECT_BODY_MAX = 4096


def connect_key(request: Request) -> str:
    return "connect:" + auth.client_prefix(sign_in_key(request))


async def read_capped(request: Request, limit: int) -> bytes | None:
    """The body, or None once it passes `limit`: read as it arrives, so a chunked body with no
    length cannot be made to fill memory."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def connect_refusal(request: Request, headline: str, detail: str, status: int = 400, action: dict | None = None) -> HTMLResponse:
    """A connect request that cannot go on is answered on a page and never by redirect."""
    return refusal(request, status, headline, detail, action)


def connect_blocked(request: Request) -> HTMLResponse | None:
    if not AUTH["enabled"]:
        # With sign-in off anyone on the network would be "the owner" and could mint a token.
        # The button goes to the switch in Settings and carries this request along, so turning sign-in on
        # lands back on the consent page.
        back = signin_return(site_path(request) + (f"?{request.url.query}" if request.method == "GET" and request.url.query else ""))
        href = f"{request.scope.get('root_path', '')}/settings/access" + (f"?next={quote(back, safe='')}" if back else "") + "#signin"
        action = {"label": "Turn on sign-in", "href": href}
        if cf_candidate(request):
            action = {"label": "Use Cloudflare Access", "href": f"{request.scope.get('root_path', '')}/settings/signin/cloudflare", "post": True, "fields": {"next": back} if back else {}}
        return connect_refusal(request, "Sign-in is off.", "Turn sign-in on before connecting an app.", 403, action)
    return None


def connect_page(request: Request, params: dict, request_params) -> HTMLResponse:
    r = auth_page(
        request,
        "connect.html",
        client=params["client"],
        destination=params["redirect_uri"],
        fields={k: request_params.get(k, "") for k in ("client", "redirect_uri", "state", "code_challenge", "code_challenge_method")},
    )
    # A browser holds the answer to a form post to the page's form-action, which is this site;
    # the app's own scheme is the one more place this page may send the owner.
    r.headers["Content-Security-Policy"] = CSP.replace("form-action 'self'", f"form-action 'self' {connect.form_action(params['redirect_uri'])}")
    return r


@site.get("/connect", response_class=HTMLResponse)
def connect_form(request: Request):
    blocked = connect_blocked(request)
    if blocked:
        return blocked
    params, problem = connect.check_request(request.query_params, APP_REDIRECTS)
    if problem:
        return connect_refusal(request, "Can't connect that app.", problem)
    if (
        params["redirect_uri"] in APP_REDIRECTS
        and (auth.valid_session(AUTH["secret"], session_of(request)) or getattr(request.state, "cf", None))
        and (known_app := app_known(params["redirect_uri"], params["client"]))
    ):
        # An install that lists an app's redirect trusts that app once someone has allowed it here by hand:
        # after that, someone already signed in is sent straight back to it with a code, and the sign-in sheet
        # just closes. The token still shows in Settings, and Forget there brings the Allow page back.
        return issue_code(params, share_place=bool(known_app["share_place"]))
    return connect_page(request, params, request.query_params)


@site.post("/connect")
async def connect_answer(request: Request):
    """Allow or Cancel. The parameters travel in the form, so they are checked again."""
    blocked = connect_blocked(request)
    if blocked:
        return blocked
    form = await request.form()
    params, problem = connect.check_request(form, APP_REDIRECTS)
    if problem:
        return connect_refusal(request, "Can't connect that app.", problem)
    uri, state = params["redirect_uri"], params["state"]
    if form.get("decision") != "allow":
        return RedirectResponse(connect.redirect_to(uri, error="access_denied", state=state), status_code=303)
    share_place = form.get("share_place") == "on"
    await asyncio.to_thread(remember_app, uri, params["client"], share_place)
    return await asyncio.to_thread(issue_code, params, share_place)


def remember_app(uri: str, client: str, share_place: bool = False) -> None:
    """An app allowed by hand, with the choice made on the Allow page: a skipped connect keeps it."""
    with db() as conn:
        conn.execute(
            "INSERT INTO cc_connect_apps (redirect_uri, client, share_place) VALUES (%s, %s, %s) ON CONFLICT (redirect_uri, client) DO UPDATE SET share_place = EXCLUDED.share_place",
            (uri, client, share_place),
        )
        conn.commit()


def app_known(uri: str, client: str) -> dict | None:
    """The remembered choice of an app that was allowed by hand and whose token is still there, else None: revoke
    the token, however, and the next connect asks again."""
    with db() as conn:
        return conn.execute(
            "SELECT a.share_place FROM cc_connect_apps a JOIN cc_api_tokens t ON t.hash = a.token_hash WHERE a.redirect_uri = %s AND a.client = %s",
            (uri, client),
        ).fetchone()


def remembered_apps() -> list[dict]:
    with db() as conn:
        return conn.execute("SELECT a.redirect_uri, a.client FROM cc_connect_apps a JOIN cc_api_tokens t ON t.hash = a.token_hash ORDER BY a.allowed_at DESC").fetchall()


@site.post("/settings/connect/forget")
async def connect_forget(request: Request):
    """Bring the Allow page back for an app that was allowed once."""
    form = await request.form()

    def forget() -> None:
        with db() as conn:
            conn.execute("DELETE FROM cc_connect_apps WHERE redirect_uri = %s AND client = %s", (form.get("redirect_uri") or "", form.get("client") or ""))
            conn.commit()

    await asyncio.to_thread(forget)
    return RedirectResponse(f"{request.scope.get('root_path', '')}/settings/access?saved=forgot", status_code=303)


def issue_code(params: dict, share_place: bool = False) -> RedirectResponse:
    """A one-time code for the app, kept as its hash, and the redirect that hands it over."""
    uri, state = params["redirect_uri"], params["state"]
    code = connect.new_code()
    now = datetime.now(timezone.utc)
    with db() as conn:
        conn.execute("DELETE FROM cc_connect_codes WHERE expires_at < %s", (now,))
        conn.execute(
            "INSERT INTO cc_connect_codes (hash, challenge, client, redirect_uri, expires_at) VALUES (%s, %s, %s, %s, %s)",
            (connect.code_hash(code), params["code_challenge"], params["client"], uri, now + timedelta(seconds=connect.CODE_TTL_S)),
        )
        if share_place:
            conn.execute("UPDATE cc_connect_codes SET share_place = true WHERE hash = %s", (connect.code_hash(code),))
        conn.commit()
    return RedirectResponse(connect.redirect_to(uri, code=code, state=state), status_code=303)


def redeem_code(code: str, verifier: object, device: object, redirect_uri: object = None) -> str | None:
    """The one try a code gets: it is deleted whatever happens next. A status token,
    or None for a code that is unknown, spent, expired, or paired with the wrong verifier."""
    with db() as conn:
        row = conn.execute("DELETE FROM cc_connect_codes WHERE hash = %s RETURNING challenge, client, redirect_uri, expires_at, share_place", (connect.code_hash(code),)).fetchone()
        conn.commit()
    if row is None or row["expires_at"] <= datetime.now(timezone.utc) or not connect.pkce_ok(verifier, row["challenge"]):
        return None
    if redirect_uri is not None and redirect_uri != row["redirect_uri"]:
        return None  # optional, but when the app says where it was sent, it must be where the owner sent it
    label = connect.token_label(row["client"], device)
    share_place = bool(row.get("share_place"))
    raw = store_token(label, "status", None, share_place)
    with db() as conn:
        # An app that was allowed by hand now lives and dies with this token (a skipped connect moves it on).
        conn.execute("UPDATE cc_connect_apps SET token_hash = %s WHERE redirect_uri = %s AND client = %s", (auth.token_hash(raw), row["redirect_uri"], row["client"]))
        conn.commit()
    recorder.notify(f"An app connected to Car Command: {label}" + (", and can see which place each car is at (place name, home or away)" if share_place else ""), "login")
    return raw


@site.post(connect.TOKEN_PATH)
async def connect_token(request: Request):
    """An app trades its code and verifier for a read token. Open without a session (the guard
    lets this path through); failures are slowed like sign-in failures, and say only invalid_grant."""
    invalid = JSONResponse({"error": "invalid_grant"}, status_code=400, headers={"Cache-Control": "no-store"})
    key, now = connect_key(request), time.time()
    held = CONNECT_FAILURES.wait(key, now)
    if held > 0:
        return JSONResponse({"error": "slow_down"}, status_code=429, headers={"Retry-After": str(int(held) + 1), "Cache-Control": "no-store"})
    raw_body = await read_capped(request, CONNECT_BODY_MAX)
    if raw_body is None:
        return JSONResponse({"error": "too_large"}, status_code=413, headers={"Cache-Control": "no-store"})
    try:
        body = json.loads(raw_body)
    except (ValueError, RecursionError):
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    raw = None
    if AUTH["enabled"] and isinstance(code, str) and code:
        raw = await asyncio.to_thread(redeem_code, code, body.get("code_verifier"), body.get("device"), body.get("redirect_uri"))
    if raw is None:
        CONNECT_FAILURES.record(key, now)
        return invalid
    CONNECT_FAILURES.clear(key)
    return JSONResponse({"token": raw, "kind": "status"}, headers={"Cache-Control": "no-store"})


@site.post("/logout")
def logout(request: Request):
    r = RedirectResponse(f"{request.scope.get('root_path', '')}/login", status_code=303)
    set_session(r, request, None)
    return r


def manifest_for(root: str) -> dict:
    """The install manifest. Start URL and scope carry the mount prefix, or the installed app
    would open the LAN root when it was added from /cc."""
    base = f"{root}/"
    return {
        "name": "Car Command",
        "short_name": "Cars",
        "start_url": base,
        "scope": base,
        "display": "standalone",
        "background_color": "#F6F5F0",
        "theme_color": "#1B1A21",
        "icons": [
            {"src": f"{root}/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": f"{root}/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            # the same files: the icon keeps the mark inside the maskable safe circle
            {"src": f"{root}/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": f"{root}/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }


@site.get("/manifest.webmanifest", include_in_schema=False)
def manifest(request: Request):
    return JSONResponse(manifest_for(request.scope.get("root_path", "")), media_type="application/manifest+json")


@site.get(tesla_keys.PUBLIC_PATH)
def tesla_public_key():
    """The app's public key for Tesla, or 404 before Connect Tesla has made one. Never makes one."""
    pem = tesla_keys.stored(state_get, VAULT)
    if not pem:
        # plain, not the site's error page: that page draws the garage, and this path is open to anyone
        return PlainTextResponse("No Tesla key yet.", status_code=404)
    return Response(tesla_keys.public_pem(pem), media_type="application/x-pem-file")


@site.get("/health")
def health():
    return {"ok": True}


@site.get("/favicon.ico", include_in_schema=False)
def favicon():
    # The reduced glyph at 32 px, for the browsers that ask for /favicon.ico before reading the page's links.
    return Response((HERE / "static" / "favicon-32.png").read_bytes(), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@site.get("/", response_class=HTMLResponse)
def now(request: Request):
    # The site opens on the whole garage: a household with more than one car lands on all of them.
    vehicles = all_vehicles() if "vin" not in request.query_params else []
    car = garage_car() if len(vehicles) > 1 else pick(request)
    if car.get("empty"):
        return page(request, "welcome.html", car=car, active="now", connector=connector_down())
    if car.get("garage"):
        cars = []
        for v in all_vehicles():
            c = live_vehicle(v["vin"])
            c["ribbon"] = ribbon(v["vin"])
            c["last_drive"] = next(iter(drives(v["vin"], 1)), None)
            c["route"] = tesla_stream_worker.route(v["vin"])
            c["ident"] = car_identities([v])[v["vin"]]
            cars.append(c)
        cars.sort(key=lambda c: (not c["live"], -(c["updated"].timestamp() if c.get("updated") else 0)))
        today = today_by_car()
        end = datetime.now(timezone.utc)
        acts = activity_banners(cars)
        return page(
            request,
            "garage.html",
            car=car,
            cars=cars,
            acts=acts,
            act_sig=activity.signature(acts),
            today=today,
            line=nowpage.today_line(cars, today, units_mod.ACTIVE, {b["vin"]: b["kind"] for b in acts}),
            ticks=nowpage.strip_ticks(end - timedelta(hours=24), end, TZ),
            pw=energy_now(),
            active="now",
            live_vin=GARAGE,
        )
    series = battery_series(car["vin"])
    ds = drives(car["vin"], 5)
    spans = [d["start_date"] for d in ds] + [d["destination_date"] for d in ds if d.get("destination_date")]
    acts = activity_banners([car])
    return page(
        request,
        "now.html",
        **now_card_data(car),
        acts=acts,
        act_sig=activity.signature(acts),
        thumbs=route_thumbs(ds, min(spans), max(spans), 64, 36) if spans else {},
        car=car,
        ribbon=ribbon(car["vin"]),
        spark=sparkline(series),
        software=software_history(car["vin"]),
        spark_first=series[0][0] if series else None,
        drives=ds,
        charges=charges(car["vin"], 5),
        ticks=nowpage.strip_ticks(datetime.now(timezone.utc) - timedelta(hours=24), datetime.now(timezone.utc), TZ),
        pw=energy_now(),
        active="now",
        live_vin=car["vin"],
    )


# ---- the live Now page: each open Now or garage page holds one stream, and gets its car's
# card again whenever what the card shows has changed. Change is found by drawing the card
# from the same cached data the page uses and comparing the markup; data moves about once
# a minute, and "last seen 3 min ago" ticking over is a change worth sending too.
STREAM_S = float(os.environ.get("CC_STREAM_S", "15"))
STREAM_BEAT_S = 20.0  # a quiet stream says so this often, inside the 25 s a proxy waits
STREAM_MAX_S: float | None = None  # tests bound the stream; a page's runs until it closes
LIVE_RIBBONS: dict[str, tuple[datetime, list]] = {}  # vin -> (the minute it was drawn to, the strip)


_GARAGE_CARD = None


def _garage_card():
    """The garage card macro as a template of its own, compiled once."""
    global _GARAGE_CARD
    if _GARAGE_CARD is None:
        _GARAGE_CARD = templates.env.from_string('{% import "_parts.html" as parts %}{{ parts.car_card(c, root, colors, u, today, ticks) }}')
    return _GARAGE_CARD


def now_card_data(car: dict) -> dict:
    """What the Now card shows beyond the car itself. The page and the stream both take it
    from here: the stream once drew the card without the tread line and it vanished on the
    first update."""
    return {
        "today": today_by_car().get(car["vin"]),
        "due": service_due(car["vin"], car.get("odometer_mi")),
        "tread_last": next(iter(tread_log([car["vin"]])), None),
        "route": tesla_stream_worker.route(car["vin"]),  # from memory: the stream's newest values
        "ident": car_identities([{"vin": car["vin"]}])[car["vin"]],
    }


ACT_LOOKBACK = timedelta(hours=12)  # no drive or charge the banner reads back further than this


def activity_rows(conn, car: dict, kind: str, now: datetime) -> list[dict]:
    """The readings a banner's run is found in, newest first: the TeslaFi feed's rows for a car
    read from the feed, else the positions every live source writes for a drive, else a charge
    still open in the history. Each is one indexed read of at most the last twelve hours."""
    vin, since = car["vin"], now - ACT_LOOKBACK
    if car.get("source") == "feed":
        rows = conn.execute(
            """SELECT at, data->>'carState' AS car_state, data->>'odometer' AS odometer, data->>'battery_level' AS level,
                      data->>'battery_range' AS range, data->>'charger_power' AS kw, data->>'charge_energy_added' AS kwh_added
               FROM cc_teslafi_rows WHERE vin = %s AND at >= %s ORDER BY at DESC LIMIT 900""",
            (vin, since),
        ).fetchall()
        return [activity.feed_row(r) for r in rows]
    if kind == "driving":
        rows = conn.execute("SELECT at, state, odometer, level FROM cc_positions WHERE vin = %s AND at >= %s ORDER BY at DESC LIMIT 900", (vin, since)).fetchall()
        return [activity.position_row(r) for r in rows]
    s = conn.execute(
        "SELECT session_start_date AS at, start_level AS level FROM charging_sessions WHERE vin = %s AND session_end_date IS NULL AND session_start_date >= %s ORDER BY session_start_date DESC LIMIT 1",
        (vin, since),
    ).fetchone()
    # an open session is the run itself: its start, and now as its newest reading
    return [{"at": now, "state": "Charging", "level": car.get("soc")}, {"at": s["at"], "state": "Charging", "level": s["level"]}] if s else []


def live_route_of(conn, vin: str, since: datetime, now: datetime) -> dict | None:
    """Where a Tesla is navigating: the stream's memory, else its raw records since the drive
    began (the newest V records first, read only until the destination turns up)."""
    got = tesla_stream_worker.route(vin, now)
    if got or not identity.is_tesla(vin):
        return got
    # the destination is sent once, as it is set; the time to go every minute: one newest record of each
    rows = conn.execute(
        """(SELECT at, record FROM cc_tesla_raw WHERE vin = %s AND kind = 'V' AND at >= %s
                  AND record @? '$.data[*] ? (@.key == "DestinationName")' ORDER BY at DESC LIMIT 1)
           UNION ALL
           (SELECT at, record FROM cc_tesla_raw WHERE vin = %s AND kind = 'V' AND at >= %s
                  AND record @? '$.data[*] ? (@.key == "MinutesToArrival")' ORDER BY at DESC LIMIT 1)""",
        (vin, since, vin, max(since, now - tesla_nav.LIVE_FRESH)),
    ).fetchall()
    recs = sorted(((r["at"], r["record"] if isinstance(r["record"], dict) else json.loads(r["record"])) for r in rows), key=lambda x: x[0], reverse=True)
    return tesla_nav.route_from_records(recs, now)


def activity_banners(cars: list[dict]) -> list[dict]:
    """A banner per car driving or charging now, in the cars' order. A car at rest costs
    nothing here: the reads are only for a car that is out."""
    now = datetime.now(timezone.utc)
    active = [(c, k) for c in cars if (k := activity.kind(c, now))]
    if not active:
        return []
    energy, out = vehicle_energy(), []
    with db() as conn:
        feed_table = conn.execute("SELECT to_regclass('cc_teslafi_rows') AS t").fetchone()["t"]
        for c, k in active:
            if c.get("source") == "feed" and not feed_table:
                c = dict(c, source=None)
            span = activity.run(activity_rows(conn, c, k, now), "Driving" if k == "driving" else "Charging", now)
            route = live_route_of(conn, c["vin"], span[0]["at"] if span else now - timedelta(hours=1), now) if k == "driving" else None
            cap, per_rated = energy.get(c["vin"], (None, None))
            out.append(activity.banner(c, k, span, now, route=route, capacity_kwh=cap or c.get("capacity_kwh"), kwh_per_rated_mi=per_rated))
    return sorted(out, key=lambda b: b["name"].lower())  # the page and the stream list the cars in different orders


ACT_KEY = "activity"  # the banners' place in a stream's cards (data-live="activity"): no vin is called that


def _activity_html(acts: list[dict], root: str, colors: dict, link: bool) -> str:
    return templates.env.get_template("_activity.html").render(acts=acts, act_sig=activity.signature(acts), act_link=link, root=root, colors=colors)


def live_cards(vin: str | None, root: str) -> dict[str, dict]:
    """The cards a stream sends, shared by every open page for ten seconds: each drawing asks
    the connector and the database, and five tabs need not ask five times."""
    return cached(f"live-cards:{vin}:{root}", 10, lambda: draw_live_cards(vin, root))


def draw_live_cards(vin: str | None, root: str) -> dict[str, dict]:
    """One car's Now card, or every garage card. Keyed by vin."""
    vehicles = all_vehicles()
    if vin == GARAGE:
        today = today_by_car()
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)  # to the minute: drawn to the second, every card differed on every look
        ticks = nowpage.strip_ticks(end - timedelta(hours=24), end, TZ)
        colors = car_colors(vehicles)
        out, cars = {}, []
        for v in vehicles:
            c = live_vehicle(v["vin"])
            kept = LIVE_RIBBONS.get(v["vin"])
            if not kept or kept[0] != end:
                kept = LIVE_RIBBONS[v["vin"]] = (end, ribbon(v["vin"], end=end))  # one per car, read again when the minute turns
            c["ribbon"] = kept[1]
            c["last_drive"] = cached(f"live-last-drive:{v['vin']}", 60, lambda v=v: next(iter(drives(v["vin"], 1)), None))
            c["route"] = tesla_stream_worker.route(v["vin"])
            c["ident"] = car_identities([v])[v["vin"]]
            html = _garage_card().render(c=c, root=root, colors=colors, today=today.get(c["vin"]), ticks=ticks)
            out[c["vin"]] = {"html": html, "lat": c.get("lat"), "lon": c.get("lon")}
            cars.append(c)
        out[ACT_KEY] = {"html": _activity_html(activity_banners(cars), root, colors, link=True)}
        return out
    car = live_vehicle(vin)
    if not car.get("vin"):
        return {}
    q = f"?vin={car['vin']}" if len(vehicles) > 1 else ""
    html = templates.env.get_template("_now_hero.html").render(car=car, root=root, q=q, **now_card_data(car))
    acts = _activity_html(activity_banners([car]), root, car_colors(vehicles), link=False)
    return {car["vin"]: {"html": html, "lat": car.get("lat"), "lon": car.get("lon")}, ACT_KEY: {"html": acts}}


class StreamEnded(Exception):
    """Raised by a stream's look when nothing more should be sent: the token it holds was revoked."""


async def sse_pump(request: Request, label: str, look):
    """The loop behind both streams. `look` (run off the event loop) returns the frames to send
    now, or raises when it cannot draw. A client that goes ends this by cancellation (the server
    cancels the response the moment it is gone); the disconnect check in the wait is a second
    guard, not the one that usually fires."""
    failing = False  # said once when drawing starts failing, once when it recovers: not every look
    started = quiet = time.monotonic()
    yield livestream.HEARTBEAT  # headers and a first line before a slow connector is asked
    while True:
        try:
            frames = await asyncio.to_thread(look)
            if failing:
                LOG.info("stream %s: drawing again", label)
            failing = False
        except StreamEnded:
            return
        except Exception as err:  # noqa: BLE001 - a card that cannot be drawn now may be drawn next time
            if not failing:
                LOG.warning("stream %s: %s", label, err)
            failing, frames = True, []
        for frame in frames:
            yield frame
        if frames:
            quiet = time.monotonic()
        waited = 0.0
        while waited < STREAM_S:
            if STREAM_MAX_S is not None and time.monotonic() - started >= STREAM_MAX_S:
                return
            if await request.is_disconnected():
                return
            if time.monotonic() - quiet >= STREAM_BEAT_S:
                yield livestream.HEARTBEAT
                quiet = time.monotonic()
            step = min(1.0, STREAM_S - waited)
            await asyncio.sleep(step)
            waited += step


def sse_response(events) -> StreamingResponse:
    return StreamingResponse(events, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def log_safe(text) -> str:
    """As the logs see it: from the query string, so no line breaks to forge a line with."""
    return str(text).replace("\r", " ").replace("\n", " ")[:40]


@site.get("/stream")
async def stream(request: Request, vin: str | None = None):
    """Server-sent events for the live Now page: a `card` event per card that changed."""
    root = request.scope.get("root_path", "")
    prev: dict[str, str] = {}

    def look() -> list[str]:
        nonlocal prev
        sent, prev = livestream.changed(prev, live_cards(vin, root))
        return [livestream.sse("card", c) for c in sent]

    return sse_response(sse_pump(request, log_safe(vin), look))


_STATES: dict[tuple, tuple[float, dict]] = {}  # (car or None for all, location hidden) -> (when read, states)
_STATES_LOCK = threading.Lock()
STREAM_CAP = 4  # open /api/stream connections per bearer token; a household's own tabs and phones are not capped
STREAMS_OPEN: dict[str, int] = {}


def shared_states(vin: str | None, hide_location: bool, share_place: bool = False) -> dict[str, dict]:
    """api_states, read once for however many streams ask within half a tick, so ten open apps
    cost one read of the cars, not ten. The lock makes a second asker wait for the first's result."""
    key = (vin if vin and vin != GARAGE else None, hide_location, hide_location and share_place)  # each variant is its own entry
    with _STATES_LOCK:
        at, states = _STATES.get(key, (0.0, {}))
        now = time.monotonic()
        if now - at >= STREAM_S / 2:
            states = api_states(*key)
            for stale in [k for k, (t, _) in _STATES.items() if now - t > 60]:
                del _STATES[stale]
            _STATES[key] = (now, states)
        return states


def api_states(vin: str | None, hide_location: bool = False, share_place: bool = False) -> dict[str, dict]:
    """Each car's /api/now answer, keyed by vin: every car, or the one asked for."""
    vins = [vin] if vin and vin != GARAGE else [v["vin"] for v in all_vehicles()]
    states = with_colors({v: jsonable(live_vehicle(v)) for v in vins})
    return {v: auth.status_payload(s, share_place) for v, s in states.items()} if hide_location else states


def with_colors(states: dict[str, dict]) -> dict[str, dict]:
    """Each car's chart colour beside its state, as the site draws it: `color` on a light
    ground, `color_dark` on a dark one, so an app can match the car's colour on its pages."""
    if not states:
        return states
    idx = car_colors(all_vehicles())
    for vin, car in states.items():
        if vin in idx and isinstance(car, dict):
            _, light, dark = identity.PALETTE[idx[vin]]
            car["color"], car["color_dark"] = light, dark
    return states


@site.get("/api/stream")
async def api_stream(request: Request, vin: str | None = None):
    """Server-sent events for apps: a `now` event per car, holding what /api/now answers for it,
    whenever anything in it has changed. Every car's state comes first, then only the changes;
    a comment line keeps a quiet stream open. `?vin=` limits it to one car, and a token for one
    car is held to its own by the guard, as on every /api/ path."""
    if vin and vin != GARAGE and vin not in {v["vin"] for v in await asyncio.to_thread(all_vehicles)}:
        return JSONResponse({"error": "no such car"}, status_code=404)
    prev: dict[str, str] = {}
    token = getattr(request.state, "token", None)
    held = auth.token_hash(auth.bearer_of(request.headers.get("authorization")) or "") if token else None
    if held and STREAMS_OPEN.get(held, 0) >= STREAM_CAP:
        return JSONResponse({"error": "too_many_streams"}, status_code=429)
    if held:
        STREAMS_OPEN[held] = STREAMS_OPEN.get(held, 0) + 1
    released = []

    def release() -> None:
        if held and not released:
            released.append(1)
            STREAMS_OPEN[held] = max(0, STREAMS_OPEN.get(held, 1) - 1)

    async def events():
        try:
            async for frame in sse_pump(request, "api " + log_safe(vin), look):
                yield frame
        finally:
            release()

    def look() -> list[str]:
        nonlocal prev
        if held and held not in TOKENS:
            raise StreamEnded  # revoked while open: no more of the car, however long the connection lives
        now = TOKENS.get(held) or token or {}  # the token as it is now: the owner may have changed what it may see
        status = now.get("kind") == "status"
        sent, prev = livestream.changed_states(prev, shared_states(vin, status, status and bool(now.get("share_place"))))
        return [livestream.sse("now", s) for s in sent]

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}, background=BackgroundTask(release))


def list_window(month: str | None, from_: str | None, to: str | None, latest) -> dict:
    """A month by default; a custom range when from/to are given (either end open)."""
    d0, d1 = parse_day(from_), parse_day(to)
    if d0 or d1:
        start = datetime(d0.year, d0.month, d0.day, tzinfo=TZ) if d0 else datetime(2000, 1, 1, tzinfo=TZ)
        end = (datetime(d1.year, d1.month, d1.day, tzinfo=TZ) + timedelta(days=1)) if d1 else datetime.now(TZ) + timedelta(days=1)
        label = f"{d0.strftime('%b %-d, %Y') if d0 else 'the beginning'} to {d1.strftime('%b %-d, %Y') if d1 else 'today'}"
        return {"start": start, "end": end, "label": label, "ym": None, "custom": True, "from": d0, "to": d1, "is_current": True, "prev": None, "next": None, "prev_label": "", "next_label": ""}
    w = month_window(month, TZ, latest)
    w["custom"] = False
    return w


def sort_base(request: Request) -> str:
    """The current query string without its sort, for header links to build on."""
    keep = [(k, v) for k, v in request.query_params.multi_items() if k not in ("sort", "dir")]
    return ("?" + urlencode(keep) + "&") if keep else "?"


@site.get("/drives", response_class=HTMLResponse)
def drives_page(request: Request, month: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None, sort: str | None = None, dir: str = "desc"):
    car = pick(request)
    w = list_window(month, from_, to, latest_dates(car["vin"])["drive"])
    qp = request.query_params
    flt = {"frm": qp.get("at") or None, "to": qp.get("dest") or None, "both_ways": qp.get("both") == "on", "min_mi": _float_or_none(qp.get("min")), "text": qp.get("q") or None}
    hidden = qp.get("show") == "hidden"
    rows = hidden_drives(car["vin"]) if hidden else filter_drives(drives(car["vin"], None, w["start"], w["end"]), **flt)
    rows, key, desc = sort_rows(rows, sort, dir != "asc", DRIVE_SORTS)
    days = daypage.by_day(rows, TZ, "start_date") if key == "start" else None
    return page(
        request,
        "drives.html",
        car=car,
        drives=rows,
        days=days,
        window=w,
        sort=key,
        desc=desc,
        sort_base=sort_base(request),
        active="drives",
        named_places=named_places(),
        now_local=datetime.now(TZ),
        said=qp.get("said"),
        flt=dict(flt, raw=dict(qp)),
        hidden=hidden,
    )


def _float_or_none(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


@site.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, month: str | None = None, year: int | None = None):
    car = pick(request)
    if year is not None:
        return calendar_year(request, car, year)
    w = month_window(month, TZ, latest_dates(car["vin"])["drive"])
    # a day's drives can start just outside the month in UTC; widen the fetch by a day each side
    ds = drives(car["vin"], None, w["start"] - timedelta(days=1), w["end"] + timedelta(days=1))
    cs = charges(car["vin"], None, w["start"] - timedelta(days=1), w["end"] + timedelta(days=1))
    weeks = calendar_grid(ds, cs, w, TZ, WEEK_START)
    month_cells = [c for week in weeks for c in week if not c["other"]]
    totals = {k: sum(c[k] for c in month_cells) for k in ("drives", "miles", "charges", "kwh")}
    # The month's driving and charging tables above the grid. summarize() already
    # produces exactly these figures and every drive already carries its own energy,
    # priced by its own year, so this costs no query: the rows are the ones the grid
    # above was built from, narrowed to the month the grid is showing.
    inside = lambda t: t is not None and w["start"] <= t < w["end"]
    rows = summarize([d for d in ds if inside(d.get("start_date"))], [c for c in cs if inside(c.get("session_start_date"))], "month", TZ, None, None)
    month = rows[0] if rows else None
    if month:
        month["days_driven"] = sum(1 for c in month_cells if c["drives"])
        month["best_day"] = max((c for c in month_cells if c["drives"]), key=lambda c: c["miles"], default=None)
    vins = [v["vin"] for v in all_vehicles()] if car.get("garage") else [car["vin"]]
    return page(
        request,
        "calendar.html",
        car=car,
        weeks=weeks,
        window=w,
        totals=totals,
        month=month,
        center=map_center(vins),
        max_miles=max((c["miles"] for c in month_cells), default=0.0),
        charge_pins=charge_pins([c for c in cs if inside(c.get("session_start_date"))]),
        active="calendar",
    )


def charge_pins(rows: list[dict]) -> list[dict]:
    """Charges as map pins: one per spot (to about 100 m), with the count, the kWh and the place."""
    pins: dict[tuple, dict] = {}
    for c in rows:
        lat, lon = c.get("session_position_latitude"), c.get("session_position_longitude")
        if lat is None or lon is None:
            continue
        key = (round(lat, 3), round(lon, 3))
        pin = pins.setdefault(key, {"lat": key[0], "lon": key[1], "n": 0, "kwh": 0.0, "where": c.get("where") or "", "fast": False, "v": c.get("vin")})
        pin["n"] += 1
        pin["kwh"] += c.get("kwh") or 0.0
        pin["fast"] = pin["fast"] or (c.get("charging_type") or "").upper() == "DC"
    return sorted(pins.values(), key=lambda p: -p["kwh"])


def calendar_year(request: Request, car: dict, year: int) -> HTMLResponse:
    """Twelve small months, each day shaded by its miles: the shape of a year at a glance."""
    year = max(2000, min(year, datetime.now(TZ).year))
    start, end = datetime(year, 1, 1, tzinfo=TZ), datetime(year + 1, 1, 1, tzinfo=TZ)
    ds = drives(car["vin"], None, start - timedelta(days=1), end + timedelta(days=1))
    cs = charges(car["vin"], None, start - timedelta(days=1), end + timedelta(days=1))
    months = year_grids(ds, cs, year, TZ, WEEK_START)
    cells = [c for m in months for week in m["weeks"] for c in week if not c["other"]]
    totals = {k: sum(m["totals"][k] for m in months) for k in ("drives", "miles", "charges", "kwh")}
    totals["days_driven"] = sum(m["days_driven"] for m in months)
    best = max((c for c in cells if c["drives"]), key=lambda c: c["miles"], default=None)
    return page(
        request,
        "calendar_year.html",
        car=car,
        year=year,
        months=months,
        totals=totals,
        best=best,
        max_miles=max((c["miles"] for c in cells), default=0.0),
        this_year=datetime.now(TZ).year,
        active="calendar",
    )


_driveprint_cache: dict[str, tuple[datetime, list]] = {}


def driveprint(vin: str) -> list[list]:
    """Every 100 m cell the car has reported from: where, how many times, and when it
    first arrived. Grouped once an hour per vehicle; 522K positions become ~60K cells.

    The first-seen date rides along because the grouping is already paying for it — the
    same GROUP BY, 335 ms for every car — and it is the whole of "new ground on this
    drive". Map payloads drop it again, so the JSON is the same size it was."""
    hit = _driveprint_cache.get(vin)
    now = datetime.now(timezone.utc)
    if hit and now - hit[0] < timedelta(hours=1):
        return hit[1]
    with db() as conn:
        rows = conn.execute(
            """SELECT ROUND(latitude::numeric, 3) AS la, ROUND(longitude::numeric, 3) AS lo, COUNT(*) AS n, MIN(at) AS first_at
                               FROM cc_positions WHERE vin = %s GROUP BY 1, 2""",
            (vin,),
        ).fetchall()
    cells = [[float(r["la"]), float(r["lo"]), int(r["n"]), r["first_at"]] for r in rows]
    _driveprint_cache[vin] = (now, cells)
    return cells


def new_ground(vin: str, start: datetime, end: datetime) -> int:
    """Cells this car reported from for the first time between `start` and `end`.

    Read off the hourly cell grouping rather than asked of the database: the question
    "had this car ever been here before" is one comparison per cell once the first-seen
    date exists, and a correlated lookup per cell without it."""
    return sum(1 for cell in driveprint(vin) if cell[3] is not None and start <= cell[3] <= end)


def map_center(vins: list[str]) -> list[float]:
    """Where a map opens before its data arrives: the newest known position of these cars."""
    with db() as conn:
        r = conn.execute("SELECT latitude, longitude FROM cc_positions WHERE vin = ANY(%s) ORDER BY at DESC LIMIT 1", (vins,)).fetchone()
    return [r["latitude"], r["longitude"]] if r else [0.0, 0.0]


@site.get("/map", response_class=HTMLResponse)
def map_page(request: Request, month: str | None = None):
    """A month of routes and charges on the quiet base, cars and charges as chips, the
    cells of everywhere the cars have been one chip away."""
    car = pick(request)
    vins = [v["vin"] for v in all_vehicles()] if car.get("garage") else [car["vin"]]
    vin = None if car.get("garage") else car["vin"]
    w = month_window(month, TZ, latest_dates(vin)["drive"])
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n, MIN(at) AS since FROM cc_positions WHERE vin = ANY(%s)", (vins,)).fetchone()
    pins = charge_pins(charges(vin, None, w["start"], w["end"]))
    return page(request, "map.html", car=car, vins=vins, positions=n["n"], since=n["since"], center=map_center(vins), window=w, pins=pins, active="map")


@site.get("/api/month-routes")
def api_month_routes(vin: str | None = None, month: str | None = None):
    """Per-drive polylines for one month, downsampled, with the car's colour index; the Calendar's map."""
    car_vins = [v["vin"] for v in all_vehicles()] if vin == GARAGE else [pick_vin(vin)]
    colors = car_colors(all_vehicles())
    w = month_window(month, TZ)
    out = []
    with db() as conn:
        for v in car_vins:
            rows = conn.execute(
                """SELECT at, latitude, longitude FROM cc_positions WHERE vin = %s AND at >= %s AND at < %s AND UPPER(state) = 'DRIVING' ORDER BY at""", (v, w["start"], w["end"])
            ).fetchall()
            # A drive matched to the roads draws that line, and its readings are left out of
            # the plain lines below (the same drive twice would be a straight-cut ghost).
            spans = matched_spans(conn, v, w["start"], w["end"])
            for _a, _b, pts in spans:
                out.append({"c": colors.get(v, 0), "v": v, "pts": compact_line(pts)})
            line, last, k = [], None, 0
            for r in rows:
                while k < len(spans) and spans[k][1] < r["at"]:
                    k += 1
                if k < len(spans) and spans[k][0] <= r["at"] <= spans[k][1]:
                    # end the plain line here: carried on after the drive, it would draw a
                    # straight chord across the matched one
                    if len(line) >= 2:
                        out.append({"c": colors.get(v, 0), "v": v, "pts": line[::3] + [line[-1]]})
                    line, last = [], None
                    continue
                if last is not None and (r["at"] - last).total_seconds() > 600 and len(line) >= 2:
                    out.append({"c": colors.get(v, 0), "v": v, "pts": line[::3] + [line[-1]]})
                    line = []
                line.append([round(r["latitude"], 4), round(r["longitude"], 4)])
                last = r["at"]
            if len(line) >= 2:
                out.append({"c": colors.get(v, 0), "v": v, "pts": line[::3] + [line[-1]]})
    return JSONResponse(out)


def matched_spans(conn, vin: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime, list]]:
    """(start, end, line) for one car's drives in [start, end) that are drawn matched to the
    roads (routematch), in time order; a drive whose end has changed since is not one."""
    lines = routematch.matched_lines(conn, vin, start, end)
    if not lines:
        return []
    trips = conn.execute("SELECT start_date, destination_date FROM trips WHERE vin = %s AND start_date >= %s AND start_date < %s AND destination_date IS NOT NULL", (vin, start, end)).fetchall()
    out = []
    for t in trips:
        pts = routematch.line_for(lines, t["start_date"], t["destination_date"])
        if pts:
            out.append((t["start_date"], t["destination_date"], pts))
    return sorted(out, key=lambda s: s[0])


def compact_line(pts: list) -> list:
    """A matched line for a month's map: four decimals (about 10 m) and no repeated vertex."""
    out: list = []
    for la, lo in pts:
        p = [round(la, 4), round(lo, 4)]
        if not out or out[-1] != p:
            out.append(p)
    return out


_driveprint_body: dict[str, tuple[datetime, bytes, tuple]] = {}


def driveprint_body(key: str) -> bytes:
    """The finished JSON for one map, cached as bytes.

    The per-vehicle cells were already cached, so the grouping query ran once an hour —
    and the endpoint still took twenty-five seconds every single time, because it rebuilt
    a hundred and eighty thousand four-element lists and serialised them again on every
    request. The Map page loads this, so opening the map meant half a minute of nothing.
    Cached here the second visit is a memory copy.

    Each cell carries its car's place in the garage's order, not a colour: the page looks
    the colour up when it draws, so a colour chosen in Settings shows at once. A car added
    or removed changes the order, and the body is built again for it."""
    hit = _driveprint_body.get(key)
    now = datetime.now(timezone.utc)
    vehicles = all_vehicles()
    order = tuple(v["vin"] for v in vehicles)
    if hit and now - hit[0] < timedelta(hours=1) and hit[2] == order:
        return hit[1]
    at = {vin: i for i, vin in enumerate(order)}
    if key == GARAGE:
        cells = [[*cell[:3], at[vin]] for vin in order for cell in driveprint(vin)]
    else:
        cells = [[*cell[:3], at.get(key, 0)] for cell in driveprint(key)]
    body = json.dumps(cells, separators=(",", ":")).encode()
    _driveprint_body[key] = (now, body, order)
    return body


@site.get("/api/driveprint")
def api_driveprint(vin: str | None = None):
    """[[lat, lon, n, car], ...], car being its place in the garage's order (the switcher's);
    vin=all gives every car."""
    key = GARAGE if vin == GARAGE else pick_vin(vin)
    return Response(content=driveprint_body(key), media_type="application/json")


def pick_vin(vin: str | None) -> str:
    """A VIN the garage knows, live or imported; the live car when none is given."""
    if vin and any(v["vin"] == vin for v in all_vehicles()):
        return vin
    return live_vehicle(None)["vin"]


def since_last_charge(vin: str, when: datetime, kwh_per_rated_mi: float | None) -> dict | None:
    """Battery stats since the last charge, as of `when`; day.since_charge decides which readings still stand."""
    with db() as conn:
        last = conn.execute("SELECT session_end_date, end_level FROM charging_sessions WHERE vin = %s AND session_end_date <= %s ORDER BY session_end_date DESC LIMIT 1", (vin, when)).fetchone()
        if not last:
            return None
        t0 = last["session_end_date"]
        LEVEL_AT = "SELECT l.level AS v, l.last_date FROM drive_levels l JOIN drives d ON d.id = l.drive_id WHERE d.vin = %s AND l.first_date <= %s ORDER BY l.first_date DESC LIMIT 1"
        # Same rewrite as the facts queries, and this one is on a page: with the join
        # inside, each of these scans every drive_ranges row rather than seeking the
        # (drive_id, first_date) index.
        RANGE_AT = "SELECT r.range AS v, r.last_date FROM drive_ranges r WHERE r.drive_id = (SELECT id FROM drives WHERE vin = %s) AND r.first_date <= %s ORDER BY r.first_date DESC LIMIT 1"
        lv0, lv1 = conn.execute(LEVEL_AT, (vin, t0)).fetchone(), conn.execute(LEVEL_AT, (vin, when)).fetchone()
        r0, r1 = conn.execute(RANGE_AT, (vin, t0)).fetchone(), conn.execute(RANGE_AT, (vin, when)).fetchone()
        moved = conn.execute(
            """SELECT COALESCE(SUM(destination_odometer - start_odometer) FILTER (WHERE destination_odometer IS NOT NULL), 0) AS mi,
                      MAX(COALESCE(destination_date, start_date)) AS last_move
               FROM trips WHERE vin = %s AND start_date >= %s AND start_date <= %s""",
            (vin, t0, when),
        ).fetchone()
    pair = lambda r: (r["v"], r["last_date"]) if r and r["v"] is not None else None
    return since_charge(t0, when, last["end_level"], pair(lv0), pair(lv1), pair(r0), pair(r1), moved["mi"] if moved else 0.0, moved["last_move"] if moved else None, kwh_per_rated_mi)


def day_bounds(day: str | None) -> tuple[datetime, datetime, date]:
    """Local midnight to midnight for a YYYY-MM-DD; today when absent or malformed."""
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date() if day else datetime.now(TZ).date()
    except ValueError:
        d = datetime.now(TZ).date()
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    return start, start + timedelta(days=1), d


def day_for_car(v: dict, start: datetime, end: datetime, now_utc: datetime) -> dict:
    """Everything one car did in [start, end): ribbon, sessions, battery line, routes."""
    vin = v["vin"]
    until = min(end, now_utc)
    states = state_rows(vin, "states", "state", start - timedelta(days=1), until)
    charging = state_rows(vin, "charging_states", "state", start - timedelta(days=1), until)
    # a car asleep since the evening before has no reading near midnight; the last one may be many hours back
    lookback = start - timedelta(hours=48)
    levels = series_between(vin, "drive_levels", "level", lookback, until, True)
    ds = drives(vin, None, start - timedelta(hours=6), end)
    cs = charges(vin, None, start - timedelta(hours=12), end)
    cap, k = vehicle_energy().get(vin, (None, None))
    k = rated_mile_kwh(rated_mile_years(), vin, start, k, TZ)
    ranges = series_between(vin, "drive_ranges", "range", lookback, until, True)
    temps = series_between(vin, "outside_temperatures", "outside_temperature", lookback, until, False)
    inside = series_between(vin, "cc_inside_temperatures", "inside_f", lookback, until, False)
    with db() as conn:
        modes = [
            (m["first_date"], m["last_date"], (m["sentry"], m["climate"]))
            for m in conn.execute(
                "SELECT first_date, last_date, sentry, climate FROM cc_modes WHERE vin = %s AND last_date >= %s AND first_date < %s ORDER BY first_date", (vin, start, until)
            ).fetchall()
        ]
        track = conn.execute("SELECT at, latitude, longitude, state, elevation FROM cc_positions WHERE vin = %s AND at >= %s AND at < %s ORDER BY at", (vin, start, end)).fetchall()
        matched = routematch.matched_lines(conn, vin, lookback, end, "simple")
    rows = day_sessions(ds, cs, states, levels, start, until, ranges, inside, modes, k) if until > start else []
    # one polyline per drive, so parked hours do not draw a line to nowhere
    routes = []
    for r in rows:
        if r["kind"] == "drive":
            a, b = r["start"], r["end"] or until
            inside_drive = [p for p in track if a <= p["at"] <= b]
            pts = routematch.choose([[p["latitude"], p["longitude"]] for p in inside_drive], routematch.line_for(matched, r["start"], r["end"]))
            if len(pts) >= 2:
                routes.append(pts)
            _series, r["climb_ft"], r["descent_ft"] = elevation_profile(inside_drive)
    return {
        "vin": vin,
        "name": v["name"],
        "live": v["live"],
        "ribbon": ribbon_segments(states, charging, start, until) if until > start else [],
        "since_charge": since_last_charge(vin, until, k),
        "places": time_at_places(rows, start, until),
        "rows": rows,
        "totals": day_totals(rows, cap, k),
        "routes": routes,
        "chart": chartdata.payload(
            start,
            end,
            {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": chartdata.thin([(t, lv) for t, lv in levels if t <= until], start, until)},
            chartdata.extras(ranges, temps, start, until, units_mod.ACTIVE),
            chartdata.lane(ribbon_segments(states, charging, start, until)) if until > start else [],
            [
                {
                    "a": r["start"].timestamp(),
                    "b": (r["end"] or until).timestamp(),
                    "kind": r["kind"],
                    "title": r["title"],
                    "figs": daypage.day_figs(r, units_mod.ACTIVE, current_settings().get("currency", "$")),
                }
                for r in rows
                if r["kind"] in ("drive", "charge") and r.get("start")
            ],
            ticks=chartdata.ticks(start, end, TZ),
        ),
    }


@site.get("/day", response_class=HTMLResponse)
def day_page(request: Request, date: str | None = None, show: str = "all"):
    """One day: every drive, charge and parked stretch in order, per car."""
    car = pick(request)
    show = show if show in ("all", "drives", "charges", "parked", "idles", "sleeps") else "all"
    start, end, d = day_bounds(date)
    now_utc = datetime.now(timezone.utc)
    cars = all_vehicles() if car.get("garage") else [{"vin": car["vin"], "name": car["name"], "live": car["live"]}]
    blocks = [day_for_car(v, start, end, now_utc) for v in cars]
    # a car that was not being logged that day (a car bought this year, in last year's calendar) has nothing to say; busiest car first
    blocks = [b for b in blocks if b["rows"] and (any(seg["label"] != "unknown" for seg in b["ribbon"]) or b["totals"]["drives"] or b["totals"]["charges"])]
    blocks.sort(key=lambda b: (b["totals"]["drives"] + b["totals"]["charges"], b["totals"]["miles"]), reverse=True)
    if show != "all":
        kind = {"drives": "drive", "charges": "charge", "parked": "parked", "idles": "parked", "sleeps": "parked"}[show]
        sub = {"idles": "idle", "sleeps": "sleep"}.get(show)
        for b in blocks:
            b["rows"] = [r for r in b["rows"] if r["kind"] == kind and (sub is None or r.get("sub") == sub)]
    return page(
        request,
        "day.html",
        car=car,
        day=d,
        blocks=blocks,
        show=show,
        today=datetime.now(TZ).date(),
        prev=d - timedelta(days=1),
        next=(d + timedelta(days=1)) if d < datetime.now(TZ).date() else None,
        active="timeline",
        month=d.strftime("%Y-%m"),
    )


KINDS = ("all", "drives", "charges", "software")


@site.get("/timeline", response_class=HTMLResponse)
def timeline_page(request: Request, month: str | None = None, kind: str = "all"):
    """Every car's drives, charges and software updates in one feed, newest first, by day."""
    car = pick(request)
    kind = kind if kind in KINDS else "all"
    vin = None if car.get("garage") else car["vin"]
    w = month_window(month, TZ, latest_dates(vin)["drive"])
    ds = drives(vin, None, w["start"], w["end"]) if kind in ("all", "drives") else []
    cs = charges(vin, None, w["start"], w["end"]) if kind in ("all", "charges") else []
    sw = software_events(vin, w["start"], w["end"]) if kind in ("all", "software") else []
    days = timeline_days(ds, cs, sw, TZ, parked=kind == "all")
    return page(
        request,
        "timeline.html",
        car=car,
        days=days,
        window=w,
        kind=kind,
        kinds=KINDS,
        active="timeline",
        counts={"drives": len(ds), "charges": len(cs), "software": len(sw)},
        thumbs=route_thumbs(ds, w["start"], w["end"]),
    )


def parked_ends(conn, vin: str, start: datetime, end: datetime) -> tuple[list | None, list | None]:
    """The car's last fix before a drive and its first after: where it was parked either side."""
    q = "SELECT latitude, longitude FROM cc_positions WHERE vin = %s AND at {} %s ORDER BY at {} LIMIT 1"
    before = conn.execute(q.format("<", "DESC"), (vin, start)).fetchone()
    after = conn.execute(q.format(">", "ASC"), (vin, end)).fetchone()
    return ([before["latitude"], before["longitude"]] if before else None), ([after["latitude"], after["longitude"]] if after else None)


def route_thumbs(trips: list[dict], start: datetime, end: datetime, width: int = 96, height: int = 48) -> dict[int, str]:
    """{trip id: inline SVG of its route} for one page of drives: one positions query a
    car for the window, the same rows the Calendar's month map fetches, drawn small."""
    out: dict[int, str] = {}
    by_vin: dict[str, list[dict]] = {}
    for t in trips:
        by_vin.setdefault(t["vin"], []).append(t)
    with db() as conn:
        for vin, mine in by_vin.items():
            # every fix, not just the driving ones, and a margin either side: a route starts and
            # ends at the parked fix around it (thumbs.anchored)
            rows = conn.execute(
                "SELECT at, latitude, longitude FROM cc_positions WHERE vin = %s AND at >= %s AND at < %s ORDER BY at", (vin, start - timedelta(hours=12), end + timedelta(hours=12))
            ).fetchall()
            drawn = routes_for(mine, [(r["at"], r["latitude"], r["longitude"]) for r in rows])
            lines = routematch.matched_lines(conn, vin, start - timedelta(hours=12), end + timedelta(hours=12), "thumb")
            for t in mine:
                pts = routematch.line_for(lines, t["start_date"], t.get("destination_date"))
                if pts:
                    drawn[t["id"]] = pts  # stored thinned at match time
            for trip_id, pts in drawn.items():
                out[trip_id] = route_svg(pts, width, height)
    return out


@site.get("/charges", response_class=HTMLResponse)
def charges_page(request: Request, month: str | None = None, from_: str | None = Query(None, alias="from"), to: str | None = None, sort: str | None = None, dir: str = "desc"):
    car = pick(request)
    w = list_window(month, from_, to, latest_dates(car["vin"])["charge"])
    qp = request.query_params
    hidden = qp.get("show") == "hidden"
    rows = (
        hidden_charges(car["vin"])
        if hidden
        else filter_charges(charges(car["vin"], None, w["start"], w["end"]), where=qp.get("place") or None, kind=qp.get("kind") or None, min_kwh=_float_or_none(qp.get("min")))
    )
    rows, key, desc = sort_rows(rows, sort, dir != "asc", CHARGE_SORTS)
    days = daypage.by_day(rows, TZ, "session_start_date") if key == "start" else None
    return page(
        request,
        "charges.html",
        car=car,
        charges=rows,
        days=days,
        window=w,
        sort=key,
        desc=desc,
        sort_base=sort_base(request),
        active="charges",
        named_places=named_places(),
        now_local=datetime.now(TZ),
        said=qp.get("said"),
        flt=dict(qp),
        hidden=hidden,
    )


def named_places() -> list[dict]:
    """The places somebody named, for the by-hand forms: a charge or a drive is placed by picking one."""
    return [p for p in places_detail() if not p["auto"]]


def place_named(name: str | None) -> dict | None:
    name = (name or "").strip().lower()
    return next((p for p in places_detail() if p["name"].lower() == name), None) if name else None


def last_odometer(vin: str) -> float | None:
    with db() as conn:
        r = conn.execute("SELECT destination_odometer AS o FROM trips WHERE vin = %s AND destination_odometer IS NOT NULL ORDER BY start_date DESC LIMIT 1", (vin,)).fetchone()
    return float(r["o"]) if r and r["o"] is not None else None


@site.post("/charges/manual")
async def charge_by_hand(request: Request):
    form = await request.form()
    row = manual.charge_from_form(form, TZ, datetime.now(timezone.utc), place_named(form.get("place")))
    back = f"/charges?vin={form.get('vin', '')}"
    if isinstance(row, str):
        return to_site(request, back + "&said=" + quote(row))
    with db() as conn:
        new = conn.execute(
            """INSERT INTO charging_sessions (vin, plug_connected_date, session_start_date, session_end_date, plug_disconnected_date, start_level, end_level,
                                             charging_type, real_charged, real_cost, session_position_latitude, session_position_longitude)
               VALUES (%(vin)s, %(session_start_date)s, %(session_start_date)s, %(session_end_date)s, %(session_end_date)s, %(start_level)s, %(end_level)s,
                       %(charging_type)s, %(real_charged)s, %(real_cost)s, %(latitude)s, %(longitude)s) RETURNING id""",
            row,
        ).fetchone()
        conn.execute("INSERT INTO cc_manual (kind, ref_id, note) VALUES ('charge', %s, %s)", (new["id"], (form.get("note") or "")[:200] or None))
        conn.commit()
    return to_site(request, f"/charges/{new['id']}?vin={row['vin']}")


@site.post("/charges/{session_id}/remove")
def charge_by_hand_remove(request: Request, session_id: int, vin: str = Form("")):
    """Only a hand-entered charge can be removed here; the loggers' rows are not the page's to delete."""
    with db() as conn:
        if conn.execute("SELECT 1 FROM cc_manual WHERE kind = 'charge' AND ref_id = %s", (session_id,)).fetchone():
            conn.execute("DELETE FROM cc_charge_facts WHERE session_id = %s", (session_id,))
            conn.execute("DELETE FROM charging_sessions WHERE id = %s", (session_id,))
            conn.execute("DELETE FROM cc_manual WHERE kind = 'charge' AND ref_id = %s", (session_id,))
            conn.commit()
    return RedirectResponse(f"{request.scope.get('root_path', '')}/charges" + (f"?vin={vin}" if vin else ""), status_code=303)


@site.post("/drives/manual")
async def drive_by_hand(request: Request):
    form = await request.form()
    vin = (form.get("vin") or "").strip()
    row = manual.drive_from_form(form, TZ, datetime.now(timezone.utc), place_named(form.get("from_place")), place_named(form.get("to_place")), last_odometer(vin) if vin else None)
    back = f"/drives?vin={vin}"
    if isinstance(row, str):
        return to_site(request, back + "&said=" + quote(row))
    with db() as conn:
        new = conn.execute(
            """INSERT INTO trips (vin, start_date, destination_date, start_odometer, destination_odometer,
                                  start_position_latitude, start_position_longitude, destination_position_latitude, destination_position_longitude)
               VALUES (%(vin)s, %(start_date)s, %(destination_date)s, %(start_odometer)s, %(destination_odometer)s,
                       %(start_latitude)s, %(start_longitude)s, %(end_latitude)s, %(end_longitude)s) RETURNING id""",
            row,
        ).fetchone()
        if row["level_start"] is not None:
            conn.execute(
                "INSERT INTO cc_trip_facts (trip_id, level_start, level_end, temp_f, range_start, range_end) VALUES (%s, %s, %s, -999, -999, -999) ON CONFLICT (trip_id) DO NOTHING",
                (new["id"], row["level_start"], row["level_end"]),
            )
        conn.execute("INSERT INTO cc_manual (kind, ref_id, note) VALUES ('drive', %s, %s)", (new["id"], (form.get("note") or "")[:200] or None))
        conn.commit()
    return to_site(request, f"/drives/{new['id']}?vin={vin}")


@site.post("/drives/{trip_id}/remove")
def drive_by_hand_remove(request: Request, trip_id: int, vin: str = Form("")):
    with db() as conn:
        if conn.execute("SELECT 1 FROM cc_manual WHERE kind = 'drive' AND ref_id = %s", (trip_id,)).fetchone():
            conn.execute("DELETE FROM cc_trip_facts WHERE trip_id = %s", (trip_id,))
            conn.execute("DELETE FROM cc_drive_tags WHERE trip_id = %s", (trip_id,))
            conn.execute("DELETE FROM trips WHERE id = %s", (trip_id,))
            conn.execute("DELETE FROM cc_manual WHERE kind = 'drive' AND ref_id = %s", (trip_id,))
            conn.commit()
    return RedirectResponse(f"{request.scope.get('root_path', '')}/drives" + (f"?vin={vin}" if vin else ""), status_code=303)


def sleep_block(vin: str, name: str, w: dict | None, since: datetime, until: datetime, first_day, last_day, now_utc: datetime, search: dict | None = None) -> dict:
    states = state_rows(vin, "states", "state", since, until)
    charging = state_rows(vin, "charging_states", "state", since, until)
    days = days_between(states, charging, first_day, last_day, TZ, now_utc)
    first_known = next((i for i, d in enumerate(days) if d["known_h"] > 0), max(len(days) - 1, 0))
    days = days[first_known:]  # nothing to say about days before logging began
    known = [d for d in days if d["known_h"] > 0]
    span = known if w else known[-7:]
    summary = {
        "asleep_pct": (sum(d["hours"]["sleeping"] for d in span) / sum(d["known_h"] for d in span) * 100) if span else None,
        "wakeups_per_day": (sum(d["wakeups"] for d in span) / len(span)) if span else None,
        "driving_h": sum(d["hours"]["driving"] for d in span),
        "charging_h": sum(d["hours"]["charging"] for d in span),
        "days": len(span),
    }
    levels = series_between(vin, "drive_levels", "level", since, until, True)
    idles = idle_periods(states, levels, charging, until)
    with db() as conn:
        sentry = [
            (r["first_date"], r["last_date"], bool(r["sentry"]))
            for r in conn.execute("SELECT first_date, last_date, sentry FROM cc_modes WHERE vin = %s AND last_date > %s AND first_date < %s ORDER BY first_date", (vin, since, until)).fetchall()
        ]
    idles = idle_details(idles, drives(vin, None, since - timedelta(days=2), until), sentry)
    idles = filter_idles(idles, **(search or {}))
    days = idles_by_day(list(reversed(days)), idles, TZ)
    return {"vin": vin, "name": name, "days": days, "summary": summary, "known": bool(known), "streak": current_streak(states, now_utc) if not w else None, "idles": idles}


@site.get("/sleep", response_class=HTMLResponse)
def sleep_page(request: Request, month: str | None = None, min_h: str | None = None, max_h: str | None = None, place: str | None = None, sentry: str | None = None):
    car = pick(request)
    now_utc = datetime.now(timezone.utc)
    search = {"min_h": _num(min_h), "max_h": _num(max_h), "place": (place or "")[:60], "sentry_only": sentry == "on"}
    if month:
        w = month_window(month, TZ)
        since, until = w["start"] - timedelta(days=1), min(w["end"], now_utc)
        first_day, last_day = w["start"].date(), (w["end"] - timedelta(days=1)).date()
    else:
        w = None
        since, until = now_utc - timedelta(days=15), now_utc
        first_day, last_day = (now_utc.astimezone(TZ) - timedelta(days=13)).date(), now_utc.astimezone(TZ).date()
    cars = all_vehicles() if car.get("garage") else [{"vin": car["vin"], "name": car["name"]}]
    blocks = [sleep_block(v["vin"], v["name"], w, since, until, first_day, last_day, now_utc, search) for v in cars]
    blocks = [b for b in blocks if b["known"]]
    idents = car_identities(cars)
    return page(
        request,
        "sleep.html",
        car=car,
        blocks=blocks,
        window=w,
        parked_rates=parked_rates([{**c, "make": idents[c["vin"]]["make"]} for c in cars]),
        search=search,
        searching=any(v for v in search.values()),
        active="sleep",
    )


def _num(text: str | None) -> float | None:
    try:
        v = float(text) if text not in (None, "") else None
    except ValueError:
        return None
    return v if v is not None and 0 <= v <= 10000 else None


def tire_series(vin: str, days: int = 30) -> dict[str, list[tuple[datetime, float]]]:
    """Four stepped series from the tire runs of the last `days`; empty when the car never reported."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with db() as conn:
        rows = conn.execute("SELECT first_date, last_date, fl, fr, rl, rr FROM cc_tires WHERE vin = %s AND last_date >= %s ORDER BY first_date", (vin, since)).fetchall()
    now = datetime.now(timezone.utc)
    return {w: run_points([(r["first_date"], r["last_date"], r[w]) for r in rows], since, now) for w in ("fl", "fr", "rl", "rr")}


def battery_block(v: dict) -> dict:
    vin = v["vin"]
    level = drive_series(vin, "drive_levels", "level", 30)
    rng = drive_series(vin, "drive_ranges", "range", 30)
    full = drive_series(vin, "drive_ranges_estimated_full", "range_estimated_full", 90)
    temp = vehicle_series(vin, "outside_temperatures", "outside_temperature", 30)
    u = units_mod.ACTIVE
    now = datetime.now(timezone.utc)
    dist = lambda s: [(t, u.distance(x)) for t, x in s if x is not None]
    chart = chartdata.series_chart(
        now - timedelta(days=30),
        now,
        {"key": "level", "label": "Battery", "unit": "%", "series": level, "scale": "pct"},
        [
            {"key": "range", "label": "Range", "unit": " " + u.labels["dist"], "series": dist(rng), "scale": "zero"},
            # -40 is the sensor's "no reading" in either scale, the one temperature where they agree
            {"key": "temp", "label": "Outside", "unit": u.labels["temp"], "series": [(t, u.temperature(x)) for t, x in temp if x is not None and x > NO_READING_C], "scale": "tight"},
        ],
        TZ,
    )
    full_chart = (
        chartdata.series_chart(now - timedelta(days=90), now, {"key": "full", "label": "Range at 100%", "unit": " " + u.labels["dist"], "series": dist(full), "scale": "steady"}, [], TZ)
        if full
        else None
    )
    tires = tire_series(vin)
    names = {"fl": "Front left", "fr": "Front right", "rl": "Rear left", "rr": "Rear right"}
    # one scale for the four, so a soft tire reads as lower than the others
    every = [v for pts in tires.values() for _, v in pts]
    lo, hi = (math.floor(min(every)) - 1, math.ceil(max(every)) + 1) if every else (None, None)
    tire_charts = {w: line_chart(pts, unit=" psi", tz=TZ, label=names[w], width=360, height=120, y_min=lo, y_max=hi) for w, pts in tires.items() if pts} if any(tires.values()) else {}
    firsts = {k: s[0][0] for k, s in (("level", level), ("range", rng), ("full", full), ("temp", temp)) if s}
    return {"car": live_vehicle(vin), "chart": chart, "full": full_chart, "tires": tire_charts, "firsts": firsts, "points": len(level)}


@site.get("/battery", response_class=HTMLResponse)
def battery_page(request: Request):
    car = pick(request)
    cars = all_vehicles() if car.get("garage") else [{"vin": car["vin"], "name": car["name"]}]
    blocks = [battery_block(v) for v in cars]
    packs = {c["vin"]: c["pack"] for c in capacities()}  # the same pack, and the same words for where it came from, as Cars and sources
    return page(request, "battery.html", car=car, blocks=blocks, packs=packs, active="battery")


def level_at(vin: str, when: datetime) -> float | None:
    with db() as conn:
        row = conn.execute(
            """SELECT l.level FROM drive_levels l JOIN drives d ON d.id = l.drive_id
                              WHERE d.vin = %s AND l.first_date <= %s ORDER BY l.first_date DESC LIMIT 1""",
            (vin, when),
        ).fetchone()
    return row["level"] if row else None


def series_between(vin: str, table: str, column: str, start: datetime, end: datetime, per_drive: bool) -> list[tuple[datetime, float]]:
    join = "JOIN drives d ON d.id = l.drive_id" if per_drive else ""
    owner = "d.vin" if per_drive else "l.vin"
    with db() as conn:
        rows = conn.execute(
            f"""SELECT l.first_date, l.last_date, l.{column} AS v FROM {table} l {join}
                                WHERE {owner} = %s AND l.last_date >= %s AND l.first_date <= %s AND l.{column} IS NOT NULL
                                ORDER BY l.first_date""",
            (vin, start, end),
        ).fetchall()
    # Each row holds one value from first_date to last_date: emit both ends so steps draw as steps.
    out: list[tuple[datetime, float]] = []
    for r in rows:
        out.append((max(r["first_date"], start), r["v"]))
        out.append((min(r["last_date"], end), r["v"]))
    return out


def runs_between(vin: str, table: str, columns: str, start: datetime, end: datetime, per_drive: bool) -> list[tuple]:
    """(first_date, last_date, value...) for every run touching a window.

    series_between flattens a run into its two endpoints, which is what a chart wants and
    exactly what a table of readings cannot use: the value and the stretch it covers have
    to stay together to be lined up against the samples."""
    join = "JOIN drives d ON d.id = l.drive_id" if per_drive else ""
    owner = "d.vin" if per_drive else "l.vin"
    with db() as conn:
        rows = conn.execute(
            f"""SELECT l.first_date, l.last_date, {columns} FROM {table} l {join}
                WHERE {owner} = %s AND l.last_date >= %s AND l.first_date <= %s ORDER BY l.first_date""",
            (vin, start, end),
        ).fetchall()
    return [tuple(r.values()) for r in rows]


# How many rows of readings a detail page will render. A twenty-second sample rate makes
# a six-hour drive over a thousand fixes, and nobody reads the thousandth; past this the
# table is thinned evenly and says by how much.
SAMPLE_ROWS = 400
NO_READING_C = -39.5  # the -40 sentinel with room for rounding


@site.get("/drives/{trip_id}", response_class=HTMLResponse)
def drive_detail(request: Request, trip_id: int):
    trip = drive(None, trip_id)
    if trip is None:
        raise HTTPException(404, "No such drive.")
    car = live_vehicle(trip["vin"])
    with db() as conn:
        pos = conn.execute(
            """SELECT start_position_latitude, start_position_longitude, destination_position_latitude, destination_position_longitude
                              FROM trips WHERE id = %s""",
            (trip_id,),
        ).fetchone()
    end = trip["destination_date"] or datetime.now(timezone.utc)
    lvl_start = trip["level_start"] if trip["level_start"] is not None else level_at(car["vin"], trip["start_date"])
    lvl_end = trip["level_end"] if trip["level_end"] is not None else level_at(car["vin"], end)
    _cap, k = vehicle_energy().get(trip["vin"], (None, None))
    k = rated_mile_kwh(rated_mile_years(), trip["vin"], trip["start_date"], k, TZ)
    stats = trip_stats(trip["miles"], trip["duration"], lvl_start, lvl_end, car["capacity_kwh"], trip.get("range_start"), trip.get("range_end"), k)
    stats["max_mph"] = trip.get("max_mph")
    stats["kwh_basis"] = (
        "rated range used × this car's measured kWh per rated mile" if (k and trip.get("range_start") is not None and trip.get("range_end") is not None) else "capacity × SoC drop, an estimate"
    )
    levels = series_between(car["vin"], "drive_levels", "level", trip["start_date"], end, True)
    with db() as conn:
        track = conn.execute(
            "SELECT at, latitude, longitude, elevation, speed, level, odometer, power_kw FROM cc_positions WHERE vin = %s AND at BETWEEN %s AND %s ORDER BY at", (car["vin"], trip["start_date"], end)
        ).fetchall()
        readings = anchored([[r["latitude"], r["longitude"]] for r in track], *parked_ends(conn, car["vin"], trip["start_date"], end))
        matched = routematch.matched_line(conn, car["vin"], trip["start_date"], trip["destination_date"], "full")
    route = routematch.choose(readings, matched)
    elev, stats["climb_ft"], stats["descent_ft"] = elevation_profile(track)
    speeds = [(r["at"], r["speed"]) for r in track if r.get("speed") is not None]
    # Power is the car's own reading, negative while regenerating, so no y_min: the axis
    # has to cross zero or the braking half of the drive is cropped off the bottom.
    watts = [(r["at"], r["power_kw"]) for r in track if r.get("power_kw") is not None]
    if watts:
        # Peaks, not an integral. Integrating these looked easy and was wrong: a
        # once-a-minute sample of a signal that spikes on every acceleration came out
        # 23 % above the energy the rated range says, on a drive where the rated range
        # and the plain SoC drop agreed with each other. Peak draw and peak recovery are
        # readings rather than reconstructions, and are as trustworthy as the top speed
        # beside them.
        stats["peak_draw_kw"] = max(kw for _t, kw in watts)
        recovered = min(kw for _t, kw in watts)
        stats["peak_regen_kw"] = -recovered if recovered < 0 else None
        stats["regen_minutes"] = sum((t1 - t0).total_seconds() / 60 for (t0, kw0), (t1, _k) in zip(watts, watts[1:]) if kw0 < 0 and 0 < (t1 - t0).total_seconds() < 900)
    # How much of this drive was ground the car had never covered. Counted the same way
    # on both sides — Postgres rounding the coordinates, not Python's — because the
    # cells it is compared against were grouped in the database, and a half-step
    # disagreement between the two would let "new" come out above "covered".
    with db() as conn:
        covered = conn.execute(
            """SELECT COUNT(*) AS n FROM (SELECT DISTINCT ROUND(latitude::numeric, 3), ROUND(longitude::numeric, 3)
                                          FROM cc_positions WHERE vin = %s AND at BETWEEN %s AND %s) c""",
            (car["vin"], trip["start_date"], end),
        ).fetchone()["n"]
    if covered:
        stats["cells_covered"] = covered
        stats["cells_new"] = new_ground(car["vin"], trip["start_date"], end)
    rated_runs = runs_between(car["vin"], "drive_ranges", "l.range", trip["start_date"], end, True)
    outside_runs = runs_between(car["vin"], "outside_temperatures", "l.outside_temperature", trip["start_date"], end, False)
    readings = drive_samples(
        track,
        rated=rated_runs,
        outside=outside_runs,
        inside=runs_between(car["vin"], "cc_inside_temperatures", "l.inside_f", trip["start_date"], end, False),
    )
    # Cabin temperature at either end of the drive, off the readings already lined up
    # for the table below rather than another pair of lookups.
    cabin = [r["inside"] for r in readings if r.get("inside") is not None]
    if cabin:
        stats["inside_start"], stats["inside_end"] = cabin[0], cabin[-1]
    outside = [r["outside"] for r in readings if r.get("outside") is not None and r["outside"] > chartdata.NO_READING]
    if outside:
        stats["outside_start"], stats["outside_end"] = outside[0], outside[-1]
    samples, every = thinned(readings, SAMPLE_ROWS)
    cur = current_settings().get("currency", "$")
    navigated = tesla_nav.for_trip([r for r in nav_rows() if r["vin"] == trip["vin"]], trip["start_date"], trip["destination_date"])
    event = {
        "a": trip["start_date"].timestamp(),
        "b": end.timestamp(),
        "kind": "drive",
        "title": f"{trip['from']} → {trip['to']}",
        "figs": daypage.day_figs(daypage.as_drive(trip), units_mod.ACTIVE, cur),
    }
    chart = chartdata.drive_chart(trip["start_date"], end, levels, speeds, elev, watts, chartdata.run_series(rated_runs), chartdata.run_series(outside_runs), event, units_mod.ACTIVE, TZ)
    return page(
        request,
        "drive.html",
        samples=samples,
        every=every,
        fixes=len(track),
        car=car,
        trip=trip,
        hidden=is_hidden("drive", trip["id"]),
        pos=pos,
        stats=stats,
        lvl_start=lvl_start,
        lvl_end=lvl_end,
        chart=chart,
        route=route,
        matched=route is matched,
        readings=[[r["latitude"], r["longitude"]] for r in track] if route is matched else [],
        navigated=tesla_nav.clean_name(navigated["dest_name"]) if navigated else None,
        tags=tag_names(),
        active="drives",
    )


def detail_cost(session: dict, price_now, home=None) -> None:
    """The cost a charge's page shows: the stored one, as every list shows it, with the
    plan's split from the stored facts. Only a charge with no stored price yet (one still
    going) at home is priced here, on the plan, by `price_now()` (session_cost over its
    power profile). A price typed in, billed, or set on the place is never re-worked."""
    if session.get("cost_source") == "plan" and session.get("period_kwh"):
        session["cost_parts"] = dict(session["period_kwh"])
        return
    if session.get("by_hand") or session.get("cost") is not None or session.get("cost_source") not in (None, "none"):
        return
    home = home or is_home
    if not (session.get("at_home") or home(session.get("session_position_latitude"), session.get("session_position_longitude"))):
        return
    priced = price_now()
    if priced:
        parts = {priced["labels"][k].lower(): v for k, v in priced["kwh"].items()}
        session["cost"], session["rate_plan"], session["cost_parts"], session["cost_source"] = priced["cost"], priced["plan"], parts, "plan"


@site.get("/charges/{session_id}", response_class=HTMLResponse)
def charge_detail(request: Request, session_id: int):
    session = charge(None, session_id)
    if session is None:
        raise HTTPException(404, "No such charge.")
    car = live_vehicle(session["vin"])
    start = session["session_start_date"] or session["plug_connected_date"]
    end = session["session_end_date"] or session["plug_disconnected_date"]
    with db() as conn:
        powers = conn.execute(
            "SELECT first_date, last_date, power FROM charging_powers WHERE vin = %s AND last_date >= %s AND first_date <= %s ORDER BY first_date",
            (car["vin"], start, end or datetime.now(timezone.utc)),
        ).fetchall()
    window_end = end or datetime.now(timezone.utc)
    with db() as conn:
        el = (
            conn.execute(
                "SELECT first_date, last_date, volts, amps FROM cc_charging_electrics WHERE vin = %s AND last_date >= %s AND first_date <= %s ORDER BY first_date", (car["vin"], start, window_end)
            ).fetchall()
            if conn.execute("SELECT to_regclass('cc_charging_electrics') AS t").fetchone()["t"]
            else []
        )
    electrics = [(r["first_date"], r["last_date"], r["volts"], r["amps"]) for r in el]
    # Every energy figure on this page comes off one series, so the stand-in for a car
    # that stopped reporting charger_power is applied once, here, not per figure.
    kws = with_measured_power([(r["first_date"], r["last_date"], r["power"]) for r in powers], electrics)
    stats = charge_stats(kws, start, end, session["start_level"], session["end_level"])
    # The car's own meter is the total, as it is on every list and total that reads the
    # stored fact; the power profile says when the energy went in, not how much. They are
    # not the same number — a charger delivers a few percent more than the battery takes
    # — and this page was the only one deriving its own, so it could say 44.9 kWh under a
    # list that said 42.6.
    stats["kwh_profile"] = stats.get("kwh")  # what the power readings add up to: the charger's side
    if session.get("kwh"):
        stats["kwh"] = session["kwh"]
        gained = stats.get("gained_pct")
        stats["kwh_per_100"] = (stats["kwh"] / gained * 100) if gained and gained > 0 else None
        # Averaged over the time the charger was actually delivering, not over the
        # session's wall clock. A car that sits plugged in at 0 kW for two hours and
        # then charges for one did not average a third of its charging power, and with
        # the total coming from the meter and the peak from the profile, a wall-clock
        # average could come out above the peak it is supposed to sit under.
        stop = end or datetime.now(timezone.utc)
        live = sum((min(last, stop) - max(first, start)).total_seconds() for first, last, kw in kws if kw and kw > 0 and last > start and first < stop)
        hours = (live / 3600) or ((stop - start).total_seconds() / 3600)
        stats["avg_kw"] = (stats["kwh"] / hours) if hours > 0 else None
    stats.update(electrics_stats(electrics, start, window_end))
    detail_cost(session, lambda: session_cost(kws, start, window_end, TZ))
    level_runs = runs_between(car["vin"], "drive_levels", "l.level", start, window_end, True)
    rated_runs = runs_between(car["vin"], "drive_ranges", "l.range", start, window_end, True)
    cur = current_settings().get("currency", "$")
    event = {"a": start.timestamp(), "b": window_end.timestamp(), "kind": "charge", "title": session["where"], "figs": daypage.day_figs(daypage.as_charge(session), units_mod.ACTIVE, cur)}
    chart = chartdata.charge_chart(
        start,
        window_end,
        run_points(kws, start, window_end),
        chartdata.run_series(level_runs),
        chartdata.run_series(rated_runs),
        [(f, v) for f, _l, v, _a in electrics if v is not None],
        [(f, a) for f, _l, _v, a in electrics if a is not None],
        event,
        units_mod.ACTIVE,
        TZ,
    )
    readings = charge_samples(
        start,
        window_end,
        kws,
        level=level_runs,
        rated=rated_runs,
        volts=[(f, l, v) for f, l, v, _a in electrics],
        amps=[(f, l, a) for f, l, _v, a in electrics],
    )
    samples, every = thinned(readings, SAMPLE_ROWS)
    bill = None
    if session.get("cost_source") == "billed" and session.get("session_start_date"):
        with db() as conn:
            bill = billed.invoice_for(conn, session["vin"], session["session_start_date"])
    return page(
        request,
        "charge.html",
        car=car,
        session=session,
        bill=bill,
        hidden=is_hidden("charge", session["id"]),
        stats=stats,
        chart=chart,
        samples=samples,
        every=every,
        readings=len(readings),
        active="charges",
    )


@site.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.headers.get("accept", "").startswith("application/json") or site_path(request).startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    try:
        car = pick(request)
    except HTTPException:
        car = {"name": "Car Command", "model": "", "updated_age": "unknown"}
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "root": request.scope.get("root_path", ""),
            "now": datetime.now(TZ),
            "vehicles": [],
            "q": "",
            "car": car,
            "status": exc.status_code,
            "detail": exc.detail,
            "active": "",
            "css_v": CSS_VERSION,
            "js_v": JS_VERSION,
            "csrf": csrf_for(request),
            "auth_on": AUTH["enabled"],
        },
        status_code=exc.status_code,
    )


def _place_view(p: dict, cells: dict, cur: str) -> dict:
    """A place as a row or the detail page shows it: home, its town, its price in words."""
    p = {**with_street([p], cells)[0], "home": is_home_place(p)}
    p["city"] = placespage.city_of(p)
    p["price_text"] = placeprice.describe(p.get("price"), p["home"], cur) if (p.get("price") or p["home"]) else ""
    return p


@site.get("/places", response_class=HTMLResponse)
def places_page(request: Request, merge: int | None = None):
    """One map of every place, the named ones as their icons and the found ones as dots, beside
    a list: the found ones first, to name one after another, then the named. The page itself
    carries only its frame and the counts; the places come from /places/index.json once it
    has drawn, and the map starts after the arrival. `merge`: open with that place picking
    the places to take in (a place's own page links here)."""
    car = pick(request)  # the switcher says All cars when it is every car
    every = places_detail()
    found = sum(1 for p in every if p["auto"])
    return page(request, "places.html", car=car, named=len(every) - found, found=found, merge=merge, active="places")


@site.get("/places/index.json")
def places_index():
    """Every place for the Places page's list and map, in the page's order (placespage.index_rows),
    with the paths of the icons they show. Read from the cached stays and navigation names."""
    every, cells = places_detail(), geocells()
    rows = placespage.index_rows(
        every,
        place_stays(),
        place_nav(),
        lambda p: placespage.city_of(with_street([p], cells)[0]),
        lambda p: placeicons.resolve_icon(p["name"], p.get("icon")),
    )
    icons = {k: placeicons.PLACE_ICONS[k] for k in sorted({r.get("ic") for r in rows} - {None})}
    return JSONResponse({"places": rows, "icons": icons}, headers={"Cache-Control": "no-store"})


@site.get("/places/card", response_class=HTMLResponse)
def place_card_markup(request: Request):
    """The naming card's markup (_placecard.html), fetched once a page, the first time a place
    name is tapped: no page carries it until then."""
    html = templates.get_template("_placecard.html").render(root=request.scope.get("root_path", ""), cur=current_settings().get("currency", "$"))
    return HTMLResponse(html, headers={"Cache-Control": "private, max-age=300"})


def wants_json(request: Request) -> bool:
    """The naming card asks for JSON; a plain form gets its redirect as before."""
    return "application/json" in request.headers.get("accept", "")


def place_payload(place_id: int) -> dict | None:
    """A place as the naming card and the Places page read it after a save: what it shows (the
    name with its icon, as every page draws it), its price, and every circle that answers as
    it, so each name on the page inside one can change in place."""
    p = next((x for x in places_detail() if x["id"] == place_id), None)
    if p is None:
        return None
    home = is_home_place(p)
    return {
        "id": p["id"],
        "name": p["name"],
        "auto": bool(p["auto"]),
        "icon": placeicons.resolve_icon(p["name"], p.get("icon")),
        "stored_icon": p.get("icon"),  # None: nothing chosen, the icon shown is the name's guess
        "latitude": p["latitude"],
        "longitude": p["longitude"],
        "radius_m": p["radius_m"],
        "visits": placespage.visits(p, place_stays()),
        "price": p.get("price"),
        "home": home,
        "price_text": placeprice.describe(p.get("price"), home, current_settings().get("currency", "$")),
        "circles": placespage.circles(p),
        "ids": [p["id"], *(c["id"] for c in p.get("parts") or [])],
        "label": str(place_markup(p["name"], place_icon_map())),
    }


def _saved(request: Request, place_id: int | None, target: str):
    """After a change: the place as JSON for the naming card, else the page's redirect."""
    if wants_json(request):
        got = place_payload(place_id) if place_id is not None else None
        return JSONResponse({"place": got}, status_code=200 if got else 404)
    return to_site(request, target)


def _offer_merge(request: Request, form, name: str | None, lat: float, lon: float, skip: int | None, named: bool = False) -> JSONResponse | None:
    """Naming a place with the very name of one nearby (500 m) is offered as a merge instead:
    a 409 that names it, and nothing saved. The card asks once; `separate=1` (Keep both) or a
    plain form skips the question. `named`: the place being renamed is one somebody named, so
    the offer says `confirm` and the card takes it through Merge's confirm sheet, never in one
    tap; a found place (or a bare stop) merges at once."""
    if not name or not wants_json(request) or form.get("separate") == "1":
        return None
    same = placespage.same_name_near(name, lat, lon, places_detail(), skip=skip)
    return JSONResponse({"offer": {**same, "confirm": named}}, status_code=409) if same else None


@site.get("/icons/places.json")
def place_icons_file():
    """Every place icon, for the picker's search and its More: key -> [title, SVG paths,
    search words]. Asked only when the picker needs more than the common icons it carries;
    its URL carries the file's hash (PLACE_ICONS_V), so a browser keeps it until it changes."""
    return Response(placeicons.ICONS_BYTES, media_type="application/json", headers={"Cache-Control": "private, max-age=31536000, immutable"})


def _maps_on() -> bool:
    """Whether the local maps add-on is configured: the address field offers suggestions."""
    return bool(MAPS.base)


@site.get("/places/new", response_class=HTMLResponse)
def place_new(request: Request, lat: float | None = None, lon: float | None = None):
    """Add a place: a map to put the pin on (where it was asked for, else where the car is,
    else among the places already named) and the same fields a place's page has."""
    car = single(request)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        named = [p for p in places_detail() if not p["auto"]]
        if car.get("lat") is not None and car.get("lon") is not None:
            lat, lon = car["lat"], car["lon"]
        elif named:
            lat, lon = named[0]["latitude"], named[0]["longitude"]
        else:
            lat, lon = None, None
    p = {"id": None, "name": "", "icon": None, "latitude": lat, "longitude": lon, "radius_m": 150.0, "auto": False, "price": None, "address": None, "user_address": None, "parts": []}
    return page(request, "place.html", car=car, p={**p, "home": False, "city": "", "street": None}, st=None, choices=[], maps_on=_maps_on(), new=True, active="places")


@site.get("/places/{place_id:int}", response_class=HTMLResponse)
def place_detail(request: Request, place_id: int):
    """One place: a map to drag it or resize it on, its name, icon, price and address, what
    stopping there has looked like, Merge with… and Remove."""
    car = pick(request)
    every = places_detail()
    p = next((x for x in every if x["id"] == place_id), None)
    if p is None:
        raise HTTPException(404, "No such place.")
    view = {**_place_view(p, geocells(), current_settings().get("currency", "$")), "visits": placespage.visits(p, place_stays())}
    choices = [{**c, "far": placespage.distance_words(c["m"])} for c in placespage.merge_choices(p, every)]
    nav = place_nav().get(place_id) if p["auto"] else None  # never offered over a name the owner gave
    return page(request, "place.html", car=car, p=view, st=place_stays().get(place_id), choices=choices, maps_on=_maps_on(), new=False, nav=nav, active="places")


AROUND_TRIPS = 12  # the latest drives into or out of a place whose traces its map draws
AROUND_MINUTES = 10  # of each drive, at the place's end


def place_traces(p: dict) -> list:
    """The car's own positions around a place: the last minutes of the latest drives that
    ended there and the first of those that left, cut to 800 m around it. From the trips
    table (a few thousand rows) and cc_positions by car and time (its index)."""
    lat, lon, r = float(p["latitude"]), float(p["longitude"]), max(float(p["radius_m"]) * 1.5, 150.0)
    dlat = r / 111_320.0
    dlon = r / max(1.0, 111_320.0 * math.cos(math.radians(lat)))
    box = (lat - dlat, lat + dlat, lon - dlon, lon + dlon)
    near = timedelta(minutes=AROUND_MINUTES)
    runs = []
    with db() as conn:
        trips = conn.execute(
            """SELECT vin, start_date, destination_date,
                      destination_position_latitude BETWEEN %s AND %s AND destination_position_longitude BETWEEN %s AND %s AS arrived
               FROM trips WHERE destination_date IS NOT NULL AND (
                   destination_position_latitude BETWEEN %s AND %s AND destination_position_longitude BETWEEN %s AND %s
                   OR start_position_latitude BETWEEN %s AND %s AND start_position_longitude BETWEEN %s AND %s)
               ORDER BY start_date DESC LIMIT %s""",
            (*box, *box, *box, AROUND_TRIPS),
        ).fetchall()
        for t in trips:
            line = routematch.matched_line(conn, t["vin"], t["start_date"], t["destination_date"], "simple")
            if line:
                runs.append([(la, lo) for la, lo in line])  # the whole drive on the roads; traces_near cuts it to the place
                continue
            a, b = (t["destination_date"] - near, t["destination_date"]) if t["arrived"] else (t["start_date"], t["start_date"] + near)
            rows = conn.execute("SELECT latitude, longitude FROM cc_positions WHERE vin = %s AND at BETWEEN %s AND %s ORDER BY at", (t["vin"], a, b)).fetchall()
            runs.append([(row["latitude"], row["longitude"]) for row in rows])
    return placespage.traces_near(lat, lon, runs)


@site.get("/places/{place_id:int}/around")
def place_around(place_id: int):
    """What the site holds around a place, for its map without a street background: the
    car's own drive traces there. Asked by the page after it has drawn, never waited on."""
    p = next((x for x in places_detail() if x["id"] == place_id), None)
    if p is None:
        raise HTTPException(404, "No such place.")
    return {"traces": place_traces(p)}


def checked_point(lat, lon, radius=None) -> tuple[float, float, float | None]:
    """A posted position (and size) as numbers, or a 400: a NaN or an infinity stored in
    cc_places breaks every page that reads the places."""
    try:
        lat, lon = float(lat), float(lon)
        radius = None if radius in (None, "") else float(radius)
    except (TypeError, ValueError):
        raise HTTPException(400, "Not a coordinate.") from None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Not a coordinate.")
    if radius is not None and not math.isfinite(radius):
        raise HTTPException(400, "Not a size.")
    return lat, lon, radius


def _posted_where(form) -> tuple[float | None, float | None, float | None] | None:
    """What the owner moved on a place's page: (latitude, longitude, radius_m), None in any
    they did not. The page marks them itself (moved: the pin was dragged or put on a picked
    address; resized: the slider was moved), so a Save of the name alone moves nothing, and
    a size the slider never touched (one over its range, say) is never clamped."""
    pin, size = form.get("moved") == "1", form.get("resized") == "1"
    if not (pin or size):
        return None
    lat, lon, radius = checked_point(form.get("latitude"), form.get("longitude"), form.get("radius_m") if size else None)
    if size and radius is None:
        raise HTTPException(400, "Not a size.")
    return (lat if pin else None, lon if pin else None, clamp_radius(radius) if size else None)


@site.post("/places/{place_id:int}/edit")
async def place_edit(request: Request, place_id: int):
    """Everything on a place's page in one save: name, icon, price, its own address, where
    it is and how big. What changes which charges are this place's, or what they cost, is
    worked out again in the background (rename_place)."""
    form = await request.form()
    name = " ".join(str(form.get("name") or "").split())[:60] or None
    icon = _valid_icon(form.get("icon"))
    price = _posted_price(form)
    own = posted_address(str(form.get("address") or ""), str(form.get("address_lookup") or ""))
    where = _posted_where(form)
    if wants_json(request):
        before = await asyncio.to_thread(lambda: next((x for x in places_detail() if x["id"] == place_id), None))
        if before is None:
            raise HTTPException(404, "No such place.")
        if name and name != before["name"]:
            offer = await asyncio.to_thread(_offer_merge, request, form, name, before["latitude"], before["longitude"], place_id, not before["auto"])
            if offer:
                return offer
    await asyncio.to_thread(lambda: rename_place(place_id, name, icon if "icon" in form else KEEP, price, own, where))
    return await asyncio.to_thread(_saved, request, place_id, f"/places/{place_id}?saved=1")


@site.post("/places/{place_id:int}/merge")
async def place_merge(request: Request, place_id: int, other: list[int] = Form(...)):
    """Take the others (one `other` or several) into this place: their stops, visits and
    charges are this place's now, and their charges are priced again in the background."""
    if not await asyncio.to_thread(merge_places, place_id, other):
        raise HTTPException(400, "Those cannot be merged.")
    return await asyncio.to_thread(_saved, request, place_id, f"/places/{place_id}?merged=1")


@site.get("/places/{place_id:int}/merge-preview")
def place_merge_preview(place_id: int, other: list[int] = Query(...)):
    """What a merge would move, for its confirm sheet: each other place's visits, how many
    charges are labelled with it now and its price, beside the kept place's name, icon and
    price, which they all take. Reads; changes nothing."""
    every = places_detail()
    by_id = {p["id"]: p for p in every}
    if place_id not in by_id:
        raise HTTPException(404, "No such place.")
    cur = current_settings().get("currency", "$")
    index, stays = places(), place_stays()
    out = []
    with db() as conn:
        for oid in dict.fromkeys(other):
            p = by_id.get(oid)
            if p is None or oid == place_id:
                continue
            n = sum(len(place_sessions(conn, {"id": oid, "latitude": c[0], "longitude": c[1], "radius_m": c[2]}, index)) for c in placespage.circles(p))
            price = placeprice.describe(p.get("price"), is_home_place(p), cur)
            out.append({"id": oid, "name": p["name"], "auto": bool(p["auto"]), "visits": placespage.visits(p, stays), "charges": n, "price_text": price})
    return {"keep": place_payload(place_id), "others": out}


@site.get("/places/search")
def places_search(q: str = "", lat: float | None = None, lon: float | None = None):
    """Suggestions for a typed address, from the local maps add-on's own index only: the
    text is never sent to a public geocoder. Without the add-on there are none, and the
    page says so."""
    if not _maps_on():
        return {"results": [], "available": False}
    if lat is not None and lon is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    raw = MAPS.search(q[:100], lat, lon) if len(q.strip()) >= 2 else {"results": [], "built": True}
    return {"results": ((raw or {}).get("results") or [])[:8], "available": raw is not None and raw.get("built", True) is not False}


def _valid_icon(icon: str | None) -> str | None:
    """The posted icon, or None for the DB column (unset — the place defaults). Anything
    that isn't a real icon key, "none" or blank is a bad request, not a silently ignored
    one: a typo'd value would otherwise save as a plain string nothing ever draws."""
    icon = (icon or "").strip()
    if not icon:
        return None
    if icon == "none" or icon in ICON_KEYS:
        return icon
    raise HTTPException(400, "Not a recognised icon.")


@site.post("/places")
def places_add(
    request: Request,
    name: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    radius_m: float = Form(150),
    icon: str | None = Form(None),
    back: str = Form(""),
):
    name = name.strip()[:40]
    icon = _valid_icon(icon)
    latitude, longitude, radius_m = checked_point(latitude, longitude, radius_m)
    if name:
        add_place(name, latitude, longitude, clamp_radius(radius_m), icon)
    target = back if back and safe_next(back) == back else "/places"
    return to_site(request, target)


@site.post("/places/{place_id}/delete")
def places_delete(request: Request, place_id: int):
    delete_place(place_id)
    return RedirectResponse(f"{request.scope.get('root_path', '')}/places", status_code=303)


def _posted_price(form) -> tuple[bool, dict | None]:
    """(whether the form carried a price, the price); a bad one is a 400 that says why."""
    try:
        return placeprice.from_form(form)
    except placeprice.BadPrice as err:
        raise HTTPException(400, str(err)) from None


@site.post("/places/{place_id}/rename")
async def places_rename(request: Request, place_id: int, name: str = Form(...), icon: str | None = Form(None), address: str = Form(""), address_lookup: str = Form("")):
    name = name.strip()[:40]
    icon = _valid_icon(icon)
    price = _posted_price(await request.form())
    if name:
        await asyncio.to_thread(lambda: rename_place(place_id, name, icon, price, posted_address(address, address_lookup)))
    return RedirectResponse(f"{request.scope.get('root_path', '')}/places", status_code=303)


@site.post("/places/{place_id:int}/use-name")
async def places_use_name(request: Request, place_id: int, pick: int = Form(0), back: str = Form("")):
    """Use, on a found place's suggested name: names it, icon and the rest kept. The name is
    the place's own suggestion (place_nav), `pick` 0 for it and 1 on for its alternates;
    nothing typed is taken. Only a place still found (auto): a name the owner gave is never
    written over from here."""
    nav = place_nav().get(place_id)
    names = [nav["name"], *nav.get("alternates", [])] if nav else []
    name = names[pick].strip()[:40] if 0 <= pick < len(names) else ""

    def use():
        with db() as conn:
            row = place_row(conn, place_id)
        if row and row["auto"] and name:
            rename_place(place_id, name, KEEP)

    await asyncio.to_thread(use)
    target = back if back and safe_next(back) == back else "/places"
    return to_site(request, target)


@site.get("/places/candidates")
def places_candidates(lat: float, lon: float, place: int | None = None):
    """The naming card's own lookup: never cached, called only when a user opens it. A maps
    call that fails or times out (MapsClient answers None on any failure) just means no
    suggestions, not a broken card. `place`: the card was opened on that place (the Places
    page's list), which is then the place here even where another one overlaps it."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Not a coordinate.")
    raw = MAPS.candidates(lat, lon)
    every = places_detail()
    # A place already here brings the address it is known by: the owner's own, and the one
    # stored or cached for the spot, which stands in when maps has none.
    here = next((p for p in every if p["id"] == place), None) if place is not None else None
    here = here or place_for(lat, lon, places()) or {}
    # The cars' own navigation first: what the driver typed in the car, for a spot nobody named
    nav = None
    if here.get("auto"):  # a found place: the name its row on the Places page suggests
        nav = place_nav().get(here["id"])
    if nav is None and (not here or here.get("auto")):
        nav = tesla_nav.point_suggestion(lat, lon, nav_rows(), car_names(), max(float(here.get("radius_m") or 0), tesla_nav.ARRIVED_M))
    return {
        "nav": nav,
        "candidates": (raw or {}).get("candidates") or [],
        "address": (raw or {}).get("address"),
        "available": raw is not None,
        "user_address": here.get("user_address"),
        "place_address": here.get("address") or rough_label(lat, lon, geocells()),
        "place": place_payload(here["id"]) if here else None,
        "nearby": placespage.nearby(lat, lon, every, skip=here.get("id")),
    }


@site.post("/places/pin")
@site.post("/places/new")
async def places_pin(
    request: Request,
    lat: float = Form(...),
    lon: float = Form(...),
    name: str = Form(""),
    icon: str | None = Form(None),
    back: str = Form(""),
    address: str = Form(""),
    address_lookup: str = Form(""),
    radius_m: float | None = Form(None),
    merge_into: int | None = Form(None),
):
    """Pins a spot for good, from the picker (/places/pin) or the add page (/places/new,
    which always adds a place and never renames the one the pin is inside): a nearby place's name or the driver's own
    words, or with the name left blank, the address. Renaming an existing auto place through
    here keeps its id and visits. The picker's price line comes with it; "Not set" leaves a
    place's price as it was. An address typed over the looked-up one is kept as the place's
    own (places.posted_address): stored, never looked up or sent anywhere.

    From the naming card (JSON): a name that is already a place's nearby is offered as a
    merge (a 409, nothing saved); `merge_into` takes the stop into that place (pin_into),
    which keeps its own name, icon and price. Neither applies to the Add page."""
    icon = _valid_icon(icon)
    lat, lon, radius_m = checked_point(lat, lon, radius_m)
    form = await request.form()
    given, price = _posted_price(form)
    own = posted_address(address, address_lookup)
    name = name.strip() or " ".join(address.split()) or " ".join(address_lookup.split())
    adding = request.url.path.endswith("/places/new")  # the Add page: always a new place
    radius = clamp_radius(radius_m) if radius_m else 120.0
    if merge_into is not None and not adding:
        if not await asyncio.to_thread(pin_into, lat, lon, merge_into, radius):
            raise HTTPException(400, "That place cannot take this stop.")
        return await asyncio.to_thread(_saved, request, merge_into, f"/places/{merge_into}")
    if not adding and wants_json(request):
        hit = await asyncio.to_thread(lambda: place_for(lat, lon, places()))
        offer = await asyncio.to_thread(_offer_merge, request, form, name, lat, lon, hit["id"] if hit else None)
        if offer:
            return offer
    pinned = None
    if name:

        def pin():
            with db() as conn:
                return pin_place(conn, lat, lon, name, radius_m=radius, icon=icon, price=price if given and price else None, address=own, rename=not adding)

        pinned = await asyncio.to_thread(pin)
        forget_places()
    target = back if back and safe_next(back) == back else (f"/places/{pinned}" if pinned else "/places")
    return await asyncio.to_thread(_saved, request, pinned, target)


BY_HAND = "by-hand"


def software_rows(vin: str) -> list[dict]:
    """Every version the car has run, when it arrived, how long it stayed, and the odometer at install."""
    with db() as conn:
        rows = conn.execute(
            """SELECT id, version, seen_at, import_id FROM (SELECT id, version, seen_at, import_id, LAG(version) OVER (ORDER BY seen_at) AS prev
                                                           FROM cc_software WHERE vin = %s) x WHERE prev IS DISTINCT FROM version ORDER BY seen_at""",
            (vin,),
        ).fetchall()
        out = []
        for i, r in enumerate(rows):
            nxt = rows[i + 1]["seen_at"] if i + 1 < len(rows) else None
            odo = conn.execute("SELECT odometer FROM cc_positions WHERE vin = %s AND at >= %s AND odometer IS NOT NULL ORDER BY at LIMIT 1", (vin, r["seen_at"])).fetchone()
            out.append(
                {
                    "id": r["id"],
                    "version": r["version"],
                    "seen_at": r["seen_at"],
                    "until": nxt,
                    "days": ((nxt or datetime.now(timezone.utc)) - r["seen_at"]).days,
                    "odometer": odo["odometer"] if odo else None,
                    "by_hand": r["import_id"] == BY_HAND,
                }
            )
    return list(reversed(out))


@site.post("/software/add")
async def software_add(request: Request):
    """A version the logger missed (the car updated while nothing was watching), dated by hand."""
    form = await request.form()
    vin = (form.get("vin") or "").strip()
    version = (form.get("version") or "").strip()[:40]
    when = manual.parse_local(form.get("seen_on") or "", TZ)
    if vin and version and when and vin in {v["vin"] for v in all_vehicles()}:
        with db() as conn:
            conn.execute("INSERT INTO cc_software (vin, version, seen_at, import_id) VALUES (%s, %s, %s, %s)", (vin, version, when, BY_HAND))
            conn.commit()
    return to_site(request, f"/software?vin={vin}")


@site.post("/software/{row_id}/delete")
def software_delete(request: Request, row_id: int, vin: str = Form("")):
    """Only rows added by hand can be removed; what the logger saw stays."""
    with db() as conn:
        conn.execute("DELETE FROM cc_software WHERE id = %s AND import_id = %s", (row_id, BY_HAND))
        conn.commit()
    root = request.scope.get("root_path", "")
    return RedirectResponse(f"{root}/software" + (f"?vin={vin}" if vin else ""), status_code=303)


@site.get("/software", response_class=HTMLResponse)
def software_page(request: Request):
    car = pick(request)
    cars = all_vehicles() if car.get("garage") else [{"vin": car["vin"], "name": car["name"]}]
    blocks = [{"vin": v["vin"], "name": v["name"], "rows": software_rows(v["vin"])} for v in cars]
    return page(
        request,
        "software.html",
        car=car,
        blocks=[b for b in blocks if b["rows"]],
        add_cars=[v for v in all_vehicles() if not car.get("garage") and v["vin"] == car["vin"]] or (all_vehicles() if car.get("garage") else []),
        today=datetime.now(TZ).date(),
        active="software",
    )


def set_hidden(kind: str, ref_id: int, hidden: bool) -> None:
    with db() as conn:
        if hidden:
            conn.execute("INSERT INTO cc_hidden (kind, ref_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (kind, ref_id))
        else:
            conn.execute("DELETE FROM cc_hidden WHERE kind = %s AND ref_id = %s", (kind, ref_id))
        conn.commit()
    _cache.pop("today", None)


@site.post("/drives/{trip_id}/hide")
def drive_hide(request: Request, trip_id: int, vin: str = Form("")):
    set_hidden("drive", trip_id, True)
    return RedirectResponse(f"{request.scope.get('root_path', '')}/drives?show=hidden" + (f"&vin={vin}" if vin else ""), status_code=303)


@site.post("/drives/{trip_id}/unhide")
def drive_unhide(request: Request, trip_id: int, vin: str = Form("")):
    set_hidden("drive", trip_id, False)
    return to_site(request, f"/drives/{trip_id}" + (f"?vin={vin}" if vin else ""))


@site.post("/charges/{session_id}/hide")
def charge_hide(request: Request, session_id: int, vin: str = Form("")):
    set_hidden("charge", session_id, True)
    return RedirectResponse(f"{request.scope.get('root_path', '')}/charges?show=hidden" + (f"&vin={vin}" if vin else ""), status_code=303)


@site.post("/charges/{session_id}/unhide")
def charge_unhide(request: Request, session_id: int, vin: str = Form("")):
    set_hidden("charge", session_id, False)
    return to_site(request, f"/charges/{session_id}" + (f"?vin={vin}" if vin else ""))


def hidden_drives(vin: str | None) -> list[dict]:
    """The drives hidden by hand, the one list they still appear on."""
    vw, vargs = _vin_clause(vin)
    with db() as conn:
        rows = conn.execute(TRIP_SELECT + " WHERE t.id IN (SELECT ref_id FROM cc_hidden WHERE kind = 'drive')" + vw + " ORDER BY t.start_date DESC", vargs).fetchall()
    known, cells, energy, by_year = places(), geocells(), vehicle_energy(), rated_mile_years()
    return [_finish_drive(r, known, cells, energy, by_year) for r in rows]


def hidden_charges(vin: str | None) -> list[dict]:
    vw, vargs = _vin_clause(vin)
    with db() as conn:
        rows = conn.execute(
            CHARGE_SELECT + " WHERE c.id IN (SELECT ref_id FROM cc_hidden WHERE kind = 'charge')" + vw.replace("t.vin", "c.vin") + " ORDER BY c.session_start_date DESC", vargs
        ).fetchall()
    known = places()
    return [_finish_charge(r, known) for r in rows]


def is_hidden(kind: str, ref_id: int) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM cc_hidden WHERE kind = %s AND ref_id = %s", (kind, ref_id)).fetchone() is not None


@site.post("/drives/{trip_id}/tag")
def drive_tag(request: Request, trip_id: int, tag: str = Form(""), note: str = Form(""), vin: str = Form("")):
    set_tag(trip_id, tag.strip()[:40], note.strip()[:500])
    return to_site(request, f"/drives/{trip_id}" + (f"?vin={vin}" if vin else ""))


def auto_tag_rules() -> list[dict]:
    with db() as conn:
        return conn.execute("SELECT id, from_name, to_name, tag, both_ways, active FROM cc_auto_tags ORDER BY id").fetchall()


def apply_auto_tags(days: int | None = 2) -> int:
    """Tag the untagged drives the rules match: the last `days` on every tick, or all time on request. Returns how many."""
    rules = auto_tag_rules()
    if not rules:
        return 0
    since = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None
    todo = autotag.plan(rules, drives_light_named(since))
    for trip_id, tag in todo:
        set_tag(trip_id, tag, None)
    return len(todo)


def drives_light_named(since: datetime | None) -> list[dict]:
    """id, from, to and tag for finished drives since `since` (all time when None), with place names resolved as the lists resolve them."""
    with db() as conn:
        rows = conn.execute(
            TRIP_SELECT + " WHERE t.destination_date IS NOT NULL" + (" AND t.start_date >= %s" if since else "") + moved_sql() + " ORDER BY t.start_date DESC",
            [since] if since else [],
        ).fetchall()
    known = places()
    out = []
    for r in rows:
        if r.get("tag"):
            continue
        start = place_for(r["start_position_latitude"], r["start_position_longitude"], known)
        end = place_for(r["destination_position_latitude"], r["destination_position_longitude"], known)
        out.append({"id": r["id"], "tag": r.get("tag"), "from": start["name"] if start else None, "to": end["name"] if end else None})
    return out


@site.post("/tags/rules")
async def auto_tag_add(request: Request):
    form = await request.form()
    tag = (form.get("tag") or "").strip()[:40]
    frm, to = (form.get("from_name") or "").strip()[:80], (form.get("to_name") or "").strip()[:80]
    if tag and (frm or to):
        with db() as conn:
            conn.execute("INSERT INTO cc_auto_tags (from_name, to_name, tag, both_ways) VALUES (%s, %s, %s, %s)", (frm or None, to or None, tag, form.get("both_ways") == "on"))
            conn.commit()
    return to_site(request, f"/tags?vin={form.get('vin', '')}")


@site.post("/tags/rules/{rule_id}/delete")
def auto_tag_delete(request: Request, rule_id: int, vin: str = Form("")):
    with db() as conn:
        conn.execute("DELETE FROM cc_auto_tags WHERE id = %s", (rule_id,))
        conn.commit()
    return RedirectResponse(f"{request.scope.get('root_path', '')}/tags" + (f"?vin={vin}" if vin else ""), status_code=303)


@site.post("/tags/rules/apply")
def auto_tag_apply(request: Request, vin: str = Form("")):
    """Run the rules over every untagged drive ever logged. A button, because tagging a decade of drives is a decision."""
    n = apply_auto_tags(None)
    return RedirectResponse(f"{request.scope.get('root_path', '')}/tags?said={quote(f'{n} drives tagged')}" + (f"&vin={vin}" if vin else ""), status_code=303)


@site.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request, tag: str | None = None, month: str | None = None, all: str | None = None):
    """Tagged drives: one tag over a month or all time, with miles, hours and energy added up; the shape of an expense report."""
    car = pick(request)
    vin = None if car.get("garage") else car["vin"]
    names = tag_names()
    tag = tag if tag in names else (names[0] if names else None)
    w = None if all else month_window(month, TZ)
    with db() as conn:
        ids = [r["trip_id"] for r in conn.execute("SELECT trip_id FROM cc_drive_tags WHERE tag = %s", (tag,)).fetchall()] if tag else []
    ds = [d for d in drives(vin, None, w["start"] if w else None, w["end"] if w else None) if d["id"] in set(ids)] if ids else []
    totals = {
        "drives": len(ds),
        "miles": sum(d.get("miles") or 0 for d in ds),
        "hours": sum(d["duration"].total_seconds() for d in ds if d.get("duration")) / 3600,
        "kwh": sum(d.get("kwh_used") or 0 for d in ds),
        "cost": sum(d.get("cost") or 0 for d in ds),
        "cost_estimate": any(d.get("cost") and d.get("cost_estimate") for d in ds),
    }
    # the road-trip view: every tagged drive on one map, and the charges away from home between the first and the last
    routes, away = [], []
    if ds:
        first, last = min(d["start_date"] for d in ds), max(d["destination_date"] or d["start_date"] for d in ds)
        colors = car_colors(all_vehicles())
        with db() as conn:
            # each car's matched lines in one read over the tagged drives' span
            lines: dict[str, dict] = {}
            for car_vin in {d["vin"] for d in ds}:
                mine = [d["start_date"] for d in ds if d["vin"] == car_vin]
                lines[car_vin] = routematch.matched_lines(conn, car_vin, min(mine), max(mine) + timedelta(microseconds=1), "simple")
            for d in ds:
                matched = routematch.line_for(lines[d["vin"]], d["start_date"], d["destination_date"])
                if matched:
                    routes.append({"c": colors.get(d["vin"], 0), "pts": compact_line(matched)})
                    continue
                pts = conn.execute(
                    "SELECT latitude, longitude FROM cc_positions WHERE vin = %s AND at BETWEEN %s AND %s ORDER BY at", (d["vin"], d["start_date"], d["destination_date"] or d["start_date"])
                ).fetchall()
                if len(pts) >= 2:
                    ends = parked_ends(conn, d["vin"], d["start_date"], d["destination_date"] or d["start_date"])
                    line = [[round(a, 4), round(b, 4)] for a, b in anchored([[p["latitude"], p["longitude"]] for p in pts], *ends)]
                    routes.append({"c": colors.get(d["vin"], 0), "pts": line[::2] + [line[-1]]})
        away = [c for c in charges(vin, None, first, last + timedelta(hours=12)) if not c.get("at_home")]
        totals["charge_kwh"] = sum(c.get("kwh") or 0 for c in away)
        totals["charge_cost"] = sum(c.get("cost") or 0 for c in away)
    return page(
        request,
        "tags.html",
        car=car,
        tags=names,
        tag=tag,
        drives=ds,
        totals=totals,
        window=w,
        routes=routes,
        away=away,
        active="tags",
        rules=auto_tag_rules(),
        named_places=named_places(),
        said=request.query_params.get("said"),
    )


@site.get("/service", response_class=HTMLResponse)
def service_page(request: Request):
    car = pick(request)
    vin = None if car.get("garage") else car["vin"]
    items = service_items(vin)
    today = datetime.now(TZ).date()
    odos = {v["vin"]: live_vehicle(v["vin"]).get("odometer_mi") for v in all_vehicles()}
    for it in items:
        it["status"] = None if it["done"] else due_status(it, today, odos.get(it["vin"]))
    vins = [v["vin"] for v in (all_vehicles() if car.get("garage") else [car])]
    return page(
        request, "service.html", tread=tread_log(vins), tire_sets=tire_set_rows(vins), tread_today=datetime.now(TZ).date().isoformat(), car=car, items=items, odos=odos, today=today, active="service"
    )


def tire_set_rows(vins: list[str]) -> list[dict]:
    """Every set of tires these cars have worn, newest first, with the miles and the
    efficiency the car managed on them: the facts summed between fitted and removed."""
    with db() as conn:
        rows = conn.execute("SELECT t.*, v.name AS car_name FROM cc_tire_sets t JOIN vehicles v ON v.vin = t.vin WHERE t.vin = ANY(%s) ORDER BY t.fitted_on DESC, t.id DESC", (vins,)).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        since = datetime(r["fitted_on"].year, r["fitted_on"].month, r["fitted_on"].day, tzinfo=TZ)
        until = (datetime(r["removed_on"].year, r["removed_on"].month, r["removed_on"].day, tzinfo=TZ) + timedelta(days=1)) if r["removed_on"] else None
        t = {k: sum(x[k] for x in summarize_sql(r["vin"], "month", since, until)) for k in ("drives", "miles", "used_kwh")}
        r["miles"], r["drives"] = t["miles"], t["drives"]
        r["wh_per_mi"] = (t["used_kwh"] * 1000 / t["miles"]) if t["miles"] > 1 and t["used_kwh"] > 0 else None
        out.append(r)
    return out


@site.post("/service/tires")
async def tire_set_add(request: Request):
    form = await request.form()
    vin = (form.get("vin") or "").strip()
    name = (form.get("name") or "").strip()[:80]
    fitted = parse_day(form.get("fitted_on"))
    removed = parse_day(form.get("removed_on"))
    if vin and name and fitted and vin in {v["vin"] for v in all_vehicles()}:
        with db() as conn:
            conn.execute(
                "INSERT INTO cc_tire_sets (vin, name, fitted_on, fitted_odometer, removed_on, notes) VALUES (%s, %s, %s, %s, %s, %s)",
                (vin, name, fitted, _float_or_none(form.get("odometer")), removed, (form.get("notes") or "").strip()[:500] or None),
            )
            conn.commit()
    return to_site(request, f"/service?vin={vin}")


@site.post("/service/tires/{set_id}/delete")
def tire_set_delete(request: Request, set_id: int, vin: str = Form("")):
    with db() as conn:
        conn.execute("DELETE FROM cc_tire_sets WHERE id = %s", (set_id,))
        conn.commit()
    return RedirectResponse(f"{request.scope.get('root_path', '')}/service" + (f"?vin={vin}" if vin else ""), status_code=303)


def tread_log(vins: list[str]) -> list[dict]:
    """Every tread measurement for these cars, newest first, depths in millimetres."""
    with db() as conn:
        return conn.execute("SELECT t.*, v.name AS car_name FROM cc_tread t JOIN vehicles v ON v.vin = t.vin WHERE t.vin = ANY(%s) ORDER BY t.measured_on DESC, t.id DESC", (vins,)).fetchall()


@site.post("/service/tread")
async def tread_add(request: Request):
    form = await request.form()
    vin = (form.get("vin") or "").strip()
    try:
        measured_on = date.fromisoformat((form.get("measured_on") or "").strip())
    except ValueError:
        measured_on = datetime.now(TZ).date()
    unit = "32nds" if form.get("unit") == "32nds" else "mm"
    depths = [tread_mm(form.get(w), unit) for w in ("front_left", "front_right", "rear_left", "rear_right")]
    if vin and any(d is not None for d in depths):
        try:
            odometer = float((form.get("odometer") or "").strip())
        except ValueError:
            odometer = None
        with db() as conn:
            conn.execute(
                "INSERT INTO cc_tread (vin, measured_on, odometer, front_left, front_right, rear_left, rear_right, notes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (vin, measured_on, odometer, *depths, (form.get("notes") or "").strip()[:500] or None),
            )
            conn.commit()
    return to_site(request, f"/service?vin={vin}")


@site.post("/service/tread/{item_id}/delete")
async def tread_delete(request: Request, item_id: int):
    form = await request.form()
    with db() as conn:
        conn.execute("DELETE FROM cc_tread WHERE id = %s", (item_id,))
        conn.commit()
    return to_site(request, f"/service?vin={form.get('vin', '')}")


@site.post("/service")
def service_add(
    request: Request,
    vin: str = Form(...),
    what: str = Form(...),
    done_on: str = Form(""),
    odometer: str = Form(""),
    cost: str = Form(""),
    notes: str = Form(""),
    due_date: str = Form(""),
    due_miles: str = Form(""),
    kind: str = Form("done"),
    every_months: str = Form(""),
    every_miles: str = Form(""),
):
    def num(x):
        try:
            return float(x.replace(",", "")) if x.strip() else None
        except ValueError:
            return None

    def day(x):
        try:
            return datetime.strptime(x.strip(), "%Y-%m-%d").date() if x.strip() else None
        except ValueError:
            return None

    def miles(x):
        # The form is labelled in the operator's units; the column is always miles.
        return ACTIVE_UNITS.to_stored_distance(num(x))

    with db() as conn:
        em = num(every_months)
        conn.execute(
            "INSERT INTO cc_service (vin, done_on, odometer, what, cost, notes, due_date, due_miles, done, every_months, every_miles) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                vin,
                day(done_on),
                miles(odometer),
                what.strip()[:80],
                num(cost),
                notes.strip()[:1000] or None,
                day(due_date),
                miles(due_miles),
                kind == "done",
                int(em) if em and kind != "done" else None,
                miles(every_miles) if kind != "done" else None,
            ),
        )
        conn.commit()
    return to_site(request, f"/service?vin={vin}")


@site.post("/service/{item_id}/done")
def service_done(request: Request, item_id: int, vin: str = Form("")):
    with db() as conn:
        item = conn.execute("SELECT * FROM cc_service WHERE id = %s", (item_id,)).fetchone()
        conn.execute("UPDATE cc_service SET done = TRUE, done_on = COALESCE(done_on, CURRENT_DATE) WHERE id = %s", (item_id,))
        # A recurring reminder, once done, is due again: the same row shape, dated from today and the odometer now.
        nxt = service.next_reminder(dict(item), datetime.now(TZ).date(), (live_vehicle(item["vin"]) or {}).get("odometer_mi")) if item else None
        if nxt:
            conn.execute(
                "INSERT INTO cc_service (vin, what, notes, due_date, due_miles, done, every_months, every_miles) VALUES (%(vin)s, %(what)s, %(notes)s, %(due_date)s, %(due_miles)s, FALSE, %(every_months)s, %(every_miles)s)",
                nxt,
            )
        conn.commit()
    return RedirectResponse(f"{request.scope.get('root_path', '')}/service" + (f"?vin={vin}" if vin else ""), status_code=303)


@site.post("/service/{item_id}/files")
async def service_files_add(request: Request, item_id: int, vin: str = Form(""), files: list[UploadFile] = File([])):
    """Receipts and photos on a service record. The form carries the CSRF token in its
    query string because a photo is larger than the guard will read a body to find one."""
    back = "/service" + (f"?vin={vin}" if vin else "")
    with db() as conn:
        if conn.execute("SELECT 1 FROM cc_service WHERE id = %s", (item_id,)).fetchone() is None:
            raise HTTPException(404, "No such service record.")
        for up in files[:5]:
            data = await up.read(service.MAX_UPLOAD + 1)
            reason = service.allowed_upload(up.filename, up.content_type, len(data))
            if reason:
                LOG.info("service file refused: %s", reason)
                continue
            conn.execute(
                "INSERT INTO cc_service_files (item_id, filename, content_type, size, data) VALUES (%s, %s, %s, %s, %s)",
                (item_id, up.filename[:200], service.served_type(up.filename), len(data), data),
            )
        conn.commit()
    return to_site(request, back)


@site.get("/service/files/{file_id}")
def service_file(file_id: int):
    with db() as conn:
        row = conn.execute("SELECT filename, content_type, data FROM cc_service_files WHERE id = %s", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such file.")
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in row["filename"])[:120]
    # The type is the suffix's, decided at upload and checked again here; inline so a photo
    # opens in the tab; nosniff so the browser never guesses; sandboxed so that even a file
    # that fooled both cannot run a script or reach the site's cookies.
    media = service.served_type(row["filename"]) or "application/octet-stream"
    how = "inline" if media != "application/octet-stream" else "attachment"
    return Response(
        bytes(row["data"]),
        media_type=media,
        headers={"Content-Disposition": f'{how}; filename="{safe}"', "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox", "Cache-Control": "private, max-age=86400"},
    )


@site.post("/service/files/{file_id}/delete")
def service_file_delete(request: Request, file_id: int, vin: str = Form("")):
    with db() as conn:
        conn.execute("DELETE FROM cc_service_files WHERE id = %s", (file_id,))
        conn.commit()
    return RedirectResponse(f"{request.scope.get('root_path', '')}/service" + (f"?vin={vin}" if vin else ""), status_code=303)


@site.post("/service/{item_id}/delete")
def service_delete(request: Request, item_id: int, vin: str = Form("")):
    with db() as conn:
        conn.execute("DELETE FROM cc_service_files WHERE item_id = %s", (item_id,))
        conn.execute("DELETE FROM cc_service WHERE id = %s", (item_id,))
        conn.commit()
    return RedirectResponse(f"{request.scope.get('root_path', '')}/service" + (f"?vin={vin}" if vin else ""), status_code=303)


COMMON_ZONES = (
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Phoenix",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Toronto",
    "America/Vancouver",
    "America/Mexico_City",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Amsterdam",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Zurich",
    "Europe/Oslo",
    "Europe/Stockholm",
    "Europe/Warsaw",
    "Europe/Athens",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Hong_Kong",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Asia/Shanghai",
    "Australia/Sydney",
    "Australia/Melbourne",
    "Australia/Perth",
    "Pacific/Auckland",
)


def _iso(value: str | None) -> datetime | None:
    """The add-on reports times as ISO text over HTTP; the page wants a real datetime so it
    can read through the site's own `local`/`when` filters instead of showing raw ISO."""
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def settings_render(request: Request, section: str, **extra) -> HTMLResponse:
    """One Settings section; each loads only what its page shows."""
    st = current_settings()
    saved = extra.pop("saved", None)
    ctx = dict(car=pick(request), st=st, conv=settingsform.converted(st), section=section, label=settingsform.LABELS[section], active=f"settings-{section}", saved=bool(saved), said=saved)
    if section == "cars":
        ctx.update(capacities=capacities(), data_checks=data_checks())
        made = {c["vin"]: c["make"] for c in ctx["capacities"]}
        try:
            with db() as conn:
                sources.expire_pending(conn)  # a sign-in the connector never picked up keeps no password
                conn.commit()
                rows = sources.load(conn, VAULT)
        except Exception as err:  # noqa: BLE001 - no table yet: nothing kept in the app
            LOG.warning("sources: %s", err)
            rows = []
        eff = sources.effective(rows, os.environ)
        names = {v["vin"]: v["name"] for v in all_vehicles()}
        from_env = sources.env_sources(os.environ)
        # A bare token in .env names no car; the poller learns each one's car on its first poll.
        learned = [vin or teslafi.vin_for_token.get(tok) for vin, tok in sources.env_tokens(os.environ)]
        for s_env, vin in zip([s for s in from_env if s["kind"] == "teslafi"], learned):
            s_env["vin"] = vin

        status_of = source_status
        ctx.update(
            sources=[
                {
                    **{k: v for k, v in r.items() if k != "plain"},
                    "readable": r["plain"] is not None,
                    "car": names.get(r["vin"], r["label"] or r["vin"] or ""),
                    "status": status_of(r["kind"], r["vin"], r["config"]),
                }
                for r in rows
            ],
            env_sources=[{**s, "car": names.get(s["vin"], s["vin"] or "a car"), "status": status_of(s["kind"], s["vin"]), "used": s["kind"] not in eff["moved"]} for s in from_env],
            moved=eff["moved"],
            vault_ready=VAULT.ready,
            vault_problem=VAULT.problem,
            kinds=sources.KINDS,
            cc_catalogue=connectors.CATALOGUE,
            # the token file's row: no Lucid account in the app, and a car on the connector no other brand's account claims
            token_file=not any(r["kind"] == "lucid" for r in rows) and any(source_of(v) == "live" and not cc_claimed(rows, made.get(v["vin"])) for v in all_vehicles()),
        )
        ctx.update(cars_context(ctx))
        try:
            ctx["pw_card"] = energypage.cars_card(energy_config(), tesla_energy_scope())
        except Exception as err:  # noqa: BLE001 - the page does not wait on the Powerwall's line
            LOG.warning("energy card: %s", type(err).__name__)
    elif section == "money":
        ctx.update(planview.money_context(st.get("rate_plan")), rates=rates_settings(), finder=urdb_settings())
    elif section == "alerts":
        ctx.update(channels=recorder.channels(), routes=notify_mod.routes_from(st), alert_groups=notify_mod.GROUPS)
    elif section == "look":
        ctx.update(zones=COMMON_ZONES, tz_key=TZ.key, tz_default=os.environ.get("TZ", "America/Los_Angeles"))
    elif section == "maps":
        status = MAPS.status()
        regions = [
            {**r, "label": region_name(r), "why": region_reason(r.get("reason")), "mb": round((r.get("bytes") or 0) / 1e6), "added_dt": _iso(r.get("added")), "refreshed_dt": _iso(r.get("refreshed"))}
            for r in (status or {}).get("regions", [])
        ]
        ctx.update(status=status, maps_on=maps_available(status), regions=regions, wait_until_dt=_iso((status or {}).get("wait_until")))
    elif section == "energy":
        ctx.update(energy_context())
    elif section == "data":
        ctx.update(data_checks=data_checks())
    elif section == "access":
        ctx.update(
            tokens=token_list(),
            token_vehicles=all_vehicles(),
            api_log=api_log(),
            admin_anywhere=AUTH["admin_anywhere"],
            auth_enabled=AUTH["enabled"],
            has_password=bool(AUTH["password"]),
            secret_in_env=bool(os.environ.get("CC_SECRET")),
            password_in_env=bool(os.environ.get("CC_PASSWORD")),
            signin_next=signin_return(request.query_params.get("next")),
            cf_team=CF["team"],
            cf_pinned=bool(CF["aud"]),
            cf_env=CF["env_team"] or CF["env_aud"],
            cf_offer=cf_candidate(request),
            cf_user=proxyauth.identity(request.state.cf) if getattr(request.state, "cf", None) else None,
            cf_can_set=bool(auth.valid_session(AUTH["secret"], session_of(request)) and not CF["env_team"]),
            signin_need_code=True,  # turning sign-in on always asks for the code
            cf_emails=bool(EMAILS),
            cf_need_code=True,
            cf_proxy_unset=not PROXIES,
            remembered_apps=remembered_apps(),
        )
    ctx.update(extra)
    return page(request, f"settings_{section}.html", **ctx)


def source_status(kind: str, vin: str | None, config: dict | None = None):
    """How a source's poller is doing, as (last success, error). TeslaFi is one token per
    car; a TeslaMate source is rolled up over the cars it follows; a Lucid account or another
    brand's is what the connector reports (the row's own sign-in or install result comes first
    on the page)."""
    if kind in ("lucid", "carconnectivity"):
        return connector_status()
    return teslamate_status(list((config or {}).get("vins") or [])) if kind == "teslamate" else teslafi.results.get(vin)


def cc_claimed(rows: list[dict], make: str | None) -> bool:
    """Whether an account for another brand, added here, answers for a car of this make."""
    return any(r["kind"] == "carconnectivity" and r["enabled"] and connectors.claims(r["config"] or {}, make) for r in rows)


def cars_context(ctx: dict) -> dict:
    """Settings › Cars and sources: a card per car and a row per account, from what the
    section already loaded (no call to Tesla: its cars are named from the last answer kept)."""
    import carspage

    vehicles = all_vehicles()
    caps = {c["vin"]: c for c in ctx["capacities"]}
    makes = {vin: (c["make"], c["model"]) for vin, c in caps.items()}
    names = {v["vin"]: v["name"] for v in vehicles}
    writer = {v["vin"]: source_of(v) for v in vehicles}
    lucid_vins = [v["vin"] for v in vehicles if writer[v["vin"]] == "live" and carspage.is_lucid(makes.get(v["vin"], (None,))[0])]
    kept = (TESLA_CACHE.get("vehicles") or (0, []))[1]
    tesla_vins = [c["vin"] for c in kept] if isinstance(kept, list) else []
    connector = connector_status() if lucid_vins or ctx.get("token_file") else None
    live_makes = {vin: makes.get(vin, (None,))[0] for vin, k in writer.items() if k == "live"}
    accts = carspage.accounts(ctx["sources"], ctx["env_sources"], ctx["token_file"], names, lucid_vins, tesla_vins, connector, [vin for vin, k in writer.items() if k == "teslamate"], live_makes)

    def status(vin):
        kind = writer[vin]
        if kind == "live":
            return connector_status()
        if kind == "tesla":
            return tesla_stream_worker.results.get(vin)
        return teslafi.results.get(vin) if kind == "feed" else teslamate_status([vin]) if kind == "teslamate" else None

    checks = {d["vin"]: d["facts"] for d in ctx["data_checks"]}
    newest = {vin: f.get("raw_newest") or f.get("derived_newest") for vin, f in checks.items()}
    packs = {vin: c["pack"] for vin, c in caps.items()}
    choices = writer_options(writer)
    cards = carspage.cards(vehicles, makes, writer, {vin: status(vin) for vin in writer}, newest, packs, accts, choices=choices)
    for c in cards:
        c.update(writer_trouble(c["vin"], history_writer.KIND_OF_SOURCE.get(writer[c["vin"]]), c["choices"]))
    look_options(cards, vehicles)
    return {"cars": cards, "accounts": accts, "bodies": identity.BODIES}


def look_options(cards: list[dict], vehicles: list[dict]) -> None:
    """Each card's Look control: its identity, the paints its maker sells, and the chart
    colours with the ones other cars hold marked taken and the one nearest the paint marked."""
    idents, colors = car_identities(vehicles), car_colors(vehicles)
    names = {v["vin"]: v["name"] for v in vehicles}
    for c in cards:
        ident = idents.get(c["vin"])
        if not ident:
            continue
        others = {v: i for v, i in colors.items() if v != c["vin"]}
        taken = {i: names.get(v, v) for v, i in others.items()}
        # a colour on, or too close to, another car's is not offered: say whose
        close = {k: f"too close to {names.get(v, v)}" for k in identity.blocked(others.values()) - set(taken) for v in [identity.too_close(k, others, c["vin"])] if v}
        near = identity.nearest_chart((ident["paint"] or {}).get("hex"), identity.blocked(others.values()))
        c["ident"] = ident
        # each paint swatch carries its matching free chart colour, for the form to follow
        c["paints"] = [{**p, "chart": identity.nearest_chart(p["hex"], identity.blocked(others.values()))} for p in identity.paints_for(ident["make"])]
        c["auto_chart"] = identity.nearest_chart((ident["detected_paint"] or {}).get("hex"), identity.blocked(others.values()))
        mine = colors.get(c["vin"], 0)
        c["chart_name"] = identity.PALETTE[mine][0]
        c["matched"] = bool(ident["paint"]) and near == mine
        c["auto_body"] = identity.body_for(ident["make"], ident["model"])
        c["charts"] = [{"i": i, "name": p[0], "taken": taken.get(i) or close.get(i), "near": i == near} for i, p in enumerate(identity.PALETTE)]


def writer_options(writer: dict[str, str]) -> dict[str, list[dict]]:
    mate = set(teslamate.results) | set(teslamate.vins) | {vin for vin, k in _cache_feed_vins().items() if k == "teslamate"}
    return writerswitch.options(writer, tesla_stream_worker.vins, set(teslafi.vin_for_token.values()), mate)


def writer_trouble(vin: str, kind: str | None, choices: list[dict]) -> dict:
    """For a car's card and step 6: the switch under way or just done, and a writer gone silent."""
    others = [o["name"] for o in choices if not o["current"] and o["kind"] != "tesla"]
    return {"switch": switcher.status(vin), "silent": writerswitch.silent(kind, tesla_stream_worker.state.get(vin), others, datetime.now(timezone.utc))}


def _vin_q(request: Request, sep: str = "?") -> str:
    vin = request.query_params.get("vin")
    return f"{sep}vin={vin}" if vin else ""


@site.get("/settings", response_class=HTMLResponse)
def settings_home(request: Request):
    """The Settings landing: every section as a row, with a glance at what it holds."""
    groups = settingsform.index(current_settings(), len(all_vehicles()), recorder.channels(), AUTH["enabled"], energypage.glance(energy_config()))
    return page(request, "settings_index.html", car=pick(request), active="settings", groups=groups)


@site.get("/settings/{section}", response_class=HTMLResponse)
def settings_page(request: Request, section: str, saved: str | None = None):
    if section not in settingsform.LABELS:
        raise HTTPException(404, "No such settings page.")
    return settings_render(request, section, saved=saved)


def store_token(label: str, kind: str, vin: str | None, share_place: bool = False) -> str:
    """A new token, kept as its hash and last four, and live at once. The raw token is the
    caller's to show once; it goes in no log and no URL."""
    raw = auth.mint_token()
    with db() as conn:
        conn.execute("INSERT INTO cc_api_tokens (hash, label, vin, kind, last4) VALUES (%s, %s, %s, %s, %s)", (auth.token_hash(raw), label, vin, kind, raw[-4:]))
        if share_place and kind == "status":
            conn.execute("UPDATE cc_api_tokens SET share_place = true WHERE hash = %s", (auth.token_hash(raw),))
        conn.commit()
    reload_tokens()
    return raw


@site.post("/settings/tokens")
async def token_mint(request: Request):
    """Mint a token and show it once, on the page, never in a URL or a log."""
    form = await request.form()
    label = (form.get("label") or "").strip()[:40] or "token"
    kind = auth.kind_of(form.get("kind"))
    vin = (form.get("vin") or "").strip() or None
    if kind == "admin" or (vin and vin not in {v["vin"] for v in await asyncio.to_thread(all_vehicles)}):
        vin = None  # an admin token drives the whole install, so it is never one car's
    raw = await asyncio.to_thread(store_token, label, kind, vin)
    return settings_render(request, "access", minted=raw, minted_label=label, minted_kind=kind)


@site.post("/settings/tokens/anywhere")
async def token_anywhere(request: Request):
    """The owner's override: admin tokens from any network, Cloudflare included. Off by default."""
    on = (await request.form()).get("anywhere") == "on"

    def save() -> None:
        with db() as conn:
            auth.store_set(conn, ADMIN_ANYWHERE_KEY, "1" if on else "0")

    await asyncio.to_thread(save)
    AUTH["admin_anywhere"] = on
    return RedirectResponse(f"{request.scope.get('root_path', '')}/settings/access?saved=1", status_code=303)


@site.post("/settings/tokens/{token_hash}/revoke")
async def token_revoke(request: Request, token_hash: str):
    def revoke() -> None:
        with db() as conn:
            conn.execute("DELETE FROM cc_api_tokens WHERE hash = %s", (token_hash,))
            conn.commit()
        reload_tokens()

    await asyncio.to_thread(revoke)
    root = request.scope.get("root_path", "")
    return RedirectResponse(f"{root}/settings/access?saved=1", status_code=303)


@site.post("/settings/tokens/{token_hash}/place")
async def token_place(request: Request, token_hash: str):
    """Allow a status token to see where each car is, as a place name and home or away, or take that back. Takes
    effect on the next request, with no reconnect. Only a signed-in person with the form's token gets here: a token
    is not a session, and no /api/ path does this."""
    form = await request.form()
    share = form.get("share_place") == "on"

    def save() -> bool:
        with db() as conn:
            conn.execute("UPDATE cc_api_tokens SET share_place = %s WHERE hash = %s AND kind = 'status'", (share, token_hash))
            # the app this token came of remembers the latest choice, so a skipped reconnect keeps it
            conn.execute(
                "UPDATE cc_connect_apps SET share_place = %s WHERE token_hash = %s AND EXISTS (SELECT 1 FROM cc_api_tokens WHERE hash = %s AND kind = 'status')", (share, token_hash, token_hash)
            )
            conn.commit()
        was = bool((TOKENS.get(token_hash) or {}).get("share_place"))
        reload_tokens()
        return share and not was and bool((TOKENS.get(token_hash) or {}).get("share_place"))

    turned_on = await asyncio.to_thread(save)
    if turned_on:
        label = (TOKENS.get(token_hash) or {}).get("label") or "an app"
        await asyncio.to_thread(recorder.notify, f"{label} can now see which place each car is at (place name, home or away)", "login")
    return RedirectResponse(f"{request.scope.get('root_path', '')}/settings/access?saved=place", status_code=303)


def token_list() -> list[dict]:
    names = {v["vin"]: v["name"] for v in all_vehicles()}
    with db() as conn:
        # Flush the in-memory "last used" stamps first, so the page tells the truth.
        for h, t in TOKENS.items():
            if t.get("used"):
                conn.execute("UPDATE cc_api_tokens SET last_used_at = GREATEST(COALESCE(last_used_at, %s), %s) WHERE hash = %s", (t["used"], t["used"], h))
        conn.commit()
        rows = conn.execute("SELECT hash, label, vin, kind, last4, share_place, created_at, last_used_at FROM cc_api_tokens ORDER BY created_at DESC").fetchall()
    return [{**r, "kind": auth.kind_of(r["kind"]), "car": names.get(r["vin"], r["vin"]) if r["vin"] else "every car"} for r in rows]


def api_log(limit: int = 50) -> list[dict]:
    """The newest admin calls, for Settings beside the tokens."""
    with db() as conn:
        return conn.execute("SELECT at, label, last4, method, path, status, client_ip FROM cc_api_log ORDER BY at DESC, id DESC LIMIT %s", (limit,)).fetchall()


def signin_return(target: str | None) -> str | None:
    """Where turning sign-in on may send the person next: the connect page on this site, as a path
    from the site's root, so the app flow they came from carries on. Anything else is ignored."""
    if not target or safe_next(target) != target:
        return None
    return target if re.fullmatch(r"/connect(\?[^\s]*)?", target) else None


async def switch_alert(request: Request, what: str) -> None:
    """An alert on every channel that a sign-in switch was flipped, from the address the socket shows."""
    await asyncio.to_thread(recorder.notify, f"Sign-in was turned {what} from {sign_in_key(request)} at {datetime.now(TZ).strftime('%b %-d, %-I:%M %p')}", "login")


def rotate_secret(conn) -> None:
    """A new signing key, so sessions from before an earlier turn-off do not come back to life. A key set in
    .env (CC_SECRET) cannot be rotated from here."""
    if not os.environ.get("CC_SECRET"):
        auth.store_set(conn, "secret", auth.new_secret())


def setup_code_gate(request: Request, code: str | None) -> HTMLResponse | None:
    """The setup code, checked with the sign-in throttle: None when it is right, else the page that says so.
    The attempt is counted before it is checked and forgiven after, with nothing awaited in between."""
    key, now = sign_in_key(request), time.time()
    held = FAILURES.wait(key, now)
    if held > 0:
        r = settings_render(request, "access", signin_error=f"Too many tries. Wait {int(held) + 1} seconds.")
        r.status_code = 429
        return r
    FAILURES.record(key, now)
    if not auth.setup_code_ok(AUTH["secret"], code):
        r = settings_render(request, "access", signin_error="That setup code is not right.")
        r.status_code = 403
        return r
    FAILURES.clear(key)
    return None


@site.post("/settings/signin/on")
async def signin_on(request: Request):
    """Turn sign-in on without a restart: choose the household password, keep the switch where a restart
    finds it, and sign this browser in so the person who did it is not locked out. From outside the home
    network it asks for the setup code, as /setup does: otherwise anyone who reached an open site could
    choose the password first."""
    root = request.scope.get("root_path", "")
    if AUTH["enabled"]:
        return RedirectResponse(f"{root}/settings/access", status_code=303)
    form = await request.form()
    # Always the code, loopback included: a proxy that adds no header on this machine is loopback, and the owner has a
    # terminal. Only /setup, a fresh site's own page, has the first-hour exemption.
    refused = setup_code_gate(request, form.get("code"))
    if refused:
        return refused
    env_password = bool(os.environ.get("CC_PASSWORD"))
    problem = None if env_password else auth.password_problem(form.get("password") or "", form.get("confirm") or "")
    if problem:
        r = settings_render(request, "access", signin_error=problem)
        r.status_code = 400
        return r

    def save() -> None:
        with db() as conn:
            if not env_password:
                auth.store_set(conn, "password", auth.hash_password(form.get("password")))
            rotate_secret(conn)
            auth.store_set(conn, SIGNIN_KEY, "1")

    await asyncio.to_thread(save)
    await asyncio.to_thread(load_auth)
    r = RedirectResponse(f"{root}{signin_return(form.get('next')) or '/settings/access?saved=signin'}", status_code=303)
    set_session(r, request, auth.new_session(AUTH["secret"]))
    await switch_alert(request, "on")
    return r


@site.post("/settings/signin/off")
async def signin_off(request: Request):
    """Back to an open site, for someone who knows the password. Wrong tries count with sign-in failures."""
    root = request.scope.get("root_path", "")
    if not AUTH["enabled"]:
        return RedirectResponse(f"{root}/settings/access", status_code=303)
    form = await request.form()
    key, now = sign_in_key(request), time.time()
    held = FAILURES.wait(key, now)
    if held > 0:
        r = settings_render(request, "access", signin_error=f"Too many tries. Wait {int(held) + 1} seconds.")
        r.status_code = 429
        return r
    # Counted before the check and forgiven after it, with nothing awaited in between the wait and the count,
    # so guesses sent in parallel cannot all get past the same wait.
    FAILURES.record(key, now)
    if not await asyncio.to_thread(auth.verify_password, AUTH["password"], form.get("password") or ""):
        r = settings_render(request, "access", signin_error="The password is not right.")
        r.status_code = 401
        return r
    FAILURES.clear(key)

    def save() -> None:
        with db() as conn:
            auth.store_set(conn, SIGNIN_KEY, "0")

    await asyncio.to_thread(save)
    AUTH["enabled"] = False
    await switch_alert(request, "off")
    return RedirectResponse(f"{root}/settings/access?saved=signin-off", status_code=303)


def cf_header(request: Request) -> str | None:
    """The Access token this request carries, if it came from a peer that may present one."""
    header = request.headers.get(proxyauth.HEADER)
    return header if header and proxyauth.peer_trusted(request.scope.get(TruePeer.KEY), PROXIES) else None


def cf_candidate(request: Request) -> bool:
    """Whether there is something to pin: a team is configured, no application is pinned yet, and this
    request carries a token that claims to be from that team. It is not trusted yet; the pin checks it."""
    if not CF["team"] or CF["aud"]:
        return False
    claims = proxyauth.unverified_claims(cf_header(request) or "")
    return bool(claims and claims.get("iss") == proxyauth.issuer(CF["team"]) and proxyauth.first_audience(claims))


PIN_LOCK = asyncio.Lock()


@site.post("/settings/signin/cloudflare")
async def signin_cloudflare(request: Request):
    """Pin the Access application. The team is already configured and is never taken from the request: this
    only learns the audience, from a token whose signature checks against that team's own keys, and never
    replaces a pin. The team's other applications issue tokens too, so a valid one is not enough: the person
    must be listed in CC_ACCESS_EMAILS, or hold the setup code (or be the machine itself), or be signed in with
    the password. A service token, which names no person, never pins."""
    root = request.scope.get("root_path", "")
    if not CF["team"] or CF["aud"]:
        return RedirectResponse(f"{root}/settings/access", status_code=303)
    form = await request.form()
    found = await asyncio.to_thread(PROXY.pin, cf_header(request) or "", CF["team"])
    if found is None:
        r = settings_render(request, "access", signin_error="Cloudflare Access did not vouch for this request. Open the site through its Cloudflare address and try again.")
        r.status_code = 400
        return r
    aud, claims = found
    email = claims.get("email") if isinstance(claims.get("email"), str) else ""
    if not email or (EMAILS and proxyauth.normalize_email(email) not in EMAILS):
        r = settings_render(request, "access", signin_error="That Cloudflare identity may not pin the application: list your e-mail in CC_ACCESS_EMAILS in .env.")
        r.status_code = 403
        return r
    if not (EMAILS or auth.valid_session(AUTH["secret"], session_of(request))):
        refused = setup_code_gate(request, form.get("code"))
        if refused:
            return refused

    def save() -> bool:
        with db() as conn:
            if auth.store_get(conn, "cf_aud"):
                return False  # compare and set: a pin that is already there stays
            auth.store_set(conn, "cf_aud", aud)
            rotate_secret(conn)
            auth.store_set(conn, SIGNIN_KEY, "1")
        return True

    async with PIN_LOCK:  # the check and the save are one step, however many taps arrive together
        if CF["aud"] or not await asyncio.to_thread(save):
            return RedirectResponse(f"{root}/settings/access", status_code=303)
        await asyncio.to_thread(load_auth)
    who = f"{proxyauth.identity(claims)} (application …{aud[-6:]})"
    LOG.info("Cloudflare Access pinned for %s by %s", CF["team"], who)
    await asyncio.to_thread(recorder.notify, f"Sign-in through Cloudflare Access ({CF['team']}) was pinned by {who} at {datetime.now(TZ).strftime('%b %-d, %-I:%M %p')}", "login")
    return RedirectResponse(f"{root}{signin_return(form.get('next')) or '/settings/access?saved=cloudflare'}", status_code=303)


@site.post("/settings/signin/cloudflare/set")
async def signin_cloudflare_set(request: Request):
    """Name the team (and, if known, the application) by hand. Only for someone signed in with the password:
    this is what decides whose tokens the site will believe."""
    root = request.scope.get("root_path", "")
    if not auth.valid_session(AUTH["secret"], session_of(request)):
        return refusal(request, 403, "Sign in with the password to change this.", "Cloudflare Access cannot be pointed somewhere else by itself.")
    if CF["env_team"]:
        r = settings_render(request, "access", signin_error="Cloudflare Access is set in .env (CF_ACCESS_TEAM); change it there and restart.")
        r.status_code = 409
        return r
    form = await request.form()
    team, aud = proxyauth.normalize_team(form.get("team")), (form.get("aud") or "").strip()[:128]
    if not team:
        r = settings_render(request, "access", signin_error="That is not a Cloudflare Access team. Use the name before .cloudflareaccess.com.")
        r.status_code = 400
        return r

    def save() -> None:
        with db() as conn:
            auth.store_set(conn, "cf_team", team)
            auth.store_set(conn, "cf_aud", aud)

    await asyncio.to_thread(save)
    await asyncio.to_thread(load_auth)
    await switch_alert(request, f"set to use Cloudflare Access ({team})")
    return RedirectResponse(f"{root}/settings/access?saved=cloudflare-set", status_code=303)


@site.post("/settings/signin/cloudflare/off")
async def signin_cloudflare_off(request: Request):
    """Stop using Cloudflare Access. The guard has already asked for a password session or a valid Access
    token. Without a password the site then asks for one on /setup, as a new install does."""
    root = request.scope.get("root_path", "")
    if CF["env_team"] or CF["env_aud"]:
        r = settings_render(request, "access", signin_error="Cloudflare Access is set in .env (CF_ACCESS_TEAM, CF_ACCESS_AUD); remove it there and restart.")
        r.status_code = 409
        return r
    if not CF["team"]:
        return RedirectResponse(f"{root}/settings/access", status_code=303)
    if AUTH["password"]:
        # With a password, only the password undoes it: Cloudflare getting you in is not leave to leave Cloudflare.
        if not auth.valid_session(AUTH["secret"], session_of(request)):
            return refusal(request, 403, "Sign in with the password to do that.", "Stopping Cloudflare Access needs the password.")
    elif not (getattr(request.state, "cf", None) or {}).get("email"):
        return refusal(request, 403, "Sign in as a person to do that.", "A service token cannot stop Cloudflare Access.")

    def save() -> None:
        with db() as conn:
            for key in ("cf_team", "cf_aud"):
                auth.store_set(conn, key, "")

    await asyncio.to_thread(save)
    await asyncio.to_thread(load_auth)
    await switch_alert(request, "off for Cloudflare Access")
    return RedirectResponse(f"{root}/settings/access?saved=cloudflare-off", status_code=303)


@site.post("/settings/password")
async def password_change(request: Request):
    """The one household password, changed from Settings. Wrong current passwords count with
    sign-in failures; a change signs out every other device by renewing the signing key."""
    root = request.scope.get("root_path", "")
    if not AUTH["enabled"] or not AUTH["password"]:
        return RedirectResponse(f"{root}/settings/access", status_code=303)
    if os.environ.get("CC_PASSWORD"):
        # The environment's password wins at every start; a change here would report done and change nothing.
        r = settings_render(request, "access", pw_error="The password is set in .env (CC_PASSWORD); change it there and restart.")
        r.status_code = 409
        return r
    form = await request.form()
    key, now = sign_in_key(request), time.time()
    held = FAILURES.wait(key, now)
    if held > 0:
        r = settings_render(request, "access", pw_error=f"Too many tries. Wait {int(held) + 1} seconds.")
        r.status_code = 429
        return r
    FAILURES.record(key, now)  # counted before the check and forgiven after it: parallel guesses cannot all pass the same wait
    if not await asyncio.to_thread(auth.verify_password, AUTH["password"], form.get("current") or ""):
        r = settings_render(request, "access", pw_error="The current password is not right.")
        r.status_code = 401
        return r
    FAILURES.clear(key)
    problem = auth.password_problem(form.get("password") or "", form.get("confirm") or "")
    if problem:
        r = settings_render(request, "access", pw_error=problem)
        r.status_code = 400
        return r
    FAILURES.clear(key)
    with db() as conn:
        auth.store_set(conn, "password", auth.hash_password(form.get("password")))
        if not os.environ.get("CC_SECRET"):
            auth.store_set(conn, "secret", auth.new_secret())
    load_auth()
    r = RedirectResponse(f"{root}/settings/access?saved=password", status_code=303)
    set_session(r, request, auth.new_session(AUTH["secret"]))
    await asyncio.to_thread(
        recorder.notify,
        f"Car Command's password was changed from {auth.client_ip(request.headers, request.client.host if request.client else None)} at {datetime.now(TZ).strftime('%b %-d, %-I:%M %p')}",
        "login",
    )
    return r


def sources_page(request: Request, status_code: int = 200, **extra) -> HTMLResponse:
    r = settings_render(request, "cars", **extra)
    r.status_code = status_code
    return r


def _teslamate_connect(url: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10)


def teslamate_config(cars: list[dict]) -> dict:
    """Follow every car the database has, and keep the list so the page can offer each one."""
    return {"vins": [c["vin"] for c in cars], "cars": [{"vin": c["vin"], "name": c.get("name") or ""} for c in cars]}


def teslamate_status(vins: list[str]) -> tuple | None:
    """How the cars a TeslaMate source follows are doing, as one (last success, error)."""
    got = [v for k, v in teslamate.results.items() if not vins or k in vins]
    if not got:
        return None
    errors = [e for _ok, e in got if e]
    oks = [ok for ok, _e in got if ok]
    return (max(oks) if oks else None, errors[0] if errors else None)


@site.post("/settings/sources")
async def source_add(request: Request):
    """Test and add: a TeslaFi token or a TeslaMate database, saved only when it answers."""
    form = await request.form()
    kind = form.get("kind")
    if kind not in sources.KINDS:
        raise HTTPException(404, "No such kind of source.")
    if kind == "tesla":
        # a Tesla account comes only through Tesla's own sign-in, on its page
        return RedirectResponse(f"{request.scope.get('root_path', '')}/settings/sources/tesla", status_code=303)
    if not VAULT.ready:
        return sources_page(request, 409, source_error=f"Nothing secret can be saved yet: {VAULT.problem}. Run make secret-key on the host, then restart the site.")
    if kind == "lucid":
        # The connector container signs in (it carries the Lucid library) and keeps only the
        # token Lucid gives back; until then the e-mail and password wait here, sealed.
        email, password = (form.get("email") or "").strip(), form.get("password") or ""
        if not email or not password:
            return sources_page(request, 400, source_error="The Lucid account's e-mail and password, both.")
        with db() as conn:
            sources.add(conn, VAULT, "lucid", None, "Lucid", {"pending": True}, json.dumps({"email": email, "password": password}))
            sources.bump(conn)
            conn.commit()
        return sources_page(request, added={"kind": "Lucid", "pending": True})
    if kind == "carconnectivity":
        # The connector container installs the brand's plugin when its image lacks it, writes
        # the block into its config and restarts; the sealed sign-in waits here until then.
        entry = connectors.get(form.get("connector"))
        if entry is None:
            return sources_page(request, 400, source_error="Pick a brand from the list.")
        values, problem = connectors.values(entry, form)
        if problem:
            return sources_page(request, 400, source_error=problem)
        with db() as conn:
            sources.add_connector(conn, VAULT, entry.key, entry.name, connectors.row_config(entry), values)
            sources.bump(conn)
            conn.commit()
        wait = f"Adding {entry.name}: CarConnectivity installs its plugin if it needs to, then restarts. That can take a minute or two; this page shows how it went."
        return sources_page(request, added={"kind": f"{entry.name} (CarConnectivity)", "pending": True, "wait": wait})
    secret = (form.get("token") if kind == "teslafi" else form.get("url")) or ""
    secret = secret.strip()
    # Off the event loop: a check can wait 20 s on TeslaFi or 10 s on a TeslaMate host, and every page would wait with it.
    checked = await asyncio.to_thread(sources.check_teslafi, secret, teslafi.fetch) if kind == "teslafi" else await asyncio.to_thread(sources.check_teslamate, secret, _teslamate_connect)
    if not checked["ok"]:
        return sources_page(request, 400, source_error=checked["error"])
    with db() as conn:
        if kind == "teslafi":
            sources.add(conn, VAULT, "teslafi", checked["vin"], checked["name"], {}, secret)
        else:
            sources.add(conn, VAULT, "teslamate", None, "TeslaMate", teslamate_config(checked["cars"]), secret)
        conn.commit()
    apply_sources()
    return sources_page(request, added={"kind": sources.KINDS[kind], "last4": sources.last4(secret), **checked})


# ---- Connect Tesla: the app's keys come from .env; the account (tokens, sealed) is a source of kind tesla.
TESLA_DOMAIN = os.environ.get("TESLA_DOMAIN", "").strip()
TESLA_REDIRECT = os.environ.get("TESLA_REDIRECT_URI", "").strip() or (f"https://{TESLA_DOMAIN}/settings/sources/tesla/callback" if TESLA_DOMAIN else "")
TESLA_STATE_KEY = "tesla_oauth_state"
TESLA_STATE_MAX_S = 600
TESLA_LOCK = threading.Lock()  # one refresh at a time: Tesla's refresh tokens work once


def tesla_app() -> tesla_fleet.Fleet | None:
    cid, secret = os.environ.get("TESLA_CLIENT_ID", "").strip(), os.environ.get("TESLA_CLIENT_SECRET", "").strip()
    return tesla_fleet.Fleet(cid, secret) if cid and secret else None


def tesla_account(conn) -> dict | None:
    return next((r for r in sources.load(conn, VAULT) if r["kind"] == "tesla"), None)


def tesla_access(refresh: bool = True) -> tuple[str, str] | None:
    """A usable access token and the account's API base, refreshed when it is about to run out.
    The new refresh token is sealed and committed before the access token is handed out: the
    old one is already spent, so losing the new one would mean signing in again. With refresh
    off (a page reading kept answers) a token near its end is handed back as it is, unless the
    last refresh failed."""
    fleet = tesla_app()
    if not fleet:
        return None
    with TESLA_LOCK, db() as conn:
        row = tesla_account(conn)
        if not row or not row["plain"]:
            return None
        tok, base = json.loads(row["plain"]), row["config"].get("base") or tesla_fleet.NA
        if not refresh and float(tok.get("expires_at") or 0) - time.time() <= 120:
            return None if row["last_error"] else (tok["access_token"], base)
        if float(tok.get("expires_at") or 0) - time.time() > 120:
            if row["last_error"]:
                sources.noted(conn, row["id"], True, None)
                conn.commit()
            return tok["access_token"], base
        try:
            new = fleet.refresh(tok["refresh_token"])
        except tesla_fleet.TeslaError as err:
            gone = err.status in (400, 401)
            sources.noted(conn, row["id"], False, "Tesla asked to sign in again." if gone else err.public)
            conn.commit()
            return None
        new["refresh_token"] = new.get("refresh_token") or tok["refresh_token"]
        sources.reseal(conn, VAULT, row["id"], json.dumps(new), row["kind"])
        conn.commit()
        return new["access_token"], base


def tesla_check() -> dict:
    """The account's test: a token that works and the cars it answers for."""
    got = tesla_access()
    fleet = tesla_app()
    if not got or not fleet:
        return {"ok": False, "error": "Tesla needs a new sign-in: Connect Tesla, step 3."}
    try:
        cars = fleet.vehicles(*got)
    except tesla_fleet.TeslaError as err:
        return {"ok": False, "error": err.public}
    return {"ok": True, "cars": cars}


TESLA_CACHE: dict[str, tuple[float, object]] = {}
TESLA_OLD_S = 300  # the longest any answer is kept: older than this, the page says it is checking


class TeslaNotHeld(Exception):
    """A held-only read found something Tesla has not been asked yet."""


class TeslaAsk:
    """How tesla_facts gets each answer. Asking (the refresher, the buttons): a kept answer
    younger than its ttl, else Tesla; a refusal is kept for the page and asked again next time.
    Held only (the page): whatever is kept, however old, never Tesla. `stale` notes an answer
    past its ttl, `age` the oldest one used."""

    def __init__(self, held: bool = False):
        self.held, self.stale, self.age = held, False, 0.0

    def __call__(self, key: str, ttl: float, fn):
        hit, now = TESLA_CACHE.get(key), time.time()
        if self.held:
            if hit is None:
                raise TeslaNotHeld(key)
            self.stale = self.stale or now - hit[0] >= ttl
            self.age = max(self.age, now - hit[0])
            if isinstance(hit[1], tesla_fleet.TeslaError):
                raise hit[1]
            return hit[1]
        if hit and now - hit[0] < ttl and not isinstance(hit[1], tesla_fleet.TeslaError):
            return hit[1]
        try:
            value = fn()
        except tesla_fleet.TeslaError as err:
            TESLA_CACHE[key] = (time.time(), err)
            raise
        TESLA_CACHE[key] = (time.time(), value)
        return value


def _tesla_cached(key: str, ttl: float, fn):
    """Tesla's answers for a while: the page is opened and re-opened, and some calls are billed."""
    return TeslaAsk()(key, ttl, fn)


def tesla_held() -> tuple[dict | None, bool]:
    """Connect Tesla's facts from what is kept, with no call to Tesla or the domain: (facts, or
    None before the first round; whether to say it is checking). Anything missing or past its
    ttl wakes the refresher, so the next load has it."""
    ask = TeslaAsk(held=True)
    try:
        facts = tesla_facts(ask=ask)
    except TeslaNotHeld:
        facts = None
    if facts is None and tesla_refresher.rounds:
        # Asked before and cleared since by an action on the page (Register, Start, a sign-in):
        # that page wants Tesla's answer now, as it always did.
        return tesla_facts(), False
    if facts is None or ask.stale:
        tesla_refresher.wake()
    return facts, facts is None or ask.age >= TESLA_OLD_S


def tesla_key_domain() -> str:
    """The domain Tesla keeps this app's key under. Tesla holds one domain per app, and a
    stream needs its own address registered, so once a stream address is set it is the
    app's domain for the key, its registration and the pairing links; the site's domain
    stays the sign-in redirect."""
    return TESLA_STREAM_HOST if tesla_net.valid_host(TESLA_STREAM_HOST) else TESLA_DOMAIN


def tesla_public_check(pem: str, host: str | None = None) -> str | None:
    """Fetch the key the way Tesla will, from outside and with no session. None when it matches.
    `host` is the domain to read it from: the site's, or the stream address's."""
    host = host or tesla_key_domain()
    url = f"https://{host}{tesla_keys.PUBLIC_PATH}"
    try:
        r = httpx.get(url, timeout=10, follow_redirects=False, headers={"User-Agent": "carcommand/0.1"})
    except httpx.HTTPError as err:
        return f"{host} did not answer ({type(err).__name__})."
    if r.status_code != 200:
        login = " That is a sign-in page in front of it: the path needs to bypass the login at the edge." if r.status_code in (302, 303, 401, 403) else ""
        return f"{host} answered {r.status_code} for the key.{login}"
    if r.text.strip() != tesla_keys.public_pem(pem).strip():
        return f"{host} serves a different key at that path."
    return None


def tesla_facts(ask: TeslaAsk | None = None) -> dict:
    """What the checklist needs, asked of Tesla and the domain (cached, so a reload is quick).
    `ask` held only reads the kept answers and calls nothing (tesla_held)."""
    ask = ask or TeslaAsk()
    f = {"app_ok": False, "app_error": None, "key": False, "public_ok": False, "public_error": None, "registered": False, "register_error": None, "account": False, "account_error": None, "cars": []}
    fleet = tesla_app()
    if not fleet or not TESLA_DOMAIN:
        return f
    try:
        ask("partner", 300, fleet.partner_token)
        f["app_ok"] = True
    except tesla_fleet.TeslaError as err:
        f["app_error"] = err.public
        return f
    pem = tesla_keys.stored(state_get, VAULT)
    f["key"] = bool(pem)
    if pem:
        f["public_error"] = ask("public", 300, lambda: tesla_public_check(pem))
        f["public_ok"] = f["public_error"] is None
        try:
            f["registered"] = ask("registered", 300, lambda: fleet.registered_key(tesla_key_domain())) == tesla_keys.public_hex(pem)
        except tesla_fleet.TeslaError as err:
            f["register_error"] = err.public
    with db() as conn:
        row = tesla_account(conn)
    if not row:
        return f
    f["account"] = True
    got = tesla_access(refresh=not ask.held)
    if not got:
        with db() as conn:
            row = tesla_account(conn) or row
        f["account_error"] = row.get("last_error") or "Tesla needs a new sign-in."
        return f
    try:
        cars = ask("vehicles", 60, lambda: fleet.vehicles(*got))
        status = ask("fleet_status", 8, lambda: fleet.fleet_status(got[0], got[1], [c["vin"] for c in cars])) if cars else {}
    except tesla_fleet.TeslaError as err:
        f["account_error"] = err.public
        return f
    names = {v["vin"]: v["name"] for v in all_vehicles()}
    f["cars"] = [{**c, "paired": False, **status.get(c["vin"], {}), "known_as": names.get(c["vin"])} for c in cars]
    tesla_stream_facts(f, fleet, got, pem, (row.get("config") or {}).get("stream") or [], ask)
    return f


def tesla_stream_facts(f: dict, fleet, got: tuple[str, str], pem: str | None, streaming: list[str], ask: TeslaAsk | None = None) -> None:
    """Step 5: the stream address, the receiver as a car meets it, Tesla's view of each started
    car's config and the errors cars reported, and what the receiver has heard (from memory)."""
    ask = ask or TeslaAsk()
    host = TESLA_STREAM_HOST
    f.update(
        stream_host=host or None,
        stream_host_ok=tesla_net.valid_host(host),
        stream_dropped=sum(tesla_stream_worker.dropped.values()),
        stream_lost=tesla_stream_worker.lost,
        stream_error=tesla_stream_worker.error,
    )
    per = f["stream_cars"] = {}
    stored = _tesla_cached("stream_heard", 30, lambda: stream_heard(sorted(streaming)))
    now = datetime.now(timezone.utc)
    for c in f["cars"]:
        # memory knows only what came since the site started; cc_tesla_raw knows the rest
        heard = tesla_stream_worker.state.get(c["vin"]) or stored.get(c["vin"])
        per[c["vin"]] = {
            "on": c["vin"] in streaming,
            "config_known": False,
            "config": None,
            "live": bool(heard and heard["connected"] and now - heard["at"] <= tesla_live.LIVE),
            "last_at": heard["at"] if heard else None,
            "errors": list(tesla_stream_worker.car_errors.get(c["vin"]) or []),
            "sent": state_get(f"stream_fields:{c['vin']}") if c["vin"] in streaming else None,
        }
    if not f["stream_host_ok"] or not pem:
        return
    f["receiver"] = dict(ask("receiver", 60, lambda: tesla_net.reach(host)))
    expires = f["receiver"].get("expires")
    f["receiver"]["expires_at"] = datetime.fromtimestamp(expires, timezone.utc) if expires else None
    f["stream_public_error"] = ask("stream_public", 300, lambda: tesla_public_check(pem, host))
    try:
        f["stream_registered"] = ask("stream_registered", 300, lambda: fleet.registered_key(host)) == tesla_keys.public_hex(pem)
    except tesla_fleet.TeslaError as err:
        f["stream_register_error"] = err.public
    on = [vin for vin, s in per.items() if s["on"]]
    for vin in on:
        try:
            per[vin].update(config_known=True, config=ask(f"stream_config:{vin}", 60, lambda vin=vin: fleet.telemetry_config(got[0], got[1], vin)))
        except tesla_fleet.TeslaError as err:
            per[vin]["errors"].insert(0, {"name": "Tesla", "body": err.public, "at": None})
    if on:
        try:
            for e in ask("stream_errors", 300, lambda: fleet.telemetry_errors(host)):
                if e["vin"] in per:
                    per[e["vin"]]["errors"].append({"name": e["name"], "body": e["error"], "at": e["at"]})
        except tesla_fleet.TeslaError as err:
            LOG.warning("tesla stream errors: %s", err)


def stream_heard(vins: list[str]) -> dict[str, dict]:
    """Each started car's newest stored stream record, as the listener keeps it in memory:
    {"at", "connected"}. One index lookup a car ((vin, at, kind) backwards); a database that
    cannot answer leaves the page with what memory holds."""
    out = {}
    try:
        with db() as conn:
            for vin in vins:
                row = conn.execute("SELECT at, kind, record->>'status' AS status FROM cc_tesla_raw WHERE vin = %s ORDER BY at DESC LIMIT 1", (vin,)).fetchone()
                if row:
                    out[vin] = {"at": row["at"], "connected": not (row["kind"] == "connectivity" and str(row["status"] or "").upper() == "DISCONNECTED")}
    except Exception as err:  # noqa: BLE001 - the page shows what memory has
        LOG.warning("tesla stream: last messages: %s", err)
    return out


def tesla_billing() -> dict | None:
    """Whether the signed-in account's token may read charging history and bills, read from
    the token itself (no call to Tesla); None with no account."""
    with db() as conn:
        row = tesla_account(conn)
    if not row or not row["plain"]:
        return None
    try:
        token = json.loads(row["plain"]).get("access_token")
    except ValueError:
        return None
    granted = tesla_fleet.BILLING_SCOPE in tesla_fleet.granted_scopes(token)
    out = {"granted": granted, "count": 0, "read_at": None}
    if granted:
        with db() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM cc_billed_charges").fetchone()
            out["count"] = n["n"] if isinstance(n, dict) else n[0]
            out["read_at"] = billed._time(state_get(billed.STAMP))
    return out


def tesla_render(request: Request, status_code: int = 200, facts: dict | None = None, **extra) -> HTMLResponse:
    st = current_settings()
    steps = teslapage.steps(facts) if facts is not None else None
    links = {}
    if facts:
        for c in facts["cars"]:
            link = teslapage.key_link(tesla_key_domain(), c["vin"])
            links[c["vin"]] = {"url": link, "qr": None if c.get("paired") else _qr(link)}
    r = page(
        request,
        "settings_tesla.html",
        car=pick(request),
        st=st,
        conv=settingsform.converted(st),
        section="cars",
        label="Connect Tesla",
        active="settings-cars",
        back=("/settings/cars", "Cars and sources"),
        saved=False,
        said=None,
        steps=steps,
        facts=facts,
        links=links,
        tesla_domain=TESLA_DOMAIN,
        key_domain=tesla_key_domain(),
        tesla_redirect=TESLA_REDIRECT,
        tesla_scopes=tesla_fleet.SCOPES.split(),
        energy_scope=tesla_energy_scope(),
        stream=teslapage.stream_cars(facts) if facts else [],
        stream_host=TESLA_STREAM_HOST,
        stream_port=tesla_net.PORT,
        **extra,
    )
    r.status_code = status_code
    return r


def _qr(text: str) -> str:
    """An inline QR, dark on white in either theme so any phone camera reads it."""
    import segno

    return segno.make(text, error="m").svg_inline(scale=4, dark="#111", light="#fff", border=2)


@site.get("/settings/sources/tesla", response_class=HTMLResponse)
async def tesla_page(request: Request, signed: str = "", registered: str = "", fresh: str = "", streaming: str = ""):
    if fresh:
        TESLA_CACHE.clear()
        facts, checking = await asyncio.to_thread(tesla_facts), False
    else:
        facts, checking = await asyncio.to_thread(tesla_held)
    try:
        bills = await asyncio.to_thread(tesla_billing)
    except Exception as err:  # noqa: BLE001 - the checklist matters more than this line
        LOG.warning("tesla billing state: %s", err)
        bills = None
    try:
        history_step = await asyncio.to_thread(tesla_history_step, facts) if facts else []
    except Exception as err:  # noqa: BLE001 - the checklist matters more than step 6
        LOG.warning("tesla history step: %s", err)
        history_step = []
    try:
        pw = await asyncio.to_thread(energy_flow_context, dict(request.query_params))
    except Exception as err:  # noqa: BLE001 - the Tesla checklist matters more than the Powerwall's
        LOG.warning("energy flow: %s", err)
        pw = None
    return tesla_render(request, facts=facts, signed=bool(signed), registered=bool(registered), streaming=bool(streaming), billing=bills, history_step=history_step, checking=checking, pw=pw)


async def tesla_register_do() -> tuple[int, str | None]:
    """Make the key if there is none, check the domain serves it, then tell Tesla.
    (status, why not): 409 when a step before this one is missing, 400 when the domain or
    Tesla said no, 200 and None when Tesla took it. The page and the admin API both call this."""
    fleet = tesla_app()
    if not fleet or not TESLA_DOMAIN:
        return 409, "Add the app's keys and domain to .env first (step 1)."
    if not VAULT.ready:
        return 409, f"Nothing secret can be kept yet: {VAULT.problem}."
    pem = tesla_keys.ensure(state_get, state_set, VAULT)
    TESLA_CACHE.clear()
    problem = await asyncio.to_thread(tesla_public_check, pem)
    if not problem:
        try:
            await asyncio.to_thread(fleet.register, tesla_key_domain())
        except tesla_fleet.TeslaError as err:
            problem = err.public
    TESLA_CACHE.clear()
    return (400, problem) if problem else (200, None)


@site.post("/settings/sources/tesla/register")
async def tesla_register(request: Request):
    status, problem = await tesla_register_do()
    if status == 409:
        return tesla_render(request, 409, tesla_error=problem)
    if problem:
        facts = await asyncio.to_thread(tesla_facts)
        facts["register_error"] = facts["register_error"] or problem
        return tesla_render(request, 400, facts=facts)
    return RedirectResponse(f"{request.scope.get('root_path', '')}/settings/sources/tesla?registered=1", status_code=303)


@site.get("/settings/sources/tesla/keys.json")
async def tesla_keys_json(request: Request):
    """Which cars hold the key now, for the page to tick while you are at the car."""

    def look():
        fleet, got = tesla_app(), tesla_access()
        if not fleet or not got:
            return None
        cars = _tesla_cached("vehicles", 60, lambda: fleet.vehicles(*got))
        status = _tesla_cached("fleet_status", 8, lambda: fleet.fleet_status(got[0], got[1], [c["vin"] for c in cars])) if cars else {}
        return {vin: bool(s.get("paired")) for vin, s in status.items()}

    try:
        got = await asyncio.to_thread(look)
        if got is None:
            return JSONResponse({"error": "Tesla needs a new sign-in (step 3)."}, status_code=409)
        return JSONResponse(got)
    except tesla_fleet.TeslaError as err:
        return JSONResponse({"error": err.public}, status_code=502)


def _tesla_streaming_set(change) -> list[str]:
    """Change the started cars under the refresh lock, keep them with the account, and point the listener at them."""
    with TESLA_LOCK, db() as conn:
        row = tesla_account(conn)
        if not row:
            return []
        config = dict(row.get("config") or {})
        config["stream"] = sorted(change(set(config.get("stream") or [])))
        sources.set_config(conn, row["id"], config)
        conn.commit()
    tesla_stream_worker.use(config["stream"])
    return config["stream"]


def tesla_stream_start(vin: str) -> str | None:
    """Sign a config pointing this car at the receiver and hand it to Tesla, and keep its
    stream. None when Tesla took it, else why not."""
    problem = tesla_stream_send(vin)
    if problem:
        return problem
    _tesla_streaming_set(lambda s: s | {vin})
    return None


def tesla_stream_send(vin: str) -> str | None:
    """Sign today's config for this car and hand it to Tesla; what it was sent is kept.
    None when Tesla took it, else why not. Raises TeslaError when Tesla cannot be asked."""
    fleet, access = tesla_app(), tesla_access()
    if not fleet or not access:
        return "Tesla needs a new sign-in (step 3)."
    if not tesla_net.valid_host(TESLA_STREAM_HOST):
        return "Set TESLA_STREAM_HOST in stack/.env first."
    pem = tesla_keys.stored(state_get, VAULT)
    if not pem:
        return "The app has no key yet (step 2)."
    cars = {c["vin"]: c["name"] for c in _tesla_cached("vehicles", 60, lambda: fleet.vehicles(*access))}
    if vin not in cars:
        return "That car is not on the signed-in Tesla account."
    token = tesla_jws.token(pem, tesla_net.config(TESLA_STREAM_HOST))
    answer = fleet.telemetry_create(access[0], access[1], [vin], token)
    for reason, vins in answer["skipped"].items():
        if vin in vins:
            return teslapage.skipped_text(reason, cars[vin])
    state_set(f"stream_fields:{vin}", tesla_net.fields_version())  # what it was sent: Connect Tesla says when that is out of date
    return None


def tesla_refresh_configs() -> list[str]:
    """Once per change to the field list (tesla_net.FIELDS): every started car whose config
    was signed for another list is sent the new one, and the try is kept, so a car Tesla
    refuses is not asked again until the list changes again or the owner presses Update.
    Never a car not started: each is checked against the stream list right before its send,
    under STREAM_LOCK, which Stop takes too, so a Stop pressed meanwhile wins. Tesla out of
    reach or failing (no answer, 5xx) leaves the car one more try at the next start
    (teslapage.tried_after). Returns the cars tried."""
    if not tesla_net.valid_host(TESLA_STREAM_HOST) or not tesla_app() or not tesla_access():
        return []
    started = sorted(tesla_stream_worker.vins)
    now = tesla_net.fields_version()
    tried = {v: state_get(f"stream_fields_tried:{v}") for v in started}
    todo = teslapage.to_update(started, {v: state_get(f"stream_fields:{v}") for v in started}, tried, now)
    done = []
    for vin in todo:
        with STREAM_LOCK:
            if vin not in tesla_started_now():
                continue  # stopped since: never sent again from here
            done.append(vin)
            status = 200
            try:
                problem = tesla_stream_send(vin)
            except tesla_fleet.TeslaError as err:
                problem, status = err.public, err.status
            state_set(f"stream_fields_tried:{vin}", teslapage.tried_after(tried.get(vin), now, problem is not None and (status is None or status >= 500)))
        if problem:
            LOG.warning("tesla stream: the new field list (%s) was not sent to a started car: %s", now, problem)
        else:
            LOG.info("tesla stream: a started car was sent the new field list (%s)", now)
    if done:
        TESLA_CACHE.clear()
    return done


STREAM_LOCK = threading.Lock()  # the background re-send and Stop, one at a time


def tesla_started_now() -> list[str]:
    """The cars started on Connect Tesla as the account keeps them now."""
    with db() as conn:
        row = tesla_account(conn)
    return list(((row or {}).get("config") or {}).get("stream") or [])


async def tesla_stream_register_do() -> tuple[int, str | None]:
    """Tell Tesla about the stream address: cars stream only to a domain the app registered,
    and it must serve the app's key. (status, why not), as tesla_register_do."""
    fleet, pem = tesla_app(), tesla_keys.stored(state_get, VAULT)
    if not fleet or not pem or not tesla_net.valid_host(TESLA_STREAM_HOST):
        return 409, "Finish steps 1 and 2 and set TESLA_STREAM_HOST first."
    problem = await asyncio.to_thread(tesla_public_check, pem, TESLA_STREAM_HOST)
    if not problem:
        try:
            await asyncio.to_thread(fleet.register, TESLA_STREAM_HOST)
        except tesla_fleet.TeslaError as err:
            problem = err.public
    TESLA_CACHE.clear()
    return (400, problem) if problem else (200, None)


@site.post("/settings/sources/tesla/stream/register")
async def tesla_stream_register(request: Request):
    status, problem = await tesla_stream_register_do()
    if status == 409:
        return tesla_render(request, 409, tesla_error=problem)
    if problem:
        return tesla_render(request, 400, facts=await asyncio.to_thread(tesla_facts), tesla_error=problem)
    return RedirectResponse(f"{request.scope.get('root_path', '')}/settings/sources/tesla?streaming=1#step-stream", status_code=303)


async def tesla_stream_start_do(vin: str) -> str | None:
    """Start one car's stream; None when Tesla took it, else why not."""
    try:
        problem = await asyncio.to_thread(tesla_stream_start, vin)
    except tesla_fleet.TeslaError as err:
        problem = err.public
    TESLA_CACHE.clear()
    return problem


@site.post("/settings/sources/tesla/stream/start")
async def tesla_stream_start_route(request: Request):
    problem = await tesla_stream_start_do(str((await request.form()).get("vin") or "").strip())
    if problem:
        return tesla_render(request, 400, facts=await asyncio.to_thread(tesla_facts), tesla_error=problem)
    return RedirectResponse(f"{request.scope.get('root_path', '')}/settings/sources/tesla?streaming=1#step-stream", status_code=303)


async def tesla_stream_stop_do(vin: str) -> str | None:
    """Stop keeping a car's stream, and ask Tesla to take its config back out of the car.
    None when both are done, else what is left undone."""

    def stop():
        with STREAM_LOCK:  # a background re-send under way finishes first, then this one wins
            _tesla_streaming_set(lambda s: s - {vin})

    await asyncio.to_thread(stop)

    def tell_tesla():
        fleet, access = tesla_app(), tesla_access()
        if not fleet or not access:
            return "Car Command stopped keeping its records, but Tesla needs a new sign-in (step 3) to take the config out of the car."
        try:
            fleet.telemetry_delete(access[0], access[1], vin)
        except tesla_fleet.TeslaError as err:
            return f"Car Command stopped keeping its records; Tesla still has the config ({err.public}). Stop again to retry."
        return None

    problem = await asyncio.to_thread(tell_tesla)
    TESLA_CACHE.clear()
    return problem


@site.post("/settings/sources/tesla/stream/stop")
async def tesla_stream_stop(request: Request):
    problem = await tesla_stream_stop_do(str((await request.form()).get("vin") or "").strip())
    if problem:
        return tesla_render(request, 400, facts=await asyncio.to_thread(tesla_facts), tesla_error=problem)
    return RedirectResponse(f"{request.scope.get('root_path', '')}/settings/sources/tesla#step-stream", status_code=303)


def choose_writer(vin: str, kind: str) -> str | None:
    """Ask for a car's history to be handed to another of its sources: checked here, done by
    the switcher in the background (history_writer.switch). None when it is under way."""
    v = next((v for v in all_vehicles() if v["vin"] == vin), None)
    if v is None:
        return "No such car."
    current = source_of(v)
    if kind not in {o["kind"] for o in writer_options({vin: current}).get(vin, [])}:
        return "That source does not follow this car."
    last_ok = {"teslafi": teslafi.results, "teslamate": teslamate.results}.get(kind, {}).get(vin, (None, None))[0]
    if not switcher.request(vin, kind, history_writer.KIND_OF_SOURCE.get(current), last_ok, teslamate.spans if kind == "teslamate" else None):
        return "A switch for this car is already under way."
    return None


def after_switch(vin: str, kind: str) -> None:
    """Every worker replays the car at its next tick, the new writer from the boundary."""
    _cache.pop("writers", None)
    for worker in (tesla_stream_worker, teslafi, teslamate):
        worker.last_rebuild.pop(vin, None)
    if kind != "tesla":
        tesla_stream_worker.writing.discard(vin)  # no more live positions from the stream for it
    compare_worker.forget(vin)


@site.post("/settings/sources/writer")
async def writer_choose(request: Request):
    form = await request.form()
    vin, kind = str(form.get("vin") or "").strip(), str(form.get("kind") or "").strip()
    back = "/settings/sources/tesla#step-history" if form.get("back") == "tesla" else "/settings/cars"
    problem = await asyncio.to_thread(choose_writer, vin, kind)
    if problem:
        if form.get("back") == "tesla":
            return tesla_render(request, 400, facts=await asyncio.to_thread(tesla_facts), tesla_error=problem)
        return sources_page(request, 400, source_error=problem)
    return to_site(request, back)


def tesla_history_step(facts: dict) -> list[dict]:
    """Step 6, per started car Car Command already knows: its writer, the choice, the 7-day comparison kept."""
    names = {v["vin"]: v for v in all_vehicles()}
    started = [c for c in teslapage.stream_cars(facts) if c.get("on") and c["vin"] in names]
    writer = {c["vin"]: source_of(names[c["vin"]]) for c in started}
    options = writer_options(writer)
    return [
        {
            "vin": c["vin"],
            "name": c.get("known_as") or c.get("name"),
            "choices": options.get(c["vin"], []),
            "compare": compare_worker.get(c["vin"], tesla_compare.DAYS),
            **writer_trouble(c["vin"], history_writer.KIND_OF_SOURCE.get(writer[c["vin"]]), options.get(c["vin"], [])),
        }
        for c in started
    ]


@site.get("/settings/sources/tesla/compare", response_class=HTMLResponse)
async def tesla_compare_page(request: Request, vin: str = "", days: int = tesla_compare.DAYS):
    car = next((v for v in await asyncio.to_thread(all_vehicles) if v["vin"] == vin), None)
    if car is None:
        raise HTTPException(404, "No such car.")
    days = max(1, min(tesla_compare.MAX_DAYS, days))
    got = compare_worker.get(vin, days)
    st = current_settings()
    writer = source_of(car)
    return page(
        request,
        "settings_tesla_compare.html",
        car=pick(request),
        st=st,
        conv=settingsform.converted(st),
        section="cars",
        label="Stream beside history",
        active="settings-cars",
        saved=False,
        said=None,
        subject=car,
        days=days,
        choices=writer_options({vin: writer}).get(vin, []),
        writer_name=history_writer.NAMES.get(history_writer.KIND_OF_SOURCE.get(writer, ""), "its imports"),
        **got,
    )


@site.get("/dx/charging/invoice/{invoice_id}")
async def tesla_invoice(invoice_id: str):
    """A Supercharger invoice PDF, fetched from Tesla when it is opened and handed straight
    through: the browser never sees the token, and only an invoice a stored bill names
    can be asked for."""

    def get():
        with db() as conn:
            known = conn.execute("SELECT 1 FROM cc_billed_charges WHERE invoice_id = %s LIMIT 1", (invoice_id,)).fetchone()
        if not known:
            raise HTTPException(404, "No such invoice.")
        fleet, got = tesla_app(), tesla_access()
        if not fleet or not got:
            raise HTTPException(409, "Tesla needs a new sign-in: Settings › Cars › Connect Tesla.")
        try:
            return fleet.charging_invoice(got[0], got[1], invoice_id)
        except tesla_fleet.TeslaError as err:
            raise HTTPException(502, err.public) from None

    pdf = await asyncio.to_thread(get)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", invoice_id)[:80]
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="tesla-invoice-{safe}.pdf"', "Cache-Control": "private, no-store"})


@site.get("/settings/sources/tesla/start")
def tesla_start(request: Request, energy: int = 0):
    """Out to Tesla's sign-in; the state that must come back is kept here for ten minutes. `energy`
    (from the Energy page only) asks for the Powerwall scope beside the vehicle ones."""
    fleet = tesla_app()
    if not fleet or not TESLA_REDIRECT:
        return tesla_render(request, 409, tesla_error="Add the app's keys and domain to .env first (step 1).")
    state = secrets.token_urlsafe(24)
    state_set(TESLA_STATE_KEY, f"{state} {int(time.time())}" + (" e" if energy else ""))  # "e": back to the Powerwall step afterwards
    return RedirectResponse(fleet.authorize_url(TESLA_REDIRECT, state, energy=bool(energy) or energy_scope_wanted()), status_code=303)


@site.get("/settings/sources/tesla/callback")
async def tesla_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Back from Tesla: the state must be the one we sent, once, within ten minutes."""
    kept = (state_get(TESLA_STATE_KEY) or "").split()
    try:
        fresh = len(kept) in (2, 3) and bool(state) and secrets.compare_digest(kept[0], state) and time.time() - int(kept[1]) < TESLA_STATE_MAX_S
    except ValueError:
        fresh = False  # a kept value this code did not write is a spent link
    if kept:
        state_set(TESLA_STATE_KEY, "")
    if not fresh:
        return tesla_render(request, 400, tesla_error="That sign-in link is spent or too old. Start again from step 3.")
    if error or not code:
        return tesla_render(request, 400, tesla_error="The Tesla sign-in was cancelled." if error in ("access_denied", "") else f"Tesla said: {error[:80]}")
    fleet = tesla_app()
    if not fleet:
        return tesla_render(request, 409, tesla_error="Add the app's keys and domain to .env first (step 1).")
    try:
        tokens = await asyncio.to_thread(fleet.exchange, code, TESLA_REDIRECT)
    except tesla_fleet.TeslaError as err:
        return tesla_render(request, 400, tesla_error=err.public)
    try:
        base = await asyncio.to_thread(fleet.region, tokens["access_token"])
    except tesla_fleet.TeslaError:
        base = tesla_fleet.NA  # the code is spent: keep the tokens on the default region rather than lose them
    with TESLA_LOCK, db() as conn:  # a refresh still in flight for the old account cannot write over this one
        # the cars started streaming stay started: their configs live in the cars, not in the sign-in
        old = tesla_account(conn)
        streaming = ((old or {}).get("config") or {}).get("stream") or []
        sources.add(conn, VAULT, "tesla", None, "Tesla", {"base": base, **({"stream": streaming} if streaming else {})}, json.dumps(tokens))
        conn.commit()
    TESLA_CACHE.clear()  # the cars and keys asked of the account before belong to it, not to this one
    if len(kept) == 3 and kept[2] == "e":
        # signed in from the Powerwall step: take the account's site if it has one, and land on the step again
        await asyncio.to_thread(energy_adopt_after_sign_in, fleet, tokens["access_token"], base)
        return RedirectResponse(f"{request.scope.get('root_path', '')}/settings/sources/tesla?signed=1#pw", status_code=303)
    return RedirectResponse(f"{request.scope.get('root_path', '')}/settings/sources/tesla?signed=1", status_code=303)


def _source(source_id: int) -> dict:
    with db() as conn:
        row = next((r for r in sources.load(conn, VAULT) if r["id"] == source_id), None)
    if not row:
        raise HTTPException(404, "No such source.")
    return row


async def source_test_do(source_id: int) -> dict:
    """Test one kept source as its Test button does: {"ok", "error"?, ...what it found}.
    404 (HTTPException) for no such source. The result is noted on the row, except a
    Lucid account's: the connector's sign-in is its test, and its report is the result."""
    row = _source(source_id)
    if row["kind"] in ("lucid", "carconnectivity"):
        ok_at, error = connector_status()
        return {"ok": not error, "error": error, "name": "CarConnectivity reports its cars" if not error else ""}
    if not row["plain"]:
        checked = {"ok": False, "error": "This source was saved with another key and cannot be read. Remove it and add it again."}
    elif row["kind"] == "teslafi":
        checked = await asyncio.to_thread(sources.check_teslafi, row["plain"], teslafi.fetch)
    elif row["kind"] == "tesla":
        checked = await asyncio.to_thread(tesla_check)
    else:
        checked = await asyncio.to_thread(sources.check_teslamate, row["plain"], _teslamate_connect)
    with db() as conn:
        sources.noted(conn, source_id, checked["ok"], checked.get("error"))
        conn.commit()
    return checked


@site.post("/settings/sources/{source_id}/test")
async def source_test(request: Request, source_id: int):
    checked = await source_test_do(source_id)
    return sources_page(request, 200 if checked["ok"] else 400, tested={"id": source_id, **checked})


@site.post("/settings/sources/{source_id}/remove")
async def source_remove(request: Request, source_id: int):
    form = await request.form()
    row = _source(source_id)
    with db() as conn:
        if row["kind"] == "carconnectivity":
            # The connector takes it out of its config, uninstalls its plugin when asked and deletes the row.
            # A second Remove before it has acted marks it again (keeping an earlier "and its plugin"), never deletes it.
            asked = form.get("uninstall") == "on" or bool((row["config"] or {}).get("state") == "remove" and (row["config"] or {}).get("uninstall"))
            sources.retire(conn, source_id, row["config"] or {}, asked)
        else:
            sources.remove(conn, source_id)
        sources.bump(conn)  # the connector looks again: a removed Lucid source hands it back to the token file
        conn.commit()
    TESLA_CACHE.clear()  # a removed Tesla account's cars must not linger on Connect Tesla
    apply_sources()
    return to_site(request, f"/settings/cars?saved=1{_vin_q(request, '&')}")


@site.post("/settings/sources/{source_id}/cars")
async def source_cars(request: Request, source_id: int):
    """Which of a TeslaMate database's cars to follow."""
    form = await request.form()
    row = _source(source_id)
    chosen = [k[len("vin_") :] for k in form if k.startswith("vin_") and form.get(k) == "on"]
    with db() as conn:
        # The database's whole list stays beside the choice, so a car unticked can be ticked again.
        sources.set_config(conn, source_id, {**row["config"], "vins": chosen})
        conn.commit()
    apply_sources()
    return to_site(request, f"/settings/cars?saved=1{_vin_q(request, '&')}")


@site.post("/settings/sources/move")
async def source_move(request: Request):
    """Copy the sources in .env into the table. .env itself is not touched; the page then
    says which lines are no longer read."""
    if not VAULT.ready:
        return sources_page(request, 409, source_error=f"Nothing secret can be saved yet: {VAULT.problem}. Run make secret-key on the host, then restart the site.")
    # Everything is checked before anything is written: moving some cars and not others would
    # leave the rest unread in .env, since a moved kind no longer reads .env at all.
    ready, problems = [], []
    for vin, token in sources.env_tokens(os.environ):
        vin = vin or teslafi.vin_for_token.get(token)  # a bare token is named by the car the poller learned
        checked = {"ok": True, "vin": vin, "name": ""} if vin else await asyncio.to_thread(sources.check_teslafi, token, teslafi.fetch)
        if checked["ok"]:
            ready.append(("teslafi", checked["vin"], checked.get("name") or "", {}, token))
        else:
            problems.append(checked["error"])
    url = (os.environ.get("TESLAMATE_DB_URL") or "").strip()
    if url:
        checked = await asyncio.to_thread(sources.check_teslamate, url, _teslamate_connect)
        if checked["ok"]:
            ready.append(("teslamate", None, "TeslaMate", teslamate_config(checked["cars"]), url))
        else:
            problems.append(checked["error"])
    if problems:
        return sources_page(request, 400, source_error="Nothing moved; .env is still what is read. " + " ".join(problems))
    with db() as conn:
        for kind, vin, label, config, secret in ready:
            sources.add(conn, VAULT, kind, vin, label, config, secret)
        conn.commit()
    apply_sources()
    return to_site(request, f"/settings/cars?saved=moved{_vin_q(request, '&')}")


def rates_settings() -> dict:
    """The regional averages as Settings › Money shows them: one small status row and the
    regions home charges were in, never the file itself."""
    try:
        with db() as conn:
            status = averages_mod.status_row(conn)
            homes = conn.execute(
                "SELECT region, COUNT(*) AS n, COUNT(*) FILTER (WHERE cost_source = 'average') AS est FROM cc_charge_facts WHERE at_home AND region <> '' GROUP BY region ORDER BY 2 DESC LIMIT 5"
            ).fetchall()
    except Exception as err:  # noqa: BLE001 - no table yet: the page still shows the bundled copy
        LOG.warning("rate averages status: %s", err)
        status, homes = {}, []
    return averages_mod.settings_view(averages_mod.active(), status, homes, datetime.now(timezone.utc), RATES_URL, TZ)


@site.post("/settings/money/averages")
async def rates_refresh(request: Request):
    """Settings' "Update now": the background worker downloads the averages file on its
    next pass; the page does not wait for it."""
    rates_worker.request()
    return to_site(request, f"/settings/money?saved=averages{_vin_q(request, '&')}")


def _maps_redirect(request: Request, saved: str = "1") -> RedirectResponse:
    return to_site(request, f"/settings/maps?saved={saved}{_vin_q(request, '&')}")


@site.post("/settings/maps/regions/add")
async def maps_region_add(request: Request):
    form = await request.form()
    region_id = (form.get("id") or "").strip()
    if not valid_region_id(region_id):
        raise HTTPException(400, "Not a valid region id.")
    MAPS.add(region_id)
    return _maps_redirect(request)


@site.post("/settings/maps/regions/remove")
async def maps_region_remove(request: Request):
    form = await request.form()
    region_id = (form.get("id") or "").strip()
    if not valid_region_id(region_id):
        raise HTTPException(400, "Not a valid region id.")
    MAPS.remove(region_id)
    return _maps_redirect(request)


@site.post("/settings/maps/service")
async def maps_service(request: Request):
    form = await request.form()
    kw: dict = {"auto": form.get("auto") == "on"}
    if form.get("refresh") in ("monthly", "quarterly", "off"):
        kw["refresh"] = form.get("refresh")
    MAPS.settings(**kw)
    return _maps_redirect(request)


@site.post("/settings/maps/retry")
async def maps_retry(request: Request):
    MAPS.retry()
    return _maps_redirect(request)


@site.post("/settings/{section}")
async def settings_save(request: Request, section: str):
    """Save one section: only the settings it owns, only the fields its page showed."""
    if section not in settingsform.LABELS or section == "access":
        raise HTTPException(404, "No such settings page.")
    form = await request.form()
    was = current_settings()
    values = settingsform.parse(section, form, was, channels=recorder.channels(), kinds=notify_mod.KINDS)
    repricing = section == "money" and settingsform.reprices(values, was)
    refused = None
    with db() as conn:
        for key, value in values.items():
            settings_mod.save(conn, key, value)
        if section == "cars" and form.get("look"):
            refused = save_look(conn, form)
        if section == "cars":
            caps = capacities()
            for vin, cap in settingsform.capacity_changes(form, {c["vin"]: c["capacity"] for c in caps}, {c["vin"]: c["pack"]["figure"] for c in caps}).items():
                conn.execute("UPDATE drives SET capacity = %s WHERE vin = %s", (cap, vin))
        if repricing:
            forget_home_prices(conn)
        if section == "money" and settingsform.fast_rate_changed(values, was):
            # fast charges priced at a default take the new rates (one billed or priced by
            # its place keeps its price)
            conn.execute("DELETE FROM cc_charge_facts WHERE NOT at_home AND (cost_source = 'dc' OR (cost_source IS NULL AND plan LIKE 'DC at%%'))")
        conn.commit()
    if section == "maps" and (service := settingsform.maps_service(form)):
        MAPS.settings(**service)  # the add-on keeps its own, so after the commit, never inside it
    if section == "cars":
        _cache.pop("identity", None)  # after the commit: a page drawn in between would cache the old look
    settings_changed()
    if refused:
        r = settings_render(request, "cars", look_error=refused)
        r.status_code = 409
        return r
    return to_site(request, f"/settings/{section}?saved={'rates' if repricing else '1'}{_vin_q(request, '&')}")


def save_look(conn, form) -> str | None:
    """One car's paint, chart colour and drawing, or why not. A chart colour picked by hand on,
    or too close to, another car's is refused with that car's name (the page offers none, so
    only a stale page sends one); nothing is saved then. A new paint with the chart colour left
    alone takes the free colour that matches it."""
    vin = form.get("look")
    vehicles = all_vehicles()
    if vin not in {v["vin"] for v in vehicles}:
        return None
    ident = car_identities(vehicles)[vin]
    colors = car_colors(vehicles)
    names = {v["vin"]: v["name"] for v in vehicles}
    paint, chart, body = identity.parse_choice(form, vin, ident["make"])
    touched = form.get(f"chart_set_{vin}") == "1"
    clash = identity.too_close(chart, colors, vin) if chart is not None and (touched or chart != colors.get(vin)) else None
    if clash:
        return f"{identity.PALETTE[chart][0]} is too close to {names.get(clash, clash)}'s colour; pick another."
    after = identity.paint_of(paint, ident["make"]) if paint else ident["detected_paint"]
    others = identity.blocked(i for v, i in colors.items() if v != vin)
    chart = identity.chart_after_look(chart, colors.get(vin), touched, (ident["paint"] or {}).get("key"), after, others)
    identity.save_choice(conn, vin, paint, chart, body)
    return None


def forget_home_prices(conn) -> None:
    """Prices depend on the rates, so a rate change forgets the priced facts and the
    recorder rebuilds them in the background, in batches. Only on a rate change: saving the
    page to switch units used to throw away thousands of rows and re-price all of them.
    Home charges, and any elsewhere whose place is priced on the home plan."""
    conn.execute("DELETE FROM cc_charge_facts WHERE at_home OR cost_source = 'plan'")


def settings_changed() -> None:
    _cache.pop("settings", None)
    _cache.pop("home_rates", None)
    _cache.pop("vehicle_energy", None)
    _cache.pop("today", None)  # the drive and charge thresholds decide what counts today
    apply_settings(current_settings())


def set_home_plan(plan: dict | None) -> bool:
    """Keep `plan` as the home plan (None for none); True when that re-prices home charges.
    A typed-in plan replaced by one that is not is kept as manual_plan, in the same
    transaction, so Stop using can bring it back."""
    was = current_settings().get("rate_plan")
    repricing = cost.prices_differ(plan, was)
    with db() as conn:
        if (was or {}).get("source") == "manual" and (plan or {}).get("source") != "manual" and plan is not None:
            settings_mod.save(conn, "manual_plan", was)
        settings_mod.save(conn, "rate_plan", plan)
        if repricing:
            forget_home_prices(conn)
        conn.commit()
    settings_changed()
    return repricing


def _money_redirect(request: Request, repricing: bool = False, anchor: str = "") -> RedirectResponse:
    return to_site(request, f"/settings/money?saved={'rates' if repricing else '1'}{_vin_q(request, '&')}{anchor}")


# ---- Find my plan: OpenEI's rate database, asked only when the user presses a button, and
# only with what they typed or picked. The key is sealed in cc_state like every secret kept
# in the app. One client per button press: its requests share one deadline.
URDB_RELAY = (os.environ.get("CC_URDB_URL") or "").strip() or None


def urdb_client() -> urdb.Client:
    key = None
    if not URDB_RELAY:
        try:
            with db() as conn:
                key = urdb.load_key(conn, VAULT)
        except Exception as err:  # noqa: BLE001 - no table yet: no key
            LOG.warning("openei key: %s", type(err).__name__)
    return urdb.Client(key=key, relay=URDB_RELAY)


def urdb_settings() -> dict:
    """What the Find my plan box needs on a plain page load: whether a key or a relay is
    set, and a saved key's last four. Nothing is fetched."""
    hint = None
    if not URDB_RELAY:
        try:
            with db() as conn:
                hint = urdb.key_hint(conn)
        except Exception as err:  # noqa: BLE001 - no table yet: no key
            LOG.warning("openei key: %s", type(err).__name__)
    return {
        "relay": bool(URDB_RELAY),
        "key_hint": hint,
        "signup": urdb.SIGNUP,
        "vault_ready": VAULT.ready,
        "vault_problem": VAULT.problem,
    }


def _finder_form(form) -> dict:
    """What the box posted: the text, the plan-name filter, and the utility picked from the
    list (its EIA id), or one the text names outright."""
    q = " ".join((form.get("q") or "").split())[:120]
    contains = " ".join((form.get("contains") or "").split())[:60]
    eia = (form.get("eia") or "").strip()
    named = None if eia else urdb.utility_for(q)
    return {
        "q": q,
        "contains": contains,
        "eia": eia or (str(named["eia"]) if named else ""),
        "name": named["name"] if named else q,
    }


def urdb_search_items(client, f: dict, cache_only: bool = False):
    """The search's rates: by EIA id when a utility was picked, and by its name when OpenEI
    files nothing under the id; else by ZIP or the name as typed."""
    if not f["eia"]:
        return urdb.fetch(db, client, urdb.search_params(f["q"]), cache_only=cache_only)
    items = urdb.fetch(db, client, urdb.search_params(eia=f["eia"]), cache_only=cache_only)
    if items is not None:  # only the utility asked for, should `eia` ever be ignored
        items = urdb.as_found([x for x in items if str(x.get("eiaid") or f["eia"]) == str(f["eia"])], items.more)
    if items is not None and not items and f["name"] and not urdb.ZIP.match(f["name"]):
        items = urdb.fetch(db, client, urdb.search_params(f["name"]), cache_only=cache_only)
    return items


def urdb_listing(client, f: dict, cache_only: bool = False) -> dict:
    """A search as the page lists it; with cache_only, only what is cached (else unlisted)."""
    items = urdb_search_items(client, f, cache_only)
    if items is None:
        return dict(f)
    return {
        **f,
        "searched": True,
        **urdb.listing(items, datetime.now(TZ).date(), f["contains"]),
    }


def urdb_plan(client, label: str, f: dict) -> dict:
    """The plan `label` is a version of, with every version of it: those in the search's
    list (from the day's cache), and any older ones it does not hold, followed back
    through `supersedes` one page at a time."""
    chosen = urdb.fetch(db, client, urdb.page_params(label))
    if not chosen:
        raise urdb.URDBError("OpenEI has no plan with that id.")
    if f["q"] or f["eia"]:
        siblings = urdb_search_items(client, f) or []
    elif chosen[0].get("eiaid"):
        siblings = urdb.fetch(db, client, urdb.search_params(eia=chosen[0]["eiaid"])) or []
    else:
        siblings = []
    known = {x["label"]: x for x in [*siblings, *chosen] if x.get("label")}

    def page(lab: str):
        if lab not in known:
            got = urdb.fetch(db, client, urdb.page_params(lab))
            if got:
                known[lab] = got[0]
        return known.get(lab)

    urdb.chain(page, label)
    return urdb.to_plan(urdb.versions_of(label, list(known.values())))


@site.post("/settings/money/plan-search")
async def urdb_search(request: Request):
    """Find my plan's Search: the request is OpenEI's, with what was typed or picked."""
    f = _finder_form(await request.form())
    try:
        found = await asyncio.to_thread(lambda: urdb_listing(urdb_client(), f))
    except urdb.URDBError as err:
        found = {**f, "error": str(err)}
    return settings_render(request, "money", find=found)


@site.post("/settings/money/plan-search/preview")
async def urdb_preview(request: Request):
    """A plan from the list, with every version of it, shown before it is used. The list
    stays on the page from the day's cache, not a second search."""
    form = await request.form()
    f = _finder_form(form)
    found: dict = dict(f)

    def work():
        client = urdb_client()
        plan = urdb_plan(client, form.get("label") or "", f)
        return plan, (urdb_listing(client, f, cache_only=True) if f["q"] or f["eia"] else found)

    try:
        plan, found = await asyncio.to_thread(work)
    except urdb.URDBError as err:
        return settings_render(request, "money", find={**found, "error": str(err)})
    return settings_render(
        request,
        "money",
        find=found,
        preview={**urdb.preview(plan), "grid": planview.grid_view(plan)},
    )


@site.post("/settings/money/plan-search/use")
async def urdb_use(request: Request):
    """Use this plan: it becomes the home plan, and home charges are re-priced in the background."""
    form = await request.form()
    f = _finder_form(form)
    try:
        plan = await asyncio.to_thread(lambda: urdb_plan(urdb_client(), form.get("label") or "", f))
    except urdb.URDBError as err:
        return settings_render(request, "money", find={**f, "error": str(err)})
    return _money_redirect(request, set_home_plan(plan))


@site.post("/settings/money/plan/clear")
async def home_plan_clear(request: Request):
    """Stop using a plan shown as a grid: back to the typed-in plan it replaced, or to none,
    so prices can be typed in."""
    kept = current_settings().get("manual_plan")
    return _money_redirect(request, set_home_plan(kept if cost.valid_plan(kept) else None))


_UTILITY_BYTES: bytes | None = None


@site.get("/settings/money/utilities.json")
def utility_list():
    """The bundled utility list, for the search box to match against as you type. Fetched
    by the page once, from here: typing sends nothing anywhere."""
    global _UTILITY_BYTES  # noqa: PLW0603 - read once
    if _UTILITY_BYTES is None:
        try:
            _UTILITY_BYTES = urdb.UTILITIES.read_bytes()
        except OSError:
            _UTILITY_BYTES = b'{"utilities": [], "aliases": {}}'
    return Response(
        _UTILITY_BYTES,
        media_type="application/json",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@site.post("/settings/money/plan-search/key")
async def urdb_key(request: Request):
    """Keep, or forget, the OpenEI key. Sealed; only its last four are ever shown."""
    form = await request.form()
    if form.get("remove"):
        with db() as conn:
            urdb.forget_key(conn)
            conn.commit()
        return _money_redirect(request, anchor="#find")
    try:
        key = urdb.check_key(form.get("key") or "")
        if not VAULT.ready:
            raise urdb.URDBError(f"The key cannot be kept: {VAULT.problem}.")
    except urdb.URDBError as err:
        return settings_render(request, "money", find={"key_error": str(err)})
    with db() as conn:
        urdb.save_key(conn, VAULT, key)
        conn.commit()
    return _money_redirect(request, anchor="#find")


@site.get("/charging", response_class=HTMLResponse)
def charging_page(request: Request, days: int | None = None):
    """Where the energy came from: every charge by place, by the level it ended at, and by Supercharger."""
    car = pick(request)
    vin = None if car.get("garage") else car["vin"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None
    summary = charging_summary(charges(vin, None, since))
    return page(request, "charging.html", car=car, s=summary, days=days, active="charging")


def summarize_sql(vin: str | None, by: str, since: datetime | None, until: datetime | None = None, span: timedelta | None = None) -> list[dict]:
    """The Stats table straight from the facts tables, grouped in the database: one query for drives,
    one for charges. The same row shape summarize() returns, without loading every drive.
    `span` keeps only the first stretch of each period (its first 4 days 17 hours, say), so
    a month in progress can be set beside the same days of the months before it."""
    trunc = {"day": "day", "week": "week", "month": "month"}[by]
    vw, vargs = _vin_clause(vin)
    where_since = (" AND t.start_date >= %s" if since else "") + (" AND t.start_date < %s" if until else "")
    args_since = [x for x in (since, until) if x]
    if span is not None:
        where_since += f" AND (t.start_date AT TIME ZONE %s) - date_trunc('{trunc}', t.start_date AT TIME ZONE %s) < %s"
        args_since += [TZ.key, TZ.key, span]
    with db() as conn:
        d = conn.execute(
            f"""
            SELECT date_trunc('{trunc}', t.start_date AT TIME ZONE %s)::date AS period, COUNT(*) AS drives,
                   COALESCE(SUM(t.destination_odometer - t.start_odometer), 0) AS miles,
                   COALESCE(SUM(EXTRACT(EPOCH FROM (t.destination_date - t.start_date))) / 3600, 0) AS drive_h,
                   COALESCE(SUM(CASE WHEN NULLIF(f.range_start, -999) IS NOT NULL AND NULLIF(f.range_end, -999) IS NOT NULL AND f.range_start >= f.range_end AND COALESCE(vy.kwh_per_rated_mi, vf.kwh_per_rated_mi) IS NOT NULL
                                     THEN (f.range_start - f.range_end) * COALESCE(vy.kwh_per_rated_mi, vf.kwh_per_rated_mi)
                                     WHEN f.level_start >= f.level_end AND dr.capacity IS NOT NULL THEN dr.capacity * (f.level_start - f.level_end) / 100 END), 0) AS used_kwh,
                   COALESCE(SUM(CASE WHEN NULLIF(f.range_start, -999) IS NOT NULL AND NULLIF(f.range_end, -999) IS NOT NULL AND f.range_start >= f.range_end AND COALESCE(vy.kwh_per_rated_mi, vf.kwh_per_rated_mi) IS NOT NULL
                                     THEN (f.range_start - f.range_end) * COALESCE(vy.kwh_per_rated_mi, vf.kwh_per_rated_mi) * vf.home_rate END), 0) AS drive_cost,
                   COALESCE(bool_or(vf.home_rate_estimate AND vf.home_rate IS NOT NULL), FALSE) AS drive_cost_estimate,
                   COALESCE(SUM(x.fsd_miles), 0) AS fsd_miles
            FROM trips t
            LEFT JOIN cc_trip_facts f ON f.trip_id = t.id
            LEFT JOIN cc_vehicle_facts vf ON vf.vin = t.vin
            -- The drive's own year where that year measured enough charges to have a
            -- figure, the all-time one where it did not. The same rule rated_mile_kwh()
            -- applies row by row in Python, written out here because this page adds up
            -- eight years in the database rather than loading them.
            LEFT JOIN cc_vehicle_year_facts vy ON vy.vin = t.vin AND vy.year = EXTRACT(year FROM t.start_date AT TIME ZONE %s)::int
            LEFT JOIN drives dr ON dr.vin = t.vin
            LEFT JOIN cc_trip_extras x ON x.vin = t.vin AND x.start_date = t.start_date
            WHERE t.destination_date IS NOT NULL{vw}{where_since}{moved_sql()} GROUP BY 1""",
            [TZ.key, TZ.key, *vargs, *args_since],
        ).fetchall()
        c = conn.execute(
            f"""
            SELECT date_trunc('{trunc}', t.session_start_date AT TIME ZONE %s)::date AS period, COUNT(*) AS charges,
                   COALESCE(SUM(f.kwh), 0) AS added_kwh, COALESCE(SUM(f.cost), 0) AS cost,
                   COUNT(f.cost) AS priced, COALESCE(SUM(f.cost) FILTER (WHERE f.cost_source = 'average'), 0) AS avg_cost
            FROM charging_sessions t LEFT JOIN cc_charge_facts f ON f.session_id = t.id
            WHERE t.session_start_date IS NOT NULL{vw}{where_since.replace('start_date', 'session_start_date')} GROUP BY 1""",
            [TZ.key, *vargs, *args_since],
        ).fetchall()
    rows: dict = {}
    for r in d:
        rows[r["period"]] = {
            "period": r["period"],
            "drives": r["drives"],
            "miles": float(r["miles"]),
            "drive_h": float(r["drive_h"]),
            "used_kwh": float(r["used_kwh"]),
            "drive_cost": float(r["drive_cost"]),
            "drive_cost_estimate": bool(r["drive_cost_estimate"]) and float(r["drive_cost"]) > 0,
            "fsd_miles": float(r["fsd_miles"]),
            "charges": 0,
            "added_kwh": 0.0,
            "cost": 0.0,
            "priced": 0,
            "avg_cost": 0.0,
        }
    for r in c:
        row = rows.setdefault(
            r["period"],
            {
                "period": r["period"],
                "drives": 0,
                "miles": 0.0,
                "drive_h": 0.0,
                "used_kwh": 0.0,
                "drive_cost": 0.0,
                "fsd_miles": 0.0,
                "charges": 0,
                "added_kwh": 0.0,
                "cost": 0.0,
                "priced": 0,
                "avg_cost": 0.0,
            },
        )
        row["charges"], row["added_kwh"], row["cost"] = r["charges"], float(r["added_kwh"]), float(r["cost"])
        row["priced"], row["avg_cost"] = r["priced"], float(r["avg_cost"])
    out = []
    for r in sorted(rows.values(), key=lambda x: x["period"], reverse=True):
        r["wh_per_mi"] = (r["used_kwh"] * 1000 / r["miles"]) if r["miles"] > 1 and r["used_kwh"] > 0 else None
        r["cost_per_mi"] = (r["cost"] / r["miles"]) if r["miles"] > 1 and r["cost"] > 0 else None
        out.append(r)
    return out


def car_efficiency() -> dict:
    """(vin, year) -> Wh a mile each car used that year, and (vin, None) -> all time: drive
    energy over miles, from drives whose energy is known, the same energy Stats adds up.
    Kept ten minutes; the comparison with a petrol car turns charged kWh into miles with it."""

    def load():
        with db() as conn:
            rows = conn.execute(
                f"""
                SELECT vin, yr, SUM(mi) AS miles, SUM(kwh) AS kwh FROM (
                  SELECT t.vin, EXTRACT(year FROM t.start_date AT TIME ZONE %s)::int AS yr, t.destination_odometer - t.start_odometer AS mi,
                         CASE WHEN NULLIF(f.range_start, -999) IS NOT NULL AND NULLIF(f.range_end, -999) IS NOT NULL AND f.range_start >= f.range_end AND COALESCE(vy.kwh_per_rated_mi, vf.kwh_per_rated_mi) IS NOT NULL
                              THEN (f.range_start - f.range_end) * COALESCE(vy.kwh_per_rated_mi, vf.kwh_per_rated_mi)
                              WHEN f.level_start >= f.level_end AND dr.capacity IS NOT NULL THEN dr.capacity * (f.level_start - f.level_end) / 100 END AS kwh
                  FROM trips t
                  LEFT JOIN cc_trip_facts f ON f.trip_id = t.id
                  LEFT JOIN cc_vehicle_facts vf ON vf.vin = t.vin
                  LEFT JOIN cc_vehicle_year_facts vy ON vy.vin = t.vin AND vy.year = EXTRACT(year FROM t.start_date AT TIME ZONE %s)::int
                  LEFT JOIN drives dr ON dr.vin = t.vin
                  WHERE t.destination_date IS NOT NULL AND t.destination_odometer > t.start_odometer{moved_sql()}
                ) d WHERE kwh > 0 GROUP BY vin, yr""",
                [TZ.key, TZ.key],
            ).fetchall()
        return efficiency_by_year([(r["vin"], r["yr"], float(r["miles"] or 0), float(r["kwh"] or 0)) for r in rows])

    return cached("car_efficiency", 600, load)


def charge_groups(vin: str | None, since: datetime | None, until: datetime | None) -> list[dict]:
    """A window's charges grouped by car, year, region, month and where the cost came
    from: count, kWh, cost and the months whose average priced them. One small query
    serves the estimate footnote and the comparison with a petrol car."""
    vw, vargs = _vin_clause(vin)
    where = (" AND c.session_start_date >= %s" if since else "") + (" AND c.session_start_date < %s" if until else "")
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT c.vin, EXTRACT(year FROM c.session_start_date AT TIME ZONE %s)::int AS year, to_char(c.session_start_date AT TIME ZONE %s, 'YYYY-MM') AS ym,
                   NULLIF(f.region, '') AS region, f.cost_source, COUNT(*) AS n, COALESCE(SUM(f.kwh), 0) AS kwh,
                   CASE WHEN COUNT(f.cost) = COUNT(*) THEN SUM(f.cost) END AS cost, MIN(f.rate_month) AS rate_lo, MAX(f.rate_month) AS rate_hi
            FROM charging_sessions c JOIN cc_charge_facts f ON f.session_id = c.id
            WHERE c.session_start_date IS NOT NULL{vw.replace('t.vin', 'c.vin')}{where}
            GROUP BY 1, 2, 3, 4, 5, (f.cost IS NULL)""",
            [TZ.key, TZ.key, *vargs, *[x for x in (since, until) if x]],
        ).fetchall()
    return [{**r, "kwh": float(r["kwh"]), "cost": float(r["cost"]) if r["cost"] is not None else None} for r in rows]


def parse_day(x: str | None):
    try:
        return datetime.strptime(x.strip(), "%Y-%m-%d").date() if x and x.strip() else None
    except ValueError:
        return None


@site.get("/stats", response_class=HTMLResponse)
def stats_page(
    request: Request,
    by: str = "week",
    over: float = 1.0,
    range_: str | None = Query(None, alias="range"),
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    vs: str | None = None,
    month: str | None = None,
    period: str | None = None,
    year: str | None = None,
):
    by = by if by in statspage.GRAINS else "week"
    over = over if over in statspage.OVER else 1.0
    vs = vs if vs in ("prev", "yoy") else None
    car = pick(request)
    vin = None if car.get("garage") else car["vin"]
    now = datetime.now(TZ)
    # The page's one period: a month (the default, this one), a year, all time or two dates;
    # the upper bound is exclusive, and a period still running runs up to now. An old
    # ?range= link opens where it meant.
    d0, d1 = parse_day(from_), parse_day(to)
    legacy = {} if period else statspage.legacy_state(range_, now)
    rng = statspage.period(period or legacy.get("period") or range_, legacy.get("month", month), legacy.get("year", year), d0, d1, now)
    since, until = rng["since"], rng["until"]
    state = {
        "period": rng["key"],
        "month": rng["ym"] if rng["key"] == "month" and not rng["in_progress"] else None,
        "year": str(rng["year"]) if rng["key"] == "year" and not rng["in_progress"] else None,
        "from": from_ if rng["key"] == "custom" else None,
        "to": to if rng["key"] == "custom" else None,
        "vs": vs,
        "by": by,
        "over": str(int(over)),
        "vin": car["vin"] if len(all_vehicles()) > 1 else None,
    }
    ctl = statspage.controls(state, rng)
    # A year or two dates can be set beside the period before, span for span; a month is
    # already set beside the month before and its average.
    compare = None
    other = statspage.versus(rng, vs) if rng["key"] in ("year", "custom") else None
    if other:
        rows2 = summarize_sql(vin, "month", other["since"], other["until"])
        t2 = {
            k2: sum(r[k2] for r in rows2)
            for k2 in (
                "drives",
                "miles",
                "drive_h",
                "used_kwh",
                "charges",
                "added_kwh",
                "cost",
                "fsd_miles",
                "priced",
                "avg_cost",
            )
        }
        t2["wh_per_mi"] = (t2["used_kwh"] * 1000 / t2["miles"]) if t2["miles"] > 1 and t2["used_kwh"] > 0 else None
        compare = {**other, "totals": t2}
    rows = summarize_sql(vin, by, since, until)  # grouped in the database; the facts already live there
    # The month the tiles, bars and comparison are about, and the twelve before it: the
    # period's own month, or for a year the last month of it.
    if rng["key"] == "month":
        mw = month_window(rng["ym"], TZ)
    elif rng["key"] == "year" and not rng["in_progress"]:
        mw = month_window(f"{rng['year']}-12", TZ)
    else:
        mw = month_window(None, TZ)
    focus = (mw["start"].year, mw["start"].month)
    y13, m13 = statspage.fetch_from(focus)
    start13 = datetime(y13, m13, 1, tzinfo=TZ)
    fraction = (
        min(
            1.0,
            max(
                0.0,
                (now - mw["start"]).total_seconds() / (mw["end"] - mw["start"]).total_seconds(),
            ),
        )
        if mw["is_current"]
        else 1.0
    )
    # Month rows can be cut on the range's bounds only when both fall on a first of a month.
    whole = statspage.month_aligned(since) and statspage.month_aligned(until)
    odometer = None
    if car.get("garage"):
        # One month query per car serves both the bars and the "By car" totals when the
        # range is whole months; otherwise each car's range is its own small query.
        car_months, per_car = {}, []
        for v in all_vehicles():
            mrows = summarize_sql(
                v["vin"],
                "month",
                (min(since, start13) if since and whole else (None if whole else start13)),
                None,
            )
            car_months[v["vin"]] = statspage.by_month(mrows)
            if whole:
                t = statspage.window_sum(
                    mrows,
                    since.date() if since else None,
                    until.date() if until else None,
                )
            else:
                t = statspage.window_sum(summarize_sql(v["vin"], "month", since, until), None, None)
            t["vin"], t["name"] = v["vin"], v["name"]
            per_car.append(t)
        order = [t["vin"] for t in per_car]
        bins = {"temp": [], "speed": []}
    else:
        cap, k = vehicle_energy().get(car["vin"], (None, None))
        ds = [d for d in drives_light(vin, since) if until is None or d["start_date"] < until]
        per_car = []
        bins = efficiency_bins(ds, car["capacity_kwh"], k, over)
        odo = sorted((d["destination_date"], d["destination_odometer"]) for d in ds if d["destination_date"] and d["destination_odometer"])
        if len(odo) >= 2:
            odometer = chartdata.series_chart(
                odo[0][0],
                odo[-1][0],
                {
                    "key": "odo",
                    "label": "Odometer",
                    "unit": " " + units_mod.ACTIVE.labels["dist"],
                    "series": [(t, units_mod.ACTIVE.distance(v)) for t, v in odo],
                    "scale": "tight",
                },
                [],
                TZ,
            )
        reuse = by == "month" and until is None and (since is None or since <= start13)
        mrows = rows if reuse else summarize_sql(vin, "month", start13, None)
        car_months = {car["vin"]: statspage.by_month(mrows)}
        order = [car["vin"]]
    months: dict = {}
    for cm in car_months.values():
        for ym, r in cm.items():
            months[ym] = statspage.add(months.get(ym), r)
    first = statspage.first_full(months, focus) or focus
    since_m = statspage.history_start(months, focus)
    # A month still running is read against the same days of the months before it: one
    # small query of each month's first stretch, only while the month is current.
    like, day = None, None
    if fraction < 1:
        elapsed = now.replace(tzinfo=None) - mw["start"].replace(tzinfo=None)
        like = statspage.by_month(summarize_sql(vin, "month", start13, None, span=elapsed))
        day = now.day
    cmp = statspage.compare(months, focus, fraction, max(first, statspage.shift(focus, -12)), like)
    proj = next((m["proj"] for m in cmp if m["key"] == "miles"), None)
    mbars = statspage.bars(car_months, order, focus, first, units_mod.ACTIVE, proj=proj, since=since_m)
    mbars_phone = statspage.bars(
        car_months,
        order,
        focus,
        first,
        units_mod.ACTIVE,
        w=360,
        h=190,
        short=True,
        proj=proj,
        since=since_m,
    )
    totals = {
        k2: sum(r[k2] for r in rows)
        for k2 in (
            "drives",
            "miles",
            "drive_h",
            "used_kwh",
            "charges",
            "added_kwh",
            "cost",
            "fsd_miles",
            "priced",
            "avg_cost",
        )
    }
    totals["wh_per_mi"] = (totals["used_kwh"] * 1000 / totals["miles"]) if totals["miles"] > 1 and totals["used_kwh"] > 0 else None
    st = current_settings()
    # The window's charges by region and month: what the estimates rest on, and each
    # charge's energy as petrol at its own region's price that month.
    groups = charge_groups(vin, since, until)
    rates = averages_mod.active()
    estimate = estimate_note(groups, rates)
    petrol = petrol_comparison(groups, car_efficiency(), st.get("gas_mpg"), rates, st.get("gas_price"))
    return page(
        request,
        "stats.html",
        petrol=petrol,
        estimate=estimate,
        sun=sun_by_month(car["vin"]),
        car=car,
        rows=rows,
        totals=totals,
        per_car=per_car,
        by=by,
        over=over,
        rate_plan=cost.current_plan(),
        bins=bins,
        odometer=odometer,
        month_window=mw,
        fraction=fraction,
        cmp=cmp,
        heads=statspage.heads(focus, fraction, day),
        mbars=mbars,
        mbars_phone=mbars_phone,
        rng=rng,
        ctl=ctl,
        compare=compare,
        period=rng["key"],
        active="stats",
    )


FEED_COLUMNS = ["Date", "carState", "battery_level", "est_battery_range", "odometer", "speed", "charger_power", "charge_energy_added", "outside_temp", "inside_temp", "car_version"]


def day_readings(car: dict, day: date) -> tuple[str, list[str], list[dict]]:
    """A day's raw readings for one car: the feed's rows as they came, or the connector
    tree's leaves each time it changed. Returns (source, columns, rows) with rows as flat dicts."""
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    source = source_of(car)
    with db() as conn:
        if source == "feed":
            rows = [
                dict(r["data"] if isinstance(r, dict) else r[1], **{"at": r["at"] if isinstance(r, dict) else r[0]})
                for r in conn.execute("SELECT at, data FROM cc_teslafi_rows WHERE vin = %s AND at >= %s AND at < %s ORDER BY at", (car["vin"], start, end)).fetchall()
            ]
            cols = [c for c in FEED_COLUMNS if any(c in r for r in rows)]
            return source, cols, rows
        rows = []
        for r in conn.execute("SELECT at, data FROM cc_raw WHERE vin = %s AND at >= %s AND at < %s ORDER BY at", (car["vin"], start, end)).fetchall():
            at, data = (r["at"], r["data"]) if isinstance(r, dict) else (r[0], r[1])
            flat = {"at": at}
            for path, val in raw_leaves(data):
                flat[path] = val
            rows.append(flat)
        seen = {k for r in rows for k in r}
        cols = ["at"]
        for pattern in (
            r"drives/.*level/val$",
            r"range/val$",
            r"odometer/val$",
            r"^state/val$",
            r"speed/val$",
            r"position/(latitude|longitude)/val$",
            r"charging/power/val$",
            r"charging/state/val$",
            r"outside_temperature/val$",
            r"connection_state/val$",
        ):
            cols += sorted(k for k in seen if re.search(pattern, k) and k not in cols)
        return "live", cols[:14], rows


def raw_leaves(tree, path: str = ""):
    if isinstance(tree, dict):
        for k, v in tree.items():
            yield from raw_leaves(v, f"{path}/{k}" if path else k)
    elif isinstance(tree, list):
        for i, v in enumerate(tree):
            yield from raw_leaves(v, f"{path}/{i}")
    else:
        yield path, tree


@site.get("/data", response_class=HTMLResponse)
def data_page(request: Request, date: str | None = None):
    """Every reading of one day, as it arrived: the rows behind everything else."""
    car = pick(request)
    if car.get("garage"):
        car = next((v for v in all_vehicles()), car)
    start, end, d = day_bounds(date)
    source, cols, rows = day_readings(car, d)
    every = max(1, len(rows) // 400)  # the page shows a few hundred; the CSV has them all
    return page(
        request,
        "data.html",
        car=car,
        day=d,
        source=source,
        cols=cols,
        rows=rows[::every],
        every=every,
        count=len(rows),
        prev=d - timedelta(days=1),
        next=(d + timedelta(days=1)) if d < datetime.now(TZ).date() else None,
        active="timeline",
    )


@site.get("/data.csv")
def data_csv(request: Request, date: str | None = None):
    car = pick(request)
    if car.get("garage"):
        car = next((v for v in all_vehicles()), car)
    start, end, d = day_bounds(date)
    source, cols, rows = day_readings(car, d)
    keys = ["at"] + sorted({k for r in rows for k in r} - {"at"})
    return _csv(rows, keys, f"carcommand-{car['name']}-{d.isoformat()}.csv")


def _csv(rows: list[dict], columns: list[str], filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([(v.isoformat() if isinstance(v, datetime) else (round(v.total_seconds() / 60) if isinstance(v, timedelta) else v)) for v in (r.get(c) for c in columns)])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def api_vin(vin: str | None) -> str | None:
    """The car a JSON endpoint is about.

    `?vin=all` is the garage on every page of the site, and the list queries already
    understand it, but these endpoints used to hand it straight to live_vehicle, which
    knows only real cars and answered 404. Anything that is not the garage still goes
    through live_vehicle, so an unknown vin is still a 404 rather than a silent fall
    back to whichever car happens to be first."""
    return GARAGE if vin == GARAGE else live_vehicle(vin)["vin"]


# car_name leads both exports because ?vin=all threads every car into one file, and
# without it the rows of a three-car garage are indistinguishable.
@site.get("/drives.csv")
def drives_csv(vin: str | None = None):
    return _csv(
        drives(api_vin(vin), None),
        ["car_name", "id", "start_date", "destination_date", "from", "to", "miles", "duration", "start_odometer", "destination_odometer", "level_start", "level_end"],
        "carcommand-drives.csv",
    )


@site.get("/charges.csv")
def charges_csv(vin: str | None = None):
    return _csv(
        charges(api_vin(vin), None),
        ["car_name", "id", "plug_connected_date", "session_start_date", "session_end_date", "plug_disconnected_date", "where", "charging_type", "start_level", "end_level", "kwh", "cost", "duration"],
        "carcommand-charges.csv",
    )


@site.get("/api/whoami")
def api_whoami(request: Request):
    """What a token is, for the script that holds it: its label and which car it may
    ask about. Answers for a signed-in browser too, as the household."""
    token = getattr(request.state, "token", None)
    return JSONResponse({"label": token["label"], "vin": token["vin"], "kind": token.get("kind", "read")} if token else {"label": "signed in", "vin": None, "kind": None})


# ---- the admin API -----------------------------------------------------------------
# Setup, driven by a script or an assistant with an admin token: the same actions as the
# Settings buttons, through the same functions. The guard has already checked the token,
# the network and written the call down (admin_call); these only do the work. Errors are
# {"error": ...}; nothing secret is ever in an answer (adminapi names every field it passes).


@site.exception_handler(adminapi.BadRequest)
async def admin_bad_request(request: Request, exc: adminapi.BadRequest):
    return JSONResponse({"error": str(exc)}, status_code=400)


async def json_body(request: Request) -> dict:
    """The request's JSON object, {} for none; a 400 for anything that is not one."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise adminapi.BadRequest("The body is not JSON.") from None
    if not isinstance(body, dict):
        raise adminapi.BadRequest("The body is a JSON object.")
    return body


def _done(status: int, problem: str | None, **extra) -> JSONResponse:
    return JSONResponse({"error": problem}, status_code=status) if problem else JSONResponse({"ok": True, **extra})


@site.get("/api/admin/tesla")
async def admin_tesla(fresh: str = ""):
    """Connect Tesla's checklist: each step, the registered domain, and per car the key, the
    stream config and the last message. ?fresh=1 asks Tesla again rather than the cache."""
    if fresh:
        TESLA_CACHE.clear()
    facts = await asyncio.to_thread(tesla_facts)
    return adminapi.tesla(facts, teslapage.steps(facts), TESLA_DOMAIN, tesla_key_domain())


@site.post("/api/admin/tesla/register")
async def admin_tesla_register():
    return _done(*await tesla_register_do(), domain=tesla_key_domain())


@site.post("/api/admin/tesla/stream/register")
async def admin_tesla_stream_register():
    return _done(*await tesla_stream_register_do(), host=TESLA_STREAM_HOST)


@site.post("/api/admin/tesla/stream/start")
async def admin_tesla_stream_start(request: Request):
    vin = adminapi.vin_of(await json_body(request))
    return _done(400, await tesla_stream_start_do(vin), vin=vin)


@site.post("/api/admin/tesla/stream/stop")
async def admin_tesla_stream_stop(request: Request):
    vin = adminapi.vin_of(await json_body(request))
    return _done(400, await tesla_stream_stop_do(vin), vin=vin)


@site.get("/api/admin/sources")
def admin_sources():
    """Every source kept in the app, with how its poller is doing; never its secret."""
    with db() as conn:
        rows = sources.load(conn, VAULT)
    names = {v["vin"]: v["name"] for v in all_vehicles()}
    return {"sources": [adminapi.source(r, names.get(r["vin"], r["label"] or r["vin"] or ""), sources.KINDS, source_status(r["kind"], r["vin"], r["config"])) for r in rows]}


@site.post("/api/admin/sources/{source_id}/test")
async def admin_source_test(source_id: int):
    checked = adminapi.tested(await source_test_do(source_id))
    return JSONResponse(checked, status_code=200 if checked["ok"] else 400)


def _place_rows(place_id: int | None = None) -> list[dict]:
    rows = places_detail()
    return rows if place_id is None else [r for r in rows if r["id"] == place_id]


@site.get("/api/admin/places")
def admin_places():
    currency = current_settings().get("currency") or "$"
    stays = place_stays()
    return {"places": [adminapi.place(p, is_home_place(p), currency, placespage.visits(p, stays)) for p in _place_rows()]}


@site.patch("/api/admin/places/{place_id}")
async def admin_place_patch(request: Request, place_id: int):
    """Rename, re-icon, price or re-address one place, checked as the Places forms are."""
    changes = adminapi.place_patch(await json_body(request), _valid_icon)
    if not await asyncio.to_thread(_place_rows, place_id):
        raise HTTPException(404, "No such place.")
    await asyncio.to_thread(
        rename_place,
        place_id,
        changes.get("name"),
        changes.get("icon", KEEP),
        changes.get("price", (False, None)),
        changes.get("address", (False, None)),
    )
    _cache.pop("geocells", None)
    row = (await asyncio.to_thread(_place_rows, place_id))[0]
    return adminapi.place(row, is_home_place(row), current_settings().get("currency") or "$", placespage.visits(row, place_stays()))


@site.get("/api/admin/money")
def admin_money():
    """The home plan, fast-charging and petrol prices, and the regional averages' state."""
    st = current_settings()
    ctx = planview.money_context(st.get("rate_plan"))
    return adminapi.money(st, ctx["plan"], ctx["schedule_text"], rates_settings())


@site.post("/api/admin/money/averages/refresh")
def admin_money_refresh():
    """As Settings' "Update now": the worker downloads the averages on its next pass."""
    rates_worker.request()
    return JSONResponse({"ok": True, "queued": True}, status_code=202)


@site.api_route("/api/admin/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
def admin_nothing(rest: str):
    """Last of the admin routes: anything else under /api/admin/ is a JSON 404, not a page."""
    return JSONResponse({"error": f"No admin call at /api/admin/{rest[:80]}."}, status_code=404)


def vehicle_models() -> dict[str, tuple[str | None, str | None]]:
    """vin -> (maker, model) as stored, for Settings › Cars and sources."""
    with db() as conn:
        rows = conn.execute("SELECT vin, manufacturer, model FROM vehicles").fetchall() or []
    return {r["vin"]: (r["manufacturer"], r["model"]) for r in rows}


def capacities() -> list[dict]:
    """Each car's usable pack size as the site has it, for Settings › Cars: the connector's
    figure, the model's, or what was typed here; None when nothing knows it. `pack` says which,
    and the figure a reset goes back to."""
    import carspage

    energy, live, made = vehicle_energy(), garage(), vehicle_models()
    out = []
    for v in all_vehicles():
        tree = live.get(v["vin"]) or {}
        make, model = made.get(v["vin"]) or (None, None)
        make, model = leaf(tree, "manufacturer") or make, leaf(tree, "model") or model
        reported = leaf(tree, "drives", "primary", "battery", "available_capacity") or leaf(tree, "battery_capacity_kwh")
        cap = (energy.get(v["vin"]) or (None, None))[0]
        pack = carspage.pack(cap, reported, make or "the car", carspage.model_figure(make, model))
        out.append({"vin": v["vin"], "name": v["name"], "capacity": cap, "make": make, "model": model, "pack": pack})
    return out


def data_checks() -> list[dict]:
    """Per car: where readings come from, the last check's facts, and its findings. For Settings › Data."""
    notes = health_notes()
    out = []
    for v in all_vehicles():
        got = notes.get(v["vin"]) or {}
        facts = dict(got.get("car") or {})
        for k in ("raw_newest", "derived_newest", "checked_at"):
            if facts.get(k):
                facts[k] = datetime.fromisoformat(facts[k])
        out.append({"vin": v["vin"], "name": v["name"], "source": source_name(v), "facts": facts, "findings": got.get("findings", [])})
    return out


@site.get("/api/health")
def api_health(vin: str | None = None):
    """The data checks as JSON: what arrives, what gets logged, and what is missing between
    them. Every car, or one for `?vin=`, which is all a token for one car may ask for."""
    checks = data_checks()
    if vin and vin != GARAGE:
        checks = [c for c in checks if c["vin"] == vin]
    return JSONResponse(jsonable(checks))


def status_view(request: Request, value):
    """What a status token is shown: the payload with where the car is taken out."""
    token = getattr(request.state, "token", None)
    return auth.status_payload(value, bool(token.get("share_place"))) if token and token.get("kind") == "status" else value


@site.get("/api/now")
def api_now(request: Request, vin: str | None = None):
    """One car, or every car for `?vin=all` — a list, because that is what the garage is."""
    if vin == GARAGE:
        cars = with_colors({v["vin"]: jsonable(live_vehicle(v["vin"])) for v in all_vehicles()})
        return JSONResponse(status_view(request, list(cars.values())))
    car = jsonable(live_vehicle(vin))
    return JSONResponse(status_view(request, with_colors({car.get("vin") or "": car}).get(car.get("vin") or "", car)))


@site.get("/energy/row", response_class=HTMLResponse)
def energy_row():
    """The Powerwall row as Now draws it, for the page to redraw itself: from the newest reading
    in memory, empty when there is none fresh."""
    html = templates.env.from_string('{% import "_parts.html" as parts %}{{ parts.pw_row(pw) }}').render(pw=energy_now())
    return HTMLResponse(html.strip(), headers={"Cache-Control": "no-store"})


def energy_today() -> dict | None:
    """Today's kWh from the stored rows, from local midnight. Kept a minute."""

    def load():
        lo = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        with db() as conn:
            return energy.totals(conn, lo, lo + timedelta(days=1))

    return cached("energy_today", 60, load)


@site.get("/api/energy")
def api_energy(request: Request):
    """The house now (from memory) and its kWh today. Read tokens and a signed-in session only: a status
    token is not let in (auth.STATUS_PATHS), and a token for one car is refused here, since this is the
    house's and not that car's."""
    token = getattr(request.state, "token", None)
    if token and token.get("vin"):
        return JSONResponse({"error": "this token is for one car; the house's energy is not any one car's"}, status_code=403)
    live = ENERGY_LIVE.get(datetime.now(timezone.utc), energy.LIVE_MAX_AGE_S)
    return JSONResponse(jsonable({"reading": live, "today": energy_today()}))


@site.get("/api/drives")
def api_drives(vin: str | None = None):
    return JSONResponse(jsonable(drives(api_vin(vin))))


@site.get("/api/charges")
def api_charges(vin: str | None = None):
    return JSONResponse(jsonable([{k: v for k, v in r.items() if k != "mix"} for r in charges(api_vin(vin))]))  # the bar is for the page; its figures are the src_ keys


def jsonable(value):
    """Whatever a page holds, in the types JSON has, all the way down.

    It used to convert the top level only. /api/now?vin=<the Tesla> answered 500 for
    weeks because that car's payload carries a nested "meters" dict whose "at" is a
    datetime, one level below where the converting stopped. Nothing caught it: only the
    HTML pages were ever fetched or timed, and a template renders a datetime perfectly
    well. Decimal is here for the same reason rather than for a bug seen — a SQL ROUND()
    comes back as one, and it would fail exactly this way on whichever endpoint grew it
    first."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):  # after datetime: a datetime is a date
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return float(value)
    return value


# The same site answers at / (the LAN port) and at /cc (behind the tunnel
# through Traefik, which does not strip the prefix). Mounting sets
# request.scope["root_path"] so every link and asset URL is right either way.
def compose_daily_summary(car: dict, day) -> str:
    """Yesterday for this car, from the same tables the pages use."""
    vin = car["vin"]
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
    end_local = start_local + timedelta(days=1)
    day_drives = [d for d in drives(vin, 500) if d["destination_date"] and start_local <= d["start_date"] < end_local]
    day_charges = [c for c in charges(vin, 500) if c["session_start_date"] and start_local <= c["session_start_date"] < end_local]
    states = state_rows(vin, "states", "state", start_local - timedelta(days=1))
    charging = state_rows(vin, "charging_states", "state", start_local - timedelta(days=1))
    rows = daily_breakdown(states, charging, 2, TZ, end_local.astimezone(timezone.utc))
    breakdown = next((r for r in rows if r["date"] == day), None)
    return daily_summary_text(car["name"], day, breakdown, day_drives, day_charges, car.get("soc"), car.get("range_mi"))


STATE_DDL = """
CREATE TABLE IF NOT EXISTS cc_state (
    key   VARCHAR PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def state_get(key: str) -> str | None:
    """Operational state that has to outlive a restart — what has already been sent."""
    with db() as conn:
        row = conn.execute("SELECT value FROM cc_state WHERE key = %s", (key,)).fetchone()
    return (row["value"] if isinstance(row, dict) else row[0]) if row else None


def state_set(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
        conn.commit()


def state_delete(key: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM cc_state WHERE key = %s", (key,))
        conn.commit()


def write_geocoder_state(value: str) -> None:
    """cc_state.geocoder_url and sources_version change together, in one transaction: the
    connector must never see the new routing without also seeing the bump that makes it
    look again (and never restart on a version bump that isn't there yet either). The
    value is a local URL, "public" or "" (off); the connector reads a missing row as off."""
    with db() as conn:
        conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("geocoder_url", value))
        sources.bump(conn)
        conn.commit()


def endpoints_since(since: datetime | None) -> list[tuple[float, float, datetime]]:
    """Drive start/destination points and charge positions after `since` (all history when
    None), lat/lon/date only, ordered by date — what the maps worker feeds to
    `mapsworker.cells()`. Not the 100 m cc_geocells bins pending_cells uses; this is the
    raw points the worker buckets to 0.1-degree cells itself."""
    clause = "e.d > %s" if since else "e.d IS NOT NULL"
    args = (since,) if since else ()
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT e.lat, e.lon, e.d FROM (
                SELECT start_position_latitude AS lat, start_position_longitude AS lon, start_date AS d FROM trips
                UNION ALL SELECT destination_position_latitude, destination_position_longitude, destination_date FROM trips WHERE destination_date IS NOT NULL
                UNION ALL SELECT session_position_latitude, session_position_longitude, session_start_date FROM charging_sessions
            ) e WHERE e.lat IS NOT NULL AND {clause} ORDER BY e.d""",
            args,
        ).fetchall()
    return [(r["lat"], r["lon"], r["d"]) for r in rows]


def requeue_public_geocells() -> None:
    """A new local maps build appeared: mark every publicly-named cell pending for the
    local pass (discover.requeue_public_geocells), so the local namer takes over without
    pages losing their place names in the meantime."""
    with db() as conn:
        _requeue_public_geocells(conn)


def maps_tick(w) -> None:
    """One pass of the maps worker, off the event loop: ask for the regions the cars went
    to, remember how far it got, and when the add-on reports a new version, hand every
    publicly-named cell back to the local namer."""
    w.tick()
    if w.since:
        state_set("maps_seen_through", w.since.isoformat())
    v = (MAPS.status() or {}).get("version")
    if v is not None and str(v) != state_get("maps_version"):
        requeue_public_geocells()
        state_set("maps_version", str(v))


def route_candidates(version: str, limit: int) -> list[dict]:
    """The newest finished drives with no route row for this maps build (routematch)."""
    with db() as conn:
        return conn.execute(routematch.CANDIDATES_SQL, routematch.candidates_args(version, limit)).fetchall()


def route_positions(trip: dict) -> tuple[list[tuple], list | None, list | None]:
    """A drive's own (at, lat, lon) readings and where the car was parked either side."""
    with db() as conn:
        rows = conn.execute(
            "SELECT at, latitude, longitude FROM cc_positions WHERE vin = %s AND at BETWEEN %s AND %s ORDER BY at", (trip["vin"], trip["start_date"], trip["destination_date"])
        ).fetchall()
        before, after = parked_ends(conn, trip["vin"], trip["start_date"], trip["destination_date"])
    return [(r["at"], r["latitude"], r["longitude"]) for r in rows], before, after


def route_save(trip: dict, row: dict) -> None:
    with db() as conn:
        conn.execute(routematch.SAVE_SQL, routematch.save_args(trip, row))
        conn.commit()


def route_fail(trip: dict, reason: str, version: str) -> None:
    with db() as conn:
        conn.execute(routematch.FAIL_SQL, routematch.fail_args(trip, reason, version))
        conn.commit()


ROUTES_EVERY_S = 30  # a tick matches up to six drives, two seconds apart: about 12 a minute at most


def routes_tick(w) -> None:
    """One pass of the route matcher, off the event loop (routematch.Worker)."""
    line = routematch.summary(w.tick())
    if line:
        LOG.info(line)


def geocoder_tick(down_streak: int) -> tuple[int, str | None, bool, bool, str | None]:
    """One geocoder-routing decision: write cc_state.geocoder_url (and bump
    sources_version) if it changed. Returns (down_streak, url, up, public_ok, maps_url)
    so a caller that also wants to name cells this tick does not call MAPS.status()
    a second time.

    Called once at startup, before any warm-up, right after settings are applied, so the
    connector (which reads no decision as off) gets one at once; and every minute after
    that from geocode_loop. MAPS.status() has a 3 s timeout, which is
    an acceptable one-time cost at startup."""
    maps_url = MAPS.base
    up = maps_available(MAPS.status())
    public_ok = current_settings().get("public_geocoder", False)
    url = geocoder_url(maps_url, up, public_ok)
    current = state_get("geocoder_url")
    want, down_streak = geocoder_route(maps_url, up, public_ok, down_streak, current)
    if want != current:
        write_geocoder_state(want)
    return down_streak, url, up, public_ok, maps_url


def maybe_send_summary(car: dict) -> None:
    """Yesterday's summary, once, after 07:00.

    The "once" used to live in a module-level dict, so every restart forgot it and the
    next tick sent yesterday's summary again. On a day with several deploys that is
    several identical messages, hours apart, each looking like a fresh report of a day
    that ended long ago. It lives in the database now, where a restart cannot lose it."""
    last = state_get("last_summary")
    due = summary_due(datetime.now(TZ), date.fromisoformat(last) if last else None, at=SUMMARY_AT)
    if due is None:
        return
    # Recorded before sending: a duplicate is worse than a miss, because a miss is
    # obvious tomorrow and a duplicate looks like real news.
    state_set("last_summary", datetime.now(TZ).date().isoformat())
    recorder.notify(compose_daily_summary(car, due), "daily_summary")


# The recorder has to sample at least as often as the connector refreshes, or the
# connector's rate is irrelevant: this loop is what reads the tree and writes a row, so
# whatever it sleeps is the real resolution of a drive. Same two numbers the connector
# gets, from the same environment, so they cannot drift apart.
def parse_hhmm(value) -> dtime | None:
    try:
        h, m = str(value).strip().split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def maybe_plug_reminder() -> None:
    """Once a day after the set hour: any car that is home, parked, low, and has said it
    is not plugged in. The date it last went out lives in cc_state, as the summary's
    does, so a restart cannot repeat it. Nothing is fetched before the hour."""
    st = current_settings()
    at = parse_hhmm(st.get("plug_reminder_at"))
    if at is None:
        return  # blanked on Settings: off
    now_local = datetime.now(TZ)
    if now_local.time() < at:
        return
    below = float(st.get("plug_reminder_below") or PLUG_REMINDER_BELOW_PCT)
    for v in all_vehicles():
        key = f"plug_reminder:{v['vin']}"
        last = state_get(key)
        last_day = date.fromisoformat(last) if last else None
        if last_day == now_local.date():
            continue
        text = plug_in_reminder(live_vehicle(v["vin"]), now_local, last_day, at, below)
        if text:
            state_set(key, now_local.date().isoformat())
            recorder.notify(text, "plug_reminder")


recorder = Recorder(
    fetch_live=live_vehicle,
    connect=db,
    interval_s=int(os.environ.get("CC_POLL_IDLE_S", 60)),
    driving_interval_s=int(os.environ.get("CC_POLL_DRIVING_S", 10)),
)
teslafi = TeslaFiPoller(os.environ.get("TESLAFI_TOKENS", "").split(","), connect=db, tz=TZ)
teslamate = TeslaMatePoller(os.environ.get("TESLAMATE_DB_URL"), connect=db, tz=TZ)
# The cars' own stream: listens only once the stream address is set and a car is started on Connect Tesla.
TESLA_STREAM_HOST = os.environ.get("TESLA_STREAM_HOST", "").strip()
tesla_stream_worker = tesla_live.TeslaStream(connect=db, url=(os.environ.get("TESLA_STREAM_ZMQ", "").strip() or None) if TESLA_STREAM_HOST else None, tz=TZ)
compare_worker = tesla_compare.Comparer(connect=db, tz=TZ)
switcher = writerswitch.Switcher(connect=db, tz=TZ, after=lambda vin, kind: after_switch(vin, kind))
weather_worker = weather.WeatherWorker(connect=db, fetch=weather.http_fetch, enabled=lambda: WEATHER_ON)


def source_of(v: dict) -> str:
    """Where a car's readings come from: the connector, the writer chosen for it, the TeslaFi
    feed, TeslaMate live, the Tesla stream, or nothing but imports."""
    if v.get("live"):
        return "live"
    vin = v["vin"]
    if vin in writers_chosen():
        return history_writer.SOURCE[writers_chosen()[vin]]
    if vin in set(teslafi.vin_for_token.values()) or _cache_feed_vins().get(vin) == "feed":
        return "feed"
    if _cache_feed_vins().get(vin) == "teslamate":
        return "teslamate"
    if vin in tesla_stream_worker.vins:
        return "tesla"
    return "history"


def writers_chosen() -> dict[str, str]:
    """Each car's chosen writer (history_writer), kept a minute; a choice clears it."""

    def load():
        with db() as conn:
            return history_writer.all_chosen(conn)

    return cached("writers", 60, load)


def _cache_feed_vins() -> dict[str, str]:
    def load():
        with db() as conn:
            rows = conn.execute("SELECT id, vin FROM cc_imports WHERE id LIKE 'teslafi-live:%%' OR id LIKE 'teslamate-live:%%'").fetchall()
        return {(r["vin"] if isinstance(r, dict) else r[1]): ("feed" if str(r["id"] if isinstance(r, dict) else r[0]).startswith("teslafi") else "teslamate") for r in rows}

    return cached("feed_vins", 600, load)


def source_name(v: dict) -> str:
    """A car's source by the plain name Cars and sources gives it: "Lucid (CarConnectivity)",
    "TeslaFi (API)", "Tesla (direct)", "TeslaMate"."""
    import carspage

    return carspage.writer_name(source_of(v), (identity_rows()[1].get(v["vin"]) or (None, None))[0])


def health_cars() -> list[dict]:
    return [{"vin": v["vin"], "name": v["name"], "source": source_of(v), "source_name": source_name(v)} for v in all_vehicles()]


raw_keeper = raw.RawKeeper(connect=db)
health_worker = health_mod.HealthWorker(connect=db, cars=health_cars, notify=lambda text, kind: recorder.notify(text, kind))
# The regional average prices, kept current in the background. The URL is the whole
# published file, the same for everyone: no parameters, nothing about any car.
RATES_URL = os.environ.get("CC_RATES_URL") or averages_mod.RATES_URL
# The makers' prefixes of Tesla's VINs (Fremont, Austin, Shanghai, Berlin, the newer
# lines): the cars whose charging history is worth asking Tesla for.
TESLA_WMIS = ("5YJ", "7SA", "LRW", "XP7", "7G2", "SFZ")


def billed_vins(conn) -> list[str]:
    return [v for v in (r["vin"] if isinstance(r, dict) else r[0] for r in conn.execute("SELECT vin FROM vehicles").fetchall()) if v and v[:3].upper() in TESLA_WMIS]


billed_worker = billed.BilledWorker(connect=db, access=lambda: tesla_access(), fleet=lambda: tesla_app(), vins=billed_vins, clear=clear_prices)
tesla_refresher = teslapage.Refresher(lambda: tesla_facts() if tesla_app() else None)
identity_worker = identity.Worker(
    connect=db,
    access=lambda: tesla_access(),
    fleet=lambda: tesla_app(),
    streamed=lambda: dict(tesla_stream_worker.identity),
    after=lambda: _cache.pop("identity", None),
    vins=lambda: [v["vin"] for v in all_vehicles()],
)
rates_worker = averages_mod.RatesWorker(connect=db, fetch=averages_mod.http_get, url=RATES_URL, after_change=lambda conn: reopen_average_prices(conn, TZ), missing=averages_mod.missing_regions)


def health_notes() -> dict[str, dict]:
    """The last data check per car, as stored by the worker; a minute old at most."""

    def load():
        with db() as conn:
            rows = conn.execute("SELECT key, value FROM cc_state WHERE key LIKE 'health:%%'").fetchall()
        out = {}
        for r in rows:
            key, value = (r["key"], r["value"]) if isinstance(r, dict) else (r[0], r[1])
            got = health_mod.decode(value)
            if got:
                out[key.split(":", 1)[1]] = got
        return out

    return cached("health_notes", 60, load)


def health_findings(car: dict) -> list[dict]:
    """The findings the footer shows: this car's, or every car's for the garage."""
    notes = health_notes()
    vins = [v["vin"] for v in all_vehicles()] if car.get("garage") else [car["vin"]]
    return [f for vin in vins for f in (notes.get(vin) or {}).get("findings", [])]


def apply_schema(conn) -> None:
    """Every table this app owns, created if missing.

    Under a lock_timeout, and that is the whole point. A CREATE or ALTER that cannot get
    its lock does not wait politely — it queues, and every reader queuing behind it stops
    too, so one slow backfill turns a deploy into an outage of the whole site. That has
    happened twice: 2026-09-05 for eight minutes, and again on 2026-09-06 for six while a
    charge-facts backfill held the table. Three seconds, then give up and try again.
    """
    for name, ddl in (
        ("places", PLACES_DDL),
        ("software", SOFTWARE_DDL),
        ("facts", FACTS_DDL),
        ("tags", TAGS_DDL),
        ("service", SERVICE_DDL),
        ("settings", settings_mod.DDL),
        ("auth", auth.DDL),
        ("api tokens and log", auth.API_DDL),
        ("app connect codes", connect.DDL),
        ("sources", sources.DDL),
        ("state", STATE_DDL),
        ("rate averages", averages_mod.DDL),
        ("tesla bills", billed.DDL),
        ("tesla stream", tesla_live.DDL),
        ("tesla navigation", tesla_nav.DDL),
        ("house energy", energy.DDL),
        ("plan search cache", urdb.DDL),
        # The importer's and the recorder's tables are read by the site whether or not
        # anything was ever imported or recorded, so they are created here rather than on
        # first use. /day queried cc_inside_temperatures and 500'd on a fresh install.
        ("imports", IMPORT_DDL),
        ("positions", RECORDER_DDL),
        ("trip extras", "CREATE TABLE IF NOT EXISTS cc_trip_extras (vin VARCHAR NOT NULL, start_date TIMESTAMPTZ NOT NULL, fsd_miles DOUBLE PRECISION, PRIMARY KEY (vin, start_date))"),
        ("weather", weather.DDL),
        ("by hand", manual.DDL),
        ("auto tags", autotag.DDL),
        ("geocells", GEOCELL_DDL),
        ("trip routes", routematch.DDL),
        ("car identity", identity.DDL),
        # One-time: every geocell already named got there from the public Nominatim, since
        # there was no other source before cc_geocells.source existed.
        ("geocells backfill", GEOCELL_BACKFILL),
        ("geocells rename", RENAME_LOCAL_ONCE),
    ):
        for attempt in range(1, SCHEMA_ATTEMPTS + 1):
            try:
                # SET LOCAL, on every attempt: a rollback undoes a plain SET, so a retry ran
                # with no timeout at all, and a session-wide one would outlive this block on
                # a pooled connection. A commit per table, so a lock on one keeps the rest.
                conn.execute("SET LOCAL lock_timeout = '3s'")
                # Turns with the connector's grants, which rewrite every table's catalogue row
                # (stack/cc/plugin_role.py); held to the commit, under the same timeout.
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (GRANTS_LOCK,))
                conn.execute(ddl)
                conn.commit()
                break
            except psycopg.errors.LockNotAvailable:
                conn.rollback()
                if attempt == SCHEMA_ATTEMPTS:
                    # Better to serve the site against the schema that is already there
                    # than to hold the door shut waiting for a lock.
                    LOG.warning("schema: %s is still locked after %d tries; carrying on without it", name, SCHEMA_ATTEMPTS)
                    break
                LOG.info("schema: %s is locked, retrying (%d/%d)", name, attempt, SCHEMA_ATTEMPTS)
                time.sleep(SCHEMA_RETRY_S)


SCHEMA_ATTEMPTS = 5
GRANTS_LOCK = 0x6363_6772_6E74  # "ccgrnt", as in stack/cc/plugin_role.py
SCHEMA_RETRY_S = 2


def apply_sources() -> dict:
    """Point the pollers at what the sources say: the table's, or .env's until a kind is moved in."""
    try:
        with db() as conn:
            rows = sources.load(conn, VAULT)
    except Exception as err:  # noqa: BLE001 - no table yet, or no database: .env as before
        LOG.warning("sources: %s; using .env", err)
        rows = []
    eff = sources.effective(rows, os.environ)
    teslafi.use(eff["teslafi"])
    teslamate.use(eff["teslamate"]["url"], eff["teslamate"]["vins"])
    tesla_stream_worker.use(tesla_streaming(rows))
    return eff


def tesla_streaming(rows: list[dict]) -> list[str]:
    """The cars started on Connect Tesla: the only ones whose stream is kept."""
    account = next((r for r in rows if r["kind"] == "tesla"), None)
    return list(((account or {}).get("config") or {}).get("stream") or [])


# ---- The house's energy, from Tesla's cloud: live status every five minutes (kept in memory for
# the pages) and, once someone starts it, the five-minute history in cc_energy. The config is in cc_state.
ENERGY_LIVE = energy.Live()
ENERGY_CLOUD = {"site": ""}  # the Tesla site id while the cloud source is on; read by its worker, set by apply_energy


def energy_config() -> dict:
    return energy.load_config(state_get)


def energy_save(cfg: dict) -> None:
    energy.save_config(state_set, cfg)


def energy_write(rows: list[dict], source: str = "cloud") -> None:
    with db() as conn:
        energy.store(conn, rows, source)
        conn.commit()


def energy_have_day(day) -> bool:
    """Whether a local day already holds enough readings that the cloud need not be asked."""
    lo = datetime(day.year, day.month, day.day, tzinfo=TZ)
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM cc_energy WHERE site = %s AND at >= %s AND at < %s", (energy.SITE, lo, lo + timedelta(days=1))).fetchone()
    return bool(row) and int(row["n"] or 0) >= energy_cloud.DAY_ROWS


cloud_worker = energy_cloud.CloudWorker(
    fleet=lambda: tesla_app(),
    access=lambda: tesla_access(),
    site=lambda: ENERGY_CLOUD["site"],
    tz=TZ.key,
    get=state_get,
    put=state_set,
    write=lambda rows: energy_write(rows, "cloud"),
    have_day=energy_have_day,
    live=ENERGY_LIVE,
)


def apply_energy() -> dict:
    """Point the cloud worker at the site, when one is switched on. Cheap to call again after any change."""
    cfg = energy_config()
    ENERGY_CLOUD["site"] = cfg["cloud"]["site_id"] if cfg["cloud"]["enabled"] else ""
    cloud_worker.tz = TZ.key
    return cfg


def energy_now() -> dict | None:
    """The Powerwall row for Now: the newest reading from memory, or None. Never a query."""
    return energypage.row(ENERGY_LIVE.get(datetime.now(timezone.utc), energy.LIVE_MAX_AGE_S))


def energy_scope_wanted() -> bool:
    """Whether a sign-in should keep the Powerwall scope: the token already holds it, or the cloud
    source is on. A car re-sign-in (which replaces the token) must not drop what the Powerwall uses."""
    try:
        held = tesla_energy_scope()
        return bool((held or {}).get("granted")) or bool(energy_config()["cloud"]["enabled"])
    except Exception as err:  # noqa: BLE001 - the sign-in goes ahead with the vehicle scopes
        LOG.warning("energy: %s", err)
        return False


def energy_adopt_after_sign_in(fleet, access: str, base: str) -> None:
    """Right after the energy sign-in: the one site on the account becomes the source. Several are
    left for the step to ask about; a failure is left for the step to show."""
    try:
        if tesla_fleet.ENERGY_SCOPE in tesla_fleet.granted_scopes(access):
            energy_adopt(fleet, (access, base), "")
    except Exception as err:  # noqa: BLE001 - the sign-in itself worked
        LOG.warning("energy: %s", type(err).__name__)


def tesla_energy_scope() -> dict | None:
    """Whether the signed-in Tesla account's token may read a Powerwall, from the token itself
    (no call to Tesla); None with no account."""
    with db() as conn:
        row = tesla_account(conn)
    if not row or not row["plain"]:
        return None
    try:
        token = json.loads(row["plain"]).get("access_token")
    except ValueError:
        return None
    return {"granted": tesla_fleet.ENERGY_SCOPE in tesla_fleet.granted_scopes(token)}


def energy_context() -> dict:
    cfg, now = energy_config(), datetime.now(timezone.utc)
    try:
        scope = tesla_energy_scope()
    except Exception as err:  # noqa: BLE001 - the page still opens
        LOG.warning("energy: %s", err)
        scope = None
    live = ENERGY_LIVE.get(now, 3600.0)
    return {
        "cloud": energypage.cloud_view(cfg, scope, cloud_worker.status, energy_cloud.progress(state_get), datetime.now(TZ).date() - timedelta(days=1)),
        "reading": energypage.row(live),
        "reading_age": energypage.ago(live["age_s"]) if live else "",
    }


def sun_by_month(vin: str | None) -> list[dict]:
    """The share of home charging that was the sun's, by month, from the stored splits. Kept
    ten minutes: it is the Stats page, and nothing on it changes faster than a charge ends."""

    def load():
        vw, vargs = _vin_clause(vin)
        with db() as conn:
            rows = conn.execute(
                "SELECT to_char(c.session_start_date AT TIME ZONE %s, 'YYYY-MM') AS ym, SUM(x.solar_kwh) AS solar, SUM(x.battery_kwh) AS battery, SUM(x.grid_kwh) AS grid,"
                " SUM(x.battery_kwh * COALESCE(x.battery_sun_frac, 0)) AS battery_sun"
                " FROM cc_charge_sources x JOIN charging_sessions c ON c.id = x.session_id JOIN cc_charge_facts f ON f.session_id = c.id AND f.at_home"
                " WHERE x.status = 'ok'" + vw.replace("t.vin", "c.vin") + " GROUP BY 1 ORDER BY 1 DESC LIMIT 24",
                [TZ.key, *vargs],
            ).fetchall()
        return energypage.sun_months(rows)

    return cached(f"sun_months:{vin or 'all'}", 600, load)


def energy_flow_context(query: dict) -> dict | None:
    """The Powerwall steps of Connect Tesla: where they stand, and what the last action said (kept
    in the query by energy_reply). A page view reads only what is kept: the token's own scopes, the
    config, and the account's sites if an earlier action asked for them. It never calls Tesla and
    never writes; adopting the site is the Powerwall step's button, and the energy sign-in's callback."""
    if not tesla_app():
        return None
    ctx = energy_context()
    cfg, scope = energy_config(), tesla_energy_scope()
    hit = TESLA_CACHE.get("energy_sites")
    choices = hit[1] if hit and isinstance(hit[1], list) and time.time() - hit[0] < 900 else None
    prog = energy_cloud.progress(state_get)
    return {"flow": energypage.flow(cfg, scope, choices, prog, datetime.now(TZ).date() - timedelta(days=1)), "cloud": ctx["cloud"], "error": query.get("pwe")}


def energy_page(request: Request, status_code: int = 200, **extra) -> HTMLResponse:
    r = settings_render(request, "energy", **extra)
    r.status_code = status_code
    return r


def energy_reply(request: Request, back, code: int = 200, saved: str = "", anchor: str = "pw", **extra):
    """Answer an energy action where it was asked. From the Tesla flow (`back` = tesla) it is a
    redirect to the same step of that page, what it has to say carried in the query; from Settings ›
    Energy it is that page, or its saved notice."""
    if back == "tesla":
        q = {}
        if extra.get("energy_error"):
            q["pwe"] = str(extra["energy_error"])[:300]
        if saved:
            q["pws"] = saved
        return to_site(request, f"/settings/sources/tesla{'?' + urlencode(q) if q else ''}#{anchor}")
    if saved:
        return to_site(request, f"/settings/energy?saved={saved}")
    return energy_page(request, code, **extra)


def energy_adopt(fleet, got, pick: str | None = None, known: list[dict] | None = None) -> tuple[str | None, list[dict]]:
    """Use the energy site on the Tesla account: `pick` (a site id), else the only one, else the
    first battery. (why not, the sites) -- the page asks which when there are several and none
    was picked by the caller that passes pick=''. Reads Tesla, so off the event loop."""
    try:
        found = known if known is not None else fleet.energy_sites(*got)
    except tesla_fleet.TeslaError as err:
        return (energy_cloud.SCOPE_WORDS if err.status in (401, 403) else err.public), []
    if not found:
        return "That Tesla account has no Powerwall or solar site.", []
    chosen = next((x for x in found if x["site_id"] == pick), None) if pick else None
    if chosen is None and pick is None:
        chosen = next((x for x in found if x["kind"] == "battery"), found[0])
    if chosen is None and len(found) == 1:
        chosen = found[0]
    if chosen is None:
        TESLA_CACHE["energy_sites"] = (time.time(), found)  # the page asks which, from what was just read
        return None, found
    try:
        installed = fleet.energy_installed(got[0], got[1], chosen["site_id"]) or ""
    except tesla_fleet.TeslaError:
        installed = ""
    cfg = energy_config()
    cfg["cloud"] = {"enabled": True, "site_id": chosen["site_id"], "name": chosen["name"], "installed": installed}
    energy_save(cfg)
    apply_energy()
    return None, found


@site.post("/settings/energy/cloud")
async def energy_cloud_on(request: Request):
    """Use the Tesla account's Powerwall: the site picked, else the one on the account, and switch the source on."""
    form = await request.form()
    back = form.get("back")
    fleet, got = tesla_app(), tesla_access()
    if not fleet or not got:
        return energy_reply(request, back, 400, anchor="pw", energy_error="Connect your Tesla account first (Settings › Cars and sources › Connect Tesla).")
    hit = TESLA_CACHE.get("energy_sites")
    known = hit[1] if form.get("site") and hit and isinstance(hit[1], list) and time.time() - hit[0] < 900 else None  # a pick is made from the list that was shown
    pick = form.get("site") or ("" if back == "tesla" else None)  # in the flow, no pick means "the only one"; on Energy, the first battery
    why, _found = await asyncio.to_thread(energy_adopt, fleet, got, pick, known)
    if why:
        return energy_reply(request, back, 400, anchor="pw", energy_error=why)
    return energy_reply(request, back, saved="cloud", anchor="pw")


@site.get("/settings/energy/progress")
def energy_progress():
    """The backfill's progress as JSON, for the page's bar to redraw from; a read of two small values."""
    cfg, prog = energy_config(), energy_cloud.progress(state_get)
    view = energypage.cloud_view(cfg, None, None, prog, datetime.now(TZ).date() - timedelta(days=1))
    return JSONResponse(energypage.backfill_json(prog, view["backfill"]["total"]), headers={"Cache-Control": "no-store"})


@site.post("/settings/energy/cloud/off")
async def energy_cloud_off(request: Request):
    form = await request.form()
    back = form.get("back")
    cfg = energy_config()
    cfg["cloud"] = energy.default_config()["cloud"]
    energy_save(cfg)
    cloud_worker.stop_backfill()
    apply_energy()
    return energy_reply(request, back, saved="cloud-off", anchor="pw")


@site.post("/settings/energy/backfill")
async def energy_backfill(request: Request):
    form = await request.form()
    back = form.get("back")
    if form.get("do") == "stop":
        cloud_worker.stop_backfill()
        return energy_reply(request, back, saved="backfill-stopped", anchor="pw-history")
    if not ENERGY_CLOUD["site"]:
        return energy_reply(request, back, 400, anchor="pw-history", energy_error="Turn on the Tesla account first.")
    await asyncio.to_thread(cloud_worker.start_backfill, datetime.now(TZ).date() - timedelta(days=1))
    return energy_reply(request, back, saved="backfill", anchor="pw-history")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with db() as conn:
        apply_schema(conn)
        try:
            if migrate_home(conn, queue_reprice):
                LOG.info("places: home now follows the icon")
        except Exception as err:  # noqa: BLE001 - the site still starts; it tries again next start
            LOG.warning("places: home migration: %s", err)
            conn.rollback()
        try:
            bound = sources.bind_sealed(conn, VAULT)  # once: every source secret sealed for the row it is in
        except Exception as err:  # noqa: BLE001 - the site still starts; the connector binds them at its start too
            LOG.warning("sources: could not bind sealed secrets (%s)", type(err).__name__)
            conn.rollback()
            bound = 0
        conn.commit()
    if bound:
        LOG.info("sources: bound %s sealed secrets to their rows", bound)

    load_auth()
    # An in-process cache of the trip id each car last got a message about, so the common
    # tick costs no query. It is only a cache: the answer that survives a restart is in
    # cc_state, and that is the one consulted whenever this does not already know.
    cached_trip: dict[str, int] = {}

    def drive_summary(car):
        """One message per finished drive, once the database plugin has closed it."""
        latest = next(iter(drives(car["vin"], 1)), None)
        if not latest or latest.get("destination_date") is None:
            return
        if cached_trip.get(car["vin"]) == latest["id"]:
            return
        key = f"last_drive:{car['vin']}"
        due = drive_announcement_due(latest, state_get(key), datetime.now(timezone.utc))
        cached_trip[car["vin"]] = latest["id"]
        # Written before the message goes out, as the daily summary is: a duplicate reads
        # as real news and a miss does not.
        state_set(key, str(latest["id"]))
        if not due:
            return
        bits = [f"{latest['miles']:.1f} mi in {humanize_duration(latest['duration'])}"]
        if latest.get("wh_per_mi"):
            bits.append(f"{latest['wh_per_mi']:.0f} Wh/mi")
        if latest.get("level_start") is not None and latest.get("level_end") is not None:
            bits.append(f"{latest['level_start']:.0f}→{latest['level_end']:.0f}%")
        if latest.get("cost"):
            bits.append(f"${latest['cost']:.2f}")
        recorder.notify(f"{car['name']} drove {latest['from']} → {latest['to']}: " + ", ".join(bits) + ".", "drive_summary")

    def on_snapshot(car):
        record_software(car["vin"], car.get("software"))
        try:
            held = _cache.get("garage_tree")
            if held:
                raw_keeper.keep(car["vin"], held[1].get(car["vin"]) or {})
        except Exception as err:  # noqa: BLE001
            LOG.warning("raw: %s", err)
        try:
            record_runs(car)
        except Exception as err:  # noqa: BLE001
            LOG.warning("runs: %s", err)
        maybe_send_summary(car)
        try:
            maybe_plug_reminder()
        except Exception as err:  # noqa: BLE001
            LOG.warning("plug reminder: %s", err)
        fill_missing_facts()
        try:
            apply_auto_tags()
        except Exception as err:  # noqa: BLE001
            LOG.warning("auto tags: %s", err)
        try:
            drive_summary(car)
        except Exception as err:  # noqa: BLE001
            LOG.warning("drive summary: %s", err)

    recorder.on_snapshot = on_snapshot
    apply_settings(current_settings())
    # Before anything else starts, including the warm-up below: the connector reads no
    # decision as off, so write it at once and an install that allows the public geocoder
    # (or has local maps) names its stops from the first minute.
    try:
        down_streak = (await asyncio.to_thread(geocoder_tick, 0))[0]
    except Exception as err:  # noqa: BLE001
        LOG.warning("geocoder: %s", err)
        down_streak = 0
    recorder.start()
    apply_sources()  # the pollers start with what the sources say: the table's, or .env's
    weather_worker.start()
    health_worker.start()
    rates_worker.start()
    billed_worker.start()
    identity_worker.start()
    tesla_refresher.start()
    try:
        with db() as conn:
            await asyncio.to_thread(energy.drop_gateway, state_get, state_set, state_delete, conn)  # a local gateway was once read; nothing of it is kept
            await asyncio.to_thread(energy.reset_sources, state_get, state_set, conn)  # splits made by an older model are redone
            conn.commit()
        await asyncio.to_thread(apply_energy)
    except Exception as err:  # noqa: BLE001 - no table or key yet: the house is simply not read
        LOG.warning("energy: %s", err)
    cloud_worker.start()

    async def geocode_loop():
        # Warm the finished bodies, not just the cells: the cells were never the slow
        # part. Every car plus the garage, which is the key the Map opens on.
        try:
            await asyncio.to_thread(lambda: [driveprint_body(k) for k in [GARAGE, *(v["vin"] for v in all_vehicles())]])
        except Exception as err:  # noqa: BLE001
            LOG.warning("driveprint warm-up: %s", err)
        # Every minute: local maps first, the public Nominatim only if Settings > Maps
        # allows it, otherwise nothing is looked up at all. A rate that matters only to
        # the public server (one lookup a second) relaxes once a local one answers.
        # down_streak carries over from the startup tick above and then lives here,
        # across ticks of this one loop, so a blip in the local service does not restart
        # the connector (geocoder_route, fix round 1 item 3).
        nonlocal down_streak
        while True:
            try:
                down_streak, url, up, public_ok, maps_url = await asyncio.to_thread(geocoder_tick, down_streak)
                if url:
                    local = up and bool(maps_url) and url.startswith(maps_url.rstrip("/"))
                    # A local "unable" falls back to the public Nominatim once, only when
                    # Settings > Maps allows it; never the other way around.
                    fallback_url = NOMINATIM if local and public_ok else None
                    n = await asyncio.to_thread(lambda: geocode_cells(db_plain(), 200 if local else 30, 0.0 if local else 1.1, url, "local" if local else "public", fallback_url))
                    if n:
                        _cache.pop("geocells", None)
            except Exception as err:  # noqa: BLE001
                LOG.warning("geocoder: %s", err)
            await asyncio.sleep(60)

    geo_task = asyncio.create_task(geocode_loop())

    async def maps_loop():
        # Never runs on a page path: every 600 s, in the background, asking MAPS (3 s
        # timeout per call) for the regions the cars actually go to. Unset MAPS_URL means
        # no add-on configured at all, so there is nothing to ask.
        if not MAPS.base:
            return
        w = MapsWorker(MAPS, endpoints_since, lambda: datetime.now(timezone.utc))
        seen = await asyncio.to_thread(state_get, "maps_seen_through")
        if seen:
            w.since = datetime.fromisoformat(seen)
        while True:
            try:
                await asyncio.to_thread(maps_tick, w)
            except Exception as err:  # noqa: BLE001
                LOG.warning("maps worker: %s", err)
            await asyncio.sleep(600)

    maps_task = asyncio.create_task(maps_loop())

    async def routes_loop():
        # Never on a page path: drives with sparse readings matched to the roads on the
        # local maps add-on, a few each tick, newest first, spaced so its place lookups
        # always get a turn. Unset MAPS_URL: nothing to match against.
        if not MAPS.base:
            return
        w = routematch.Worker(MAPS, route_candidates, route_positions, route_save, route_fail, sleep=time.sleep)
        await asyncio.sleep(120)  # after the start-up warm-ups
        while True:
            try:
                await asyncio.to_thread(routes_tick, w)
            except Exception as err:  # noqa: BLE001
                LOG.warning("routes: %s", err)
            await asyncio.sleep(ROUTES_EVERY_S)

    routes_task = asyncio.create_task(routes_loop())

    async def stream_config_check():
        # Once, a minute after start: a started car whose config predates the field list is
        # sent the new one (tesla_refresh_configs). Off the start path; nothing waits on it.
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(tesla_refresh_configs)
        except Exception as err:  # noqa: BLE001
            LOG.warning("tesla stream config check: %s", err)

    config_task = asyncio.create_task(stream_config_check())
    yield
    config_task.cancel()
    geo_task.cancel()
    maps_task.cancel()
    routes_task.cancel()
    await recorder.stop()
    await teslafi.stop()
    tesla_stream_worker.stop()
    tesla_refresher.stop()
    cloud_worker.stop()


# No docs of its own: the outer app has no routes to describe, and its /docs answered before sign-in.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# The bare prefix, before the mounts. Starlette's Mount only matches "/cc/...", and the
# catch-all mount at "/" then swallows a bare "/cc" and answers 404 from inside the site
# — so the router's own redirect-slashes never gets a look. Anyone who types the short
# URL without a trailing slash, which is everyone, got {"detail":"Not Found"}.
for _prefix in ("/carcommand", "/cc"):
    app.add_route(_prefix, lambda request, to=_prefix + "/": RedirectResponse(to, status_code=308), methods=["GET", "HEAD"])

app.mount("/carcommand", site)
app.mount("/cc", site)
app.mount("/", site)


class TruePeer:
    """What uvicorn runs (app:server, without --proxy-headers): keeps the socket's own
    address in the scope, then lets uvicorn's proxy-header middleware put the forwarded
    client in its place as --proxy-headers did. The sign-in throttle and the logs go on
    reading the forwarded address; only the admin network rule needs the one nobody can
    write, and a request that reached the app any other way has none and is refused."""

    KEY = "cc_peer"

    def __init__(self, inner):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        self.inner = ProxyHeadersMiddleware(inner, trusted_hosts="*")

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope[self.KEY] = (scope.get("client") or (None,))[0]
        await self.inner(scope, receive, send)


server = TruePeer(app)
