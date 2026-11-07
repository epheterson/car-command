"""Import a car's history from Tessie, read-only, with the user's own Tessie token.

    python3 tessie_import.py [--vin VIN] [--since 2024-01-01] [--again] [--no-curves] [--step 5] [--dry-run]

Tessie's API (https://developer.tessie.com, base https://api.tessie.com, `Authorization: Bearer <token>`;
a token is made at dash.tessie.com/settings/api). Only GET requests go out, and the only things in them are
the token (a header, never the address), the VIN and a time range. Endpoints and fields, as the published
OpenAPI file describes them (docs/sources.md lists what was checked and what is assumed):

    GET /vehicles                      {results: [{vin, last_state: {display_name, ...}}]}
    GET /{vin}/drives?from&to          {results: [{id, started_at, ended_at (unix s), starting_/ending_ latitude,
                                       longitude, starting_battery, ending_battery, average_outside_temperature,
                                       max_speed, odometer_distance, energy_used (kWh), tag, ...}]}
    GET /{vin}/charges?from&to         {results: [{id, started_at, ended_at, latitude, longitude, is_supercharger,
                                       odometer, energy_added, energy_used, starting_battery, ending_battery, cost}]}
    GET /{vin}/states?from&to&interval&condense=false
                                       {results: [{timestamp, latitude, longitude, speed, power, odometer, battery_level,
                                       battery_range, charger_power, charger_voltage, charger_actual_current, ...}]}

Drives and charges come from the lists; each drive's route (position, speed, power, odometer, level, range) and
each charge's power, volts and amps come from the car's historical states inside that drive or charge, at
`--step` seconds (1 returns every reading Tessie holds). Everything is written through `history.write_history`,
one import per car per month (`tessie:{vin}:{YYYY-MM}`, rolling), so a rerun replaces a month and nothing doubles.

Where another source already holds a car's time (an export, a TeslaMate load, the live logger), Tessie fills only
the time none of them cover: a drive or charge that overlaps another import's span, or starts after the
live logger began, is left out, and a month wholly inside such a span is not even asked for. Months older than the
last full month that are already loaded are skipped, which is also how an interrupted run carries on.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from history import _t, fill_after_import, live_since, write_history
from teslafi_history import _rows, build_rows, save_trip_facts
from teslafi_import import USABLE_KWH, _Runs, model_from_vin

LOG = logging.getLogger("carcommand.tessie")
BASE = os.environ.get("TESSIE_URL", "https://api.tessie.com")
UTC = timezone.utc
PAUSE = float(os.environ.get("TESSIE_PAUSE_S", "1.0"))  # between requests: Tessie documents no rate limit, so one a second
EARLIEST = datetime(2012, 6, 1, tzinfo=UTC)  # no Tesla was delivered before this
STOP_AFTER = 12  # months in a row with nothing in them, walking back, before the history is taken to have begun
DC_KW = 25.0  # a charge that drew more than this is DC even where Tessie does not call it a Supercharger
LABEL = "tessie import"
KM_PER_MI = 1.609344


class TessieError(Exception):
    """Tessie could not be asked or did not answer; the message never holds the token."""


class TessieAuth(TessieError):
    """The token was refused."""


def import_id(vin: str, month: datetime) -> str:
    return f"tessie:{vin}:{month:%Y-%m}"


# ---- the client
def http_fetcher(token: str, pause: float = PAUSE, tries: int = 6, client=None, sleep=time.sleep):
    """`fetch(path, **params) -> parsed JSON`: GET only, the token in a header, one request per `pause` seconds,
    and a wait (the server's Retry-After, else doubling) before trying a 429, 5xx or dropped connection again."""
    import httpx

    http = client or httpx.Client(base_url=BASE, timeout=120, follow_redirects=False)
    last = [0.0]

    def fetch(path: str, **params):
        for attempt in range(tries):
            wait = last[0] + pause - time.monotonic()
            if wait > 0:
                sleep(wait)
            last[0] = time.monotonic()
            try:
                r = http.get(path, params={k: v for k, v in params.items() if v is not None}, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            except httpx.TransportError as err:
                why, retry_after = type(err).__name__, None
            else:
                if r.status_code in (401, 403):
                    raise TessieAuth("Tessie refused the token. Make a new one at dash.tessie.com/settings/api.")
                if r.status_code == 404:
                    raise TessieError("Tessie does not know that car on this token.")
                if r.status_code == 429 or r.status_code >= 500:
                    why, retry_after = f"HTTP {r.status_code}", r.headers.get("retry-after")
                elif r.status_code != 200:
                    raise TessieError(f"Tessie answered HTTP {r.status_code}.")
                else:
                    try:
                        return r.json()
                    except ValueError:
                        raise TessieError("Tessie's answer was not JSON.") from None
            if attempt + 1 == tries:
                raise TessieError(f"Tessie kept failing ({why}); run it again later, it carries on where it stopped.")
            try:
                delay = min(300.0, float(retry_after)) if retry_after else min(120.0, 2.0 ** (attempt + 2))
            except ValueError:
                delay = 30.0
            LOG.info("tessie: %s, waiting %.0f s", why, delay)
            sleep(delay)
        raise TessieError("unreachable")  # pragma: no cover

    return fetch


def results(body) -> list:
    """The `results` list of an answer; a shape that is not that is an error, not an empty month."""
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise TessieError("Tessie's answer was not the list this importer expects.")
    return body["results"]


def vehicles(fetch) -> list[dict]:
    """The token's cars as {vin, name}; `last_state.display_name` is the car's name in the Tesla app."""
    out = []
    for v in results(fetch("/vehicles")):
        vin = (v.get("vin") or "").strip()
        if vin:
            out.append({"vin": vin, "name": ((v.get("last_state") or {}).get("display_name") or "").strip() or vin})
    return out


# ---- shapes
def _f(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def ts(v) -> datetime | None:
    """A unix timestamp in seconds (milliseconds, should one come, are told by their size)."""
    x = _f(v)
    if x is None or x <= 0:
        return None
    return datetime.fromtimestamp(x / 1000 if x > 1e11 else x, tz=UTC)


def trip_from(d: dict) -> dict | None:
    start, end = ts(d.get("started_at")), ts(d.get("ended_at"))
    if start is None or end is None or end <= start:
        return None
    return {
        "start_date": start,
        "destination_date": end,
        "start_odometer": None,  # from the readings inside the drive: the list holds only the distance
        "destination_odometer": None,
        "start_position_latitude": _f(d.get("starting_latitude")),
        "start_position_longitude": _f(d.get("starting_longitude")),
        "destination_position_latitude": _f(d.get("ending_latitude")),
        "destination_position_longitude": _f(d.get("ending_longitude")),
        "max_mph": _f(d.get("max_speed")),
        "kwh": _f(d.get("energy_used")) or None,
        "temp_f": _f(d.get("average_outside_temperature")),  # asked for in °F
        "tag": (d.get("tag") or "").strip() or None,
        "miles": _f(d.get("odometer_distance")),
    }


def charge_from(d: dict) -> dict | None:
    start, end = ts(d.get("started_at")), ts(d.get("ended_at"))
    if start is None or end is None or end <= start:
        return None
    sc = d.get("is_supercharger") is True
    return {
        "session_start_date": start,
        "session_end_date": end,
        "start_level": _f(d.get("starting_battery")),
        "end_level": _f(d.get("ending_battery")),
        "session_position_latitude": _f(d.get("latitude")),
        "session_position_longitude": _f(d.get("longitude")),
        "session_odometer": _f(d.get("odometer")),
        "real_charged": _f(d.get("energy_added")) or None,
        "charging_type": "DC" if sc else "AC",
        "network": "supercharger" if sc else None,
        "volts": None,
        "amps": None,
        "power_kw": None,
        "software": None,
    }


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Compass degrees from point a to point b (latitude, longitude)."""
    p1, p2, dl = math.radians(a[0]), math.radians(b[0]), math.radians(b[1] - a[1])
    return math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360


def moved(a, b, metres: float = 5.0) -> bool:
    return math.hypot((b[0] - a[0]) * 111_320, (b[1] - a[1]) * 111_320 * math.cos(math.radians(a[0]))) > metres


def samples_in(rows: list, lo: datetime, hi: datetime) -> list[dict]:
    """The historical-state readings inside [lo, hi], one per second, oldest first."""
    seen, out = set(), []
    for r in rows:
        t = ts(r.get("timestamp")) if isinstance(r, dict) else None
        if t is None or t < lo or t > hi or t in seen:
            continue
        seen.add(t)
        out.append(
            {
                "t": t,
                **{k: _f(r.get(k)) for k in ("latitude", "longitude", "speed", "power", "odometer", "battery_level", "battery_range", "charger_power", "charger_voltage", "charger_actual_current")},
            }
        )
    return sorted(out, key=lambda s: s["t"])


def route_of(samples: list[dict]) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """(positions, level stretches, range stretches) from a drive's readings. Positions are the writer's tuples
    (time, lat, lon, heading, level, odometer, speed, state, elevation, power_kw); heading is the bearing from
    the previous point, since the readings carry none; elevation is left out (its unit is not documented)."""
    pts = [s for s in samples if s["latitude"] is not None and s["longitude"] is not None]
    positions, heading = [], None
    for i, s in enumerate(pts):
        here = (s["latitude"], s["longitude"])
        prev = (pts[i - 1]["latitude"], pts[i - 1]["longitude"]) if i else None
        nxt = (pts[i + 1]["latitude"], pts[i + 1]["longitude"]) if i + 1 < len(pts) else None
        if prev and moved(prev, here):
            heading = bearing(prev, here)
        elif heading is None and nxt and moved(here, nxt):
            heading = bearing(here, nxt)
        positions.append((s["t"], here[0], here[1], heading, s["battery_level"], s["odometer"], s["speed"], "DRIVING", None, s["power"]))
    levels, ranges = _Runs(), _Runs()
    for s in samples:
        levels.add(s["t"], s["battery_level"])
        ranges.add(s["t"], s["battery_range"])
    return positions, levels.done(), ranges.done()


def curve_of(samples: list[dict], end: datetime) -> tuple[list[tuple], list[tuple], list[tuple], float]:
    """(power stretches, volts and amps stretches, level stretches, peak kW) from a charge's readings; each stretch
    runs to the next reading, the last to the charge's end."""
    power, electric, level = _Runs(), _Runs(), _Runs()
    for s in samples:
        power.add(s["t"], s["charger_power"])
        level.add(s["t"], s["battery_level"])
        if s["charger_voltage"] and s["charger_actual_current"]:
            electric.add(s["t"], (s["charger_voltage"], s["charger_actual_current"]))

    def to_end(rows):
        """_Runs stretches end at their last reading; stretch each to the next one's start, the last to `end`."""
        rows = list(rows)
        return [(a, rows[i + 1][0] if i + 1 < len(rows) else max(b, end), v) for i, (a, b, v) in enumerate(rows)]

    peak = max((s["charger_power"] or 0.0 for s in samples), default=0.0)
    return to_end(power.done()), to_end(electric.done()), level.done(), peak


def unit_scale(trip: dict, samples: list[dict]) -> float:
    """Odometer and speed in miles. They are asked for in miles (distance_format=mi); this only guards against a
    reply in kilometres: the readings' odometer rise against the drive's own distance says which."""
    odo = [s["odometer"] for s in samples if s["odometer"] is not None]
    if len(odo) >= 2 and trip.get("miles") and trip["miles"] > 0.5 and odo[-1] > odo[0]:
        if abs((odo[-1] - odo[0]) / trip["miles"] - KM_PER_MI) < 0.15:
            return 1 / KM_PER_MI
    return 1.0


def with_route(trip: dict, samples: list[dict]) -> tuple[dict, list[tuple], list[tuple], list[tuple]]:
    """The trip with its odometer ends from its readings, and the route, level and range stretches."""
    k = unit_scale(trip, samples)
    if k != 1.0:
        samples = [{**s, "odometer": s["odometer"] * k if s["odometer"] is not None else None, "speed": s["speed"] * k if s["speed"] is not None else None} for s in samples]
    pos, levels, ranges = route_of(samples)
    odo = [s["odometer"] for s in samples if s["odometer"] is not None]
    t = dict(trip)
    if odo:
        t["start_odometer"], t["destination_odometer"] = odo[0], odo[-1]
    return t, pos, levels, ranges


# ---- what other sources already hold
def merge(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[list[datetime]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def covers(cover: list[tuple[datetime, datetime]], a: datetime, b: datetime) -> bool:
    """Whether any covered span touches [a, b]."""
    i = bisect_left(cover, (a,))
    return (i > 0 and cover[i - 1][1] >= a) or (i < len(cover) and cover[i][0] <= b)


def inside(cover: list[tuple[datetime, datetime]], a: datetime, b: datetime) -> bool:
    """Whether one covered span holds all of [a, b]."""
    i = bisect_left(cover, (a,))
    return (i > 0 and cover[i - 1][0] <= a and cover[i - 1][1] >= b) or (i < len(cover) and cover[i][0] <= a and cover[i][1] >= b)


def coverage(conn, vin: str, far: datetime) -> list[tuple[datetime, datetime]]:
    """Time other sources hold for this car: every import's span but Tessie's own, and everything from when a live
    source began (the connector, a feed, the stream) onward."""
    rows = [_t(r) for r in conn.execute("SELECT first_at, last_at FROM cc_imports WHERE vin = %s AND id NOT LIKE 'tessie:%%' AND first_at IS NOT NULL AND last_at IS NOT NULL", (vin,)).fetchall()]
    spans = [(r[0], r[1]) for r in rows]
    live = live_since(conn, vin)
    if live is not None:
        spans.append((live, far))
    return merge(spans)


def clusters(items: list[tuple[datetime, datetime, dict]], cover) -> list[list]:
    """Items (start, end, row) in time order, cut wherever a covered span lies between two: a write replaces its
    whole window, so no window may hold another source's time."""
    out: list[list] = []
    for it in sorted(items, key=lambda x: x[0]):
        if out and not covers(cover, out[-1][-1][1], it[0]):
            out[-1].append(it)
        else:
            out.append([it])
    return out


def month_windows(now: datetime, since: datetime | None):
    """(start, end) of calendar months in UTC, the current one first, back to `since` or the earliest car."""
    floor = since or EARLIEST
    y, m = now.year, now.month
    while True:
        start = datetime(y, m, 1, tzinfo=UTC)
        end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
        if end <= floor:
            return
        yield start, end
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)


def loaded_for_good(conn, iid: str, end: datetime) -> bool:
    """Whether a month was loaded after it was over (a day's grace for the drives that straddled its end)."""
    row = _t(conn.execute("SELECT loaded_at FROM cc_imports WHERE id = %s", (iid,)).fetchone())
    return bool(row and row[0] >= end + timedelta(days=1))


# ---- one month
def month_out(vin: str, name: str, trips, sessions, drive_reads: dict, charge_reads: dict) -> dict | None:
    """The writer's shape for one window of drives and charges, with the routes and curves read for them.
    `drive_reads` and `charge_reads` map a start to its readings (absent: summary only)."""
    sess_rows, curves = [], []
    for c in sessions:
        reads = charge_reads.get(c["session_start_date"])
        if reads:
            p, e, lv, peak = curve_of(reads, c["session_end_date"])
            curves.append((p, e, lv))
            if peak > DC_KW:
                c = {**c, "charging_type": "DC"}
            c = {**c, "power_kw": None}  # the curve replaces the session's one figure
        sess_rows.append(c)
    trip_rows, positions, levels, ranges = [], [], [], []
    for t in trips:
        reads = drive_reads.get(t["start_date"])
        if reads:
            t, pos, lv, rg = with_route(t, reads)
            positions += pos
            levels += lv
            ranges += rg
        trip_rows.append(t)
    out = build_rows(trip_rows, sess_rows, vin, name)
    if out is None:
        return None
    out["positions"] = sorted(positions, key=lambda p: p[0])
    out["drive_levels"] = _rows(out["drive_levels"] + levels + [r for _p, _e, lv in curves for r in lv])
    out["drive_ranges"] = _rows(ranges)
    out["charging_powers"] = _rows(out["charging_powers"] + [r for p, _e, _l in curves for r in p])
    out["charging_electrics"] = _rows(out["charging_electrics"] + [r for _p, e, _l in curves for r in e])
    out["rows"] += len(positions)
    return out


def import_car(
    conn,
    fetch,
    vin: str,
    name: str,
    tz: ZoneInfo,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
    again: bool = False,
    curves: bool = True,
    step: int = 5,
    dry_run: bool = False,
    progress=lambda pct, text: None,
    say=print,
) -> dict:
    """Walk the car's months from now back, load each that other sources do not hold, and say what came in."""
    now = now or datetime.now(UTC)
    model = model_from_vin(vin)
    cover = coverage(conn, vin, now + timedelta(days=366))
    total = {"trips": 0, "charging_sessions": 0, "cc_positions": 0, "months": 0, "left_out": 0, "no_route": 0, "first": None, "last": None}
    empty = walked = 0
    for start, end in month_windows(now, since):
        walked += 1
        progress(min(95.0, walked * 1.5), f"{name}: {start:%b %Y}")
        iid = import_id(vin, start)
        if not again and inside(cover, start, end):
            continue  # all of it is another source's
        if not again and loaded_for_good(conn, iid, end):
            empty = 0
            continue
        span = {"from": int(start.timestamp()), "to": int(end.timestamp()) - 1}
        drives = [t for t in map(trip_from, results(fetch(f"/{vin}/drives", distance_format="mi", temperature_format="f", timezone="UTC", **span))) if t]
        charges = [c for c in map(charge_from, results(fetch(f"/{vin}/charges", distance_format="mi", timezone="UTC", **span))) if c]
        if not drives and not charges:
            empty += 1
            if since is None and empty >= STOP_AFTER:
                break
            continue
        empty = 0
        kept = [(t["start_date"], t["destination_date"], t) for t in drives if not covers(cover, t["start_date"], t["destination_date"])]
        kept += [(c["session_start_date"], c["session_end_date"], c) for c in charges if not covers(cover, c["session_start_date"], c["session_end_date"])]
        total["left_out"] += len(drives) + len(charges) - len(kept)
        if not kept:
            continue
        if not dry_run:
            conn.execute("DELETE FROM cc_imports WHERE id = %s", (iid,))  # this month is written afresh, window by window
        for group in clusters(kept, cover):
            trips = [r for _a, _b, r in group if "start_date" in r]
            sessions = [r for _a, _b, r in group if "session_start_date" in r]
            drive_reads = {
                t["start_date"]: samples_in(
                    results(
                        fetch(
                            f"/{vin}/states",
                            condense="false",
                            interval=step,
                            distance_format="mi",
                            temperature_format="f",
                            timezone="UTC",
                            **{"from": int(t["start_date"].timestamp()), "to": int(t["destination_date"].timestamp())},
                        )
                    ),
                    t["start_date"],
                    t["destination_date"],
                )
                for t in trips
            }
            charge_reads = {}
            if curves:
                for c in sessions:
                    a, b = c["session_start_date"], c["session_end_date"]
                    charge_reads[a] = samples_in(
                        results(
                            fetch(
                                f"/{vin}/states", condense="false", interval=60, distance_format="mi", temperature_format="f", timezone="UTC", **{"from": int(a.timestamp()), "to": int(b.timestamp())}
                            )
                        ),
                        a,
                        b,
                    )
            total["no_route"] += sum(1 for t in trips if not drive_reads.get(t["start_date"]))
            out = month_out(vin, name, trips, sessions, drive_reads, charge_reads)
            if out is None:
                continue
            total["first"] = min(total["first"] or out["first"], out["first"])
            total["last"] = max(total["last"] or out["last"], out["last"])
            if dry_run:
                total["trips"] += len(out["trips"])
                total["charging_sessions"] += len(out["charging_sessions"])
                total["cc_positions"] += len(out["positions"])
                continue
            counts = write_history(conn, vin, name, "Tesla", model, USABLE_KWH.get(model or ""), out, iid, LABEL, tz, rolling=True)
            fill_after_import(conn, vin, tz)
            save_trip_facts(conn, vin, out["trips"])
            conn.commit()
            for k in ("trips", "charging_sessions", "cc_positions"):
                total[k] += counts.get(k, 0)
        total["months"] += 1
        say(f"{name} {start:%Y-%m}: {len(drives)} drives, {len(charges)} charges")
    return total


def main(argv: list[str] | None = None) -> int:
    import argparse

    import psycopg

    ap = argparse.ArgumentParser(description="import a car's history from Tessie (read-only; the token is TESSIE_TOKEN in stack/.env)")
    ap.add_argument("--vin", help="one car; default every car on the token")
    ap.add_argument("--since", help="stop walking back at this date (YYYY-MM-DD); default: until 12 empty months in a row")
    ap.add_argument("--again", action="store_true", help="load months already loaded, and months another source seems to hold")
    ap.add_argument("--no-curves", action="store_true", help="skip each charge's power curve (one request fewer per charge)")
    ap.add_argument("--step", type=int, default=5, help="seconds between a drive's readings; 1 asks for every one Tessie holds")
    ap.add_argument("--dry-run", action="store_true", help="read from Tessie and count, write nothing")
    args = ap.parse_args(argv)
    token = (os.environ.get("TESSIE_TOKEN") or "").strip()
    if not token:
        print("Set TESSIE_TOKEN in stack/.env (a token from dash.tessie.com/settings/api).", file=sys.stderr)
        return 2
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC) if args.since else None
    tz = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
    fetch = http_fetcher(token)
    try:
        cars = [c for c in vehicles(fetch) if not args.vin or c["vin"] == args.vin]
        if not cars:
            print("No such car on that token.", file=sys.stderr)
            return 1
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            for car in cars:
                got = import_car(conn, fetch, car["vin"], car["name"], tz, since=since, again=args.again, curves=not args.no_curves, step=max(1, args.step), dry_run=args.dry_run)
                print(
                    f"{car['name']}: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in got.items() if v and k not in ("first", "last")) + (" (dry run, nothing written)" if args.dry_run else "")
                )
    except TessieError as err:
        print(str(err), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
