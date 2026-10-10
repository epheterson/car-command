"""User settings, stored as one JSON document per key in cc_settings and cached in process.
Defaults are the household's, but everything here is editable on the Settings page."""

from __future__ import annotations

import json
from datetime import date

import cost

DEFAULTS = {
    "currency": "$",
    # "imperial" (miles, mph, °F, Wh/mi, feet) or "metric". Storage is always imperial;
    # this only decides how a page says a number. See units.Units.
    "units": "imperial",
    # "system" follows the device; "light" or "dark" overrides it. The dark palette is
    # the same either way; this only decides who chooses.
    "theme": "system",
    # Lists and totals ignore a drive shorter than this and a charge that added less than
    # this: the two-minute shuffle in the driveway, the three-minute plug-in blip.
    "min_drive_mi": 0.1,
    "min_charge_kwh": 0.0,
    # Calendar weeks begin on this day; 12- or 24-hour clock; month-first or day-first dates.
    "week_start": "monday",
    "clock": "12",
    "date_order": "mdy",
    # Local time after which yesterday's summary goes out.
    "summary_at": "07:00",
    # Alert kinds switched off; every kind is on until it is.
    "muted_alerts": [],
    "alert_routes": None,
    "dc_per_min": None,  # fast charging billed by the minute, on top of dc_price
    "tire_low_psi": None,  # alert when any tire reads below / above these; blank is off
    "tire_high_psi": None,  # kind -> [channels]; None means every kind goes everywhere (or muted_alerts, from before routing)
    # Weather at the start of each drive from Open-Meteo, fetched in the background. Off
    # means no request leaves the house; on sends a rounded coordinate and a date. Off for
    # a new install, like the map services; load() keeps an existing install's weather on.
    "weather": False,
    # Whether map backgrounds and stop names may come from public services. Both start
    # off for a new install (nothing goes to a third party by default); load() migrates an
    # existing install to True once, so it keeps the tiles and geocoding it already had.
    "public_tiles": False,
    "public_geocoder": False,
    # The home time-of-use plan, in cost.py's shape. None until one is set: home charges
    # then carry no cost rather than one priced on somebody else's utility.
    "rate_plan": None,
    "manual_plan": None,  # a typed-in plan, kept while an OpenEI one is tried, for Stop using
    # The petrol car Stats compares against (blank: a 30 mpg car, said on the page) and,
    # optionally, one fuel price for every charge (blank: the average petrol price for
    # each charge's region and month, marked as an estimate).
    # Flat price per kWh for fast charging away from home. Blank means DC sessions show
    # no cost, which is honest; a number here is one you know you paid.
    "dc_price": None,
    # The same two prices for Tesla Superchargers, where the data says a fast charge was
    # one (TeslaFi's and TeslaMate's charger type, Tesla's billing). dc_price and
    # dc_per_min are then every other fast charger, and any whose network is not known.
    "sc_price": None,
    "sc_per_min": None,
    "gas_mpg": None,
    "gas_price": None,
    # The plug-in reminder: local time after which a car that is home, parked, below the
    # level and has said it is unplugged gets one message a day.
    # IANA zone name for every date on the site. None means the container's TZ, which is
    # what it always was; the switch exists so nobody has to edit a compose file to move.
    "timezone": None,
    "plug_reminder_at": "21:00",
    "plug_reminder_below": 50,
    # Before plans: the plan's name, its dated rate rows and its schedule rows. Read only
    # by load(), once, to turn an older install's prices into a plan; None is "never saved".
    "plan": None,
    "rates": None,
    "schedule": None,
}

DDL = """
CREATE TABLE IF NOT EXISTS cc_settings (
    key   VARCHAR PRIMARY KEY,
    value JSONB NOT NULL
);
"""


def load(conn) -> dict:
    conn.execute(DDL)
    out = dict(DEFAULTS)
    rows = conn.execute("SELECT key, value FROM cc_settings").fetchall()
    seen = set()
    for r in rows:
        k, v = (r["key"], r["value"]) if isinstance(r, dict) else (r[0], r[1])
        # psycopg hands JSONB back already decoded, so a string here is the setting's
        # value, not JSON text. This used to json.loads() it, which fails on anything
        # that is not itself JSON — "$", "Home", "imperial" — and every page that
        # reads settings answered 500 the first time anyone pressed Save. It was latent
        # only because the form had never been submitted: the defaults are strings too,
        # but they never pass through here.
        out[k] = v
        seen.add(k)
    if "public_tiles" not in seen and "public_geocoder" not in seen:
        # Decided once and written down, so the answer cannot change later. No rows at
        # all is a brand-new install: it starts opted out, and writing False now stops
        # the first save of any other section (which writes only that section's keys)
        # from making it look like an old install next time. Rows without these keys are
        # an install from before the switches existed: it keeps today's behaviour
        # (public tiles and geocoder) rather than going dark on upgrade.
        # Written straight to the row, not through save(): load() runs inside the
        # caller's own `with db() as conn` block, which commits on exit, and a
        # minimal connection double (see test_settings.py) need not support commit().
        decided = bool(rows)
        for key in ("public_tiles", "public_geocoder"):
            out[key] = decided
            conn.execute("INSERT INTO cc_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, json.dumps(decided)))
    if "rate_plan" not in seen:
        # Also decided once. An install from before plans priced every home charge on its
        # saved rates and schedule, or on the built-in ones where it had saved none; that
        # becomes its plan, so no cost moves. A brand-new install starts with none.
        out["rate_plan"] = cost.legacy_plan(out.get("plan"), out.get("rates"), out.get("schedule")) if rows else None
        conn.execute("INSERT INTO cc_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("rate_plan", json.dumps(out["rate_plan"])))
    if "weather" not in seen:
        # Decided once, like the map services: a new install starts with weather off, so no
        # position leaves the house until someone turns it on; an install from before keeps
        # the weather it already had.
        out["weather"] = bool(rows)
        conn.execute("INSERT INTO cc_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("weather", json.dumps(out["weather"])))
    if "sc_price" not in seen and "sc_per_min" not in seen:
        # Decided once, too. Fast charging had one price for every network; it becomes the
        # Supercharger price as well, so no fast charge changes cost when the two are split.
        for new, old in (("sc_price", "dc_price"), ("sc_per_min", "dc_per_min")):
            out[new] = out.get(old) if rows else None
            conn.execute("INSERT INTO cc_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (new, json.dumps(out[new])))
    return out


def save(conn, key: str, value) -> None:
    conn.execute("INSERT INTO cc_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, json.dumps(value)))
    conn.commit()


def parse_rate_rows(form_rows: list[dict]) -> list[dict]:
    """Form rows (strings) into rate rows; blank or unparsable rows are dropped."""
    out = []
    for r in form_rows:
        try:
            d = date.fromisoformat(r.get("from", "").strip())
            vals = {k: float(r.get(k, "").strip()) for k in ("off_peak", "part_peak", "peak")}
        except (ValueError, AttributeError):
            continue
        if all(0 <= v < 10 for v in vals.values()):
            out.append({"from": d.isoformat(), **vals})
    return sorted(out, key=lambda x: x["from"])


def valid_zone(name) -> str | None:
    """An IANA zone name the system knows, or None for anything else — blank included."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = (name or "").strip()
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name
